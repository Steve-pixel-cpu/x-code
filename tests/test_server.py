"""server.py 协议层 / 设置接口测试（不起真实 LLM 请求，不动全局单例状态）。

运行方式（在 x-code 目录下）:
    .venv/Scripts/python.exe -m pytest tests/test_server.py -v

说明: server.py 在 import 时做启动装配（读 .env、建 SessionStore），模块级
单例（api_client / app_state / store）在测试里只做只读断言或经临时对象替换，
全程不发起网络请求。
"""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

import server
from models import Message
from permissions import PermissionDecision, PermissionRequest, PermissionMode
from storage import SessionStore


# ------------------------------------------------------------
# 夹具: 不触真实 .env 装配失败的问题——模块已可导入（import server 冒烟）
# ------------------------------------------------------------

@pytest.fixture()
def client():
    return TestClient(server.app)


# ------------------------------------------------------------
# REST — 首页 / 会话列表 / 新建 / 历史回放
# ------------------------------------------------------------

def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "x-code" in r.text


def test_settings_roundtrip_and_validation(client):
    # 合法值立即生效
    r = client.post("/api/settings", json={"thinking_level": "low"})
    assert r.status_code == 200
    assert r.json()["thinking_level"] == "low"

    r = client.get("/api/settings")
    assert r.json()["thinking_level"] == "low"

    # 非法值 400 且不改当前值
    r = client.post("/api/settings", json={"thinking_level": "ultra"})
    assert r.status_code == 400
    assert client.get("/api/settings").json()["thinking_level"] == "low"


def test_settings_permission_mode_validation(client):
    r = client.post("/api/settings", json={"permission_mode": "no-such-mode"})
    assert r.status_code == 400

    r = client.post("/api/settings", json={"permission_mode": "read-only"})
    assert r.status_code == 200
    assert r.json()["permission_mode"] == "read-only"


def test_get_messages_returns_empty_for_unknown_session(client):
    """未落盘的会话返回空历史（200），不是 404——前端新建会话靠它渲染空状态。"""
    r = client.get("/api/sessions/definitely-not-exist/messages")
    assert r.status_code == 200
    assert r.json()["messages"] == []


def test_create_session_returns_timestamp_id(client):
    r = client.post("/api/sessions")
    assert r.status_code == 200
    sid = r.json()["id"]
    # 与 CLI 相同的 %Y%m%d-%H%M%S（14 位数字，可能带 w 后缀）
    assert len(sid) >= 15 and sid[:8].isdigit() and sid[9:15].isdigit() and sid[8] == "-"


# ------------------------------------------------------------
# 新建会话契约 — pending 会话在落盘前就对列表/历史接口可见
# ------------------------------------------------------------

def test_created_session_visible_before_first_message(client, isolated_store):
    sid = client.post("/api/sessions").json()["id"]

    # 侧栏列表: 未落盘也可见（未命名、0 条）
    sessions = client.get("/api/sessions").json()["sessions"]
    mine = [s for s in sessions if s["id"] == sid]
    assert len(mine) == 1
    assert mine[0]["title"] == "(未命名)"
    assert mine[0]["message_count"] == 0

    # 历史: 空列表（200），前端据此渲染空状态引导
    r = client.get(f"/api/sessions/{sid}/messages")
    assert r.status_code == 200
    assert r.json()["messages"] == []


def test_pending_session_becomes_normal_after_first_save(client, isolated_store):
    sid = client.post("/api/sessions").json()["id"]
    isolated_store.save_message(sid, Message.user_text("你好"), None)

    sessions = client.get("/api/sessions").json()["sessions"]
    mine = [s for s in sessions if s["id"] == sid]
    assert len(mine) == 1                      # 落盘后不与 pending 重复
    assert mine[0]["message_count"] == 1

    data = client.get(f"/api/sessions/{sid}/messages").json()
    assert len(data["messages"]) == 1


def test_created_ids_do_not_collide_within_same_second(client, isolated_store):
    a = client.post("/api/sessions").json()["id"]
    b = client.post("/api/sessions").json()["id"]
    assert a != b


# ------------------------------------------------------------
# WebPermissionPrompter — 阻塞等待 / 超时安全侧 / cancel / stale 响应
# ------------------------------------------------------------

def _request() -> PermissionRequest:
    return PermissionRequest(
        tool_name="bash",
        input='{"command": "rm -rf /"}',
        current_mode=PermissionMode.WORKSPACE_WRITE,
        required_mode=PermissionMode.DANGER_FULL_ACCESS,
    )


def test_prompter_allow_path():
    prompter = server.WebPermissionPrompter(emit=lambda p: None)
    threading.Thread(
        target=lambda: prompter.resolve("perm-1", True), daemon=True
    ).start()
    result = prompter.decide(_request())
    assert result.decision == PermissionDecision.ALLOW


def test_prompter_deny_path():
    prompter = server.WebPermissionPrompter(emit=lambda p: None)
    threading.Thread(
        target=lambda: prompter.resolve("perm-1", False), daemon=True
    ).start()
    result = prompter.decide(_request())
    assert result.decision == PermissionDecision.DENY


