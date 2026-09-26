import contextlib
import json
import re
import subprocess
import sys
import threading
from json import JSONDecodeError

import anthropic
import httpx2 as httpx
import openai
from openai import OpenAI
from pydantic import BaseModel

# 流式读超时拆分: connect 上限压到 15s——stalled 建连快速失败落入重试
# 循环的 should_stop 轮询点, 打断立即生效（此前 connect/read 共用 300s,
# 等首包最坏干等 5 分钟且不可打断）。read 仍 300s: 两条流式事件之间的
# 最大间隔, 防一条 stalled 连接把 run_turn 永久挂死。
API_CONNECT_TIMEOUT_S = 15.0
API_READ_TIMEOUT_S = 300.0


def _api_stream_timeout() -> httpx.Timeout:
    return httpx.Timeout(connect=API_CONNECT_TIMEOUT_S,
                         read=API_READ_TIMEOUT_S,
                         write=30.0, pool=API_CONNECT_TIMEOUT_S)


def _openai_client(api_key: str, base_url: str | None, timeout,
                   max_retries: int = 0) -> OpenAI:
    """构造 openai 客户端的单点入口, 兼容 openai 3.x 的构造期凭据校验。

    3.x 起 OpenAI(api_key="") 直接抛 Missing credentials（1.x 允许, 调用时
    才校验）。而本项目的「未配置」态就是空 key（server._apply_provider_config
    的回退分支、初始化页接管前的状态）, 必须能构造出客户端。
    SDK 对此提供的开关是私有参数 _enforce_credentials=False（官方注释声明
    未来可能移除）, 所以隔离在这里: 若未来版本删掉该参数, 只需改这一个
    函数（例如换占位 key 方案）, 三处调用点不动。"""
    try:
        return OpenAI(api_key=api_key, base_url=base_url,
                      timeout=timeout, max_retries=max_retries,
                      _enforce_credentials=False)
    except TypeError:
        # 老版本（<3.x）没有该参数: 空 key 本就合法, 直接构造
        return OpenAI(api_key=api_key, base_url=base_url,
                      timeout=timeout, max_retries=max_retries)

from abc import ABC, abstractmethod
from typing import List, Literal, Dict, final, Optional, Callable

from models import (
    Message,
    ToolResultContentBlock,
    ToolContentBlock,
    TextContentBlock,
    ImageContentBlock,
    FileContentBlock,
)
from prompt import SYSTEM_PROMPT_DYNAMIC_BOUNDARY
from retry import (
    ApiError as RetryApiError,
    ConnectionError as RetryConnectionError,
    AuthError as RetryAuthError,
    HttpApiError as RetryHttpApiError,
    RetryAborted,
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
    stop_reason: Optional[str] = None   # "end_turn" | "max_tokens" | ... 截断自愈靠它


AssistantEvent = TextDeltaEvent | ToolUseEvent | MessageStopEvent

# ============================================================================
# 协议中立线级事件（wire events）
#
# 各 ApiClient 实现把自家 SDK 的原始流解析成这一套事件, 按"线上真实顺序"
# 经 stream(on_event=...) 回调观察者。协议线格式只在实现内解析一次;
# server 的浏览器镜像层/终端回显之外的消费方只认这套事件, 协议知识
# 不出 api_client（新增 OpenAI 协议时 server 零改动）。
#
# 事件顺序约定 = 线上顺序:
#   正文   → WireTextDelta
#   思考块 → WireThinkingStart … WireThinkingDelta … WireThinkingEnd
#   工具块 → WireToolStart(id,name) … (参数分片内部拼装) … WireToolEnd(id,name,input_json)
#   收尾   → [WireUsage] WireStop(stop_reason)   —— 每次调用恰好一个 WireStop
# ============================================================================
class WireTextDelta(BaseModel):
    type: Literal['wire_text_delta'] = 'wire_text_delta'
    text: str


class WireThinkingStart(BaseModel):
    type: Literal['wire_thinking_start'] = 'wire_thinking_start'


class WireThinkingDelta(BaseModel):
    type: Literal['wire_thinking_delta'] = 'wire_thinking_delta'
    text: str   # 思考文本片段（终端只累计字数, server 忽略内容）


class WireThinkingEnd(BaseModel):
    type: Literal['wire_thinking_end'] = 'wire_thinking_end'


class WireToolStart(BaseModel):
    type: Literal['wire_tool_start'] = 'wire_tool_start'
    id: str
    name: str


class WireToolEnd(BaseModel):
    type: Literal['wire_tool_end'] = 'wire_tool_end'
    id: str
    name: str
    input_json: str   # 完整参数 JSON 串（解析前保证非空, 空则 "{}"）


class WireUsage(BaseModel):
    """一次调用的 token 用量, 在 WireStop 之前恰好发一次（如拿到的话）。"""
    type: Literal['wire_usage'] = 'wire_usage'
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class WireStop(BaseModel):
    type: Literal['wire_stop'] = 'wire_stop'
    stop_reason: Optional[str] = None   # 内部归一值: "end_turn"|"max_tokens"|"tool_use"|...


WireEvent = (WireTextDelta | WireThinkingStart | WireThinkingDelta
             | WireThinkingEnd | WireToolStart | WireToolEnd
             | WireUsage | WireStop)

WireObserver = Callable[[WireEvent], None]

# 支持的供应商协议。缺省 anthropic —— 既有配置不含 protocol 字段时行为不变。
KNOWN_PROTOCOLS = ("anthropic", "openai")
DEFAULT_PROTOCOL = "anthropic"


def normalize_protocol(value) -> str:
    """供应商 protocol 字段归一; 空/None = anthropic（向后兼容）。非法值抛 ValueError。"""
    v = str(value or "").strip().lower() or DEFAULT_PROTOCOL
    if v not in KNOWN_PROTOCOLS:
        raise ValueError(f"unsupported protocol {value!r} (known: {KNOWN_PROTOCOLS})")
    return v


_BASE_URL_V1_TAIL = re.compile(r"/v1/?$", re.IGNORECASE)


def normalize_base_url(url, protocol: str = "anthropic") -> str:
    """规范化供应商 base_url（按协议分规则）。CLI 与 Web 的保存/测试/应用
    三处共用, 行为一致。

    anthropic: SDK 在 base_url 后自动拼 /v1/messages, 用户照 OpenAI 习惯
    粘贴带 /v1 的地址会请求 /v1/v1/messages → 404。统一剥掉结尾的字面
    /v1 段与多余斜杠（智谱 /api/anthropic 这类真实路径原样保留）。
    openai: 实际请求 URL = base_url + "/chat/completions", 版本段须由
    用户自带（官方约定 base_url 以 /v1 结尾）。因此 /v1 原样保留、裸
    主机补缺省 /v1, 仅去尾斜杠; 自定义前缀路径（企业网关等）原样保留。
    """
    text = str(url or "").strip()
    while text.endswith("/"):
        text = text[:-1]
    if normalize_protocol(protocol) == "openai":
        if not text:
            return ""
        rest = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", text)
        return text if "/" in rest else text + "/v1"
    return _BASE_URL_V1_TAIL.sub("", text)

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

# 思考等级 → OpenAI reasoning_effort（OpenAI 协议线）。跨端点没有统一的
# 思考参数, 这里取事实标准 reasoning_effort（OpenAI 官方, 多数兼容端点
# 跟随; DeepSeek/Qwen 等不认的端点报 400 时剥参重试一次并本实例禁用,
# 见 OpenAIApiClient.stream 的降级分支）。"max" 无对应档位: OpenAI 最高
# 就是 high, 且端点缺省多为 medium, 映射成 high 才保住"最大思考"语义。
THINKING_LEVEL_TO_REASONING_EFFORT = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "high",
}

