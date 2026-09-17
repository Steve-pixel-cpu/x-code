"""测试: 多会话并行支持 — 会话级思考等级 / 并发上限排队 / 审批无超时。"""
import threading

import pytest
from fastapi.testclient import TestClient

import server
from models import Session
from permissions import PermissionPolicy
from storage import SessionStore


@pytest.fixture()
def client():
    return TestClient(server.app)


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    fake = SessionStore(storage_dir=tmp_path)
    monkeypatch.setattr(server, "store", fake)
    server._pending_sessions.clear()
    return fake


@pytest.fixture(autouse=True)
def clean_web_sessions():
    """测试创建的 WebSession 用后即清——全局 _sessions 里的 fake runtime
    会泄漏进后续请求（如 /api/settings 会遍历 runtime.set_permission_mode）。"""
    saved = dict(server._sessions)
    yield
    server._sessions.clear()
    server._sessions.update(saved)


@pytest.fixture()
def clean_slots():
    """并发槽位/队列测试隔离: 前后各清一次, 不受其他测试残留影响。"""
    with server._turn_slots:
        pass  # 先保证满额可用
    while server._turn_slots.acquire(blocking=False):
        pass  # 把残留的 release 全部吃掉
    for _ in range(server.MAX_CONCURRENT_TURNS):
        server._turn_slots.release()   # 恢复到满额
    server._queued_turns.clear()
    yield
    server._queued_turns.clear()


# ------------------------------------------------------------
# 会话级思考等级: WebSession 持有, runtime 注入, 互不串
# ------------------------------------------------------------

def test_web_session_has_own_thinking_level(client, isolated_store):
    """每个 WebSession 的思考等级初值为全局默认, 但各自独立可改。"""
    a = server.get_or_create_web_session("s-a")
    b = server.get_or_create_web_session("s-b")
    assert a.thinking_level == b.thinking_level  # 初值一致（全局默认）

    a.thinking_level = "high"
    b.thinking_level = "low"
    assert a.thinking_level == "high"            # b 的修改不波及 a
    assert b.thinking_level == "low"


def test_settings_thinking_level_propagates_to_sessions(client, isolated_store):
    """全局设置变更: 存活会话的 runtime 与运行态同步更新。"""
    ws = server.get_or_create_web_session("s-lvl")

    class _FakeRuntime:
        level = None
        def set_thinking_level(self, level):
            self.level = level
    ws.runtime = _FakeRuntime()

    client.post("/api/settings", json={"thinking_level": "max"})

    assert ws.thinking_level == "max"        # 运行态已更新
    assert ws.runtime.level == "max"         # runtime 已注入


def test_runtime_thinking_level_isolation():
    """内核层: runtime 持有自己的等级, 不再读写 api_client 实例状态。"""
    from runtime import ConversationRuntime

    class _Client:
        thinking_level = "medium"
        def stream(self, **kwargs):
            return []

    client = _Client()
    rt = ConversationRuntime(
        session=Session(),
        api_client=client,
        tool_executor=lambda **kw: None,
        permission_policy=PermissionPolicy(active_mode="danger-full-access"),
        system_prompt=[],
    )
    assert rt.thinking_level() == "medium"   # 初值取自 client
    rt.set_thinking_level("low")
    assert rt.thinking_level() == "low"
    assert client.thinking_level == "medium"  # client 实例状态未被改动


# ------------------------------------------------------------
# 全局并发上限: 信号量 + 排队
# ------------------------------------------------------------

def test_turn_queue_releases_and_starts(clean_slots):
    """槽位释放后 _drain_queued_turns 唤醒排队轮次; 跳过已叫停项。"""
    acquired = server._turn_slots.acquire(blocking=False)
    assert acquired  # 测试前置: 先占住一个槽位, 模拟"仅剩一个空位"

    ws_busy = server.get_or_create_web_session("q-busy")
    ws_stop = server.get_or_create_web_session("q-stop")
    for ws in (ws_busy, ws_stop):
        ws.busy = True
    ws_stop.stop_requested = True   # 排队期间被叫停 → 应被跳过

    server._queued_turns.append(
        (ws_busy, "hi", None,
         server.WebPermissionPrompter(lambda p: None)))
    server._queued_turns.append(
        (ws_stop, "nope", None,
         server.WebPermissionPrompter(lambda p: None)))

    spawned = []
    original = server._spawn_turn_thread
    server._spawn_turn_thread = lambda ws, text, e, p: spawned.append(ws.session_id)
    try:
        server._drain_queued_turns()
    finally:
        server._spawn_turn_thread = original

    # 队列清空; 正常项被启动, 叫停项被跳过
    assert len(server._queued_turns) == 0
    assert spawned == ["q-busy"]


def test_queue_fifo_order(clean_slots):
    """多会话排队时严格 FIFO。"""
    got = []
    original = server._spawn_turn_thread
    server._spawn_turn_thread = lambda ws, text, e, p: got.append(ws.session_id)
    try:
        assert server._turn_slots.acquire(blocking=False)
        for i in range(3):
            ws = server.get_or_create_web_session(f"fifo-{i}")
            ws.busy = True
            server._queued_turns.append(
                (ws, "x", None, server.WebPermissionPrompter(lambda p: None)))
        server._drain_queued_turns()
    finally:
        server._spawn_turn_thread = original
    assert got == ["fifo-0", "fifo-1", "fifo-2"]
