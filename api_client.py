import json
from json import JSONDecodeError

import anthropic
from pydantic import BaseModel
from abc import ABC, abstractmethod
from typing import List, Literal, Dict, final, Optional

from models import Message, ToolResultContentBlock, ToolContentBlock, TextContentBlock


class TextDeltaEvent(BaseModel):
    type: Literal['text_delta'] = 'text_delta'
    text: str


class ToolUseEvent(BaseModel):
    type: Literal['tool_use'] = 'tool_use'
    id: str
    name: str
    input: str


class MessageStopEvent(BaseModel):
    type: Literal['message_stop'] = 'message_stop'


AssistantEvent = TextDeltaEvent | ToolUseEvent | MessageStopEvent


class ApiClient(ABC):
    @abstractmethod
    def stream(self, system_prompt: list[str], messages: list) -> List[AssistantEvent]:
        """流式处理，返回事件列表"""
        ...

def _convert_message(message: list[Message]) -> list[dict]:
    result: list[dict] = []
    for msg in message:
        if msg.role == 'tool':
            content = []
            for block in msg.content:
                if isinstance(block, ToolResultContentBlock):
                    tr: dict = {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": block.output,
                    }
                    if block.is_error:
                        tr["is_error"] = True
                    content.append(tr)
            if content:
                result.append({"role": "user","content": content})
        elif msg.role == "assistant":
            content = []
            for block in msg.content:
                if isinstance(block, TextContentBlock):
                    content.append({"type": "text", "text": block.text})
                elif isinstance(block, ToolContentBlock):
                    try:
                        input_content = json.loads(block.input)
                    except JSONDecodeError:
                        input_content = {"raw": block.input}

                    tr: dict = {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": input_content,
                    }
                    content.append(tr)
            if content:
                result.append({"role": "assistant","content": content})
        elif msg.role == "user":
            content = []
            for block in msg.content:
                if isinstance(block, TextContentBlock):
                    content.append({"type": "text", "text": block.text})
            if content:
                result.append({"role": "user","content": content})
    merged:list[dict] = []
    for entry in result:
        if merged and merged[-1]["role"] == entry["role"]:
            merged[-1]["content"].extend(entry["content"])
        else:
            merged.append(entry)
    return merged


class ClaudeApiClient(ApiClient):
    def __init__(self,
                 api_key: str,
                 model: str,
                 tools: list[dict] | None = None,
                 emit_output: bool = True):

        self.model = model
        self.tools = tools or []
        self.emit_output = emit_output
        # 初始化客户端（提前创建，避免每次 stream 都新建）
        self.client = anthropic.Anthropic(api_key=api_key)


    def stream(self, system_prompt: list[str], messages: list[Message]) -> List[AssistantEvent]:
        events: List[AssistantEvent] = []
        _system_prompt = "\n".join(system_prompt)
        kwargs = {
            "model": self.model,
            "messages": _convert_message(messages),
            "system_prompt": _system_prompt,
            "max_tokens": 8096,

        }
        if self.tools:
            kwargs["tools"] = self.tools
        streaming_text = False  # 追踪是否正在流式输出文本

        import sys as _sys
        with self.client.messages.stream(**kwargs) as stream:
            for event in stream:
                if event.type == 'content_block_delta':
                    if self.emit_output:
                        if not streaming_text:
                            _sys.stdout.write("\n")
                            streaming_text = True
                        _sys.stdout.write(event.delta.text)
                        _sys.stdout.flush()
                    events.append(TextDeltaEvent(text=event.delta.text))

                elif event.type == 'content_block_stop':
                    if event.content_block.type == 'tool_use':
                        if self.emit_output and streaming_text:
                            _sys.stdout.write("\n")
                            _sys.stdout.flush()
                            streaming_text = False
                    events.append(ToolUseEvent(id=event.content_block.id,
                                               name=event.content_block.name,
                                               input=event.content_block.input))

                elif event.type == 'message_stop':
                    if self.emit_output and streaming_text:
                        _sys.stdout.write("\n")
                        _sys.stdout.flush()
                        streaming_text = False

                    events.append(MessageStopEvent())

        return  events