# --- prompt caching ---
# 断点（tools 末位 → system 静态段 → system 动态段 → messages 最后一块滚动）
# 之间的前缀在迭代间逐字节一致, 命中后服务端对前缀只按缓存读计价/预填充
# ——长会话里每步的输入成本和首 token 延迟都随历史增长而不再随之线性变贵
# 变慢。动态段（环境/git 快照/CLAUDE.md/技能清单）会话内字节级稳定, 给它
# 打断点是为 messages 前缀断裂的时机（压缩/MicroCompact 换视图、计划模式
# 切换）兜底: tools+整个 system 仍按缓存读, 只有 messages 部分全价。四处
# 断点 = Anthropic 允许的上限。
CACHE_CONTROL = {"type": "ephemeral"}

_ansi_lock = threading.Lock()
_ansi_enabled = False


def _ensure_ansi() -> None:
    """部分Windows控制台默认关闭 ANSI（VT）转义支持；shell 跑一次空命令
    会经 cmd.exe 初始化控制台从而启用。旧式写法是 os.system("")（已软废弃）。
    进程内一次就够——放在 stream() 里会让每次 LLM 调用都冷启动一个 cmd.exe。"""
    global _ansi_enabled
    with _ansi_lock:
        if _ansi_enabled:
            return
        try:
            subprocess.run("", shell=True)
        except OSError:
            pass
        _ansi_enabled = True


def _split_system_prompt(sections: list[str]) -> tuple[list[str], list[str]]:
    """按 SYSTEM_PROMPT_DYNAMIC_BOUNDARY 把 sections 分成（静态, 动态）两段,
    边界标记本身只是构建器与 API 客户端之间的内部约定, 不发给模型。
    没有标记时全部视为静态——缓存只要求会话内前缀一致, 自定义 prompt 天然满足。"""
    if SYSTEM_PROMPT_DYNAMIC_BOUNDARY not in sections:
        return list(sections), []
    idx = sections.index(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)
    return sections[:idx], sections[idx + 1:]


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


class _TerminalEcho:
    """流式终端回显状态机（仅 ClaudeApiClient.stream 内使用）。

    收拢解耦前散落在事件循环里的三块终端状态（正文行/思考指示器行/思考
    字数）。emit=False 时全部 no-op。样式与换行行为与解耦前逐字节一致;
    这里只管终端显示, 绝不影响 wire 事件与返回事件。"""
    def __init__(self, emit: bool):
        self.enabled = emit
        self.text_open = False       # 正在流式输出正式回复文本
        self.thinking_open = False   # 思考指示器行正在原地刷新（仅终端，不进事件流）
        self.thinking_chars = 0      # 当前思考块累计字符数
        self.out = sys.stdout

    def on_block_start(self):
        """新内容块开始：先收掉上一块的指示器/正文行，防样式泄漏与挤行"""
        self.thinking_open = _end_thinking_indicator(self.out, self.thinking_open)
        self.text_open = _stop_text_line(self.out, self.text_open)

    def on_text(self, text: str):
        if not self.enabled:
            return
        # 思考→正文衔接：指示器行已在块开始收尾，这里保证正文前光标在新行
        self.thinking_open = _end_thinking_indicator(self.out, self.thinking_open)
        if not self.text_open:
            self.out.write("\n")
            self.text_open = True
        self.out.write(text)
        self.out.flush()

    def on_thinking(self, text: str):
        """思考内容只驱动指示器（暗灰、\\r 原地刷新），不产生任何事件。"""
        if not self.enabled:
            return
        self.text_open = _stop_text_line(self.out, self.text_open)
        if not self.thinking_open:
            self.out.write(ANSI_DIM)
            self.thinking_open = True
        self.thinking_chars += len(text)
        self.out.write(
            "\r" + ANSI_CLEAR_LINE
            + f"{THINKING_MARKER} 已思考 {self.thinking_chars} 字"
        )
        self.out.flush()

    def on_thinking_end(self):
        self.thinking_open = _end_thinking_indicator(self.out, self.thinking_open)
        self.thinking_chars = 0

    def on_tool_end(self):
        if not self.enabled:
            return
        self.text_open = _stop_text_line(self.out, self.text_open)

    def on_message_delta(self):
        if not self.enabled:
            return
        self.thinking_open = _end_thinking_indicator(self.out, self.thinking_open)
        self.text_open = _stop_text_line(self.out, self.text_open)

    def max_tokens_notice(self):
        if not self.enabled:
            return
        self.out.write("输出被 max_tokens 截断!")

    def finish(self):
        self.thinking_open = _end_thinking_indicator(self.out, self.thinking_open)
        self.text_open = _stop_text_line(self.out, self.text_open)
        if self.enabled:
            # 收尾无条件复位 ANSI 样式，防止灰色泄漏到正式输出
            self.out.write(ANSI_RESET)
            self.out.flush()


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


