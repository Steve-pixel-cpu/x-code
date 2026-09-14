from typing import Protocol, Optional, List

from pydantic import BaseModel

from api_client import AssistantEvent, TextDeltaEvent, ToolUseEvent, MessageStopEvent
from models import Message, TextContentBlock, AnyContentBlock, ToolContentBlock


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
        self._latest_turn = None
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
        if isinstance(event, ToolUseEvent):
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

        if isinstance(event, MessageStopEvent):
            finished = True

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
    # 三行逻辑：messages
    # 为空 → 原样返回
    # output；否则拼两段——output
    # 去空白后非空才算一段，hook
    # 消息一段（前缀
    # Hook
    # feedback，denied
    # 时是
    # Hook
    # feedback(denied)），两段之间空行连接。陷阱：别把空的
    # output
    # 也拼进去，会多出难看的空段。
    if not messages:
        return output

    result = ""
    first_segment = output.strip()
    if first_segment:
        result += first_segment
        result += "\n"

    if denied