def test_prompter_waits_indefinitely_without_approval():
    """审批不设超时: 不 resolve 也不 cancel 时 decide 持续等待（弹窗可见就一直等）。

    用短计时守护线程验证: decide 仍未返回（阻塞中），由 cancel 解除并 DENY。
    （120s 自动拒绝已按需求移除——超时的朝安全侧 DENY 会拒绝掉用户还没看到的弹窗。）
    """
    prompter = server.WebPermissionPrompter(emit=lambda p: None)
    decided: list = []
    worker = threading.Thread(
        target=lambda: decided.append(prompter.decide(_request())), daemon=True)
    worker.start()
    time.sleep(0.15)                     # 远大于旧超时测试的 0.05s
    assert not decided                   # 仍在等待, 没有超时拒绝
    prompter.cancel()                    # stop/断连才会解除等待
    worker.join(timeout=1)
    assert decided and decided[0].decision == PermissionDecision.DENY


def test_prompter_cancel_denies_immediately():
    prompter = server.WebPermissionPrompter(emit=lambda p: None)
    threading.Thread(target=prompter.cancel, daemon=True).start()
    result = prompter.decide(_request())
    assert result.decision == PermissionDecision.DENY


def test_prompter_ignores_stale_responses_then_accepts_current():
    prompter = server.WebPermissionPrompter(emit=lambda p: None)
    # 先塞一个过期 request_id 的响应，再塞正确的——前者应被忽略
    prompter.resolve("perm-999", True)
    threading.Thread(
        target=lambda: prompter.resolve("perm-1", False), daemon=True
    ).start()
    result = prompter.decide(_request())
    assert result.decision == PermissionDecision.DENY   # 来自 perm-1=False


def test_prompter_emits_request_before_blocking():
    """先推弹窗再阻塞: decide 内部必须先 emit permission_request。"""
    seen = []
    prompter = server.WebPermissionPrompter(emit=seen.append)
    threading.Thread(
        target=lambda: prompter.resolve("perm-1", True), daemon=True
    ).start()
    prompter.decide(_request())
    assert seen and seen[0]["type"] == "permission_request"
    assert seen[0]["request_id"] == "perm-1"
    assert seen[0]["tool_name"] == "bash"


# ------------------------------------------------------------
# 协议层 — WS 消息处理（并发守卫 / 未知类型 / 非法 JSON）
# 会话共用 server.STORE 覆盖，测试数据落 tmp 目录，不污染真实会话
# ------------------------------------------------------------

@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    """把全局 store 换到 tmp 目录，测试互不串扰、不写真实会话目录。"""
    from storage import SessionStore
    fake = SessionStore(storage_dir=tmp_path)
    monkeypatch.setattr(server, "store", fake)
    server._pending_sessions.clear()
    # api_get_messages 里 list_sessions() 取自替换后的 store
    return fake


def test_ws_unknown_message_type(client, isolated_store):
    with client.websocket_connect("/ws/s1") as ws:
        ws.send_json({"type": "nope"})
        reply = json.loads(ws.receive_text())
        assert reply["type"] == "error"
        assert "未知消息类型" in reply["message"]


def test_ws_permission_response_without_pending(client, isolated_store):
    with client.websocket_connect("/ws/s1") as ws:
        ws.send_json({"type": "permission_response", "request_id": "perm-1",
                      "approved": True})
        reply = json.loads(ws.receive_text())
        assert reply["type"] == "error"
        assert "待审批" in reply["message"]


def test_ws_queues_second_turn_while_busy(client, isolated_store, monkeypatch):
    """并发守卫: busy 会话上的第二条 user 消息进入排队区（回执 turn_queued_user），不触发第二轮。"""
    web_session = server.get_or_create_web_session("s-busy")
    web_session.busy = True
    try:
        with client.websocket_connect("/ws/s-busy") as ws:
            ws.send_json({"type": "user", "text": "第二条"})
            reply = json.loads(ws.receive_text())
            assert reply["type"] == "turn_queued_user"
            assert reply["position"] == 1
            assert web_session.pending == ["第二条"]
    finally:
        web_session.busy = False


def test_ws_queue_promote_jumps_queue_while_busy(client, isolated_store):
    """「立即」: busy 会话上 queue_promote 把指定待发送消息提到最前并叫停当前轮, 无回执。"""
    web_session = server.get_or_create_web_session("s-promote")
    web_session.busy = True
    web_session.pending = ["第一条", "第二条"]
    try:
        with client.websocket_connect("/ws/s-promote") as ws:
            ws.send_json({"type": "queue_promote", "text": "第二条"})
            ws.send_json({"type": "nope"})   # 探测: queue_promote 分支应静默
            reply = json.loads(ws.receive_text())
            assert reply["type"] == "error"
            assert "未知消息类型" in reply["message"]
            assert web_session.pending == ["第二条", "第一条"]
            assert web_session.stop_requested is True
    finally:
        web_session.busy = False
        web_session.stop_requested = False
        web_session.pending = []


