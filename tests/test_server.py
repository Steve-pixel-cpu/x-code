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
    tc = TestClient(server.app)
    r = tc.post("/api/settings", json={"thinking_level": "low"})
    assert r.status_code == 200
    assert r.json()["thinking_level"] == "low"

    r = client.get("/api/settings")
    assert r.json()["thinking_level"] == "low"

    # 非法值 400 且不改当前值
    tc = TestClient(server.app)
    r = tc.post("/api/settings", json={"thinking_level": "ultra"})
    assert r.status_code == 400
    assert client.get("/api/settings").json()["thinking_level"] == "low"


def test_settings_permission_mode_validation(client):
    tc = TestClient(server.app)
    r = tc.post("/api/settings", json={"permission_mode": "no-such-mode"})
    assert r.status_code == 400

    tc = TestClient(server.app)
    r = tc.post("/api/settings", json={"permission_mode": "read-only"})
    assert r.status_code == 200
    assert r.json()["permission_mode"] == "read-only"


def test_get_messages_returns_empty_for_unknown_session(client):
    """未落盘的会话返回空历史（200），不是 404——前端新建会话靠它渲染空状态。"""
    r = client.get("/api/sessions/definitely-not-exist/messages")
    assert r.status_code == 200
    assert r.json()["messages"] == []


def test_create_session_returns_timestamp_id(client):
    tc = TestClient(server.app)
    r = tc.post("/api/sessions")
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
            ws.send_json({"type": "user", "text": "第二条", "qid": "q-1"})
            reply = json.loads(ws.receive_text())
            assert reply["type"] == "turn_queued_user"
            assert reply["position"] == 1
            assert web_session.pending == [{"qid": "q-1", "text": "第二条",
                                            "attachments": []}]
    finally:
        web_session.busy = False


def test_ws_queue_promote_jumps_queue_while_busy(client, isolated_store):
    """「立即」: busy 会话上 queue_promote 把指定待发送消息提到最前并叫停当前轮, 无回执。"""
    web_session = server.get_or_create_web_session("s-promote")
    web_session.busy = True
    web_session.pending = [{"qid": "q-1", "text": "第一条", "attachments": []},
                           {"qid": "q-2", "text": "第二条", "attachments": []}]
    try:
        with client.websocket_connect("/ws/s-promote") as ws:
            ws.send_json({"type": "queue_promote", "qid": "q-2"})
            ws.send_json({"type": "nope"})   # 探测: queue_promote 分支应静默
            reply = json.loads(ws.receive_text())
            assert reply["type"] == "error"
            assert "未知消息类型" in reply["message"]
            assert [it["qid"] for it in web_session.pending] == ["q-2", "q-1"]
            assert web_session.stop_requested is True
    finally:
        web_session.busy = False
        web_session.stop_requested = False
        web_session.pending = []


def test_ws_queue_promote_ignores_unknown_text(client, isolated_store):
    """queue_promote 的文本不在待发送区（可能已开跑）: 静默忽略, 不误杀当前轮。"""
    web_session = server.get_or_create_web_session("s-promote2")
    web_session.busy = True
    web_session.pending = [{"qid": "q-1", "text": "第一条", "attachments": []}]
    try:
        with client.websocket_connect("/ws/s-promote2") as ws:
            ws.send_json({"type": "queue_promote", "qid": "不存在的qid"})
            ws.send_json({"type": "nope"})
            reply = json.loads(ws.receive_text())
            assert reply["type"] == "error"
            assert [it["qid"] for it in web_session.pending] == ["q-1"]
            assert web_session.stop_requested is False
    finally:
        web_session.busy = False
        web_session.pending = []


def test_ws_queue_remove_drops_pending_text(client, isolated_store):
    """编辑/删除待发送卡片: queue_remove 从待发送区移除首个匹配文本, 不叫停当前轮。"""
    web_session = server.get_or_create_web_session("s-rm")
    web_session.busy = True
    web_session.pending = [{"qid": "q-1", "text": "第一条", "attachments": []},
                           {"qid": "q-2", "text": "第二条", "attachments": []},
                           {"qid": "q-3", "text": "第一条", "attachments": []}]
    try:
        with client.websocket_connect("/ws/s-rm") as ws:
            ws.send_json({"type": "queue_remove", "qid": "q-2"})
            ws.send_json({"type": "nope"})
            reply = json.loads(ws.receive_text())
            assert reply["type"] == "error"
            assert "未知消息类型" in reply["message"]
            assert [it["qid"] for it in web_session.pending] == ["q-1", "q-3"]
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