class StreamInterrupted(Exception):
    """用户打断: 在建连前的检查点或重试退避的轮询点抛出。
    语义与 server 端 SSE 代理的 TurnInterrupted 一致——朝安全侧收束本轮。"""


# 流内打断的合成 stop_reason: stream() 消费循环里查 should_stop 命中后,
# 补一个携带此标记的 MessageStopEvent 返回（不抛异常, 已流出内容保住）。
# runtime 据此收束本轮。取值刻意与 Anthropic 原生 stop_reason 空间不相交。
INTERRUPTED_STOP_REASON = "interrupted"


class ApiClient(ABC):
    """模型客户端抽象: 协议线格式 → 中立事件（wire）/ 会话事件（Assistant）。

    子类实现 stream(): 解析自家 SDK 的原始流, 先把线级事件按线上顺序回调
    on_event 观察者（server 的浏览器镜像挂在这里）, 同时做终端回显并
    聚合返回 AssistantEvent 列表。协议知识只存在于实现内部。
    """

    #: 供应商协议标识, 子类固定（"anthropic" / "openai"）
    protocol: str = ""

    def __init__(self, on_retry: Optional[Callable[[int, int, float, RetryApiError], None]] = None):
        # 限流退避回调: send_with_retry 每次退避睡眠前调用, 参数 =
        # (即将进行的重试序号, 本曲线 max_retries, 退避秒数, 触发的错误)。
        # None = 静默重试（CLI / subagent 默认）。
        self.on_retry = on_retry

    @abstractmethod
    def stream(self, system_prompt: list[str], messages: list,
               thinking_level: Optional[str] = None, *,
               model: Optional[str] = None,
               include_tools: bool = True,
               emit_output: Optional[bool] = None,
               on_event: Optional[WireObserver] = None) -> List[AssistantEvent]:
        """流式处理，返回事件列表。

        thinking_level / model 可选参数: 多会话共用 client 时, 每轮调用
        携带自己会话的思考等级与模型, 避免共享实例状态互相串。None =
        用实例默认（CLI 单会话语义不变）。
        on_event: 线级事件观察者（可选）。解析过程中按线上真实顺序回调
        WireEvent; None = 无人观察。实现不得因观察者抛异常而改变聚合语义
        （观察者异常直接上抛, 由宿主自负）。
        """
        ...

    def generate_text(self, system: list[str], user: str, max_tokens: int = 512) -> str:
        """非流式单轮文本生成（AI 命名等 side-call）。默认不可用,
        子类按自家协议实现; 无工具、无终端回显、不进会话历史。"""
        raise NotImplementedError(f"{type(self).__name__} 不支持非流式生成")

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
                elif isinstance(block, ImageContentBlock):
                    # 图片块: base64 线格式原样透传, 由视觉模型在服务端看图
                    content.append({
                        "type": "image",
                        "source": dict(block.source),
                    })
                elif isinstance(block, FileContentBlock):
                    # 文本附件: 内部表示转成带分隔头的 text 块发给模型
                    content.append({
                        "type": "text",
                        "text": "--- 附件: " + block.name + " ---\n" + block.text,
                    })
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
    #: Anthropic Messages 协议（线格式解析只发生在本类内部）
    protocol = "anthropic"

    def __init__(self,
                 api_key: str,
                 model: str,
                 tools: list[dict] | None = None,
                 emit_output: bool = True,
                 thinking_level: str = "high",
                 base_url: str | None = None,
                 on_retry: Optional[Callable[[int, int, float, RetryApiError], None]] = None,
                 should_stop_provider: Optional[Callable[[], bool]] = None,
                 on_event_provider: Optional[Callable[[], Optional[WireObserver]]] = None):
        super().__init__(on_retry)
        # 打断检查点: 重试循环与建连入口轮询它, 让打断在退避/静默窗口内
        # 也能立即生效（None = 无人打断, CLI/subagent 默认）
        self._should_stop_provider = should_stop_provider
        # 线级事件观察者工厂: 每次开流时调用, 返回本轮的观察者（None =
        # 无人观察）。server 的浏览器镜像经它按轮挂接（contextvars 绑定）。
        self._on_event_provider = on_event_provider
        self.model = model
        self.tools = tools or []
        self.emit_output = emit_output
        self.thinking_level = thinking_level
        # prompt caching 开关: 端点对 cache_control 报"cache 相关 400"时降级
        # 关闭并本实例不再附加（见 stream() 里 _open_stream 的兜底分支）。
        self._cache_control_ok = True
        # 供应商配置: base_url/api_key 可在运行期经 configure() 切换
        self._api_key = api_key
        self._base_url = base_url
        # 线级事件观察者（协议中立 WireEvent）。解析顺序 stream() 里:
        # 显式 on_event 参数 > on_event_provider() > _on_event 静态值。
        # provider 模式供 server 用: 观察者按轮绑定（contextvars）,
        # 开流时惰性解析当前线程的 sink, 并发会话互不串线——与
        # should_stop_provider 同一套模式。
        self._on_event: Optional[WireObserver] = None
        # 流式读超时: 两条流式事件之间最大间隔 300s。没有它，一条 stalled 的
        # 连接会让 run_turn 永久挂死（CLI 卡死 / Web 端 busy 永远不解锁）
        # max_retries=0: SDK 自带重试关闭, 重试策略（退避/上限）统一归 retry.py
        self.raw_client = anthropic.Anthropic(api_key=api_key, base_url=base_url,
                                              timeout=_api_stream_timeout(),
                                              max_retries=0)
        self.client = self.raw_client

    def configure(self,
                  base_url: str | None = None,
                  api_key: str | None = None,
                  model: str | None = None,
                  on_event: Optional[WireObserver] = None) -> None:
        """运行期切换供应商/模型。base_url/api_key 有实质变化才重建底层客户端;
        None = 保持不变。注意: server 端在调用后需重新挂自己的镜像代理。"""
        if model:
            self.model = model
        if on_event is not None:
            self._on_event = on_event
        new_key = api_key if api_key is not None else self._api_key
        new_url = base_url if base_url is not None else self._base_url
        if new_key != self._api_key or new_url != self._base_url:
            self._api_key = new_key
            self._base_url = new_url
            self.raw_client = anthropic.Anthropic(
                api_key=new_key, base_url=new_url or None,
                timeout=_api_stream_timeout(), max_retries=0)
            self.client = self.raw_client

    def reset_to(self, api_key: str, model: str, base_url: str | None = None,
                 on_event: Optional[WireObserver] = None) -> None:
        """无条件重置为给定配置（回退 .env 默认时用, 会清掉自定义 base_url）。"""
        self._api_key = api_key
        self._base_url = base_url
        self.model = model
        if on_event is not None:
            self._on_event = on_event
        self.raw_client = anthropic.Anthropic(api_key=api_key, base_url=base_url,
                                              timeout=_api_stream_timeout(),
                                              max_retries=0)
        self.client = self.raw_client

    @property
    def api_key(self) -> str:
        """当前 key（subagent worker 工厂等宿主读取用）。"""
        return self._api_key

    @property
    def base_url(self) -> str | None:
        """当前 base_url（同上）。"""
        return self._base_url

    def set_thinking_level(self, level: str) -> None:
        self.thinking_level = level

    def _build_kwargs(self, converted_messages: list[dict], system_prompt: list[str],
                      thinking_level: Optional[str], use_cache: bool,
                      include_tools: bool = True,
                      model: Optional[str] = None) -> dict:
        """组装请求参数。use_cache 时打四处 cache_control 断点（上限）:
        tools 末位、system 静态段、system 动态段、messages 最后一块（滚动
        断点）。滚动断点让"上一迭代结束时的全部历史"成为下一调用的缓存前缀,
        全价只付一次; 动态段断点在 messages 前缀断裂时（压缩换视图/计划
        模式切换）保住 tools+整个 system 的缓存读。
        include_tools=False 用于非会话调用的 side-call（如压缩摘要器）:
        不给工具可调, 也不把工具声明白白算进输入。
        model 覆盖: None = 用实例默认（多会话共用 client 时按轮携带）。"""
        static, dynamic = _split_system_prompt(system_prompt)
        system_blocks: list[dict] = []
        if static:
            block = {"type": "text", "text": "\n\n".join(static)}
            if use_cache:
                block["cache_control"] = dict(CACHE_CONTROL)
            system_blocks.append(block)
        if dynamic:
            block = {"type": "text", "text": "\n\n".join(dynamic)}
            if use_cache:
                block["cache_control"] = dict(CACHE_CONTROL)
            system_blocks.append(block)

        kwargs: dict = {
            "model": model if model is not None else self.model,
            "messages": converted_messages,
            "max_tokens": 32768,
        }
        if system_blocks:
            kwargs["system"] = system_blocks
        if self.tools and include_tools:
            # 浅拷贝: 断点不能写进多会话共享的 spec 列表
            tools = [dict(t) for t in self.tools]
            if use_cache:
                tools[-1] = {**tools[-1], "cache_control": dict(CACHE_CONTROL)}
            kwargs["tools"] = tools
        level = thinking_level if thinking_level is not None else self.thinking_level
        budget = THINKING_LEVEL_TO_BUDGET.get(level)
        if budget is not None:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        if use_cache and converted_messages:
            # 滚动断点。converted 是本次调用现转的临时结构; 先剥掉可能残留的
            # 旧标记再加, 保证降级重建（use_cache=False）后 messages 干净
            last_blocks = converted_messages[-1]["content"]
            if isinstance(last_blocks, list) and last_blocks:
                clean = {k: v for k, v in last_blocks[-1].items()
                         if k != "cache_control"}
                clean["cache_control"] = dict(CACHE_CONTROL)
                last_blocks[-1] = clean
        elif converted_messages:
            last_blocks = converted_messages[-1]["content"]
            if isinstance(last_blocks, list) and last_blocks:
                last_blocks[-1] = {k: v for k, v in last_blocks[-1].items()
                                   if k != "cache_control"}
        return kwargs

    def stream(self, system_prompt: list[str], messages: list[Message],
               thinking_level: Optional[str] = None, *,
               model: Optional[str] = None,
               include_tools: bool = True,
               emit_output: Optional[bool] = None,
               on_event: Optional[WireObserver] = None) -> List[AssistantEvent]:
        """thinking_level / model 可选参数: 多会话共用 client 时, 每轮调用
        携带自己会话的思考等级与模型, 避免共享实例状态互相串。None = 用
        实例默认（CLI 单会话语义不变）。
        include_tools=False / emit_output=None 供 side-call（压缩摘要器）
        使用: 不带工具声明、不在终端回放。emit_output None = 用实例默认。
        on_event: 线级事件观察者（协议中立 WireEvent, 按线上顺序回调）;
        None = 回退实例观察者（见 configure/reset_to）, 都没有则无人观察。"""
        events: List[AssistantEvent] = []
        # 观察者解析顺序: 显式参数 > provider（按轮惰性解析, server 用）
        # > 实例静态值。三个来源都没有则无人观察。
        wire = on_event
        if wire is None and self._on_event_provider is not None:
            wire = self._on_event_provider()
        if wire is None:
            wire = self._on_event
        converted_messages = _convert_message(messages)
        level = thinking_level if thinking_level is not None else self.thinking_level
        kwargs = self._build_kwargs(converted_messages, system_prompt, level,
                                    use_cache=self._cache_control_ok,
                                    include_tools=include_tools, model=model)
        emit = self.emit_output if emit_output is None else emit_output
        echo = _TerminalEcho(emit)
        # token 用量: input 侧在 message_start，output 侧在 message_delta。
        # output_tokens 含思考 tokens——思考文本不进历史，但用量进，循环层预算靠它。
        usage_acc: dict = {}
        stop_reason: Optional[str] = None   # message_delta 报 stop_reason, message_stop 落事件
        if emit and sys.stdout.isatty():
            _ensure_ansi()

        stack = contextlib.ExitStack()
        try:
            # 建连阶段（连接失败/超时/429/5xx）经 retry.py 退避重试（默认再试 2 次）;
            # 一旦开始收事件就不再重试——重放会让内容重复, 流中断直接抛给上层。
            # 重试循环带 should_stop 轮询: 打断在退避/建连静默窗口内也立即生效。
            def _open_stream():
                if (self._should_stop_provider is not None
                        and self._should_stop_provider()):
                    raise StreamInterrupted()
                try:
                    # ExitStack 只在进入成功后登记清理: 失败的尝试无残留, 可安全重试
                    return stack.enter_context(self.client.messages.stream(**kwargs))
                except anthropic.BadRequestError as e:
                    # 个别兼容端点不认 cache_control: 报错文本提到 cache 时剥掉
                    # 断点重建一次并本实例禁用; 其余 400 是真实请求错误,
                    # 维持"立即抛出不重试"的既有语义。
                    if self._cache_control_ok and "cache" in str(e).lower():
                        self._cache_control_ok = False
                        no_cache_kwargs = self._build_kwargs(
                            converted_messages, system_prompt, level,
                            use_cache=False, model=model)
                        try:
                            return stack.enter_context(
                                self.client.messages.stream(**no_cache_kwargs))
                        except Exception as e2:
                            retry_err = _map_to_retry_error(e2)
                            if retry_err is None:
                                raise e2
                            raise retry_err from e2
                    api_err = _map_to_retry_error(e)
                    if api_err is None:
                        raise
                    raise api_err from e
                except Exception as e:
                    api_err = _map_to_retry_error(e)
                    if api_err is None:
                        raise
                    raise api_err from e
            try:
                stream = send_with_retry(_open_stream, on_retry=self.on_retry,
                                         should_stop=self._should_stop_provider)
            except RetryAborted:
                raise StreamInterrupted() from None
            blocks = {}
            stopped = False
            for event in stream:
                if event.type == 'content_block_start':
                    cb = event.content_block
                    echo.on_block_start()
                    if cb.type == "tool_use":
                        blocks[event.index] = {
                            "type": "tool_use",
                            "id": cb.id,
                            "name": cb.name,
                            "json": ""
                        }
                        # 线级观察: 工具块开始即广播（前端提前建"运行中"工具卡）
                        if wire is not None:
                            wire(WireToolStart(id=cb.id, name=cb.name))
                    else:
                        blocks[event.index] = {
                            "type": cb.type,

                        }
                        if cb.type == "thinking" and wire is not None:
                            wire(WireThinkingStart())

                elif event.type == 'content_block_delta':
                    if event.delta.type == 'text_delta':
                        echo.on_text(event.delta.text)
                        if wire is not None:
                            wire(WireTextDelta(text=event.delta.text))
                        events.append(TextDeltaEvent(text=event.delta.text))
                    elif event.delta.type == 'input_json_delta':
                        info = blocks.get(event.index)
                        if info is not None and "json" in info:
                            info["json"] += event.delta.partial_json
                    elif event.delta.type == 'thinking_delta':
                        # 思考内容只驱动指示器与线级观察，绝不 append 进
                        # events 列表：一旦进入就会被存入会话历史并重放，污染上下文
                        echo.on_thinking(event.delta.thinking)
                        if wire is not None:
                            wire(WireThinkingDelta(text=event.delta.thinking))
                    else:
                        # 其余 delta（如 signature_delta）暂不处理
                        pass
                elif event.type == 'content_block_stop':
                    info = blocks.pop(event.index, None)
                    if info and info["type"] == "tool_use":
                        echo.on_tool_end()
                        input_json = info["json"] or "{}"
                        if wire is not None:
                            wire(WireToolEnd(id=info["id"], name=info["name"],
                                             input_json=input_json))
                        events.append(ToolUseEvent(id=info["id"], name=info["name"],
                                                   input=input_json))
                    else:
                        # thinking 块结束：收指示器行
                        echo.on_thinking_end()
                        if wire is not None:
                            wire(WireThinkingEnd())
                elif event.type == 'message_start':
                    usage = getattr(event.message, "usage", None)
                    if usage is not None:
                        _collect_usage(usage_acc, usage)

                elif event.type == 'message_delta':
                    usage = getattr(event, "usage", None)
                    if usage is not None:
                        _collect_usage(usage_acc, usage)
                    echo.on_message_delta()
                    if event.delta.stop_reason == "max_tokens":
                        echo.max_tokens_notice()
                    stop_reason = getattr(event.delta, "stop_reason", None) or stop_reason

                elif event.type == 'message_stop':
                    echo.finish()
                    if wire is not None:
                        if usage_acc:
                            wire(WireUsage(**usage_acc))
                        wire(WireStop(stop_reason=stop_reason))

                    events.append(MessageStopEvent(
                        usage=UsageInfo(**usage_acc) if usage_acc else None,
                        stop_reason=stop_reason,
                    ))
                # 流内打断检查点（循环体末尾）: 先把手头事件完整处理进
                # events, 再查 should_stop——正文/思考/大工具参数 JSON 流式
                # 期间点停止, 收完当前网络块即生效, 不再等整段流自然结束
                # （大参数 JSON 流式可达几十秒）。放在末尾而非入口: 入口检查
                # 会白白丢掉已到达未处理的事件。break 而非抛异常: 保住已
                # 流出内容进历史, 由收尾补合成 stop 让 runtime 在一致点
                # 收束（与流后打断同一出口）。
                if (self._should_stop_provider is not None
                        and self._should_stop_provider()):
                    stopped = True
                    break
            if stopped and not any(isinstance(e, MessageStopEvent)
                                   for e in events):
                # 打断收尾: 补合成 stop 事件（stop_reason="interrupted"）,
                # runtime 据此在一致点收束本轮——补 error tool_result 后抛
                # TurnInterrupted, 历史不留悬空 tool_use。usage 照常携带,
                # 已流出部分的用量不丢。
                echo.finish()
                if wire is not None:
                    if usage_acc:
                        wire(WireUsage(**usage_acc))
                    wire(WireStop(stop_reason=INTERRUPTED_STOP_REASON))
                events.append(MessageStopEvent(
                    usage=UsageInfo(**usage_acc) if usage_acc else None,
                    stop_reason=INTERRUPTED_STOP_REASON,
                ))
        finally:
            stack.close()

        return  events

    def generate_text(self, system: list[str], user: str, max_tokens: int = 512) -> str:
        """非流式单轮文本生成（AI 命名等 side-call）。静态 system 直拼,
        无工具、无终端回显; anthropic 兼容端点都支持无 thinking 的普通请求。"""
        msg = self.raw_client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system="\n\n".join(system) if system else anthropic.NOT_GIVEN,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(
            b.text for b in msg.content if getattr(b, "type", "") == "text"
        )




