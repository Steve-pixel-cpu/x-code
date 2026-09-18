"""测试: retry.py 接入 api_client —— 建连可重试错误退避重试, 非重试错误立即抛出。"""
import anthropic
import httpx2 as httpx
import pytest

from api_client import ClaudeApiClient
from retry import HttpApiError as RetryHttpApiError


class _NS:
    """模拟 anthropic SDK 原始事件对象（stream() 按 .type 属性分派）。"""
    def __init__(self, **kw):
        self.__dict__.update(kw)


_RAW_SDK_EVENTS = [
    _NS(type="message_start",
        message=_NS(usage=_NS(input_tokens=5, output_tokens=None,
                              cache_creation_input_tokens=None,
                              cache_read_input_tokens=None))),
    _NS(type="message_delta",
        delta=_NS(stop_reason="end_turn"),
        usage=_NS(input_tokens=None, output_tokens=6,
                  cache_creation_input_tokens=None,
                  cache_read_input_tokens=None)),
    _NS(type="message_stop"),
]


def _anthropic_error(status: int) -> anthropic.APIStatusError:
    resp = httpx.Response(status, request=httpx.Request("POST", "https://example.com"))
    cls = {429: anthropic.RateLimitError, 400: anthropic.BadRequestError}[status]
    return cls(f"HTTP {status}", response=resp, body=None)


def _make_client(monkeypatch, fail_times: int, exc: Exception):
    """构造 ClaudeApiClient 并把底层客户端换成可编程的假流: 前 fail_times 次进流即抛。"""
    calls = {"n": 0}

    class FakeStream:
        def __enter__(self):
            calls["n"] += 1
            if calls["n"] <= fail_times:
                raise exc
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            yield from _RAW_SDK_EVENTS

    class FakeMessages:
        def stream(self, **kwargs):
            return FakeStream()

    client = ClaudeApiClient(api_key="k", model="m")
    monkeypatch.setattr(client, "client",
                        type("FakeClient", (), {"messages": FakeMessages()})())
    return client, calls


def test_stream_retries_on_rate_limit_then_succeeds(monkeypatch):
    client, calls = _make_client(monkeypatch, fail_times=1, exc=_anthropic_error(429))
    events = client.stream(system_prompt=["s"], messages=[])
    assert calls["n"] == 2                       # 第一次 429 退避后重试成功
    assert events[-1].usage.input_tokens == 5    # 事件流完整, 无重复


def test_stream_does_not_retry_non_retryable(monkeypatch):
    client, calls = _make_client(monkeypatch, fail_times=1, exc=_anthropic_error(400))
    with pytest.raises(RetryHttpApiError):
        client.stream(system_prompt=["s"], messages=[])
    assert calls["n"] == 1                       # 400 不可重试: 立即抛出


def test_sdk_builtin_retry_is_disabled():
    """SDK 自带重试必须关闭: 重试策略统一归 retry.py, 避免双重退避。"""
    client = ClaudeApiClient(api_key="k", model="m")
    assert client.raw_client.max_retries == 0
