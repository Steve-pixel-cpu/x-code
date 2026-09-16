import json
import subprocess
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

# 思考内容的终端样式：暗灰色。只用于终端展示，绝不进入事件流/会话历史
ANSI_DIM = "\033[2m"
ANSI_RESET = "\033[0m"
THINKING_MARKER = "──── 思考中 ────"


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
            "system": _system_prompt,
            "max_tokens": 32768,

        }
        if self.tools:
            kwargs["tools"] = self.tools
        streaming_text = False      # 正在流式输出正式回复文本
        streaming_thinking = False  # 正在流式输出思考内容（仅终端，不进事件流）
        # 部分Windows控制台默认关闭 ANSI（VT）转义支持；shell 跑一次空命令
        # 会经 cmd.exe 初始化控制台从而启用。旧式写法是 os.system("")（已软废弃）
        subprocess.run("", shell=True)

        import sys as _sys
        with self.client.messages.stream(**kwargs) as stream:
            blocks = {}
            for event in stream:
                if event.type == 'content_block_start':
                    cb = event.content_block
                    if self.emit_output and streaming_thinking:
                        # 新块开始：先结束思考样式，防止灰色泄漏进新块
                        _sys.stdout.write("\n" + ANSI_RESET)
                        _sys.stdout.flush()
                        streaming_thinking = False
                    if cb.type == "tool_use":
                        blocks[event.index] = {
                            "type": "tool_use",
                            "id": cb.id,
                            "name": cb.name,
                            "json": ""
                        }
                    else:
                        blocks[event.index] = {
                            "type": cb.type,

                        }


                elif event.type == 'content_block_delta':
                    if event.delta.type == 'text_delta':
                        if self.emit_output:
                            if streaming_thinking:
                                # 思考结束转正式回复：换行并恢复正常颜色
                                _sys.stdout.write(ANSI_RESET + "\n")
                                _sys.stdout.flush()
                                streaming_thinking = False
                                streaming_text = True
                            if not streaming_text:
                                _sys.stdout.write("\n")
                                streaming_text = True
                            _sys.stdout.write(event.delta.text)
                            _sys.stdout.flush()
                        events.append(TextDeltaEvent(text=event.delta.text))
                    elif event.delta.type == 'input_json_delta':
                        blocks.get(event.index,{"json":""})["json"] += event.delta.partial_json
                    elif event.delta.type == 'thinking_delta':
                        # 思考内容只进终端（暗灰色），绝不 append 进 events 列表：
                        # 一旦进入就会被存入会话历史并重放，污染上下文
                        if self.emit_output:
                            if streaming_text:
                                _sys.stdout.write("\n")
                                streaming_text = False
                            if not streaming_thinking:
                                _sys.stdout.write(f"{ANSI_DIM}{THINKING_MARKER}\n")
                                streaming_thinking = True
                            _sys.stdout.write(event.delta.thinking)
                            _sys.stdout.flush()
                    else:
                        # 其余 delta（如 signature_delta）暂不处理
                        pass
                elif event.type == 'content_block_stop':
                    info = blocks.pop(event.index, None)
                    if info and info["type"] == "tool_use":
                        if self.emit_output and streaming_text:
                            _sys.stdout.write("\n")
                            _sys.stdout.flush()
                            streaming_text = False

                        events.append(ToolUseEvent(id=info["id"], name=info["name"], input=info["json"] or "{}"))
                elif event.type == 'message_delta':
                    if self.emit_output:
                        if streaming_thinking:
                            _sys.stdout.write("\n" + ANSI_RESET)
                            _sys.stdout.flush()
                            streaming_thinking = False
                        elif streaming_text:
                            _sys.stdout.write("\n")
                            _sys.stdout.flush()
                            streaming_text = False
                    if event.delta.stop_reason == "max_tokens":
                        _sys.stdout.write("输出被 max_tokens 截断!")



                elif event.type == 'message_stop':
                    if self.emit_output and (streaming_text or streaming_thinking):
                        _sys.stdout.write("\n")
                        _sys.stdout.flush()
                        streaming_text = False
                        streaming_thinking = False
                    if self.emit_output:
                        # 收尾无条件复位 ANSI 样式，防止灰色泄漏到正式输出
                        _sys.stdout.write(ANSI_RESET)
                        _sys.stdout.flush()

                    events.append(MessageStopEvent())

        return  events