# ============================================================================
# OpenAI Chat Completions 协议实现
# ============================================================================
def _convert_message_openai(messages: list[Message]) -> list[dict]:
    """内部消息模型 → OpenAI Chat Completions 消息数组。

    与 Anthropic 版的差异:
    - system prompt 不在消息里（stream() 单独在首部拼 system 消息）
    - tool_use 块 → assistant 消息的 tool_calls（arguments 直接用内部保存的
      JSON 串, 不经 dict 往返, 避免浮点/键序漂移）
    - role="tool" 的 tool_result → 每块一条 role="tool" 消息（OpenAI 用
      tool_call_id 配对, 不允许塞进 user 消息）
    - 图片 → image_url（data URL 原样透传, base64 线格式不重编）
    - 文本附件 → 带分隔头的 text（语义与 Anthropic 版一致）
    """
    result: list[dict] = []
    for msg in messages:
        if msg.role == "assistant":
            content_parts: list[str] = []
            tool_calls: list[dict] = []
            for block in msg.content:
                if isinstance(block, TextContentBlock):
                    content_parts.append(block.text)
                elif isinstance(block, ToolContentBlock):
                    tool_calls.append({
                        "id": block.id,
                        "type": "function",
                        "function": {"name": block.name, "arguments": block.input},
                    })
            if content_parts or tool_calls:
                entry: dict = {"role": "assistant", "content": "\n".join(content_parts)}
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                result.append(entry)
        elif msg.role == "tool":
            for block in msg.content:
                if isinstance(block, ToolResultContentBlock):
                    result.append({
                        "role": "tool",
                        "tool_call_id": block.id,
                        "content": block.output,
                    })
        elif msg.role == "user":
            parts: list[dict] = []
            for block in msg.content:
                if isinstance(block, TextContentBlock):
                    parts.append({"type": "text", "text": block.text})
                elif isinstance(block, ImageContentBlock):
                    source = dict(block.source)
                    if source.get("type") == "base64":
                        # 内部: {media_type, data} → data URL（base64 原样透传）
                        parts.append({
                            "type": "image_url",
                            "image_url": {"url": (
                                f"data:{source.get('media_type', 'image/png')}"
                                f";base64,{source.get('data', '')}"
                            )},
                        })
                elif isinstance(block, FileContentBlock):
                    parts.append({
                        "type": "text",
                        "text": "--- 附件: " + block.name + " ---\n" + block.text,
                    })
            if parts:
                if len(parts) == 1 and parts[0]["type"] == "text":
                    result.append({"role": "user", "content": parts[0]["text"]})
                else:
                    result.append({"role": "user", "content": parts})
    return result


