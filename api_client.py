from abc import ABC, abstractmethod
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel
import anthropic




class TextDeltaEvent(BaseModel):
    type: str = 'text_delta'
    text: str


class ToolUseEvent(BaseModel):
    type: str = 'tool_use'
    id: str
    name: str
    input: str


class MessageStopEvent(BaseModel):
    type: str = 'message_stop'


AssistantEvent = TextDeltaEvent | ToolUseEvent | MessageStopEvent


class ApiClient(ABC):
    @abstractmethod
    def stream(self, system_prompt: str, messages: list) -> List[AssistantEvent]:
        """流式处理，返回事件列表"""
        pass


class ClaudeApiClient(ApiClient):
    def __init__(self, api_key, model, tools=None):

        if tools is None:
            tools = []
        self.api_key = api_key
        self.model = model
        self.tools = tools
        # 初始化客户端（提前创建，避免每次 stream 都新建）
        self.client = anthropic.Anthropic(api_key=self.api_key)


    def stream(self, system_prompt: str, messages: list) -> List[AssistantEvent]:
        events: List[AssistantEvent] = []

        with self.client.messages.stream(
                model=self.model,
                system=system_prompt,
                messages=messages,
                tools=self.tools,
                max_tokens=1024,
        ) as stream:
            for event in stream:
                if event.type == 'text':
                    events.append(TextDeltaEvent(type=event.get('type'), text=event.get('text')))


        # event.type == 'text' → TextDelta
        # event.type == 'content_block_stop' → 检查是否是 tool_use 块
        # event.type == 'message_stop' → 消息结束

        return  events


