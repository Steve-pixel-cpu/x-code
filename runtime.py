from typing import Protocol, Optional, List
from concurrent.futures import ThreadPoolExecutor
import contextvars

from pydantic import BaseModel

from api_client import AssistantEvent, TextDeltaEvent, ToolUseEvent, MessageStopEvent, ApiClient
from compact import compact_session, CompactionConfig
from hooks import HookRunner, HookResult
from models import Message, TextContentBlock, AnyContentBlock, ToolContentBlock, Session
from permissions import PermissionMode, PermissionPolicy, PermissionPrompter, PermissionDecision
from prompt import PLAN_MODE_SECTION, SYSTEM_PROMPT_DYNAMIC_BOUNDARY

DEFAULT_MAX_ITERATIONS = 128
# auto-compact 触发阈值: 必须明显低于模型真实上下文窗口——一次请求还要
# 装 max_tokens=32768 的输出位, 阈值若贴着窗口设, 永远轮不到它触发,
# 只会等 API 报 context length 而整轮炸掉。取 128k 窗口的 ~75%。
DEFAULT_AUTO_COMPACT_THRESHOLD = 100_000
# 单轮输出预算（output_tokens 含思考）。正常单次调用被服务端 max_tokens=32768
# 硬顶，取两倍意味着只有"失控轮"（超长思考连环调用）会被拦下。
DEFAULT_TURN_OUTPUT_BUDGET = 65_536

# --- Token 用量追踪 ---
class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    def context_tokens(self) -> int:
        """真实上下文占用: input + 缓存写入 + 缓存读。开 prompt caching 后
        input_tokens 只计未命中部分, 单看它会严重低估。"""
        return (self.input_tokens
                + self.cache_creation_input_tokens
                + self.cache_read_input_tokens)



class UsageTracker:
    def __init__(self):
        self._latest_turn = TokenUsage()
        self._cumulative = TokenUsage()
        self._turns = 0


    def record(self, usage: TokenUsage):
        self._latest_turn = usage

        self._cumulative.input_tokens += usage.input_tokens
        self._cumulative.output_tokens += usage.output_tokens
        self._cumulative.cache_creation_input_tokens += usage.cache_creation_input_tokens
        self._cumulative.cache_read_input_tokens += usage.cache_read_input_tokens

        self._turns += 1

    def current_turn_usage(self) -> TokenUsage:
        return self._latest_turn


    def cumulative_usage(self) -> TokenUsage:
        return self._cumulative

    def turns(self) -> int:
        return self._turns


class ToolError(Exception):
    ...

class ToolOutput(str):
    """工具返回值: 本体是给模型/存储的字符串（isinstance(x, str) 恒真,
    内核零改动透传）, 附带 `_meta` 给镜像方（Web 端 EmittingToolRegistry）
    做富展示——write_file 的 diff、行数统计等。模型永远看不到 _meta。

    为什么不用 dict 返回值: ToolExecutor 协议与 CLI 的所有调用点都按 str
    处理, 改返回类型要动内核; str 子类只有 tools.py / runtime.py 两处
    需要知道它, 其余路径（API 序列化、落盘、merge_hook_feedback）拿到的
    就是普通字符串。"""
    _meta: dict = {}

    def with_meta(self, meta: dict) -> "ToolOutput":
        self._meta = meta
        return self


def result_meta(output) -> Optional[dict]:
    """从工具返回值摘 _meta（普通 str → None）。"""
    return getattr(output, "_meta", None) or None

class TurnInterrupted(Exception):
    """用户主动打断: 在历史一致点（迭代顶部 / 工具批次执行前）抛出。
    Web 端流式代理（TurnInterrupted）与重试循环（StreamInterrupted→
    由 api_client 翻译）语义一致, 三者都由调用方朝安全侧收束本轮。
    CLI 不设置 cancel_check, 永远不会抛出。"""

class ToolExecutor(Protocol):
    # tool_use_id: 事件镜像方（如 Web 端）靠它把结果配回工具卡;
    # 并行执行后结果按完成序到达, 不能再靠 FIFO 猜配对
    def execute(self, tool_name: str, input: str,
                tool_use_id: Optional[str] = None) -> str: ...
    # 成功返回字符串，失败抛 ToolError
    # （可返回 ToolOutput——str 子类, 附带 _meta 富展示元数据, 内核按 str 透传）



