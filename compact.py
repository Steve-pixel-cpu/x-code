from typing import List

from pydantic import BaseModel

from models import Message, TextContentBlock, ToolContentBlock, ToolResultContentBlock


class CompactionConfig(BaseModel):
    preserve_recent_messages: int = 4
    max_estimated_tokens: int = 200_000

class CompactionResult(BaseModel):
    summary: str
    formatted_summary: str
    compacted_messages: list[Message]
    removed_count: int


def estimate_message_tokens(msg: Message) -> int:
    res = 0
    for block in msg.content:
        if isinstance(block, TextContentBlock):
            res = res + len(block.text) // 4 + 1
        elif isinstance(block, ToolContentBlock):
            res = res + (len(block.name) + len(block.input)) // 4 + 1
        elif isinstance(block, ToolResultContentBlock):
            res = res + (len(block.name) + len(block.output)) // 4 + 1
    return res


def estimate_session_tokens(msgs: List[Message]) -> int:
    return sum(estimate_message_tokens(msg) for msg in msgs)

def should_compact(msgs: List[Message], config: CompactionConfig) -> bool:
    return len(msgs) > config.preserve_recent_messages and estimate_session_tokens(msgs) > config.max_estimated_tokens