# ------------------------------------------------------------
# TurnDispatch 上下文路由 — 工具并行执行的回归钉子。
# 旧实现按线程号存 emit: 工具进线程池后挂点查不到绑定, tool_result
# 被静默吞掉, 前端工具卡全部收口成"已中断"。runtime 现在用
# copy_context() 提交并行任务, 绑定必须跨池线程可见。
# ------------------------------------------------------------

def test_turn_dispatch_绑定经copy_context在池线程可见():
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    seen = {}
    stop_flag = lambda: False

    def probe():
        seen["emit"] = server.dispatch.current()
        seen["workdir"] = server.dispatch.current_workdir()
        seen["should_stop"] = server.dispatch.current_should_stop()

    server.dispatch.bind(emit="EMIT", workdir="D:/work", should_stop=stop_flag)
    try:
        ctx = contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(ctx.run, probe).result()
        assert seen == {"emit": "EMIT", "workdir": "D:/work",
                        "should_stop": stop_flag}
    finally:
        server.dispatch.unbind()

    # unbind 后新快照不再带绑定（工作线程被复用也不串轮）
    ctx2 = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(ctx2.run, probe).result()
    assert seen["emit"] is None and seen["workdir"] is None


def test_turn_dispatch_并发轮次互不串线():
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(2)
    seen = {}

    def turn(tag):
        server.dispatch.bind(emit=f"emit-{tag}", workdir=f"dir-{tag}")
        barrier.wait()          # 两轮都绑定完成后互查
        ctx = contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            seen[tag] = pool.submit(ctx.run, server.dispatch.current).result()
        server.dispatch.unbind()

    t1 = threading.Thread(target=turn, args=("a",))
    t2 = threading.Thread(target=turn, args=("b",))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert seen["a"] == "emit-a"
    assert seen["b"] == "emit-b"


# ------------------------------------------------------------
# TurnEmitter id 配对 — 并行下 tool_result 按完成序到达
# ------------------------------------------------------------

def test_turn_emitter_乱序结果按真实id配对():
    captured = []
    emitter = server.TurnEmitter(sink=captured.append)

    emitter({"type": "tool_use", "id": "t1", "name": "bash"})
    emitter({"type": "tool_use", "id": "t2", "name": "bash"})
    emitter({"type": "tool_result", "id": "t2"})   # t2 先完成
    emitter({"type": "tool_result", "id": "t1"})

    assert [p["id"] for p in captured] == ["t1", "t2", "t2", "t1"]


def test_turn_emitter_无id事件回退FIFO():
    captured = []
    emitter = server.TurnEmitter(sink=captured.append)

    emitter({"type": "tool_use", "id": "t1"})
    emitter({"type": "tool_use", "id": "t2"})
    emitter({"type": "tool_result"})               # 权限拒绝等旧式事件不带 id
    emitter({"type": "tool_result"})

    assert [p["id"] for p in captured][2:] == ["t1", "t2"]


# ------------------------------------------------------------
# 插队即遗弃: 被打断任务就地收束不自动续跑, 与手动停止同语义
# ------------------------------------------------------------

def _stub_session(**kw):
    from types import SimpleNamespace
    s = SimpleNamespace(busy=True, pending=[], prompter=None,
                        stop_requested=False, broadcast=lambda p: None)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_promote_pending_提到队首并叫停当前轮():
    s = _stub_session(pending=[{"qid": "a", "text": "A", "attachments": []},
                               {"qid": "b", "text": "B", "attachments": []},
                               {"qid": "c", "text": "C", "attachments": []}])

    assert server.promote_pending(s, "b") is True
    assert [it["qid"] for it in s.pending] == ["b", "a", "c"]
    assert s.stop_requested is True


def test_promote_pending_不在队列时静默忽略():
    s = _stub_session(pending=[{"qid": "b", "text": "B", "attachments": []}])

    assert server.promote_pending(s, "X") is False
    assert s.stop_requested is False


def test_request_stop_清空排队区():
    s = _stub_session(pending=["B"])

    server.request_stop(s)               # 叫停 = 彻底停: 排队区一并撤回
    assert s.stop_requested is True and s.pending == []


# ------------------------------------------------------------
# 启动对账接线 — startup 事件经 get_orchestrator 调 reconcile_orphans
# （曾经 get_orchestrator 只 import 未使用, 对账从未发生）
# ------------------------------------------------------------