def _openai_tools(tools: list[dict]) -> list[dict]:
    """工具声明: anthropic 形状（input_schema）→ OpenAI 形状（parameters）。
    纯结构转换, 不改共享 spec。"""
    return [{"type": "function",
             "function": {"name": t["name"],
                          "description": t.get("description", ""),
                          "parameters": t.get("input_schema")
                          or {"type": "object", "properties": {}}}}
            for t in tools]


def _openai_finish_reason_to_stop(reason: Optional[str]) -> Optional[str]:
    """OpenAI finish_reason → 内部 stop_reason（消费方只有 max_tokens 截断自愈）。"""
    if reason is None:
        return None
    return {"tool_calls": "tool_use",
            "function_call": "tool_use",
            "length": "max_tokens",
            "stop": "end_turn",
            "content_filter": "end_turn"}.get(reason, "end_turn")


class OpenAIApiClient(ApiClient):
    #: OpenAI Chat Completions 协议（线格式解析只发生在本类内部）
    protocol = "openai"

    def __init__(self,
                 api_key: str,
                 model: str,
                 tools: list[dict] | None = None,
                 emit_output: bool = True,
                 thinking_level: str = "high",
                 base_url: str | None = None,
                 on_retry: Optional[Callable[[int, int, float, RetryApiError], None]] = None,
                 should_stop_provider: Optional[Callable[[], bool]] = None,
                 on_event_provider: Optional[Callable[[], Optional[WireObserver]]] = None):
        super().__init__(on_retry)
        self._should_stop_provider = should_stop_provider
        self._on_event_provider = on_event_provider
        self._on_event: Optional[WireObserver] = None
        # reasoning_effort 开关: 端点对它报 400 时降级关闭（见 stream() 里
        # _open_stream 的兜底分支）, 与 ClaudeApiClient 的 cache_control
        # 降级同一套模式。
        self._reasoning_effort_ok = True
        self.model = model
        self.tools = tools or []
        self.emit_output = emit_output
        self.thinking_level = thinking_level
        self._api_key = api_key
        self._base_url = base_url
        # 流式读超时/SDK 自带重试关闭, 语义与 ClaudeApiClient 一致:
        # 重试策略统一归 retry.py
        self.raw_client = _openai_client(api_key, base_url,
                                         timeout=_api_stream_timeout())
        self.client = self.raw_client

    def configure(self,
                  base_url: str | None = None,
                  api_key: str | None = None,
                  model: str | None = None,
                  on_event: Optional[WireObserver] = None) -> None:
        """运行期切换供应商/模型（语义与 ClaudeApiClient.configure 一致）。"""
        if model:
            self.model = model
        if on_event is not None:
            self._on_event = on_event
        new_key = api_key if api_key is not None else self._api_key
        new_url = base_url if base_url is not None else self._base_url
        if new_key != self._api_key or new_url != self._base_url:
            self._api_key = new_key
            self._base_url = new_url
            self.raw_client = _openai_client(new_key, new_url or None,
                                             timeout=_api_stream_timeout())
            self.client = self.raw_client

    def reset_to(self, api_key: str, model: str, base_url: str | None = None,
                 on_event: Optional[WireObserver] = None) -> None:
        """无条件重置为给定配置（语义与 ClaudeApiClient.reset_to 一致）。"""
        self._api_key = api_key
        self._base_url = base_url
        self.model = model
        if on_event is not None:
            self._on_event = on_event
        self.raw_client = _openai_client(api_key, base_url, timeout=300.0)
        self.client = self.raw_client

    @property
    def api_key(self) -> str:
        """当前 key（subagent worker 工厂等宿主读取用）。"""
        return self._api_key

    @property
    def base_url(self) -> str | None:
        """当前 base_url（同上）。"""
        return self._base_url

    def set_thinking_level(self, level: str) -> None:
        self.thinking_level = level

    @staticmethod
    def _map_to_retry_error(e: Exception) -> Optional[RetryApiError]:
        """openai SDK 异常 → retry.ApiError（分类与 anthropic 版平行）。"""
        if isinstance(e, openai.AuthenticationError):
            return RetryAuthError(str(e))
        if isinstance(e, openai.RateLimitError):
            return RetryHttpApiError(429, str(e))
        if isinstance(e, openai.APIConnectionError):   # 含 APITimeoutError
            return RetryConnectionError(str(e))
        if isinstance(e, openai.APIStatusError):
            return RetryHttpApiError(e.status_code, str(e))
        return None

    def _build_kwargs(self, converted_messages: list[dict],
                      system_prompt: list[str], include_tools: bool,
                      thinking_level: Optional[str] = None,
                      model: Optional[str] = None) -> dict:
        """组装请求参数。OpenAI 协议无 cache_control 断点（各家服务端自动
        前缀缓存）; thinking 档位映射 reasoning_effort（端点不认时 400 剥参
        降级, 见 stream() 的 _open_stream）。
        model 覆盖: None = 用实例默认（多会话共用 client 时按轮携带）。"""
        kwargs: dict = {
            "model": model if model is not None else self.model,
            "messages": converted_messages,
            "max_tokens": 32768,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if system_prompt:
            kwargs["messages"] = [
                {"role": "system", "content": "\n\n".join(system_prompt)}
            ] + converted_messages
        if self.tools and include_tools:
            kwargs["tools"] = _openai_tools(self.tools)
        level = thinking_level if thinking_level is not None else self.thinking_level
        if self._reasoning_effort_ok:
            effort = THINKING_LEVEL_TO_REASONING_EFFORT.get(level)
            if effort is not None:
                kwargs["reasoning_effort"] = effort
        return kwargs

    def stream(self, system_prompt: list[str], messages: list[Message],
               thinking_level: Optional[str] = None, *,
               model: Optional[str] = None,
               include_tools: bool = True,
               emit_output: Optional[bool] = None,
               on_event: Optional[WireObserver] = None) -> List[AssistantEvent]:
        """OpenAI 流式: chunk 解析成线级事件（观察者）+ 会话事件（返回值）。

        - delta.content → WireTextDelta / TextDeltaEvent
        - delta.tool_calls 按 index 重组（首片 id/name, 后续拼 arguments;
          个别端点连函数名都分片——id+name 齐了才广播 WireToolStart）
        - delta.reasoning_content（DeepSeek 系思考端点）→ 思考指示器/线级事件
        - usage chunk → 用量; finish_reason → stop_reason; [DONE] 收尾
        thinking_level 与 ClaudeApiClient 同语义: None = 用实例默认, 档位
        映射 reasoning_effort 下发。
        model 可选参数: None = 用实例默认（多会话按轮携带, 与 anthropic 版对齐）。
        """
        events: List[AssistantEvent] = []
        wire = on_event
        if wire is None and self._on_event_provider is not None:
            wire = self._on_event_provider()
        if wire is None:
            wire = self._on_event
        converted = _convert_message_openai(messages)
        kwargs = self._build_kwargs(converted, system_prompt, include_tools,
                                    thinking_level=thinking_level, model=model)
        emit = self.emit_output if emit_output is None else emit_output
        echo = _TerminalEcho(emit)
        if emit and sys.stdout.isatty():
            _ensure_ansi()

        # 工具块拼装中: index → {id, name, args, announced}
        pending_tools: dict[int, dict] = {}
        stopped = False

        def _emit_wire(ev: WireEvent) -> None:
            if wire is not None:
                wire(ev)

        def _open_stream():
            if (self._should_stop_provider is not None
                    and self._should_stop_provider()):
                raise StreamInterrupted()
            try:
                return self.client.chat.completions.create(**kwargs)
            except openai.BadRequestError as e:
                # 个别兼容端点不认 reasoning_effort: 带 400 即剥参重建一次
                # 并本实例禁用。不按错误文本过滤（"Extra inputs are not
                # permitted" 这类报错不点名参数）; 其余 400 是真实请求错误,
                # 重试一次仍会 400 并抛出, 只白付一次快速失败的建连。
                # kwargs 为本次调用私有, 原地剥除让 send_with_retry 的后续
                # 重试与降级视图一致。
                if self._reasoning_effort_ok and "reasoning_effort" in kwargs:
                    self._reasoning_effort_ok = False
                    kwargs.pop("reasoning_effort")
                    try:
                        return self.client.chat.completions.create(**kwargs)
                    except Exception as e2:
                        retry_err = self._map_to_retry_error(e2)
                        if retry_err is None:
                            raise
                        raise retry_err from e2
                retry_err = self._map_to_retry_error(e)
                if retry_err is None:
                    raise
                raise retry_err from e
            except Exception as e:
                retry_err = self._map_to_retry_error(e)
                if retry_err is None:
                    raise
                raise retry_err from e

        try:
            stream = send_with_retry(_open_stream, on_retry=self.on_retry,
                                     should_stop=self._should_stop_provider)
        except RetryAborted:
            raise StreamInterrupted() from None

        usage_acc: dict = {}
        stop_reason: Optional[str] = None
        try:
            for chunk in stream:
                # 流内打断检查点: 与 Anthropic 版同语义——下一个 chunk 即生效,
                # break 后在循环外补合成 stop（stop_reason="interrupted"）,
                # 已流出内容保留, runtime 在一致点收束。
                if (self._should_stop_provider is not None
                        and self._should_stop_provider()):
                    stopped = True
                    break
                if getattr(chunk, "usage", None) is not None:
                    u = chunk.usage
                    if u.prompt_tokens is not None:
                        usage_acc["input_tokens"] = u.prompt_tokens
                    if u.completion_tokens is not None:
                        usage_acc["output_tokens"] = u.completion_tokens
                    # 已含缓存的服务端在 prompt_tokens_details 报缓存读
                    cached = getattr(u, "prompt_tokens_details", None)
                    cached_tokens = getattr(cached, "cached_tokens", None)
                    if cached_tokens:
                        usage_acc["cache_read_input_tokens"] = cached_tokens
                choices = getattr(chunk, "choices", None) or []
                choice = choices[0] if choices else None
                if choice is None:
                    continue   # 纯 usage chunk（最后一个 chunk 只有 usage）
                if choice.finish_reason is not None:
                    stop_reason = (_openai_finish_reason_to_stop(choice.finish_reason)
                                   or stop_reason)
                # finish_reason=tool_calls 到达时参数流必然已完（工具参数先于
                # finish_reason）, 在此收束全部在途工具块——必须在 delta 判断
                # 之前: 部分 OpenAI 兼容端点在收尾 chunk 里给 delta=None
                if choice.finish_reason == "tool_calls":
                    for idx in sorted(pending_tools):
                        info = pending_tools[idx]
                        echo.on_tool_end()
                        input_json = info["args"] or "{}"
                        _emit_wire(WireToolEnd(id=info["id"], name=info["name"],
                                               input_json=input_json))
                        events.append(ToolUseEvent(id=info["id"], name=info["name"],
                                                   input=input_json))
                    pending_tools.clear()
                    continue
                delta = choice.delta
                if delta is None:
                    continue
                text = getattr(delta, "content", None)
                if text:
                    echo.on_text(text)
                    _emit_wire(WireTextDelta(text=text))
                    events.append(TextDeltaEvent(text=text))
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    echo.on_thinking(reasoning)
                    _emit_wire(WireThinkingDelta(text=reasoning))
                for tc in (getattr(delta, "tool_calls", None) or []):
                    info = pending_tools.get(tc.index)
                    fn = getattr(tc, "function", None)
                    if info is None:
                        info = pending_tools[tc.index] = {
                            "id": tc.id or "",
                            "name": (getattr(fn, "name", None) or ""),
                            "args": "",
                            "announced": False,
                        }
                    else:
                        if tc.id:
                            info["id"] = tc.id
                        if getattr(fn, "name", None):
                            info["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        info["args"] += fn.arguments
                    # id 与名字已齐 → 广播开始事件（每块恰好一次）
                    if not info["announced"] and info["id"] and info["name"]:
                        info["announced"] = True
                        echo.on_block_start()
                        _emit_wire(WireToolStart(id=info["id"], name=info["name"]))
        finally:
            echo.finish()

        if stopped and stop_reason is None:
            # 打断收尾: 标记 interrupted 供 runtime 识别收束; stop_reason 非
            # None 说明真实 finish_reason 已到达（打断落在收尾 chunk 之后）,
            # 按正常完成处理。
            stop_reason = INTERRUPTED_STOP_REASON
        if usage_acc:
            _emit_wire(WireUsage(**usage_acc))
        _emit_wire(WireStop(stop_reason=stop_reason))
        events.append(MessageStopEvent(
            usage=UsageInfo(**usage_acc) if usage_acc else None,
            stop_reason=stop_reason,
        ))
        return events

    def generate_text(self, system: list[str], user: str, max_tokens: int = 512) -> str:
        """非流式单轮文本生成（AI 命名等 side-call）。"""
        msgs: list[dict] = []
        if system:
            msgs.append({"role": "system", "content": "\n\n".join(system)})
        msgs.append({"role": "user", "content": user})
        resp = self.raw_client.chat.completions.create(
            model=self.model, max_tokens=max_tokens, messages=msgs,
        )
        if resp.choices and resp.choices[0].message.content:
            return resp.choices[0].message.content
        return ""


def make_api_client(protocol: str, *, api_key: str, model: str,
                    tools: list[dict] | None = None,
                    emit_output: bool = True,
                    thinking_level: str = "high",
                    base_url: str | None = None,
                    on_retry: Optional[Callable[[int, int, float, RetryApiError], None]] = None,
                    should_stop_provider: Optional[Callable[[], bool]] = None,
                    on_event_provider: Optional[Callable[[], Optional[WireObserver]]] = None) -> ApiClient:
    """按协议构造对应客户端。未知协议抛 ValueError（配置解析层负责提示）。"""
    p = normalize_protocol(protocol)
    cls = OpenAIApiClient if p == "openai" else ClaudeApiClient
    return cls(api_key=api_key, model=model, tools=tools,
               emit_output=emit_output, thinking_level=thinking_level,
               base_url=base_url, on_retry=on_retry,
               should_stop_provider=should_stop_provider,
               on_event_provider=on_event_provider)
