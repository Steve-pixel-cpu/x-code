import contextlib
import json
import subprocess
from json import JSONDecodeError

import anthropic
from pydantic import BaseModel
from abc import ABC, abstractmethod
from typing import List, Literal, Dict, final, Optional

from models import Message, ToolResultContentBlock, ToolContentBlock, TextContentBlock
from retry import (
    ApiError as RetryApiError,
    ConnectionError as RetryConnectionError,
    AuthError as RetryAuthError,
    HttpApiError as RetryHttpApiError,
    send_with_retry,
)


class TextDeltaEvent(BaseModel):
    type: Literal['text_delta'] = 'text_delta'
    text: str


class ToolUseEvent(BaseModel):
    type: Literal['tool_use'] = 'tool_use'
    id: str
    name: str
    input: str


class UsageInfo(BaseModel):
    """一次模型调用的 token 用量（来自流式事件的 message_start / message_delta）。"""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class MessageStopEvent(BaseModel):
    type: Literal['message_stop'] = 'message_stop'
    usage: Optional[UsageInfo] = None


AssistantEvent = TextDeltaEvent | ToolUseEvent | MessageStopEvent

# 思考指示器的终端样式：暗灰色、单行原地刷新。只用于终端展示，
# 绝不进入事件流/会话历史。
ANSI_DIM = "\033[2m"
ANSI_RESET = "\033[0m"
ANSI_CLEAR_LINE = "\033[K"   # 清除光标到行尾（配合 \r 原地更新）
THINKING_MARKER = "✻ 思考中…"

# 思考等级 → thinking.budget_tokens。GLM-5.3-flash 强制思考无法关闭，只能调
# 深浅；"max" 不传参数走模型默认（最高档）。budget 须 ≥1024 且 < max_tokens。
THINKING_LEVELS = ("low", "medium", "high", "max")
THINKING_LEVEL_TO_BUDGET = {
    "low": 2048,
    "medium": 8192,
    "high": 16384,
}


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


