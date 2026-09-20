"""测试: 会话权限模式持久化 + 列表回显 + 压缩摘要净化。

背景: 权限模式此前只存内存（重启回落全局默认）且 /api/sessions 不回显,
切会话时下拉框停留在别的会话的模式上, 看起来像"切换影响了其他会话";
压缩摘要原样截取工具输出, 不可解码字节(U+FFFD)与换行带出一片乱码。"""
import json

import pytest
from fastapi.testclient import TestClient

import server
from storage import SessionStore


# ------------------------------------------------------------
# storage: 权限模式记录往返
# ------------------------------------------------------------

def test_permission_mode_roundtrip(tmp_path):
    store = SessionStore(storage_dir=tmp_path)
    assert store.get_permission_mode("s-x") is None   # 无记录 → None → 回落全局默认
    store.set_permission_mode("s-x", "plan")
    store.set_permission_mode("s-x", "workspace-write")
    assert store.get_permission_mode("s-x") == "workspace-write"  # 取最新一条


# （曾有 test_permission_mode_survives_rewrite: 压缩重写会话文件后非消息
#   记录必须保留。压缩已改为请求期视图(runtime._model_view), 历史不再被
#   重写, 存储永远只追加, 记录天然保留——该保证随之退役。）


# ------------------------------------------------------------
# server: /api/sessions 回显 permission_mode
# ------------------------------------------------------------

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


def test_sessions_list_includes_permission_mode(client, isolated_store, monkeypatch):
    """存活会话回显运行值; 切换后列表跟着变。"""
    from permissions import PLAN_MODE, PermissionMode
    monkeypatch.setattr(server.app_state, "_mode", PermissionMode.WORKSPACE_WRITE)
    ws = server.get_or_create_web_session("s-mode")   # 构造时固化默认值
    server._pending_sessions.add("s-mode")   # 未落盘会话: 走 pending 列表

    resp = client.get("/api/sessions").json()
    item = next(i for i in resp["sessions"] if i["id"] == "s-mode")
    assert item["permission_mode"] == "workspace-write"   # 初值 = 全局默认

    ws.permission_mode = PLAN_MODE
    resp = client.get("/api/sessions").json()
    item = next(i for i in resp["sessions"] if i["id"] == "s-mode")
    assert item["permission_mode"] == "plan"


def test_sessions_list_falls_back_to_persisted_mode(client, isolated_store):
    """没有存活 WebSession 的会话: 回显持久化的模式记录。"""
    isolated_store.set_permission_mode("s-persist", "plan")
    resp = client.get("/api/sessions").json()
    item = next(i for i in resp["sessions"] if i["id"] == "s-persist")
    assert item["permission_mode"] == "plan"


# ------------------------------------------------------------
# compact: 摘要净化
# ------------------------------------------------------------

def test_summary_sanitizes_replacement_chars_and_newlines():
    from compact import summarize_messages
    from models import Message, ToolResultContentBlock

    messy = "default icon \ufffd\ufffd\ufffd 256\nx\n256, ok"
    msg = Message(
        role="tool",
        content=[ToolResultContentBlock(id="t1", name="bash",
                                        output=messy, is_error=False)],
    )
    summary = summarize_messages([msg])
    assert "\ufffd" not in summary
    assert "default icon 256 x 256, ok" in summary   # 换行压平、替换符剔除