# --- 构建 assistant 消息 ---
def build_assistant_message(events: list[AssistantEvent]) -> tuple[Message, Optional[TokenUsage]]:

    text_chunk= ""
    blocks: List[AnyContentBlock] = []
    finished = False
    usage = None

    for event in events:
        if isinstance(event, TextDeltaEvent):
            text_chunk+= event.text
        elif isinstance(event, ToolUseEvent):
            if text_chunk:
                text_block = TextContentBlock(
                    text= text_chunk,
                )
                text_chunk= ""
                blocks.append(text_block)
            tool_block = ToolContentBlock(
                id= event.id,
                name= event.name,
                input=event.input,
            )
            blocks.append(tool_block)

        elif isinstance(event, MessageStopEvent):
            finished = True
            if event.usage is not None:
                usage = TokenUsage(
                    input_tokens=event.usage.input_tokens,
                    output_tokens=event.usage.output_tokens,
                    cache_creation_input_tokens=event.usage.cache_creation_input_tokens,
                    cache_read_input_tokens=event.usage.cache_read_input_tokens,
                )

    if text_chunk:
        text_block = TextContentBlock(
            text=text_chunk,
        )
        blocks.append(text_block)

    if not finished:
        raise RuntimeError("消息无法结束!")

    if not blocks:
        raise RuntimeError("无消息内容!")

    message = Message(
        role= "assistant",
        content= blocks,
    )
    return message, usage



# --- Hook 反馈合并 -
def merge_hook_feedback(messages: list[str], output: str, denied: bool) -> str:

    if not messages:
        return output

    result = ""

    if output.strip():
        result += output
        result += "\n\n"

    msg = "\n".join(messages)
    if denied:
        result += f"Hook feedback (denied): {msg}"
    else:
        result += f"Hook feedback: {msg}"

    return result

# --- TurnSummary ---
class TurnSummary(BaseModel):
    assistant_messages: list[Message]
    tool_results: list[Message]
    iterations: int
    usage: TokenUsage
    # 语义 = "本请求使用了压缩视图"（过阈值且确有消息被归档）;
    # 过阈值但消息数 <= preserve_recent 无东西可压时为 False
    auto_compacted: bool
    budget_exhausted: bool = False
    iterations_exhausted: bool = False


# --- ConversationRuntime ---
# plan 模式的动态 system 段: 追加在缓存边界之后的 sections 尾部。
# 内容是给模型的行为规范——研究期只读、计划经 present_plan 提交、
# 批准后才开始写。与 permissions.py 的硬授权互为表里: 提示词管"该做
# 什么", 授权层管"能做什么"。
# 兼容别名: 注入内容统一收敛到 prompt.PLAN_MODE_SECTION（由
# _rebuild_effective_prompt 负责插进动态段）。旧测试/调用方仍可引用此名。
PLAN_MODE_INSTRUCTION = PLAN_MODE_SECTION

