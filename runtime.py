from typing import Protocol, Optional, List
from concurrent.futures import ThreadPoolExecutor
import contextvars
import json
import os
import re
import shlex
import threading
from pathlib import Path

from pydantic import BaseModel

from api_client import (AssistantEvent, TextDeltaEvent, ToolUseEvent,
                        MessageStopEvent, ApiClient, INTERRUPTED_STOP_REASON)
from compact import (
    CompactionConfig,
    SUMMARIZER_SYSTEM_PROMPT,
    SUMMARY_INSTRUCTION,
    SessionMemory,
    continuation_message,
    cut_point,
    estimate_session_tokens,
    format_compact_summary,
    summarize_messages,
)
from hooks import HookRunner, HookResult
from models import Message, TextContentBlock, AnyContentBlock, ToolContentBlock, ToolResultContentBlock, Session
from permissions import (PermissionMode, PermissionPolicy, PermissionPrompter,
                         PermissionDecision, MUTATING_SHELL_TOOLS,
                         shell_command_is_read_only)
from prompt import PLAN_MODE_SECTION, SYSTEM_PROMPT_DYNAMIC_BOUNDARY

DEFAULT_MAX_ITERATIONS = 128
# auto-compact 触发阈值: 必须明显低于模型真实上下文窗口——一次请求还要
# 装 max_tokens=32768 的输出位, 阈值若贴着窗口设, 永远轮不到它触发,
# 只会等 API 报 context length 而整轮炸掉。
# 750_000 = GLM-5.3 官方 1M 窗口的 ~75%。口径是"最近一次 API 调用的
# input+缓存读写"（见 _context_over_compact_threshold）, 不是本轮累加。
# 第三方中转若砍窗口, 用配置 contextWindow / tokenBudget 调小。
DEFAULT_AUTO_COMPACT_THRESHOLD = 750_000
# 单轮输出预算（output_tokens 含思考）。思考型模型一次"汇总证据"级的
# 大思考就能烧掉 1/4 预算（GLM 系强制思考, high 档上限 16k/次）, 预算太紧
# 会把轮次稳定掐死在"证据齐了"和"动手改"之间——排查死循环的一环。
# 262144 ≈ 容纳 16 次大思考调用; CLAUDE_TURN_TOKEN_BUDGET 可覆盖。
DEFAULT_TURN_OUTPUT_BUDGET = 262_144

# --- 轮次收束说明: 预算/迭代耗尽对模型是不可见的截断——只把警告发给
# 前端的话, 模型不知道自己被掐过, 下一轮只会重新排查。把收束原因作为
# user 消息写进历史, 下一轮模型才可能"接着干"而不是"从头查"。 ---
TURN_BUDGET_EXHAUSTED_NOTICE = (
    "[System note] This turn was stopped early because the per-turn output "
    "token budget ran out. Nothing you did is lost: every file you read, "
    "command you ran and conclusion you reached is already in this "
    "conversation. In your next turn, do NOT re-read files or re-run "
    "commands whose results you can already see, and do NOT restart the "
    "investigation — go straight to the next concrete action (make the "
    "edits, run the checks, or give the final answer)."
)
TURN_ITERATIONS_EXHAUSTED_NOTICE = (
    "[System note] This turn was stopped early because the per-turn "
    "iteration limit was reached. Your evidence and conclusions are all in "
    "this conversation. In your next turn, do NOT repeat reads or checks "
    "you have already done — continue directly with the next concrete "
    "action."
)

# --- 重复只读调用护栏: 确定性反自旋。观测到的失败模式: 大文件整读被截断
# 掐掉中段后, 模型把同一个调用原样重发几十次（一次会话里同一 read_file
# 重复 24 次）。提示词拦不住这种病理行为, 只能在执行层兜住:
# read_file/grep/glob 是文件系统的纯函数, 期间没有任何写入时, 完全相同
# 的调用结果必然相同——重复零信息, 第 3 次起直接拒绝, 零损失。
PURE_READ_TOOLS = frozenset({"read_file", "grep", "glob"})
REPEAT_WARN_ON = 2       # 第 2 次: 照常执行, 结果附加警告
REPEAT_DENY_FROM = 3     # 第 3 次起: 拒绝执行

# shell 只读判定已下沉到 permissions.py: 权限放行（plan/workspace-write
# 下只读探查不再硬拒）与护栏变异序号共用同一套保守判定。

REPEAT_WARN_TEXT = (
    "[System note] This is the 2nd time you ran this same read-only call "
    "against the same target, and nothing has been written since — the "
    "result is guaranteed identical to what you already have. Do NOT run "
    "it a 3rd time (it will be refused). If the part you need was "
    "truncated, change the parameters (read_file offset/limit to page "
    "through a large file, or a narrower grep pattern/path); otherwise "
    "act on what you already have."
)
REPEAT_DENIED_TEXT = (
    "REFUSED — this same read-only call against the same target already "
    "ran twice in this session with no writes in between, so repeating it "
    "cannot produce new information. Change the parameters instead: "
    "read_file with offset/limit to page through a large file, a narrower "
    "grep pattern or a specific path — or act on the results already in "
    "this conversation. If you suspect the content actually changed, "
    "verify that via a different tool (e.g. bash) rather than repeating "
    "this call."
)

