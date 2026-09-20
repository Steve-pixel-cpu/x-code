"""复现: 工具卡永远"运行中" — 怀疑 tool_use/tool_result 镜像事件在前端配对断裂。

驱动真实链路（worker 线程 + SSE 镜像代理 + EmittingToolRegistry + 并行工具池）,
从 WS 收全量事件, 断言: 每个 tool_use id 都有同 id 的 tool_result 到达,
且两并发会话的事件互不串线。"""
import json

import pytest
from fastapi.testclient import TestClient

import server
from permissions import ALLOW_MODE, PermissionPolicy
from server import EmittingToolRegistry
from storage import SessionStore
from tools import ToolRegistry


class _NS:
    """模拟 anthropic SDK 原始事件对象（stream() 按 .type 属性分派）。"""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _usage(**kw):
    base = dict(input_tokens=None, output_tokens=None,
                cache_creation_input_tokens=None, cache_read_input_tokens=None)
    base.update(kw)
    return _NS(**base)


def _tool_turn(*specs):
    """一次返回若干 tool_use 块的 SDK 事件流。specs = [(id, name, json参数)]"""
    events = [_NS(type="message_start", message=_NS(usage=_usage(input_tokens=5)))]
    for i, (tid, name, arg) in enumerate(specs):
        events += [
            _NS(type="content_block_start", index=i,
                content_block=_NS(type="tool_use", id=tid, name=name)),
            _NS(type="content_block_delta", index=i,
                delta=_NS(type="input_json_delta", partial_json=arg)),
            _NS(type="content_block_stop", index=i),
        ]
    events += [
        _NS(type="message_delta", delta=_NS(stop_reason="tool_use"),
            usage=_usage(output_tokens=6)),
        _NS(type="message_stop"),
    ]
    return events


def _text_turn(text="done"):
    return [
        _NS(type="message_start", message=_NS(usage=_usage(input_tokens=5))),
        _NS(type="content_block_start", index=0, content_block=_NS(type="text")),
        _NS(type="content_block_delta", index=0,
            delta=_NS(type="text_delta", text=text)),
        _NS(type="content_block_stop", index=0),
        _NS(type="message_delta", delta=_NS(stop_reason="end_turn"),
            usage=_usage(output_tokens=3)),
        _NS(type="message_stop"),
    ]


class _FakeMessages:
    """按脚本逐次返回事件流; 走 _LiveMessagesProxy 的镜像路径（保留真实转发）。"""
    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def stream(self, **kwargs):
        idx = min(self.calls, len(self._script) - 1)
        self.calls += 1
        events = self._script[idx]
        return _FakeStream(events)


class _FakeStream:
    """迭代器协议齐备: _LiveStreamProxy 以 __next__ 逐事件拉取。"""
    def __init__(self, events):
        self._it = iter(events)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)


def _install(monkeypatch, script, tool_results):
    """替换 api_client 的底层流与工具注册表, 保留镜像/发射装饰层。"""
    fake_messages = _FakeMessages(script)
    # server.api_client.client 是 _LiveClientProxy; 其 .messages 是
    # _LiveMessagesProxy, 真流在 ._real。换掉 _real 即保留全部镜像转发。
    monkeypatch.setattr(server.api_client.client.messages, "_real", fake_messages)

    inner = ToolRegistry()
    inner.register("echo_test",
                   lambda params, workdir: tool_results.get("default", "ok"))
    monkeypatch.setattr(server, "registry", EmittingToolRegistry(inner))
    return fake_messages


def _drain_until_turn_done(ws, sid=None, limit=200):
    events = []
    for _ in range(limit):
        msg = json.loads(ws.receive_text())
        events.append(msg)
        if msg.get("type") == "turn_done":
            if sid is not None:
                _wait_worker_done(sid)
            return events
    raise AssertionError("200 条事件内未见 turn_done")


def _wait_worker_done(sid, timeout=15):
    """等 turn 工作线程退出: turn_done 之后 worker 还要在 finally 里释放
    并发槽位、跑命名回退——不 etc 它就结束就断言, 槽位账目会串到下一条用例。"""
    import threading
    for t in threading.enumerate():
        if t.name == f"turn-{sid}" and t.is_alive():
            t.join(timeout=timeout)
            return
    # 没找到 = 工作线程已退出, 正常


def _assert_pairing(events):
    tool_use_ids = [e["id"] for e in events if e["type"] == "tool_use"]
    result_ids = [e["id"] for e in events
                  if e["type"] == "tool_result" and e.get("id")]
    assert tool_use_ids, "未收到任何 tool_use 镜像事件"
    for tid in tool_use_ids:
        assert tid in result_ids, f"tool_use {tid} 没有配对的 tool_result（前端会永远显示运行中）"
    return tool_use_ids


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
    saved = dict(server._sessions)
    yield
    server._sessions.clear()
    server._sessions.update(saved)