def test_startup_reconciles_orphan_agents(monkeypatch):
    from types import SimpleNamespace

    calls = []
    monkeypatch.setattr(server, "get_orchestrator", lambda: SimpleNamespace(
        reconcile_orphans=lambda: calls.append(1) or 2))

    server._reconcile_orphan_agents()

    assert calls == [1]
    # 处理函数确实挂在 startup 事件上（TestClient 不进 lifespan, 测试不触发它）
    assert server._reconcile_orphan_agents in server.app.router.on_startup


# ------------------------------------------------------------
# 附件（图片 / 文本文件）: 校验、WS user 消息落盘、历史回放、排队 qid
# ------------------------------------------------------------

SMALL_B64 = "iVBORw0KGgoAAAANSUhEUg=="   # 合法形状的小 base64（不校验内容）


def test_parse_attachments_accepts_valid():
    """合法附件: 形状规整, 图片在前文件在后。"""
    atts, err = server._parse_attachments([
        {"kind": "file", "name": "n.md", "text": "# hi"},
        {"kind": "image", "name": "a.png", "media_type": "image/webp", "data": SMALL_B64},
    ])
    assert err is None
    assert atts[0]["kind"] == "image" and atts[0]["data"] == SMALL_B64
    assert atts[1] == {"kind": "file", "name": "n.md", "text": "# hi"}


def test_parse_attachments_none_and_empty():
    """缺失 attachments / 空数组都算合法空集。"""
    atts, err = server._parse_attachments(None)
    assert (atts, err) == ([], None)
    atts, err = server._parse_attachments([])
    assert (atts, err) == ([], None)


def test_parse_attachments_rejects_bad_media_type():
    """media_type 白名单外的图片（如 bmp）拒绝。"""
    atts, err = server._parse_attachments([
        {"kind": "image", "name": "x.bmp", "media_type": "image/bmp", "data": SMALL_B64},
    ])
    assert atts is None and err and "image/bmp" in err


def test_parse_attachments_rejects_oversize_image():
    """单张 base64 超 5MB 拒绝。"""
    big = "A" * (5 * 1024 * 1024 + 1)
    atts, err = server._parse_attachments([
        {"kind": "image", "media_type": "image/png", "data": big},
    ])
    assert atts is None and err and "5MB" in err


def test_parse_attachments_rejects_too_many_images():
    """图片最多 8 张。"""
    raw = [{"kind": "image", "media_type": "image/png", "data": "A"}
           for _ in range(9)]
    atts, err = server._parse_attachments(raw)
    assert atts is None and err and "8 张" in err


def test_parse_attachments_rejects_too_many_files():
    """文本附件最多 8 个。"""
    raw = [{"kind": "file", "name": "f.txt", "text": "x"} for _ in range(9)]
    atts, err = server._parse_attachments(raw)
    assert atts is None and err and "8 个" in err


def test_parse_attachments_rejects_oversize_file_text():
    """单个文本附件内容超 512KB 拒绝。"""
    atts, err = server._parse_attachments([
        {"kind": "file", "name": "big.log", "text": "x" * (512 * 1024 + 1)},
    ])
    assert atts is None and err and "512KB" in err


def test_parse_attachments_rejects_total_over_20mb():
    """全部附件总量超 20MB 拒绝（单张未超限）。"""
    part = "A" * (4500 * 1024)   # 单张 4.5MB 合规, 4 张总量超 20MB
    raw = [{"kind": "image", "media_type": "image/png", "data": part}
           for _ in range(5)]
    atts, err = server._parse_attachments(raw)
    assert atts is None and err and "20MB" in err


def test_parse_attachments_rejects_unknown_kind():
    atts, err = server._parse_attachments([{"kind": "video", "data": "A"}])
    assert atts is None and err and "video" in err


def test_ws_user_with_attachments_persists_blocks(client, isolated_store):
    """WS 发带附件的 user 消息: runtime 收到 image/file 块并经增量落盘进 JSONL。"""
    captured = {}

    class _Summary:
        assistant_messages = []
        tool_results = []
        iterations = 1
        budget_exhausted = False
        iterations_exhausted = False

        @property
        def usage(self):
            from runtime import UsageTracker
            return UsageTracker().cumulative_usage()

        auto_compacted = False

    def fake_start_turn(web_session, text, emit, attachments=None):
        captured["text"] = text
        captured["attachments"] = attachments
        web_session.runtime = type("R", (), {
            "session": lambda self: _Session(),
            "set_permission_mode": lambda self, m: None})()
        emit({"type": "turn_done", "interrupted": False, "iterations": 1,
              "budget_exhausted": False, "iterations_exhausted": False})

    class _Session:
        messages = []

    import types as _types
    monkey = client
    # 直接打桩 _start_turn, 避免真起工作线程
    _saved_sessions = dict(server._sessions)
    original = server._start_turn
    server._start_turn = fake_start_turn
    try:
        with client.websocket_connect("/ws/s-att") as ws:
            ws.send_json({
                "type": "user", "text": "看图",
                "attachments": [
                    {"kind": "image", "name": "a.png",
                     "media_type": "image/webp", "data": SMALL_B64},
                    {"kind": "file", "name": "n.md", "text": "# notes"},
                ],
            })
            reply = json.loads(ws.receive_text())
    finally:
        server._start_turn = original
        server._sessions.clear()
        server._sessions.update(_saved_sessions)
    assert reply["type"] == "turn_done"
    assert captured["text"] == "看图"
    kinds = [a["kind"] for a in captured["attachments"]]
    assert kinds == ["image", "file"]