def _read_guard_key(tool_name: str, tool_input: str):
    """护栏记账键。read_file 做路径规范化: normpath 折叠 ./ 与分隔符,
    normcase 按平台处理大小写（Windows 文件系统不敏感→统一小写; POSIX
    敏感→原样）, 让"换个写法重读同一文件"同样计入; offset/limit 保留在
    键里——分页是设计内行为（大文件按翻页协议取窗口）, 不同范围不算
    重复。其余工具保持精确输入。解析失败回落原始输入。"""
    if tool_name != "read_file":
        return (tool_name, tool_input)
    try:
        params = json.loads(tool_input)
        norm = os.path.normcase(os.path.normpath(str(params.get("path") or "")))
        if os.name == "nt":
            norm = norm.replace("\\", "/")
        return (tool_name, norm, params.get("offset"), params.get("limit"))
    except Exception:
        return (tool_name, tool_input)

# --- 结论检查点: 本回合调用时钟的节奏性对靶提醒 ---
# 治"忘记自己的目标是啥"（实测 v2 教训）: 模型查库实锤后转入支线
# （反编译 NuGet 包确认辅助链路），连跑 20+ 条 sqlcmd/python/grep——这些
# 命令在变异判定里全是"可能改状态"，旧设计把检查点时钟挂在"只读连击"
# 上，每条都被清零，检查点在唯一需要它的螺旋里全程失明。
# 现在时钟 = 本回合全部工具调用数（与变异判定解耦, 仅新用户回合清零），
# 提醒正文直接引用原始问题: "此刻做的事在回答它, 还是途中自创的支线?"
CONCLUSION_CHECKPOINT_EVERY = 16
CONCLUSION_CHECKPOINT_TEXT = (
    "[System note] Pacing checkpoint — {n} tool calls this turn. "
    'Your original question: "{q}". Re-read it: is your current action '
    "answering THAT, or a side-question you invented along the way? "
    "If the evidence in hand already answers it, stop and write the "
    "final answer now. If one decisive call would settle the remaining "
    "uncertainty, run it yourself and close — do not hand the user "
    "homework. Auxiliary certainty (side-quests) can wait or be "
    "skipped: the user asked one thing."
)

# --- MicroCompact: 旧工具结果清除（借鉴 Claude Code microCompact 设计）---
# 工具结果是上下文膨胀的主力（读文件/命令输出动辄上万 token）。窗口再大,
# 注意力也随上下文线性稀释, 缓存读成本随之线性上涨。把保留窗口之外的
# 高产出可复现工具结果替换为占位符——块结构原样保留, tool_use/tool_result
# 配对不破坏。触发用估算 token 软阈值: 超限一次性清掉旧的, 视图随即稳定
# （缓存不再反复失效）, 直到新内容再次长过阈值。
MICROCOMPACT_TRIGGER_TOKENS = 60_000
MICROCOMPACT_KEEP_RECENT = 8        # 最近 N 条工具结果原文保留
MICROCOMPACT_MIN_CHARS = 1_000      # 太短的结果不值得清（省不了几个 token）
MICROCOMPACT_PLACEHOLDER = "[Old tool result content cleared]"
# 可清除白名单: 高产出、可复现（重跑命令/重读文件即可拿回）。todo/plan/
# agent 等低产出或不可复现的结果不动。
MICROCOMPACT_COMPACTABLE_TOOLS = frozenset({
    "read_file", "grep", "glob", "bash", "powershell",
    "web_search", "web_fetch",
})

# --- 压缩后文件重注入（借鉴 Claude Code post-compact restore）---
# 压缩摘要保结论, 但文件原文不进去。把归档区里最近读过的文件随续接消息
# 带回, 模型不必为"看手上这点事"立刻重读——压缩后重读循环的第三道闸:
# 摘要保结论 / 重注入供原文 / 护栏拦重复。
POST_COMPACT_MAX_FILES = 5
POST_COMPACT_CHARS_PER_FILE = 5_000

# --- max_tokens 截断自愈（借鉴 Claude Code 原文设计）---
# 输出被 max_tokens 掐断且没有工具调用时, 注入恢复提示继续循环而不是
# 直接结束轮次——恢复提示明确禁止道歉和复述（那会烧更多输出 token 使
# 问题恶化）。最多恢复 3 次, 防止无限循环。
MAX_OUTPUT_TOKENS_RECOVERY = 3
OUTPUT_TRUNCATED_NOTICE = (
    "[System note] Output token limit hit mid-response. Resume directly "
    "from where the output stopped — no apology, no recap, no repeating "
    "what you already wrote."
)

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
        self._turn_accum = TokenUsage()
        self._cumulative = TokenUsage()
        self._last_call = TokenUsage()
        self._turns = 0

    def begin_turn(self):
        """用户回合开始: 清空按轮累加器。调用点在 run_turn 顶部, 循环层
        预算检查与本累加器共用同一口径——预算按它触发, 展示也按它显示。"""
        self._turn_accum = TokenUsage()

    def record(self, usage: TokenUsage):
        self._last_call = usage   # 单次口径: 最近一次调用原样保存, 不累加
        self._turn_accum.input_tokens += usage.input_tokens
        self._turn_accum.output_tokens += usage.output_tokens
        self._turn_accum.cache_creation_input_tokens += usage.cache_creation_input_tokens
        self._turn_accum.cache_read_input_tokens += usage.cache_read_input_tokens

        self._cumulative.input_tokens += usage.input_tokens
        self._cumulative.output_tokens += usage.output_tokens
        self._cumulative.cache_creation_input_tokens += usage.cache_creation_input_tokens
        self._cumulative.cache_read_input_tokens += usage.cache_read_input_tokens

        self._turns += 1

    def current_turn_usage(self) -> TokenUsage:
        """本轮（一次 run_turn）的累计用量: 多次 API 调用相加, 而非最近一次。
        旧实现返回 last-call, 曾让"预算已用尽"横幅旁挂着远小于阈值的单次
        用量（如 5.1k vs 65k 阈值）, 读起来像预算在 5k 就误触发。
        只对"输出预算"这类消耗型口径成立——上下文体积必须用 latest_call_usage。"""
        return self._turn_accum

    def latest_call_usage(self) -> TokenUsage:
        """最近一次 API 调用的单次用量（替换, 不累加）。上下文体积 =
        该次调用的 input+缓存读写——每次调用的输入都携带全量历史, 单次
        口径才是"此刻上下文多大"。跨 turn 不清零: 历史只增不减, 上一轮
        末次调用就是本轮请求前的真实上下文。"""
        return self._last_call


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


