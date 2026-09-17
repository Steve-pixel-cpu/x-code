from typing import Protocol, Optional, List

from pydantic import BaseModel

from api_client import AssistantEvent, TextDeltaEvent, ToolUseEvent, MessageStopEvent, ApiClient
from compact import compact_session, CompactionConfig
from hooks import HookRunner
from models import Message, TextContentBlock, AnyContentBlock, ToolContentBlock, Session
from permissions import PermissionMode, PermissionPolicy, PermissionPrompter, PermissionDecision

DEFAULT_MAX_ITERATIONS = 128
DEFAULT_AUTO_COMPACT_THRESHOLD =  200_000
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

class ToolExecutor(Protocol):
    def execute(self, tool_name: str, input: str) -> str: ...
    # 成功返回字符串，失败抛 ToolError



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
    auto_compacted: bool
    budget_exhausted: bool = False
    iterations_exhausted: bool = False


# --- ConversationRuntime ---
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

    def thinking_level(self) -> str:
        return self._api_client.thinking_level

    def set_thinking_level(self, level: str) -> None:
        self._api_client.set_thinking_level(level)

    def _process_tool_use(self, tool_block: ToolContentBlock, prompter: Optional[PermissionPrompter]=None)-> Message | None:

        result = self._permission_policy.authorize(
            tool_name=tool_block.name,
            input=tool_block.input,
            prompter=prompter,
        )
        if result.decision == PermissionDecision.DENY:
            return Message.tool_result(
                id = tool_block.id,
                name = tool_block.name,
                output= result.reason,
                is_error = True,
            )
        elif result.decision == PermissionDecision.ALLOW:
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
                )

            is_tool_error = False
            try:
                output = self._tool_executor.execute(
                    tool_name=tool_block.name,
                    input=tool_block.input,
                )
            except Exception as e:
                output = str(e)
                is_tool_error = True

            tool_output = output
            output = merge_hook_feedback(messages=pre_res.messages, output=output, denied=False)
            post_res = self._hook_runner.run_post_tool_use(
                tool_name=tool_block.name,
                tool_input=tool_block.input,
                tool_output=tool_output,
                is_error= is_tool_error,
            )
            output = merge_hook_feedback(messages=post_res.messages, output=output, denied=post_res.denied)

            return Message.tool_result(
                id = tool_block.id,
                name = tool_block.name,
                output = output,
                is_error = is_tool_error or post_res.denied,
            )
    def compact(self)->  str:
        curr_session = self._session
        compact_reslut = compact_session(
            messages=curr_session.messages,
            config=CompactionConfig(
                max_estimated_tokens=0
            ),
        )
        if compact_reslut.removed_count == 0:
            return "Nothing to compact!"

        curr_session.messages = compact_reslut.compacted_messages
        return f"Compact sussess! Remove count = {compact_reslut.removed_count}."

    def _maybe_auto_compact(self)-> bool:
        # 信号: 最近一次调用的 input_tokens ≈ 当前上下文占用。
        # 旧实现用累计 input（计费口径，单调涨），和上下文大小无关。
        latest = self.usage().current_turn_usage()
        if latest.input_tokens >= self._auto_compact_threshold:
            curr_session = self._session
            compact_reslut = compact_session(
                messages=curr_session.messages,
                config=CompactionConfig(
                    max_estimated_tokens = 0
                ),
            )
            if compact_reslut.removed_count == 0:
                return True
            curr_session.messages = compact_reslut.compacted_messages

            return True

        return False



    def run_turn(self, user_input: str, prompter: Optional[PermissionPrompter]=None) -> TurnSummary:
        iterations = 0
        curr_session = self._session
        tool_results : list[Message] = []
        assistant_messages = []
        turn_output_tokens = 0       # 本轮累计输出（含思考），循环层预算的计量
        budget_exhausted = False
        iterations_exhausted = False

        curr_session.messages.append(Message.user_text(user_input))
        while True:
            # 循环层预算检查点: 收束发生在这里——上一迭代的工具结果已全部
            # 回填，会话历史一致，break 不会产生悬空 tool_use，也不需要异常
            # 修补。服务端 max_tokens 管单次调用上限，这里管跨次累加。
            if turn_output_tokens >= self._turn_output_budget:
                budget_exhausted = True
                break
            if iterations >= self._max_iterations:
                iterations_exhausted = True
                break

            iterations += 1
            events = self._api_client.stream(system_prompt=self._system_prompt, messages=curr_session.messages)
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

            for block in tool_use_blocks:
                tool_result_msg = self._process_tool_use(block,prompter)
                if tool_result_msg:
                    curr_session.messages.append(tool_result_msg)
                    tool_results.append(tool_result_msg)

        auto_compacted = self._maybe_auto_compact()

        return TurnSummary(
            assistant_messages=assistant_messages,
            tool_results=tool_results,
            iterations=iterations,
            usage=self.usage().cumulative_usage(),
            auto_compacted= auto_compacted,
            budget_exhausted=budget_exhausted,
            iterations_exhausted=iterations_exhausted,
        )