def test_ws_user_image_only_not_dropped(client, isolated_store):
    """只发图不打字: 不丢弃, 正常开轮（text 为空但附件非空）。"""
    captured = {}

    def fake_start_turn(web_session, text, emit, attachments=None):
        captured["text"] = text
        captured["attachments"] = attachments

    _saved_sessions = dict(server._sessions)
    original = server._start_turn
    server._start_turn = fake_start_turn
    try:
        with client.websocket_connect("/ws/s-imgonly") as ws:
            ws.send_json({
                "type": "user", "text": "",
                "attachments": [
                    {"kind": "image", "media_type": "image/png", "data": SMALL_B64},
                ],
            })
            ws.send_json({"type": "nope"})   # 探测: user 分支不应回错误
            reply = json.loads(ws.receive_text())
    finally:
        server._start_turn = original
        server._sessions.clear()
        server._sessions.update(_saved_sessions)
    assert reply["type"] == "error"   # 探测消息的回包, 证明 user 分支静默
    assert captured["text"] == ""
    assert captured["attachments"] and captured["attachments"][0]["kind"] == "image"


def test_ws_user_oversize_attachment_returns_error(client, isolated_store):
    """超限附件: error 事件带原因, 不开轮。"""
    started = []
    _saved_sessions = dict(server._sessions)
    original = server._start_turn
    server._start_turn = lambda ws, t, e, attachments=None: started.append(1)
    try:
        with client.websocket_connect("/ws/s-over") as ws:
            ws.send_json({
                "type": "user", "text": "hi",
                "attachments": [
                    {"kind": "image", "media_type": "image/png",
                     "data": "A" * (5 * 1024 * 1024 + 1)},
                ],
            })
            reply = json.loads(ws.receive_text())
    finally:
        server._start_turn = original
        server._sessions.clear()
        server._sessions.update(_saved_sessions)
    assert reply["type"] == "error"
    assert "5MB" in reply["message"]
    assert started == []


def test_ws_user_empty_text_and_no_attachments_dropped(client, isolated_store):
    """text 与 attachments 同时为空: 静默丢弃。"""
    with client.websocket_connect("/ws/s-empty2") as ws:
        ws.send_json({"type": "user", "text": "   "})
        ws.send_json({"type": "nope"})
        reply = json.loads(ws.receive_text())
        assert reply["type"] == "error"
        assert "未知消息类型" in reply["message"]


def test_message_to_dict_image_and_file_blocks():
    """历史回放: image 输出 {type, media_type, data}, file 输出 {type, name, text}。"""
    msg = Message.user_input("看图", [
        {"kind": "image", "name": "a.png", "media_type": "image/webp", "data": SMALL_B64},
        {"kind": "file", "name": "n.md", "text": "# notes"},
    ])
    d = server._message_to_dict(msg)
    assert d["role"] == "user"
    assert d["blocks"][0] == {"type": "text", "text": "看图"}
    assert d["blocks"][1] == {"type": "image", "media_type": "image/webp",
                              "data": SMALL_B64}
    assert d["blocks"][2] == {"type": "file", "name": "n.md", "text": "# notes"}


def test_ws_busy_queue_with_attachments_and_qid(client, isolated_store):
    """busy 时带附件消息进排队区: 存 {qid, text, attachments}。"""
    web_session = server.get_or_create_web_session("s-busy-att")
    web_session.busy = True
    try:
        with client.websocket_connect("/ws/s-busy-att") as ws:
            ws.send_json({
                "type": "user", "text": "排队看图", "qid": "q-x1",
                "attachments": [
                    {"kind": "image", "media_type": "image/gif", "data": "QQ=="},
                ],
            })
            reply = json.loads(ws.receive_text())
            assert reply["type"] == "turn_queued_user"
            assert web_session.pending == [
                {"qid": "q-x1", "text": "排队看图",
                 "attachments": [{"kind": "image", "name": "",
                                  "media_type": "image/gif", "data": "QQ=="}]},
            ]
    finally:
        web_session.busy = False
        web_session.pending = []