def _token_usage_from(info) -> Optional[TokenUsage]:
    """MessageStopEvent.usage → TokenUsage（None 透传）。打断零内容路径
    与 build_assistant_message 共用同一转换, 两边记账口径一致。"""
    if info is None:
        return None
    return TokenUsage(
        input_tokens=info.input_tokens,
        output_tokens=info.output_tokens,
        cache_creation_input_tokens=info.cache_creation_input_tokens,
        cache_read_input_tokens=info.cache_read_input_tokens,
    )



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
        # side-call 专用 client（utilityProvider 小模型, 见 set_utility_client）:
        # None = 未配置, 摘要类 side-call 跟主模型（原行为）
        self._utility_client: Optional[ApiClient] = None
        self._tool_executor = tool_executor
        self._permission_policy = permission_policy
        self._system_prompt = system_prompt
        self._hook_runner = hook_runner or HookRunner()
        self._session = session
        self._usage_tracker = UsageTracker()
        # 会话级思考等级: 初值取自 api_client（=全局默认）, 之后只改自己。
        # stream() 调用时带上, 多会话共用 client 也不会互相串设置。
        self._thinking_level = api_client.thinking_level
        # 会话级模型: None = 跟随 api_client 当前模型（全局默认/全局切换）;
        # 非空时 stream() 按轮携带, 会话内换模型不影响其他会话。
        self._model: Optional[str] = None
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
        # 压缩摘要缓存: (切割点, 格式化摘要)。历史只增不减, 切割点未推进
        # 超过余量时复用旧摘要、保留区随之变长——旧实现每次请求都从全量
        # 历史重算一遍摘要, 纯浪费。
        self._compact_cache: Optional[tuple[int, str]] = None
        # 压缩摘要熔断器: LLM 摘要连续失败 N 次后本会话停用（详情见
        # _build_compact_summary）。防"压缩激活 + 端点持续故障"的组合把
        # 每轮请求都拖进一次注定失败的 side-call。
        self._summary_fail_streak = 0
        self._summary_disabled = False
        # Session Memory 中间层: 后台增量维护的滚动摘要（压缩时零调用,
        # 详见 compact.SessionMemory）。_memory_busy 防并发消化（单飞行）,
        # 守护线程随 run_turn 结束拉起, 崩了只警告不追责——它始终只是
        # 优化, 覆盖不全时现场摘要兜底。
        self._session_memory = SessionMemory()
        self._memory_busy = False
        # 重复只读调用护栏状态: (tool_name, input) -> (执行次数, 记账时的
        # 变异序号)。写入/bash 等副作用工具推进变异序号, 序号变了视为
        # 首次（文件真的变了, 重读合法）。压缩激活时清零——旧结果可能
        # 已被归档, 重读重新合法。并行执行进池线程, 访问须持锁。
        self._mutation_seq = 0
        # 键为 _read_guard_key 的产出（read_file 是规范化路径+范围, 其余是
        # 精确输入）, 值为 (执行次数, 记账时的变异序号)
        self._read_calls: dict[tuple, tuple[int, int]] = {}
        # 本回合调用时钟: 所有工具调用都 +1（与变异判定解耦——sqlcmd/
        # python 这类"可能改状态"的命令恰是排查螺旋的主力, 若挂在只读
        # 连击上会被反复清零, 检查点在唯一需要它的地方失明, 见
        # CONCLUSION_CHECKPOINT 注释）。仅新用户回合/压缩激活清零
        self._turn_call_clock = 0
        # 已触发的档位: 并行批次一次跨过倍数也要命中
        self._checkpoint_mark = 0
        self._guard_lock = threading.Lock()
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

    def set_system_prompt(self, sections: list[str]) -> None:
        """整体替换基础系统提示（Web 端 skills 热装卸后重同步用）。
        重建生效视图, 权限模式段照常联动。"""
        self._system_prompt = list(sections)
        self._rebuild_effective_prompt()

    def set_command_allowlist(self, rules: list) -> None:
        """用户命令前缀白名单（Web 设置页保存后热更新给所有活跃 runtime）。
        策略对象与会话各持一份, 这里只更新本 runtime 的这份。"""
        self._permission_policy.set_command_allowlist(rules)

    def set_workspace_roots(self, roots: list) -> None:
        """workspace 根热更新（会话改绑目录 / 附加目录增删后推给活跃
        runtime）。写路径分级与 shell 敏感路径扫描以此为准。"""
        self._permission_policy.set_workspace_roots(roots)

    def add_session_allow_rule(self, rule: str) -> None:
        """本会话临时命令白名单规则（审批卡"本会话允许"/CLI 的 s 选项）。
        只改本 runtime 策略对象的这份, 不落盘。"""
        self._permission_policy.add_session_allow_rule(rule)

    def set_command_denylist(self, rules: list) -> None:
        """deny 规则热更新（Web 设置页保存后推给所有活跃 runtime）。
        任一命令段命中即整体拒绝, 优先于一切 allow。"""
        self._permission_policy.set_command_denylist(rules)

    def set_sensitive_paths(self, paths: list) -> None:
        """用户敏感路径热更新（Web 设置页增删后推给所有活跃 runtime）。"""
        self._permission_policy.set_sensitive_paths(paths)

    def thinking_level(self) -> str:
        return self._thinking_level

    def set_thinking_level(self, level: str) -> None:
        self._thinking_level = level

    def model(self) -> Optional[str]:
        """本会话请求覆盖的模型名; None = 跟随 api_client 当前模型。"""
        return self._model

    def set_model(self, model: Optional[str]) -> None:
        self._model = model

    def set_api_client(self, api_client) -> None:
        """热替换 api_client（会话切换到跨 provider 模型时, 服务端构建
        会话专属 client 挂进来）。仅替换引用, 不迁移运行态。"""
        self._api_client = api_client
        # 端点已换: 旧端点上的摘要熔断不再成立
        self._summary_fail_streak = 0
        self._summary_disabled = False

    def set_utility_client(self, client: Optional[ApiClient]) -> "ConversationRuntime":
        """绑定 side-call 专用 client（utilityProvider 小模型）。压缩摘要、
        会话记忆摘要这类"整理型"调用走它, 主循环不受影响。
        None = 未配置, side-call 跟主模型（原行为）。"""
        self._utility_client = client
        return self

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

    def _register_read_call(self, tool_name: str, tool_input: str) -> Optional[int]:
        """护栏记账 + 只读连击计数。纯读工具返回本次是第几次执行（1=首次）;
        bash/powershell 只读命令不清零计数（ls/grep/git log 类探查不该
        让护栏失忆——实测 bash 密集的会话里旧规则把护栏清成名存实亡）,
        其余工具推进变异序号、清零连击, 返回 None。并行执行下持锁串行记账。"""
        readonly = tool_name in PURE_READ_TOOLS or (
            tool_name in MUTATING_SHELL_TOOLS
            and shell_command_is_read_only(tool_name, tool_input))
        if tool_name in PURE_READ_TOOLS:
            key = _read_guard_key(tool_name, tool_input)
            with self._guard_lock:
                count, at_seq = self._read_calls.get(key, (0, self._mutation_seq))
                if at_seq != self._mutation_seq:
                    count = 0               # 有写入介入: 结果会变, 视作首次
                count += 1
                self._read_calls[key] = (count, self._mutation_seq)
                self._turn_call_clock += 1
                return count
        with self._guard_lock:
            self._turn_call_clock += 1
            if not readonly:
                self._mutation_seq += 1
        return None

    def _maybe_conclusion_checkpoint(self, msgs: List[Message],
                                     question: str) -> None:
        """结论检查点: 本回合调用时钟跨过 CONCLUSION_CHECKPOINT_EVERY 的
        倍数档位时（14→19 的并行批次也算跨过 16）, 把对靶提醒附在最新的
        工具结果输出上（不改 is_error, 不新增消息）。提醒引用原始问题,
        强制模型自查"此刻在回答用户, 还是在跑自创支线"。模型全是 frozen
        的: 用 model_copy 重建消息, 替换列表槽位。"""
        with self._guard_lock:
            n = self._turn_call_clock
            tier = n // CONCLUSION_CHECKPOINT_EVERY
            if n < CONCLUSION_CHECKPOINT_EVERY or tier <= self._checkpoint_mark:
                return
            self._checkpoint_mark = tier
        q = " ".join((question or "").split())[:160]
        note = "\n\n" + CONCLUSION_CHECKPOINT_TEXT.format(n=n, q=q)
        for idx in range(len(msgs) - 1, -1, -1):
            msg = msgs[idx]
            if msg.role != "tool" or not msg.content:
                continue
            block = msg.content[0]
            if hasattr(block, "output") and "[System note] Pacing checkpoint" not in block.output:
                new_block = block.model_copy(update={"output": block.output + note})
                msgs[idx] = msg.model_copy(update={"content": [new_block]})
            return

    def _execute_tool(self, tool_block: ToolContentBlock, pre_res: HookResult) -> Message:
        """执行管线: 工具本体 + Pre/Post hook 反馈合并 + 重复只读护栏。
        无其他共享可变状态, 同一条消息里相互独立的 tool_use 可由 run_turn
        并发调度（护栏状态自身持锁）。"""
        repeat_n = self._register_read_call(tool_block.name, tool_block.input)
        if repeat_n is not None and repeat_n >= REPEAT_DENY_FROM:
            # 拒绝执行: 结果已在历史里, 拒绝零损失。is_error 让模型把它
            # 当反馈读, 而不是当成又一次"成功但没看懂"的结果。
            return Message.tool_result(
                id = tool_block.id,
                name = tool_block.name,
                output = REPEAT_DENIED_TEXT,
                is_error = True,
            )
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
        if repeat_n is not None and repeat_n >= REPEAT_WARN_ON:
            # 第 2 次: 结果照常给（容忍一次健忘）, 但把"再犯会被拒"说明白
            output = f"{output}\n\n{REPEAT_WARN_TEXT}"

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
        # 手动触发 = 用户明确要求重试: 复位摘要熔断器
        self._summary_fail_streak = 0
        self._summary_disabled = False
        keep_from = cut_point(
            self._session.messages,
            config=CompactionConfig(max_estimated_tokens=0),
        )
        if keep_from == 0:
            return "Nothing to compact!"
        return (f"Compacted view active! Archived {keep_from} "
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
        # 真正能压掉东西才亮信号（消息数 <= preserve_recent 时无安全切割点）
        if cut_point(
            self._session.messages,
            config=CompactionConfig(max_estimated_tokens=0),
        ) == 0:
            return False
        self._compact_active = True
        # 旧只读结果可能已被归档出模型视图: 重读重新合法, 护栏清零重记;
        # 压缩后模型需要重新取证, 调用时钟与检查点一并清零留出宽限期
        with self._guard_lock:
            self._read_calls.clear()
            self._turn_call_clock = 0
            self._checkpoint_mark = 0
        if self._on_compacted is not None:
            try:
                self._on_compacted()
            except Exception as e:
                print(f"[WARN] compact hook failed: {e}")

        return True

    # 摘要重算余量: 保留区比"preserve_recent + 余量"还长时才值得重新
    # 切割+重算摘要; 未超余量就复用旧切割点, 保留区温和变长。没有这个
    # 余量, 激活后的每次迭代都会触发一次全量摘要重算。
    _COMPACT_RESUMMARY_MARGIN = 6

    # 摘要熔断阈值: LLM 压缩摘要连续失败达到该次数, 本会话停用 LLM 摘要
    # （见 _build_compact_summary; 手动 /compact 或热换 api_client 复位）
    _SUMMARY_BREAKER_LIMIT = 3

    # Session Memory 消化触发阈值: 新增消息攒够条数才值得一次后台摘要
    # side-call（摊薄成本; 太频 = 每轮都烧一次调用, 太疏 = 压缩时覆盖不全）
    _MEMORY_DIGEST_MIN_NEW = 16

    def _llm_summarize(self, archived: List[Message],
                       prev_summary: Optional[str]) -> str:
        """LLM 摘要 side-call: 把被归档的历史（或上一摘要+新归档增量）交给
        模型生成结构化摘要。这是请求路径上的一次独立调用——
        - 不进会话历史, usage 不进轮次累加器（不干扰预算/迭代口径）;
        - include_tools=False: 摘要器不需要工具, 也不该有工具可调;
        - emit_output=False: 摘要过程不在终端回放;
        - thinking 用 low 档: 摘要是整理不是推理, 控制耗时。
        配置了 utilityProvider 时走小模型 client（set_utility_client）,
        否则跟主模型。任何失败（端点错误/打断/空返回/客户端不支持
        side-call 参数）都向上抛, 由 _build_compact_summary 回退规则摘要。"""
        # 打断检查点: 用户已叫停时不再发起摘要 side-call——摘要要花一次
        # 完整的模型调用, 停止语义下多等它跑完违背直觉。TurnInterrupted
        # 在这里与流后打断同语义, 由调用方（run_turn 工作线程）统一收束。
        if self._cancel_check is not None and self._cancel_check():
            raise TurnInterrupted()
        conv: List[Message] = []
        if prev_summary:
            conv.append(Message.user_text(
                "Previous summary covering the earlier part of this "
                "conversation:\n\n" + prev_summary))
        conv.extend(archived)
        conv.append(Message.user_text(SUMMARY_INSTRUCTION))
        events = (self._utility_client or self._api_client).stream(
            system_prompt=[SUMMARIZER_SYSTEM_PROMPT],
            messages=conv,
            thinking_level="low",
            include_tools=False,
            emit_output=False,
        )
        text = "".join(e.text for e in events
                       if isinstance(e, TextDeltaEvent)).strip()
        if not text:
            raise ValueError("summarizer returned empty text")
        return text

    def _build_compact_summary(self, msgs: List[Message], keep_from: int) -> str:
        """生成归档区摘要: LLM 摘要为主, 任何失败回退规则摘要。已有旧摘要
        时只把增量部分（msgs[旧切割点:新切割点]）交给模型合并——重算发生在
        会话已逼近阈值时, 全量重喂 750k 级历史既慢又贵。

        熔断器: LLM 摘要连续失败 _SUMMARY_BREAKER_LIMIT 次（端点持续故障）
        后, 本会话停用 LLM 摘要、直接走规则摘要——否则压缩激活状态下每轮
        请求都会重试一次注定失败的 side-call, 白烧调用且拖慢响应。
        手动 /compact 或热换 api_client 时复位（用户明确要求重试/端点已换）。"""
        def _rule_fallback() -> str:
            return format_compact_summary(summarize_messages(msgs[:keep_from]))

        if self._summary_disabled:
            return _rule_fallback()
        # Session Memory 中间层（零调用路径）: 后台滚动摘要已覆盖归档区
        # → 直接当压缩结果, 现场摘要 side-call 都不用发
        mem = self._session_memory
        if mem.summary and mem.digested >= keep_from:
            return mem.summary
        prev_summary: Optional[str] = None
        archived = msgs[:keep_from]
        if self._compact_cache is not None:
            cached_cut, cached_summary = self._compact_cache
            if 0 < cached_cut < keep_from:
                prev_summary = cached_summary
                archived = msgs[cached_cut:keep_from]
        # 滚动摘要覆盖了归档区前段且比压缩缓存更新: 当增量起点用,
        # 现场摘要只喂 digested 之后的剩余部分
        if (mem.summary and 0 < mem.digested < keep_from
                and mem.digested > (self._compact_cache[0]
                                    if self._compact_cache else 0)):
            prev_summary = mem.summary
            archived = msgs[mem.digested:keep_from]
        try:
            summary = self._llm_summarize(archived, prev_summary)
        except TurnInterrupted:
            raise   # 用户打断: 不做规则摘要兜底, 直接收束本轮
        except Exception as e:
            self._summary_fail_streak += 1
            if self._summary_fail_streak >= self._SUMMARY_BREAKER_LIMIT:
                self._summary_disabled = True
                print(f"[WARN] llm summarize 连续失败 "
                      f"{self._summary_fail_streak} 次, 本会话停用 LLM 压缩摘要, "
                      f"改用规则摘要（手动 /compact 复位）")
            else:
                print(f"[WARN] llm summarize failed, fallback to rule-based: {e}")
            return _rule_fallback()
        self._summary_fail_streak = 0
        return summary

    # --- Session Memory 中间层: 后台增量维护滚动摘要 ---
    # （三层体系: MicroCompact 清旧结果 → 这里预建摘要 → 现场全量摘要兜底;
    #   详见 compact.SessionMemory）

    def _kick_session_memory_update(self) -> None:
        """轮次收束后拉起后台消化。单飞行（正在消化就跳过, 下轮再攒）、
        攒够 _MEMORY_DIGEST_MIN_NEW 条增量才动。守护线程: 进程退出不等它,
        摘要丢了也只是回落现场摘要。"""
        with self._guard_lock:
            if self._memory_busy:
                return
            if not self._session_memory.digest_due(
                    len(self._session.messages), self._MEMORY_DIGEST_MIN_NEW):
                return
            self._memory_busy = True
        threading.Thread(target=self._run_session_memory_update,
                         daemon=True, name="session-memory").start()

    def _run_session_memory_update(self) -> None:
        """后台消化: 把未消化消息增量合并进滚动摘要。走 _llm_summarize
        side-call（不进会话历史、无工具、不回放——自然不存在"摘要任务
        触发压缩"的递归）。失败放弃本批, 指针不动, 下轮重试。"""
        try:
            msgs = self._session.messages
            while True:
                start = self._session_memory.digested
                end = len(msgs)
                if end - start < self._MEMORY_DIGEST_MIN_NEW:
                    break
                # 快照: 消化期间历史可能继续增长, 多出的部分留给下一轮
                chunk = list(msgs[start:end])
                merged = self._llm_summarize(
                    chunk, self._session_memory.summary or None)
                if not merged:
                    break
                self._session_memory.summary = merged
                self._session_memory.digested = end
        except Exception as e:
            print(f"[WARN] session memory digest failed (retry next turn): {e}")
        finally:
            self._memory_busy = False

    def _microcompact_view(self, view: List[Message]) -> List[Message]:
        """MicroCompact: 估算 token 超过软阈值时, 把保留窗口之外的可复现
        工具结果替换为占位符（块结构与 role 原样, 配对不破坏）。纯视图
        操作——session.messages 不动, 展示层看不到占位符。清完即稳定:
        视图骤降, 直到新内容再次长过阈值才动下一次, 缓存不会反复失效。"""
        if estimate_session_tokens(view) <= MICROCOMPACT_TRIGGER_TOKENS:
            return view
        result_slots = [
            i for i, m in enumerate(view)
            if m.role == "tool" and len(m.content) == 1
            and isinstance(m.content[0], ToolResultContentBlock)
            and m.content[0].name in MICROCOMPACT_COMPACTABLE_TOOLS
            and len(m.content[0].output) >= MICROCOMPACT_MIN_CHARS
        ]
        keep = set(result_slots[-MICROCOMPACT_KEEP_RECENT:])
        out = list(view)
        changed = False
        cleared: list[tuple[str, str]] = []   # 被清结果的 (tool_name, tool_use_id)
        for i in result_slots:
            if i in keep:
                continue
            m, block = out[i], out[i].content[0]
            if block.output.startswith(MICROCOMPACT_PLACEHOLDER):
                continue
            # 占位符带落盘路径（若原文有）: 被清掉的旧结果可经 read_file 找回,
            # 不再是黑洞。延迟导入防环——tools 依赖 runtime 的 ToolError。
            from tools import resumable_spill_path
            spill = resumable_spill_path(block.output)
            placeholder = MICROCOMPACT_PLACEHOLDER
            if spill:
                placeholder += (f"\n[Full output saved to `{spill}` — recover "
                                "with read_file (offset/limit) if needed.]")
            new_block = block.model_copy(update={"output": placeholder})
            out[i] = m.model_copy(update={"content": [new_block]})
            changed = True
            cleared.append((block.name or "", block.id))
        if changed:
            # 内容已清出模型视图, "重读零信息"的前提失效: 同步摘除护栏
            # 记账, 否则死锁——模型被指去看"历史里的结果", 历史里却是
            # 占位符, 重读又被第 3 次拒绝
            entries = []
            for name, tid in cleared:
                found = self._tool_input_for_id(view, tid)
                if found is not None:
                    entries.append(found)
                else:
                    entries.append((name, ""))
            self._forget_read_calls(entries)
        return out if changed else view

    @staticmethod
    def _tool_input_for_id(view: List[Message], tool_use_id: str):
        """按 tool_use_id 在视图里找配对 assistant 消息的 (name, input)。
        找不到（理论上不发生——tool_result 必有配对的 tool_use）返回 None。"""
        for m in view:
            if m.role != "assistant" or not m.content:
                continue
            for b in m.content:
                if isinstance(b, ToolContentBlock) and b.id == tool_use_id:
                    return b.name, b.input
        return None

    def _forget_read_calls(self, entries: list) -> None:
        """微压缩清掉工具结果后同步摘除护栏记账。read_file 按规范化路径
        整组摘（同路径不同范围的分页读结果同样已不可见）。"""
        with self._guard_lock:
            for entry in entries:
                if not entry:
                    continue
                name, inp = entry
                key = _read_guard_key(name, inp)
                self._read_calls.pop(key, None)
                if name == "read_file" and len(key) > 2:
                    dead = [k for k in self._read_calls
                            if k[0] == "read_file" and k[1] == key[1]]
                    for k in dead:
                        self._read_calls.pop(k, None)

    def _post_compact_restore_text(self, archived: List[Message]) -> str:
        """压缩后文件重注入: 从归档区收集最近读过的文件（最多 5 个, 每个
        截 5k 字符）, 随续接摘要带回。相对路径按 cwd 解析（CLI 场景与工具
        执行一致; 解析/读取失败静默跳过——重注入是便利, 不是正确性依赖）。"""
        paths: List[str] = []
        for m in reversed(archived):
            if len(paths) >= POST_COMPACT_MAX_FILES:
                break
            if m.role != "assistant":
                continue
            for b in m.content:
                if isinstance(b, ToolContentBlock) and b.name == "read_file":
                    try:
                        p = str(json.loads(b.input).get("path") or "")
                    except Exception:
                        continue
                    if p and p not in paths:
                        paths.append(p)
                    if len(paths) >= POST_COMPACT_MAX_FILES:
                        break
        if not paths:
            return ""
        sections: List[str] = []
        for p in paths:
            try:
                fp = Path(p)
                if not fp.is_absolute():
                    fp = Path.cwd() / fp
                text = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            snippet = text[:POST_COMPACT_CHARS_PER_FILE]
            more = "\n[... truncated ...]" if len(text) > len(snippet) else ""
            sections.append(f"### {p}\n{snippet}{more}")
        if not sections:
            return ""
        return ("\n\n## Recently read files (re-injected verbatim — do not "
                "re-read unless you need a different range):\n\n"
                + "\n\n".join(sections))

    def _model_view(self) -> List[Message]:
        """给模型的会话视图: 压缩激活时 = [续接摘要] + 保留区(纯函数,
        摘要按切割点缓存), 否则原样返回全量历史。展示层永远读全量——
        压缩只影响模型。两个分支出口都过 microcompact（旧工具结果清除）。"""
        msgs = self._session.messages
        if not self._compact_active:
            return self._microcompact_view(msgs)
        config = CompactionConfig(max_estimated_tokens=0)
        if self._compact_cache is not None:
            cached_cut, cached_summary = self._compact_cache
            if (cached_cut > 0 and len(msgs) - cached_cut
                    <= config.preserve_recent_messages + self._COMPACT_RESUMMARY_MARGIN):
                return self._microcompact_view(
                    [continuation_message(cached_summary, preserved=True)]
                    + msgs[cached_cut:])
        keep_from = cut_point(msgs, config)
        if keep_from == 0:
            return self._microcompact_view(msgs)
        summary = (self._build_compact_summary(msgs, keep_from)
                   + self._post_compact_restore_text(msgs[:keep_from]))
        self._compact_cache = (keep_from, summary)
        view = ([continuation_message(summary, preserved=True)]
                + msgs[keep_from:])
        return self._microcompact_view(view)

    def _context_over_compact_threshold(self) -> bool:
        # 口径必须是"最近一次调用"的上下文体积: 每次调用的 input+缓存读写
        # ≈ 该次请求携带的全量历史。若读 current_turn_usage()（按轮累加,
        # 输出预算的正确口径）, 工具迭代 N 次就把 N 份上下文加在一起——
        # 真实上下文 15~20k 时迭代几次即误触发压缩, 模型视图骤降为
        # [摘要]+保留区, 表现为失忆→重读→死循环。
        latest = self.usage().latest_call_usage()
        return latest.context_tokens() >= self._auto_compact_threshold



    def run_turn(self, user_input: str, prompter: Optional[PermissionPrompter]=None,
                 attachments: Optional[list[dict]]=None) -> TurnSummary:
        iterations = 0
        curr_session = self._session
        tool_results : list[Message] = []
        assistant_messages = []
        self.usage().begin_turn()    # 本轮用量归零（含思考）; 预算检查读同一累加器
        with self._guard_lock:
            self._turn_call_clock = 0    # 新用户回合: 调用时钟/检查点重新计数
            self._checkpoint_mark = 0
        budget_exhausted = False
        iterations_exhausted = False
        auto_compacted = False
        output_recoveries = 0    # max_tokens 截断恢复已用次数

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
            # 修补。服务端 max_tokens 管单次调用上限，这里管跨次累加
            # （口径 = usage 按轮累加器, begin_turn 在 run_turn 顶部清零）。
            # 收束原因作为 user 消息落进历史: 对模型说明"被掐了、证据都在、
            # 下一轮直接干活"，堵死"下一轮从头再查"的分支。
            if self.usage().current_turn_usage().output_tokens >= self._turn_output_budget:
                budget_exhausted = True
                curr_session.messages.append(
                    Message.user_text(TURN_BUDGET_EXHAUSTED_NOTICE))
                self._notify_iterate()   # 一致点: 收束说明已落定
                break
            if iterations >= self._max_iterations:
                iterations_exhausted = True
                curr_session.messages.append(
                    Message.user_text(TURN_ITERATIONS_EXHAUSTED_NOTICE))
                self._notify_iterate()
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
                model=self._model,
            )
            # 流内打断: api_client 在流式消费循环里查 should_stop 命中后
            # 补 stop_reason="interrupted" 的合成 stop 返回（不抛异常,
            # 已流出内容保住）。这里照常入账/入历史, 再把消息里的 tool_use
            # 就地终局（补 error result + 通知前端闭合工具卡）后抛
            # TurnInterrupted——与"打断落在授权后执行前"的一致点同一出口。
            # 打断标记须在 build_assistant_message 之前算好: 零内容打断时
            # 该函数会因 blocks 为空直接抛"无消息内容!", 走不到下面的分支。
            flow_interrupted = any(
                isinstance(e, MessageStopEvent)
                and e.stop_reason == INTERRUPTED_STOP_REASON
                for e in events)
            # 打断落在零内容阶段（思考/建连/工具参数流式前）: 流里只有合成
            # stop, 没有任何 text/tool_use。没有消息可入史——构造空 content
            # 的 assistant 消息既过不了 build_assistant_message 的校验
            # （"无消息内容!"），塞进历史也会被 API 拒绝。记完账直接按
            # TurnInterrupted 收束, 与部分内容打断同一出口（前端只显示
            # "已停止", 不出错误气泡）。合成 stop 已带 usage, 用量不丢。
            if flow_interrupted and not any(
                    isinstance(e, (TextDeltaEvent, ToolUseEvent))
                    for e in events):
                stop_usage = next((e.usage for e in events
                                   if isinstance(e, MessageStopEvent)), None)
                if stop_usage is not None:
                    self.usage().record(usage=_token_usage_from(stop_usage))
                self._notify_iterate()   # 一致点: 用户消息已落定, 无需修补
                raise TurnInterrupted()
            message, token_usage = build_assistant_message(events)
            truncated = any(
                isinstance(e, MessageStopEvent) and e.stop_reason == "max_tokens"
                for e in events)
            assistant_messages.append(message)
            if token_usage:
                self.usage().record(usage=token_usage)
            curr_session.messages.append(message)

            if flow_interrupted:
                for block in message.content:
                    if isinstance(block, ToolContentBlock):
                        interrupted_result = Message.tool_result(
                            id=block.id,
                            name=block.name,
                            output="(用户中断了本轮对话)",
                            is_error=True,
                        )
                        curr_session.messages.append(interrupted_result)
                        tool_results.append(interrupted_result)
                        if self._on_tool_finalized is not None:
                            try:
                                self._on_tool_finalized(block,
                                                        interrupted_result)
                            except Exception as e:
                                print(f"[WARN] tool-finalized hook failed: {e}")
                self._notify_iterate()   # 一致点: 中断结果已回填
                raise TurnInterrupted()

            tool_use_blocks = []
            for block in message.content:
                if isinstance(block, ToolContentBlock):
                    tool_use_blocks.append(block)

            if not tool_use_blocks:
                # max_tokens 截断自愈: 输出被掐断且没有工具调用时, 注入恢复
                # 提示继续循环（恢复提示禁止道歉/复述——那会烧更多输出 token
                # 使问题恶化）。最多恢复 3 次, 之后按普通收束结束轮次。
                if truncated and output_recoveries < MAX_OUTPUT_TOKENS_RECOVERY:
                    output_recoveries += 1
                    curr_session.messages.append(
                        Message.user_text(OUTPUT_TRUNCATED_NOTICE))
                    self._notify_iterate()
                    continue
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
            self._maybe_conclusion_checkpoint(curr_session.messages, user_input)
            self._notify_iterate()   # 一致点: 本迭代的工具结果已全部回填

        if self._maybe_auto_compact():
            auto_compacted = True

        # 空闲点: 本轮已收束, 历史一致——后台把新增消息消化进滚动摘要,
        # 下次压缩就有机会零调用直接用
        self._kick_session_memory_update()

        return TurnSummary(
            assistant_messages=assistant_messages,
            tool_results=tool_results,
            iterations=iterations,
            usage=self.usage().current_turn_usage(),   # 本轮用量（累计口径走 usage()）
            auto_compacted= auto_compacted,
            budget_exhausted=budget_exhausted,
            iterations_exhausted=iterations_exhausted,
        )














