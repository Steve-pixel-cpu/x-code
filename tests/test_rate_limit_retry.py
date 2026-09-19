"""测试: 限流(429)专用退避曲线 + on_retry 回调（背景见 retry.py 顶部说明）。

账户级速率限制的窗口是秒~分钟级, 原连接抖动曲线(200ms→400ms, 全程 <1s)
对它形同虚设。429 现在走 2s 起步指数翻倍(上限 30s、最多重试 4 次)的长曲线;
其余可重试错误行为不变。"""
import time

import pytest

import retry
from api_client import ClaudeApiClient
import retry
from retry import HttpApiError, RetriesExhausted, RetryAborted, send_with_retry
from api_client import ClaudeApiClient, StreamInterrupted


@pytest.fixture
def sleeps(monkeypatch):
    """把真实睡眠换成记录, 让退避序列可断言、测试瞬间跑完。"""
    record: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: record.append(s))
    return record


def _always(exc):
    def _raise():
        raise exc
    return _raise


def test_rate_limit_uses_long_curve_and_exhausts_at_five_attempts(sleeps, monkeypatch):
    monkeypatch.setattr(retry.random, "uniform", lambda a, b: 1.0)   # 消抖动
    notes: list[tuple] = []
    with pytest.raises(RetriesExhausted) as ei:
        send_with_retry(_always(HttpApiError(429, "rate limited")),
                        on_retry=lambda a, m, d, e: notes.append((a, m, d)))
    assert ei.value.attempts == 5          # 1 次原始 + 4 次重试
    # 长退避被切成 <=0.2s 的分片轮询打断: 累计值即名义曲线 2/4/8/16s
    assert all(s <= 0.2 + 1e-9 for s in sleeps)
    assert sum(sleeps) == pytest.approx(30.0, abs=1e-6)
    assert [n[0] for n in notes] == [1, 2, 3, 4]   # 即将进行的重试序号
    assert all(n[1] == 4 for n in notes)           # 限流曲线的 max_retries


def test_non_rate_limit_keeps_short_curve(sleeps):
    """408/409/5xx 等其余可重试错误: 尝试次数与累计退避与旧版一致
    （退避睡眠统一按 0.2s 分片轮询打断, 故 0.4s 记为两片）。"""
    with pytest.raises(RetriesExhausted) as ei:
        send_with_retry(_always(HttpApiError(500, "boom")))
    assert ei.value.attempts == 3
    assert sleeps == [0.2, 0.2, 0.2]
    assert sum(sleeps) == pytest.approx(0.6)


def test_rate_limit_recovers_midway(sleeps):
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise HttpApiError(429, "rate limited")
        return "ok"
    assert send_with_retry(flaky) == "ok"
    assert calls["n"] == 3


def test_curve_follows_latest_error(sleeps):
    """曲线按最近一次错误选择: 500 先来走短曲线, 随后 429 切长曲线。"""
    errors = iter([HttpApiError(500, "e"), HttpApiError(429, "r"),
                   HttpApiError(429, "r"), HttpApiError(429, "r"),
                   HttpApiError(429, "r")])
    def alternating():
        raise next(errors)
    with pytest.raises(RetriesExhausted) as ei:
        send_with_retry(alternating)
    assert ei.value.attempts == 5
    assert sleeps[0] == pytest.approx(0.2)   # 首次 500: 旧曲线
    # 之后 429: 切限流长曲线（分片 <=0.2s）, 累计 4/8/16
    tail = sleeps[1:]
    assert all(s <= 0.2 + 1e-9 for s in tail)
    # 总退避 = 0.2(旧曲线) + 限流曲线三窗(各乘 0.8~1.2 抖动)
    assert 0.2 + 28 * 0.8 <= sum(sleeps) <= 0.2 + 28 * 1.2


def test_connection_error_unaffected(sleeps):
    with pytest.raises(RetriesExhausted) as ei:
        from retry import ConnectionError as RetryConnectionError
        send_with_retry(_always(RetryConnectionError("down")))
    assert ei.value.attempts == 3
    assert sleeps == [0.2, 0.2, 0.2]


def test_claude_client_passes_on_retry_through(monkeypatch):
    """客户端把 on_retry 透传给 send_with_retry（Web 端镜像的接缝）。"""
    import api_client as api_mod
    captured = {}
    def fake_send(fn, on_retry=None, should_stop=None):
        captured["on_retry"] = on_retry
        return []
    monkeypatch.setattr(api_mod, "send_with_retry", fake_send)
    callback = lambda *args: None
    client = ClaudeApiClient(api_key="k", model="m",
                             emit_output=False, on_retry=callback)
    events = client.stream(system_prompt=["s"], messages=[])
    assert events == []
    assert captured["on_retry"] is callback


def test_default_client_has_no_retry_callback():
    """默认 None: CLI / subagent 路径静默重试, 行为与旧版一致。"""
    client = ClaudeApiClient(api_key="k", model="m", emit_output=False)
    assert client.on_retry is None


def test_should_stop_aborts_before_first_attempt(sleeps):
    """打断在首次尝试前就绪: 一次都不执行, 直接 RetryAborted。"""
    calls = []
    with pytest.raises(RetryAborted):
        send_with_retry(lambda: calls.append(1), should_stop=lambda: True)
    assert calls == [] and sleeps == []


def test_should_stop_during_backoff_aborts(sleeps, monkeypatch):
    """打断落在退避分片轮询里: 不再发起下一次尝试。"""
    monkeypatch.setattr(retry.random, "uniform", lambda a, b: 1.0)
    calls = []
    flipped = {"done": False}

    def stop():
        if not flipped["done"]:
            flipped["done"] = True
            return False     # 循环顶的首轮检查放行
        return True          # 退避分片轮询: 打断

    def flaky():
        calls.append(1)
        raise HttpApiError(429, "rate limited")

    with pytest.raises(RetryAborted):
        send_with_retry(flaky, should_stop=stop)
    assert calls == [1]      # 只尝试了一次, 退避中即被打断


def test_should_stop_false_never_interferes(sleeps):
    calls = []
    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise HttpApiError(429, "rate limited")
        return "ok"
    assert send_with_retry(flaky, should_stop=lambda: False) == "ok"
    assert len(calls) == 3
    # 总退避 = 2×j1 + 4×j2, 抖动 0.8~1.2 → 落在 [4.8, 7.2]
    assert 4.8 <= sum(sleeps) <= 7.2


def test_stream_raises_stream_interrupted_when_should_stop():
    """should_stop 就绪: stream() 在建连前抛 StreamInterrupted, 不发网络请求。"""
    client = ClaudeApiClient(api_key="k", model="m", emit_output=False,
                             should_stop_provider=lambda: True)
    with pytest.raises(StreamInterrupted):
        client.stream(system_prompt=["s"], messages=[])