def test_ws_queue_promote_ignores_unknown_text(client, isolated_store):
    """queue_promote 的文本不在待发送区（可能已开跑）: 静默忽略, 不误杀当前轮。"""
    web_session = server.get_or_create_web_session("s-promote2")
    web_session.busy = True
    web_session.pending = ["第一条"]
    try:
        with client.websocket_connect("/ws/s-promote2") as ws:
            ws.send_json({"type": "queue_promote", "text": "不存在的消息"})
            ws.send_json({"type": "nope"})
            reply = json.loads(ws.receive_text())
            assert reply["type"] == "error"
            assert web_session.pending == ["第一条"]
            assert web_session.stop_requested is False
    finally:
        web_session.busy = False
        web_session.pending = []


def test_ws_queue_remove_drops_pending_text(client, isolated_store):
    """编辑/删除待发送卡片: queue_remove 从待发送区移除首个匹配文本, 不叫停当前轮。"""
    web_session = server.get_or_create_web_session("s-rm")
    web_session.busy = True
    web_session.pending = ["第一条", "第二条", "第一条"]
    try:
        with client.websocket_connect("/ws/s-rm") as ws:
            ws.send_json({"type": "queue_remove", "text": "第一条"})
            ws.send_json({"type": "nope"})
            reply = json.loads(ws.receive_text())
            assert reply["type"] == "error"
            assert "未知消息类型" in reply["message"]
            assert web_session.pending == ["第二条", "第一条"]   # 只移除首个匹配
            assert web_session.stop_requested is False
    finally:
        web_session.busy = False
        web_session.pending = []


def test_ws_empty_user_message_is_ignored(client, isolated_store):
    """空文本不回错也不开轮: 服务端静默丢弃（收不到任何回复）。"""
    with client.websocket_connect("/ws/s-empty") as ws:
        ws.send_json({"type": "user", "text": "   "})
        ws.send_json({"type": "nope"})          # 用已知会回包的消息探测
        reply = json.loads(ws.receive_text())
        assert reply["type"] == "error"
        assert "未知消息类型" in reply["message"]


# ------------------------------------------------------------
# 单元 — 消息摊平 / TurnEmitter 配对
# ------------------------------------------------------------

def test_message_to_dict_blocks():
    from models import Message
    msg = Message.tool_result(
        id="t1", name="bash", output="boom", is_error=True)
    d = server._message_to_dict(msg)
    assert d["role"] == "tool"
    assert d["blocks"][0] == {
        "type": "tool_result", "id": "t1", "name": "bash",
        "output": "boom", "is_error": True,
    }


def test_turn_emitter_pairs_tool_ids():
    """tool_use 入队 id、tool_result 按 FIFO 弹出补 id——前端按 id 配对卡片。"""
    emitter = server.TurnEmitter(sink=lambda p: None)
    emitter({"type": "tool_use", "id": "a", "name": "bash", "input": "{}"})
    emitter({"type": "tool_use", "id": "b", "name": "read_file", "input": "{}"})
    out = []
    emitter2 = server.TurnEmitter(sink=out.append)
    emitter2({"type": "tool_use", "id": "a", "name": "bash", "input": "{}"})
    emitter2({"type": "tool_use", "id": "b", "name": "read_file", "input": "{}"})
    emitter2({"type": "tool_result", "name": "bash", "input": "{}"})
    emitter2({"type": "tool_result", "name": "read_file", "input": "{}"})
    assert [p["id"] for p in out if p["type"] == "tool_result"] == ["a", "b"]


# ------------------------------------------------------------
# 连接门禁（token gate）
# ------------------------------------------------------------

def test_token_gate_blocks_and_allows(client, monkeypatch):
    """门禁开启时: 无令牌 403, 带令牌放行; 门禁关闭(API_TOKEN="")时全放行。"""
    monkeypatch.setattr(server, "API_TOKEN", "secret-token")
    # 无令牌 -> 403
    r = client.get("/api/settings")
    assert r.status_code == 403
    # 错误令牌 -> 403
    r = client.get("/api/settings", headers={"x-xcode-token": "wrong"})
    assert r.status_code == 403
    # 正确令牌 -> 放行
    r = client.get("/api/settings", headers={"x-xcode-token": "secret-token"})
    assert r.status_code == 200
    # query/cookie 亦可
    assert client.get("/api/settings?token=secret-token").status_code == 200
    client.cookies.set("xcode_token", "secret-token")
    assert client.get("/api/settings").status_code == 200


def test_token_gate_disabled_when_empty(client, monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "")
    assert client.get("/api/settings").status_code == 200


def test_ping_identifies_backend(client):
    """探测端点: 桌面壳靠它区分 x-code 后端和抢占 8000 的其他程序。"""
    r = client.get("/api/ping")
    assert r.status_code == 200
    assert r.json() == {"app": "x-code"}