def _collect_usage(acc: dict, usage) -> None:
    """把 SDK 用量对象里非 None 的字段并进 acc（message_start 与 message_delta 各报一部分）。"""
    for field in ("input_tokens", "output_tokens",
                  "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = getattr(usage, field, None)
        if value is not None:
            acc[field] = value


def _map_to_retry_error(e: Exception) -> Optional[RetryApiError]:
    """anthropic SDK 异常 → retry.ApiError，决定可否重试。None = 非 API 错误, 直接抛。"""
    if isinstance(e, anthropic.AuthenticationError):
        return RetryAuthError(str(e))
    if isinstance(e, anthropic.RateLimitError):
        return RetryHttpApiError(429, str(e))
    if isinstance(e, anthropic.APIConnectionError):   # 含 APITimeoutError
        return RetryConnectionError(str(e))
    if isinstance(e, anthropic.APIStatusError):
        return RetryHttpApiError(e.status_code, str(e))
    return None


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
                 emit_output: bool = True,
                 thinking_level: str = "medium",
                 base_url: str | None = None):

        self.model = model
        self.tools = tools or []
        self.emit_output = emit_output
        self.thinking_level = thinking_level
        # 供应商配置: base_url/api_key 可在运行期经 configure() 切换
        self._api_key = api_key
        self._base_url = base_url
        # 流式读超时: 两条流式事件之间最大间隔 300s。没有它，一条 stalled 的
        # 连接会让 run_turn 永久挂死（CLI 卡死 / Web 端 busy 永远不解锁）
        # max_retries=0: SDK 自带重试关闭, 重试策略（退避/上限）统一归 retry.py
        self.raw_client = anthropic.Anthropic(api_key=api_key, base_url=base_url,
                                              timeout=300.0, max_retries=0)
        self.client = self.raw_client

    def configure(self,
                  base_url: str | None = None,
                  api_key: str | None = None,
                  model: str | None = None) -> None:
        """运行期切换供应商/模型。base_url/api_key 有实质变化才重建底层客户端;
        None = 保持不变。注意: server 端在调用后需重新挂自己的镜像代理。"""
        if model:
            self.model = model
        new_key = api_key if api_key is not None else self._api_key
        new_url = base_url if base_url is not None else self._base_url
        if new_key != self._api_key or new_url != self._base_url:
            self._api_key = new_key
            self._base_url = new_url
            self.raw_client = anthropic.Anthropic(
                api_key=new_key, base_url=new_url or None,
                timeout=300.0, max_retries=0)
            self.client = self.raw_client

    def reset_to(self, api_key: str, model: str, base_url: str | None = None) -> None:
        """无条件重置为给定配置（回退 .env 默认时用, 会清掉自定义 base_url）。"""
        self._api_key = api_key
        self._base_url = base_url
        self.model = model
        self.raw_client = anthropic.Anthropic(api_key=api_key, base_url=base_url,
                                              timeout=300.0, max_retries=0)
        self.client = self.raw_client

    def set_thinking_level(self, level: str) -> None:
        self.thinking_level = level

    def stream(self, system_prompt: list[str], messages: list[Message],
               thinking_level: Optional[str] = None) -> List[AssistantEvent]:
        """thinking_level 可选参数: 多会话共用 client 时, 每轮调用携带
        自己会话的思考等级, 避免共享实例状态互相串。None = 用实例默认
        （CLI 单会话语义不变）。"""
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
        level = thinking_level if thinking_level is not None else self.thinking_level
        budget = THINKING_LEVEL_TO_BUDGET.get(level)
        if budget is not None:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        streaming_text = False      # 正在流式输出正式回复文本
        streaming_thinking = False  # 思考指示器行正在原地刷新（仅终端，不进事件流）
        thinking_chars = 0          # 当前思考块累计字符数
        # token 用量: input 侧在 message_start，output 侧在 message_delta。
        # output_tokens 含思考 tokens——思考文本不进历史，但用量进，循环层预算靠它。
        usage_acc: dict = {}
        # 部分Windows控制台默认关闭 ANSI（VT）转义支持；shell 跑一次空命令
        # 会经 cmd.exe 初始化控制台从而启用。旧式写法是 os.system("")（已软废弃）
        subprocess.run("", shell=True)

        import sys as _sys
        out = _sys.stdout
        stack = contextlib.ExitStack()
        try:
            # 建连阶段（连接失败/超时/429/5xx）经 retry.py 退避重试（默认再试 2 次）;
            # 一旦开始收事件就不再重试——重放会让内容重复, 流中断直接抛给上层。
            def _open_stream():
                try:
                    # ExitStack 只在进入成功后登记清理: 失败的尝试无残留, 可安全重试
                    return stack.enter_context(self.client.messages.stream(**kwargs))
                except Exception as e:
                    api_err = _map_to_retry_error(e)
                    if api_err is None:
                        raise
                    raise api_err from e
            stream = send_with_retry(_open_stream)
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
                elif event.type == 'message_start':
                    usage = getattr(event.message, "usage", None)
                    if usage is not None:
                        _collect_usage(usage_acc, usage)

                elif event.type == 'message_delta':
                    usage = getattr(event, "usage", None)
                    if usage is not None:
                        _collect_usage(usage_acc, usage)
                    if self.emit_output:
                        streaming_thinking = _end_thinking_indicator(out, streaming_thinking)
                        streaming_text = _stop_text_line(out, streaming_text)
                    if event.delta.stop_reason == "max_tokens":
                        if self.emit_output:
                            out.write("输出被 max_tokens 截断!")

                elif event.type == 'message_stop':
                    streaming_thinking = _end_thinking_indicator(out, streaming_thinking)
                    streaming_text = _stop_text_line(out, streaming_text)
                    if self.emit_output:
                        # 收尾无条件复位 ANSI 样式，防止灰色泄漏到正式输出
                        out.write(ANSI_RESET)
                        out.flush()

                    events.append(MessageStopEvent(
                        usage=UsageInfo(**usage_acc) if usage_acc else None,
                    ))
        finally:
            stack.close()

        return  events