@pytest.fixture(autouse=True)
def clean_slots():
    """并发槽位/队列隔离: 前面文件可能把计数清 0 且不补回（如排队类测试
    只占不还）——文件边界处没有存活的工作线程, 强制复位到满额是安全的。
    本文件的用例自己会等 turn 工作线程退出, 不会在用例间留下持有者。"""
    while server._turn_slots.acquire(blocking=False):
        pass
    for _ in range(server.MAX_CONCURRENT_TURNS):
        server._turn_slots.release()
    server._queued_turns.clear()
    yield
    server._queued_turns.clear()


@pytest.fixture(autouse=True)
def no_auto_title(monkeypatch):
    """跳过自动命名: turn_done 后 worker 还会跑 maybe_auto_title（真实网络
    调用）, 若超出用例的 join 时限, monkeypatch 拆除后 store.set_title 会
    落到真实 ~/.x-code/sessions——垃圾标题文件就这样写进了用户数据。"""
    monkeypatch.setattr(server, "maybe_auto_title", lambda ws: False)


@pytest.fixture(autouse=True)
def allow_all(monkeypatch):
    """测试工具无审批弹窗: 会话权限一律 ALLOW。"""
    monkeypatch.setattr(server.app_state, "_mode", ALLOW_MODE)


def test_single_turn_serial_tool_pairing(client, isolated_store, monkeypatch):
    """单会话一轮: tool_use → tool_result 同 id 成对到达。"""
    _install(monkeypatch,
             script=[_tool_turn(("tu-1", "echo_test", '{"n": 1}')), _text_turn()],
             tool_results={})
    with client.websocket_connect("/ws/s-pair") as ws:
        sid = "s-pair"
        ws.send_json({"type": "user", "text": "跑一个工具"})
        events = _drain_until_turn_done(ws, sid)
    ids = _assert_pairing(events)
    assert ids == ["tu-1"]


def test_single_turn_parallel_tools_pairing(client, isolated_store, monkeypatch):
    """同一条消息里多个 tool_use → 并行池执行: 结果乱序到达但 id 全配对。"""
    _install(monkeypatch,
             script=[
                 _tool_turn(("tu-a", "echo_test", "{}"),
                            ("tu-b", "echo_test", "{}"),
                            ("tu-c", "echo_test", "{}")),
                 _text_turn(),
             ],
             tool_results={})
    with client.websocket_connect("/ws/s-par") as ws:
        sid = "s-par"
        ws.send_json({"type": "user", "text": "并行跑三个"})
        events = _drain_until_turn_done(ws, sid)
    ids = _assert_pairing(events)
    assert sorted(ids) == ["tu-a", "tu-b", "tu-c"]


def test_two_concurrent_sessions_no_cross_talk(client, isolated_store, monkeypatch):
    """两并发会话: 各自的 tool_use/tool_result 不串线（id 前缀区分）。
    假流按请求自带的消息历史自识别会话（A/B）, 与线程交错顺序无关。"""
    from tools import ToolRegistry

    class PerSessionMessages:
        def stream(self, **kwargs):
            msgs = kwargs.get("messages", [])

            def blocks_of(role):
                for m in msgs:
                    if m.get("role") == role and isinstance(m.get("content"), list):
                        yield from (b for b in m["content"] if isinstance(b, dict))

            user_text = "".join(b.get("text", "") for b in blocks_of("user"))
            who = "A" if "A 任务" in user_text else "B"
            has_tool = any(b.get("type") == "tool_use" for b in blocks_of("assistant"))
            if not has_tool:
                return _FakeStream(_tool_turn((f"{who}-1", "echo_test", "{}")))
            return _FakeStream(_text_turn(f"{who} done"))

    inner = ToolRegistry()
    inner.register("echo_test", lambda params, workdir: "ok")
    monkeypatch.setattr(server, "registry", EmittingToolRegistry(inner))
    monkeypatch.setattr(server.api_client.client.messages, "_real",
                        PerSessionMessages())

    collected = {}
    with client.websocket_connect("/ws/cc-a") as ws_a, \
            client.websocket_connect("/ws/cc-b") as ws_b:
        ws_a.send_json({"type": "user", "text": "A 任务"})
        ws_b.send_json({"type": "user", "text": "B 任务"})
        # 两条 WS 交替收: 用后台线程收 B, 主线程收 A
        import threading
        box = {}
        def recv_b():
            box["events"] = _drain_until_turn_done(ws_b, "cc-b")
        t = threading.Thread(target=recv_b)
        t.start()
        collected["a"] = _drain_until_turn_done(ws_a, "cc-a")
        t.join(timeout=30)
        assert "events" in box, "会话 B 未在 30s 内完成"
        collected["b"] = box["events"]
    ids_a = _assert_pairing(collected["a"])
    ids_b = _assert_pairing(collected["b"])
    assert ids_a == ["A-1"] and ids_b == ["B-1"]


# ------------------------------------------------------------
# 拒绝路径: 未经执行就被终局的工具, tool_result 镜像必须照发（带显式 id）
# ------------------------------------------------------------