class ConversationRuntime:
    def __init__(self,
                 session: Session,
                 api_client: ApiClient,
                 tool_executor: ToolExecutor,
                 permission_policy: PermissionPolicy,
                 system_prompt: list[str],
                 hook_runner: Optional[HookRunner] =None):
        self._max_iterations = DEFAULT_MAX_ITERATIONS
        self._auto_compact_threshold = DEFAULT_AUTO_COMPACT_THRESHOLD
        self._turn_output_budget = DEFAULT_TURN_OUTPUT_BUDGET
        self._api_client = api_client
        self._tool_executor = tool_executor
        self._permission_policy = permission_policy
        self._system_prompt = system_prompt
        self._hook_runner = hook_runner or HookRunner()
        self._session = session
        self._usage_tracker = UsageTracker()
        # 会话级思考等级: 初值取自 api_client（=全局默认）, 之后只改自己。
        # stream() 调用时带上, 多会话共用 client 也不会互相串设置。
        self._thinking_level = api_client.thinking_level
        # 持久化钩子（Web 端增量落盘用, CLI 默认 None 行为不变）:
        # - on_iterate: 消息历史处于一致点（无悬空 tool_use）时触发——
        #   用户消息落定后、每次工具结果回填后
        # - on_compacted: 压缩视图首次激活时触发（纯通知, 历史不被改写,
        #   存储无需重写——压缩只影响 _model_view() 给模型的请求视图）
        self._on_iterate = None
        self._on_compacted = None
        # 压缩视图粘性开关: 过阈值置位后持续生效(防"压缩→恢复全量→再压缩"
        # 振荡); 历史本身不被改写, _model_view() 据此构建给模型的请求视图
        self._compact_active = False
        # 事件镜像钩子（Web 端工具卡片闭合用, CLI 默认 None 行为不变）:
        # - on_tool_finalized: 工具未经执行就被终局（权限拒绝 / hook 拦截 /
        #   prompter 拒绝）时触发——这条路径不经过 tool_executor, 镜像方
        #   （如 Web 端 EmittingToolRegistry）看不到, 不通知前端工具卡
        #   会永远停在"运行中"直到轮次收尾
        self._on_tool_finalized = None
        # 生效系统提示词: 基础段 + 计划模式段(仅 PLAN 模式)。模式切换时
        # 重建, stream 调用一律用它——见 _rebuild_effective_prompt
        self._rebuild_effective_prompt()
        # 轮次取消检查（用户打断, Web 端绑定 should_stop; CLI 默认 None）:
        # 在历史一致点轮询——迭代顶部与工具批次执行前。工具执行中的打断
        # 由工具自身轮询（tools.TOOL_CANCEL_CHECK）负责, 这里兜底覆盖
        # 不支持协作取消的工具与授权等待之后的窗口。
        self._cancel_check = None

    def _rebuild_effective_prompt(self) -> None:
        """权限模式联动系统提示词。计划模式把 PLAN_MODE_SECTION 插进动态段
        （边界之后; 无边界则追加尾部）——静态前缀逐字节不变, prompt caching
        前缀继续命中。切回其他模式即移除。"""
        base = self._system_prompt
        if self._permission_policy.active_mode == PermissionMode.PLAN:
            if SYSTEM_PROMPT_DYNAMIC_BOUNDARY in base:
                idx = base.index(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)
                self._effective_system_prompt = (
                    base[:idx + 1] + [PLAN_MODE_SECTION] + base[idx + 1:])
            else:
                self._effective_system_prompt = list(base) + [PLAN_MODE_SECTION]
        else:
            self._effective_system_prompt = list(base)

    def set_on_iterate(self, fn) -> "ConversationRuntime":
        self._on_iterate = fn
        return self

    def set_on_compacted(self, fn) -> "ConversationRuntime":
        self._on_compacted = fn
        return self

    def set_on_tool_finalized(self, fn) -> "ConversationRuntime":
        self._on_tool_finalized = fn
        return self

    def set_cancel_check(self, fn) -> "ConversationRuntime":
        """绑定轮次取消检查（用户打断）。fn 无参返回 bool; None = 无人打断
        （CLI 默认, 行为不变）。只在历史一致点抛 TurnInterrupted, 调用方
        （Web 端 worker）负责修补+落盘+收束——与流式中断同一出口。"""
        self._cancel_check = fn
        return self

    def _notify_iterate(self) -> None:
        if self._on_iterate is not None:
            try:
                self._on_iterate()
            except Exception as e:
                print(f"[WARN] persist hook failed: {e}")

    def with_max_iterations(self, n) -> "ConversationRuntime":
        self._max_iterations = n
        return self

    def  with_auto_compact_threshold(self, n) -> "ConversationRuntime":
        self._auto_compact_threshold = n
        return self

    def with_turn_output_budget(self, n) -> "ConversationRuntime":
        self._turn_output_budget = n
        return self

    def session(self)-> Session:
        return self._session

    def usage(self)-> UsageTracker:
        return self._usage_tracker

    def permission_mode(self) -> PermissionMode:
        return self._permission_policy.active_mode

    def set_permission_mode(self, mode: PermissionMode) -> None:
        self._permission_policy.set_mode(mode)
        self._rebuild_effective_prompt()   # 计划模式段随模式增减

    def thinking_level(self) -> str:
        return self._thinking_level

    def set_thinking_level(self, level: str) -> None:
        self._thinking_level = level

    def _authorize_tool_use(self, tool_block: ToolContentBlock,
                            prompter: Optional[PermissionPrompter]=None
                            ) -> tuple[Optional[Message], Optional[HookResult]]:
        """授权 + PreToolUse hook。必须串行: 交互式 prompter 要逐个弹问,
        并发授权会串位。返回 (Message, None) = 已终局（拒绝/hook 拦下）;
        返回 (None, pre_res) = 放行, 交执行管线。"""
        result = self._permission_policy.authorize(
            tool_name=tool_block.name,
            input=tool_block.input,
            prompter=prompter,
            tool_use_id=tool_block.id,
        )
        if result.decision == PermissionDecision.DENY:
            return Message.tool_result(
                id = tool_block.id,
                name = tool_block.name,
                output= result.reason,
                is_error = True,
            ), None

        pre_res = self._hook_runner.run_pre_tool_use(
            tool_name=tool_block.name,
            tool_input=tool_block.input,
        )
        if pre_res.denied:
            if pre_res.messages:
                pre_output = ("\n").join(pre_res.messages)
            else:
                pre_output = f"PreToolUse hook denied tool {tool_block.name}."
            return Message.tool_result(
                id = tool_block.id,
                name = tool_block.name,
                output = pre_output,
                is_error = True,
            ), None
        return None, pre_res

    def _execute_tool(self, tool_block: ToolContentBlock, pre_res: HookResult) -> Message:
        """执行管线: 工具本体 + Pre/Post hook 反馈合并。无共享可变状态,
        同一条消息里相互独立的 tool_use 可由 run_turn 并发调度。"""
        is_tool_error = False
        try:
            output = self._tool_executor.execute(
                tool_name=tool_block.name,
                input=tool_block.input,
                tool_use_id=tool_block.id,
            )
        except Exception as e:
            output = str(e)
            is_tool_error = True

        meta = result_meta(output)   # ToolOutput._meta（普通 str 为 None）
        tool_output = str(output)    # hook 只看文本, meta 不掺和
        output = merge_hook_feedback(messages=pre_res.messages, output=output, denied=False)
        post_res = self._hook_runner.run_post_tool_use(
            tool_name=tool_block.name,
            tool_input=tool_block.input,
            tool_output=tool_output,
            is_error= is_tool_error,
        )
        output = merge_hook_feedback(messages=post_res.messages, output=output, denied=post_res.denied)
        if not is_tool_error and not post_res.denied:
            # hook 反馈拼进文本后 output 已是新 str, meta 需重新挂上
            output = ToolOutput(str(output)).with_meta(meta) if meta else output

        # result_meta 随消息落盘, Web 端历史回放可重建富展示（diff 等）。
        # 内部 Message/持久化层认识它, API 序列化 (_convert_message) 忽略之。
        return Message.tool_result(
            id = tool_block.id,
            name = tool_block.name,
            output = output,
            is_error = is_tool_error or post_res.denied,
            result_meta = meta,
        )

    def _process_tool_use(self, tool_block: ToolContentBlock, prompter: Optional[PermissionPrompter]=None)-> Message | None:
        """单工具全流程（授权串行 → 执行）。串行路径的便捷入口。"""
        finalized, pre_res = self._authorize_tool_use(tool_block, prompter)
        if finalized is not None:
            return finalized
        return self._execute_tool(tool_block, pre_res)
    def compact(self)->  str:
        # 手动压缩: 不再删历史——压缩是"给模型的请求期视图", 置粘性标记后
        # _model_view() 即刻生效, 原始对话原样保留在内存与磁盘(展示用)。
        self._compact_active = True
        compact_result = compact_session(
            messages=self._session.messages,
            config=CompactionConfig(
                max_estimated_tokens=0
            ),
        )
        if compact_result.removed_count == 0:
            return "Nothing to compact!"
        return (f"Compacted view active! Archived {compact_result.removed_count} "
                f"earlier messages from the model context (history kept for display).")

    def _maybe_auto_compact(self)-> bool:
        # 信号: 最近一次调用的真实上下文占用（input + 缓存读写, 见
        # TokenUsage.context_tokens）。开缓存后 input_tokens 只算未命中
        # 部分, 不能单看。
        # 压缩不改写历史: 置粘性标记 _compact_active, 由 _model_view() 在
        # 构建请求时生成压缩视图。粘性是必须的——视图生效后 usage 骤降,
        # 若只看阈值会"压缩→恢复全量→再压缩"振荡。激活一次即通知一次
        # (_on_compacted, Web 端广播提示), 之后持续生效不再重复通知。
        if not self._context_over_compact_threshold():
            return False
        if self._compact_active:
            return True                      # 已激活: 持续生效, 不重复通知
        compact_result = compact_session(
            messages=self._session.messages,
            config=CompactionConfig(
                max_estimated_tokens = 0
            ),
        )
        # 语义: auto_compacted = "本请求使用了压缩视图"。过阈值但没东西
        # 可压（消息数 <= preserve_recent, 如单条超大粘贴）时不亮信号
        if compact_result.removed_count == 0:
            return False
        self._compact_active = True
        if self._on_compacted is not None:
            try:
                self._on_compacted()
            except Exception as e:
                print(f"[WARN] compact hook failed: {e}")

        return True

    def _model_view(self) -> List[Message]:
        """给模型的会话视图: 压缩激活时 = [续接摘要] + 保留区(纯函数逐请求
        重算), 否则原样返回全量历史。展示层永远读全量——压缩只影响模型。"""
        if not self._compact_active:
            return self._session.messages
        return compact_session(
            messages=self._session.messages,
            config=CompactionConfig(max_estimated_tokens=0),
        ).compacted_messages

    def _context_over_compact_threshold(self) -> bool:
        latest = self.usage().current_turn_usage()
        return latest.context_tokens() >= self._auto_compact_threshold



    def run_turn(self, user_input: str, prompter: Optional[PermissionPrompter]=None,
                 attachments: Optional[list[dict]]=None) -> TurnSummary:
        iterations = 0
        curr_session = self._session
        tool_results : list[Message] = []
        assistant_messages = []
        turn_output_tokens = 0       # 本轮累计输出（含思考），循环层预算的计量
        budget_exhausted = False
        iterations_exhausted = False
        auto_compacted = False

        # 附件（图片/文本文件）经 Message.user_input 组装成 image/file 块;
        # CLI 调用点不传附件, 行为不变
        curr_session.messages.append(
            Message.user_input(user_input, attachments))
        self._notify_iterate()   # 一致点: 用户消息已落定
        while True:
            # 打断检查点（一致点）: 用户消息/工具结果都已落定, 此刻抛出
            # 不留悬空 tool_use。打断优先于预算/迭代/压缩检查。
            if self._cancel_check is not None and self._cancel_check():
                raise TurnInterrupted()
            # 循环层预算检查点: 收束发生在这里——上一迭代的工具结果已全部
            # 回填，会话历史一致，break 不会产生悬空 tool_use，也不需要异常
            # 修补。服务端 max_tokens 管单次调用上限，这里管跨次累加。
            if turn_output_tokens >= self._turn_output_budget:
                budget_exhausted = True
                break
            if iterations >= self._max_iterations:
                iterations_exhausted = True
                break
            # 压缩检查点与预算检查同位置: 此刻历史一致。压缩只置粘性标记
            # （历史不被改写）, 真正的裁剪发生在下面 _model_view() 构建
            # 请求视图时——超限发生在单轮中途也能就地降载。
            if self._maybe_auto_compact():
                auto_compacted = True

            iterations += 1
            # 计划模式指令段的注入/移除在 set_permission_mode →
            # _rebuild_effective_prompt 里完成, stream 一律用生效提示词
            # （此前这里算过 sys_prompt 却没传给 stream, 等于从未生效）
            # messages 用模型视图: 压缩激活时 = [续接摘要] + 保留区,
            # 全量历史原样留在会话里供展示与落盘。
            events = self._api_client.stream(
                system_prompt=self._effective_system_prompt,
                messages=self._model_view(),
                thinking_level=self._thinking_level,
            )
            message,token_usage = build_assistant_message(events)
            assistant_messages.append(message)
            if token_usage:
                self.usage().record(usage=token_usage)
                turn_output_tokens += token_usage.output_tokens
            curr_session.messages.append(message)

            tool_use_blocks = []
            for block in message.content:
                if isinstance(block, ToolContentBlock):
                    tool_use_blocks.append(block)

            if not tool_use_blocks:
                break

            # 授权串行（交互式 prompter 逐个弹问）, 执行并行: 同一条消息里
            # 的多个 tool_use 本就不依赖彼此结果, 并行省掉逐个冷启动子进程
            # 的串行等待。结果按原位回填, 历史顺序与串行完全一致。
            finalized: list[Optional[Message]] = [None] * len(tool_use_blocks)
            pending: list[tuple[int, ToolContentBlock, HookResult]] = []
            for i, block in enumerate(tool_use_blocks):
                done, pre_res = self._authorize_tool_use(block, prompter)
                if done is not None:
                    finalized[i] = done
                    # 未经执行就被终局（拒绝/拦截）: 通知镜像方补发结果,
                    # 前端工具卡才能闭合。回调异常不阻断轮次。
                    if self._on_tool_finalized is not None:
                        try:
                            self._on_tool_finalized(block, done)
                        except Exception as e:
                            print(f"[WARN] tool-finalized hook failed: {e}")
                else:
                    pending.append((i, block, pre_res))

            # 打断检查点（一致点）: 打断落在授权之后、执行之前时,
            # 未执行的工具就地终局（补 error result + 通知镜像方闭合
            # 前端工具卡）, 历史保持一致后再抛出——不留悬空 tool_use。
            if pending and self._cancel_check is not None \
                    and self._cancel_check():
                for i, block, _pre in pending:
                    interrupted_result = Message.tool_result(
                        id=block.id,
                        name=block.name,
                        output="(用户中断了本轮对话)",
                        is_error=True,
                    )
                    finalized[i] = interrupted_result
                    if self._on_tool_finalized is not None:
                        try:
                            self._on_tool_finalized(block, interrupted_result)
                        except Exception as e:
                            print(f"[WARN] tool-finalized hook failed: {e}")
                for tool_result_msg in finalized:
                    if tool_result_msg:
                        curr_session.messages.append(tool_result_msg)
                        tool_results.append(tool_result_msg)
                self._notify_iterate()   # 一致点: 中断结果已回填
                raise TurnInterrupted()

            if len(pending) > 1:
                # 池线程必须能看到本轮绑定（Web 端 emit/workdir 按 contextvars
                # 路由）: 每个任务各自 copy_context()。不能共享同一份快照——
                # Context 同一时刻只允许被一个线程进入, 并发 ctx.run 会炸出
                # "cannot enter context: already entered"。
                with ThreadPoolExecutor(max_workers=min(4, len(pending))) as pool:
                    futures = [pool.submit(contextvars.copy_context().run,
                                           self._execute_tool, b, pre)
                               for _, b, pre in pending]
                    for (i, _, _), fut in zip(pending, futures):
                        finalized[i] = fut.result()
            else:
                for i, block, pre_res in pending:
                    finalized[i] = self._execute_tool(block, pre_res)

            for tool_result_msg in finalized:
                if tool_result_msg:
                    curr_session.messages.append(tool_result_msg)
                    tool_results.append(tool_result_msg)
            self._notify_iterate()   # 一致点: 本迭代的工具结果已全部回填

        if self._maybe_auto_compact():
            auto_compacted = True

        return TurnSummary(
            assistant_messages=assistant_messages,
            tool_results=tool_results,
            iterations=iterations,
            usage=self.usage().current_turn_usage(),   # 本轮用量（累计口径走 usage()）
            auto_compacted= auto_compacted,
            budget_exhausted=budget_exhausted,
            iterations_exhausted=iterations_exhausted,
        )














