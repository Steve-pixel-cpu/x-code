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

# 思考指示器的终端样式：暗灰色、单行原地刷新。只用于终端展示，
# 绝不进入事件流/会话历史。
ANSI_DIM = "\033[2m"
ANSI_RESET = "\033[0m"
ANSI_CLEAR_LINE = "\033[K"   # 清除光标到行尾（配合 \r 原地更新）
THINKING_MARKER = "✻ 思考中…"


def _end_thinking_indicator(out, streaming_thinking: bool) -> bool:
    """结束指示器行：换行收尾 + 样式复位。返回新的 streaming_thinking 状态。"""
    if not streaming_thinking:
        return False
    out.write("\n" + ANSI_RESET)
    out.flush()
    return False


def _stop_text_line(out, streaming_text: bool) -> bool:
    """正文流结束一处换行，防止指示器/下一块与正文挤同行。"""
    if not streaming_text:
        return False
    out.write("\n")
    out.flush()
    return False


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
        streaming_thinking = False  # 思考指示器行正在原地刷新（仅终端，不进事件流）
        thinking_chars = 0          # 当前思考块累计字符数
        # 部分Windows控制台默认关闭 ANSI（VT）转义支持；shell 跑一次空命令
        # 会经 cmd.exe 初始化控制台从而启用。旧式写法是 os.system("")（已软废弃）
        subprocess.run("", shell=True)

        import sys as _sys
        out = _sys.stdout
        with self.client.messages.stream(**kwargs) as stream:
            blocks = {}
            for event in stream:
                if event.type == 'content_block_start':
                    cb = event.content_block
                    # 新块开始：先收掉上一块的指示器/正文行，防样式泄漏与挤行
                    streaming_thinking = _end_thinking_indicator(out, streaming_thinking)
                    streaming_text = _stop_text_line(out, streaming_text)
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
                            # 思考→正文衔接：指示器行已在 content_block_start 收尾，
                            # 这里保证正文前光标在新行即可
                            streaming_thinking = _end_thinking_indicator(out, streaming_thinking)
                            if not streaming_text:
                                out.write("\n")
                                streaming_text = True
                            out.write(event.delta.text)
                            out.flush()
                        events.append(TextDeltaEvent(text=event.delta.text))
                    elif event.delta.type == 'input_json_delta':
                        blocks.get(event.index,{"json":""})["json"] += event.delta.partial_json
                    elif event.delta.type == 'thinking_delta':
                        # 思考内容只驱动指示器（暗灰、\r 原地刷新），绝不 append 进
                        # events 列表：一旦进入就会被存入会话历史并重放，污染上下文
                        thinking_chars += len(event.delta.thinking)
                        if self.emit_output:
                            streaming_text = _stop_text_line(out, streaming_text)
                            if not streaming_thinking:
                                out.write(ANSI_DIM)
                                streaming_thinking = True
                            out.write(
                                "\r" + ANSI_CLEAR_LINE
                                + f"{THINKING_MARKER} 已思考 {thinking_chars} 字"
                            )
                            out.flush()
                    else:
                        # 其余 delta（如 signature_delta）暂不处理
                        pass
                elif event.type == 'content_block_stop':
                    info = blocks.pop(event.index, None)
                    if info and info["type"] == "tool_use":
                        streaming_text = _stop_text_line(out, streaming_text)
                        events.append(ToolUseEvent(id=info["id"], name=info["name"], input=info["json"] or "{}"))
                    else:
                        # thinking 块结束：收指示器行
                        streaming_thinking = _end_thinking_indicator(out, streaming_thinking)
                        thinking_chars = 0
                elif event.type == 'message_delta':
                    if self.emit_output:
                        streaming_thinking = _end_thinking_indicator(out, streaming_thinking)
                        streaming_text = _stop_text_line(out, streaming_text)
                    if event.delta.stop_reason == "max_tokens":
                        out.write("输出被 max_tokens 截断!")



                elif event.type == 'message_stop':
                    streaming_thinking = _end_thinking_indicator(out, streaming_thinking)
                    streaming_text = _stop_text_line(out, streaming_text)
                    if self.emit_output:
                        # 收尾无条件复位 ANSI 样式，防止灰色泄漏到正式输出
                        out.write(ANSI_RESET)
                        out.flush()

                    events.append(MessageStopEvent())

        return  events