def test_policy_denied_tool_still_pairs(client, isolated_store, monkeypatch):
    """权限不足直接拒绝（不弹问）: 卡片也必须闭合, 否则前端永远"运行中"。
    PLAN 模式下未注册要求的工具默认 DANGER 级 → 越两级, 走"直接拒绝"分支。"""
    from permissions import PLAN_MODE
    _install(monkeypatch,
             script=[_tool_turn(("d-1", "echo_test", "{}")), _text_turn()],
             tool_results={})
    monkeypatch.setattr(server.app_state, "_mode", PLAN_MODE)
    with client.websocket_connect("/ws/s-deny") as ws:
        sid = "s-deny"
        ws.send_json({"type": "user", "text": "跑一个会被拒的工具"})
        events = _drain_until_turn_done(ws, sid)
    ids = _assert_pairing(events)
    assert ids == ["d-1"]
    denied = [e for e in events if e["type"] == "tool_result" and e.get("id") == "d-1"]
    assert denied and denied[0]["denied"] is True and denied[0]["is_error"] is True
    # 直接拒绝分支不应产生审批卡
    assert not [e for e in events if e["type"] == "permission_request"]


def test_prompter_denied_tool_pairs_with_explicit_id(client, isolated_store, monkeypatch):
    """prompt 模式: 弹审批卡, 用户点拒绝 → 结果带显式 id 闭合卡片。"""
    from permissions import PROMPT_MODE
    _install(monkeypatch,
             script=[_tool_turn(("p-1", "echo_test", "{}")), _text_turn()],
             tool_results={})
    monkeypatch.setattr(server.app_state, "_mode", PROMPT_MODE)
    with client.websocket_connect("/ws/s-prompt") as ws:
        sid = "s-prompt"
        ws.send_json({"type": "user", "text": "要审批的工具"})
        events = []
        while True:
            msg = json.loads(ws.receive_text())
            events.append(msg)
            if msg["type"] == "permission_request":
                ws.send_json({"type": "permission_response",
                              "request_id": msg["request_id"], "approved": False})
            if msg["type"] == "turn_done":
                break
    ids = _assert_pairing(events)
    assert ids == ["p-1"]
    denied = [e for e in events if e["type"] == "tool_result" and e.get("id") == "p-1"]
    assert denied and denied[0]["denied"] is True


def test_mixed_batch_denied_and_executed_all_pair(client, isolated_store, monkeypatch):
    """同一批 tool_use: 一个被策略拒绝 + 两个放行执行——三张卡全部闭合,
    id 各归各（这是 FIFO 补 id 会错位的场景）。

    PLAN 模式: echo_test 显式注册为 PLAN 级（放行执行）;
    need_danger 不注册 → 默认 DANGER 级 → 越两级直接拒绝。"""
    from permissions import PLAN_MODE
    _install(monkeypatch,
             script=[
                 _tool_turn(("m-denied", "need_danger", "{}"),
                            ("m-ok-1", "echo_test", "{}"),
                            ("m-ok-2", "echo_test", "{}")),
                 _text_turn(),
             ],
             tool_results={})
    monkeypatch.setattr(server.app_state, "_mode", PLAN_MODE)
    real_build = server.build_runtime
    def patched_build(**kwargs):
        rt = real_build(**kwargs)
        rt._permission_policy.with_tool_requirement("echo_test", PLAN_MODE)
        return rt
    monkeypatch.setattr(server, "build_runtime", patched_build)
    with client.websocket_connect("/ws/s-mix") as ws:
        sid = "s-mix"
        ws.send_json({"type": "user", "text": "混合批次"})
        events = _drain_until_turn_done(ws, sid)
    ids = _assert_pairing(events)
    assert sorted(ids) == ["m-denied", "m-ok-1", "m-ok-2"]
    by_id = {e["id"]: e for e in events if e["type"] == "tool_result"}
    assert by_id["m-denied"]["denied"] is True
    assert not by_id["m-ok-1"].get("denied")
    assert not by_id["m-ok-2"].get("denied")


def test_tool_use_started_镜像先于tool_use(client, isolated_store, monkeypatch):
    """大参数的工具 JSON 流式期（content_block_start → stop 之间）可达几十秒,
    此前前端在这段时间无任何活动指示（转圈已收、卡片未建）, 像卡死。
    修复: 块开始即镜像 tool_use_started, 前端提前建"运行中"工具卡;
    顺序必须是 started 在前、完整 tool_use 在后, id 一致。"""
    _install(monkeypatch,
             script=[_tool_turn(("tu-early", "echo_test", '{"n": 1}')), _text_turn()],
             tool_results={})
    with client.websocket_connect("/ws/s-early") as ws:
        sid = "s-early"
        ws.send_json({"type": "user", "text": "提前建卡"})
        events = _drain_until_turn_done(ws, sid)

    started_idx = next(i for i, e in enumerate(events)
                       if e["type"] == "tool_use_started")
    use_idx = next(i for i, e in enumerate(events)
                   if e["type"] == "tool_use")
    assert started_idx < use_idx                       # 先建卡, 后补全参数
    started = events[started_idx]
    assert started["id"] == "tu-early"
    assert started["name"] == "echo_test"
    assert "input" not in started or not started.get("input")   # 此刻参数还没传完
    _assert_pairing(events)                            # 原有配对语义不受影响