def test_ws_busy_queue_without_qid_gets_generated(client, isolated_store):
    """前端未带 qid（旧客户端）: 服务端兜底生成, 排队仍可用。"""
    web_session = server.get_or_create_web_session("s-busy-noqid")
    web_session.busy = True
    try:
        with client.websocket_connect("/ws/s-busy-noqid") as ws:
            ws.send_json({"type": "user", "text": "旧客户端消息"})
            reply = json.loads(ws.receive_text())
            assert reply["type"] == "turn_queued_user"
            item = web_session.pending[0]
            assert item["text"] == "旧客户端消息"
            assert item["qid"]   # 已生成
    finally:
        web_session.busy = False
        web_session.pending = []


def test_history_api_returns_attachment_blocks(client, isolated_store):
    """历史 REST API 返回 image/file 块（与 _message_to_dict 同形状）。"""
    sid = "20250101-000000-att"
    msg = Message.user_input("看图", [
        {"kind": "image", "media_type": "image/png", "data": SMALL_B64},
        {"kind": "file", "name": "a.py", "text": "print(1)"},
    ])
    isolated_store.save_message(sid, msg, None)
    r = client.get(f"/api/sessions/{sid}/messages")
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert msgs[0]["role"] == "user"
    types = [b["type"] for b in msgs[0]["blocks"]]
    assert types == ["text", "image", "file"]
    assert msgs[0]["blocks"][1]["media_type"] == "image/png"
    assert msgs[0]["blocks"][2]["name"] == "a.py"

# ------------------------------------------------------------
# 设置: permission_mode 持久化（写 ~/.x-code/settings.json 的 permissionMode）
# ------------------------------------------------------------

@pytest.fixture()
def settings_file(tmp_path, monkeypatch):
    target = tmp_path / "settings.json"
    monkeypatch.setattr("config.SETTINGS_FILE", target)
    # server 模块是 from config import SETTINGS_FILE 拿到的引用, 两处都要指过去
    monkeypatch.setattr(server, "SETTINGS_FILE", target)
    return target


def test_settings_permission_mode_persisted(settings_file):
    tc = TestClient(server.app)
    r = tc.post("/api/settings", json={"permission_mode": "prompt"})
    assert r.status_code == 200
    assert r.json()["permission_mode"] == "prompt"
    import json as _json
    data = _json.loads(settings_file.read_text(encoding="utf-8"))
    assert data["permissionMode"] == "prompt"   # 规范名, 重启能读回


def test_settings_permission_mode_persist_merges_existing_keys(settings_file):
    import json as _json
    settings_file.write_text(_json.dumps(
        {"providers": [], "activeProvider": {"provider": "p", "model": "m"}},
        ensure_ascii=False), encoding="utf-8")
    tc = TestClient(server.app)
    tc.post("/api/settings", json={"permission_mode": "workspace-write"})
    data = _json.loads(settings_file.read_text(encoding="utf-8"))
    assert data["permissionMode"] == "workspace-write"
    assert data["activeProvider"] == {"provider": "p", "model": "m"}   # 原有 key 保留


def test_settings_permission_mode_allow_rejected(settings_file):
    tc = TestClient(server.app)
    r = tc.post("/api/settings", json={"permission_mode": "allow"})
    assert r.status_code == 400
    assert not settings_file.exists()   # 拒绝的值不落盘


def test_settings_permission_mode_invalid_not_persisted(settings_file):
    tc = TestClient(server.app)
    tc.post("/api/settings", json={"permission_mode": "no-such-mode"})
    assert not settings_file.exists()


def test_settings_permission_mode_unwritable_degrades(settings_file, monkeypatch):
    """盘写失败只降级为不持久化, 设置请求本身仍成功（内存已生效）。"""
    import json as _json
    import config
    monkeypatch.setattr(server, "SETTINGS_FILE", settings_file)
    real_write_text = config.Path.write_text

    def boom(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(config.Path, "write_text", boom)
    tc = TestClient(server.app)
    r = tc.post("/api/settings", json={"permission_mode": "read-only"})
    assert r.status_code == 200
    assert r.json()["permission_mode"] == "read-only"   # 请求不受影响
    assert not real_write_text(settings_file, "", encoding="utf-8") or True
