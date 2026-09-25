"""prompt caching 规格钉子: 断点位置、边界标记不外发、端点降级。

缓存的价值前提是"前缀逐字节一致": tools 末位、system 静态段、system
动态段、messages 最后一块（滚动）四处断点（Anthropic 上限）把上一迭代
结束时的全部历史钉成下一调用的缓存前缀; 动态段会话内字节级稳定, 它的
断点在 messages 前缀断裂时（压缩换视图/计划模式切换）保住 tools+整个
system 的缓存读。断点数超 4 会被 API 拒绝; 打进共享的 tools spec 列表
会污染其他会话——这两类错误都在这里钉死。

运行方式（在 x-code 目录下）:
    uv run pytest tests/test_prompt_cache.py -v
"""

from types import SimpleNamespace

import anthropic
import httpx2 as httpx

from api_client import CACHE_CONTROL, ClaudeApiClient
from models import Message
from prompt import SYSTEM_PROMPT_DYNAMIC_BOUNDARY


class FakeStream:
    def __init__(self, events=None, enter_error: Exception | None = None):
        self._events = events or []
        self._enter_error = enter_error

    def __enter__(self):
        if self._enter_error is not None:
            raise self._enter_error
        return iter(self._events)

    def __exit__(self, *args):
        return False


def make_client(tools: list[dict] | None = None, script: list[FakeStream] | None = None):
    """ClaudeApiClient + 可编程假流: 每次调用记录 kwargs 并弹出剧本的下一流。"""
    client = ClaudeApiClient(api_key="test", model="glm-5.3-flash", tools=tools or [])
    captured: list[dict] = []
    pending = list(script or [])

    def fake_stream(**kwargs):
        captured.append(kwargs)
        return pending.pop(0) if pending else FakeStream()

    client.client = SimpleNamespace(messages=SimpleNamespace(stream=fake_stream))
    return client, captured


def _bad_request(message: str) -> anthropic.BadRequestError:
    resp = httpx.Response(400, request=httpx.Request("POST", "https://example.com"))
    return anthropic.BadRequestError(message, response=resp, body=None)


# ------------------------------------------------------------
# 断点位置 — system 边界拆分、tools 末位、messages 滚动
# ------------------------------------------------------------

def test_system_静动态段各打断点_边界标记不外发():
    client, captured = make_client()
    client.stream(
        system_prompt=["STATIC RULES", SYSTEM_PROMPT_DYNAMIC_BOUNDARY, "dynamic env"],
        messages=[Message.user_text("hi")],
    )

    system = captured[0]["system"]
    assert system == [
        {"type": "text", "text": "STATIC RULES", "cache_control": CACHE_CONTROL},
        {"type": "text", "text": "dynamic env", "cache_control": CACHE_CONTROL},
    ]


def test_断点总数不超上限4():
    """tools 末位 + system 静态 + system 动态 + messages 末位 = 恰好 4,
    再多任何一处都会被 Anthropic 400 拒绝。"""
    tools = [{"name": "read_file", "input_schema": {}},
             {"name": "bash", "input_schema": {}}]
    client, captured = make_client(tools=tools)
    client.stream(
        system_prompt=["STATIC", SYSTEM_PROMPT_DYNAMIC_BOUNDARY, "dyn"],
        messages=[Message.user_text("hi")],
    )

    kw = captured[0]
    count = sum(t.get("cache_control") is not None for t in kw["tools"])
    count += sum(b.get("cache_control") is not None for b in kw["system"])
    count += sum(b.get("cache_control") is not None
                 for m in kw["messages"] for b in m["content"])
    assert count == 4


def test_messages_滚动断点落在最后一块():
    client, captured = make_client()
    client.stream(system_prompt=["s"], messages=[Message.user_text("hi")])

    (user_msg,) = captured[0]["messages"]
    assert user_msg["role"] == "user"
    assert user_msg["content"][-1]["cache_control"] == CACHE_CONTROL


def test_tools_断点打在末位且不污染共享spec列表():
    tools = [{"name": "read_file", "input_schema": {}},
             {"name": "bash", "input_schema": {}}]
    client, captured = make_client(tools=tools)
    client.stream(system_prompt=["s"], messages=[Message.user_text("hi")])

    sent_tools = captured[0]["tools"]
    assert sent_tools[0].get("cache_control") is None
    assert sent_tools[-1]["cache_control"] == CACHE_CONTROL
    # 断点只进本次请求的浅拷贝, 多会话共享的 spec 列表必须原封不动
    assert all("cache_control" not in t for t in tools)


# ------------------------------------------------------------
# 降级 — 端点不认 cache_control 时剥掉重试一次, 之后本实例不再附加
# ------------------------------------------------------------

def test_cache相关400降级重试并禁用():
    script = [
        FakeStream(enter_error=_bad_request("cache_control: unsupported parameter")),
        FakeStream(),   # 剥掉断点后的重建请求成功
    ]
    client, captured = make_client(script=script)

    client.stream(system_prompt=["STATIC", SYSTEM_PROMPT_DYNAMIC_BOUNDARY, "dyn"],
                  messages=[Message.user_text("hi")])

    assert len(captured) == 2
    assert "cache_control" in captured[0]["system"][0]
    # 重建请求整体干净: system、tools、messages 都不再带断点
    assert all("cache_control" not in b for b in captured[1]["system"])
    assert all("cache_control" not in b
               for m in captured[1]["messages"] for b in m["content"])
    assert client._cache_control_ok is False


def test_禁用后后续调用不再附加断点():
    script = [
        FakeStream(enter_error=_bad_request("cache_control: unsupported parameter")),
        FakeStream(),
        FakeStream(),
    ]
    client, captured = make_client(script=script)
    client.stream(system_prompt=["s"], messages=[Message.user_text("hi")])

    client.stream(system_prompt=["s"], messages=[Message.user_text("again")])

    assert all("cache_control" not in b
               for m in captured[2]["messages"] for b in m["content"])
