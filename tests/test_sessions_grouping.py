"""测试: 会话列表接口返回 workdir(项目归属), 供前端按项目分组。"""
import pytest
from fastapi.testclient import TestClient

import server
from models import Message
from storage import SessionStore


@pytest.fixture()
def client():
    return TestClient(server.app)


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    """把全局 store 换到 tmp 目录，测试互不串扰、不写真实会话目录。"""
    fake = SessionStore(storage_dir=tmp_path)
    monkeypatch.setattr(server, "store", fake)
    server._pending_sessions.clear()
    return fake


def test_list_sessions_includes_workdir(client, isolated_store):
    """落盘会话: workdir 取自 WorkdirRecord; 未设置的为 None。"""
    sid = client.post("/api/sessions").json()["id"]
    isolated_store.save_message(sid, Message.user_text("你好"), None)

    sessions = client.get("/api/sessions").json()["sessions"]
    mine = [s for s in sessions if s["id"] == sid]
    assert len(mine) == 1
    assert mine[0]["workdir"] is None          # 没设置工作目录 → None

    isolated_store.set_workdir(sid, r"D:\workplace\demo")
    sessions = client.get("/api/sessions").json()["sessions"]
    mine = [s for s in sessions if s["id"] == sid]
    assert mine[0]["workdir"] == r"D:\workplace\demo"


def test_pending_session_workdir_is_none(client, isolated_store):
    """未落盘的 pending 会话: 列表可见且 workdir 为 None, 前端归入未设置分组。"""
    sid = client.post("/api/sessions").json()["id"]
    sessions = client.get("/api/sessions").json()["sessions"]
    mine = [s for s in sessions if s["id"] == sid]
    assert len(mine) == 1
    assert mine[0]["workdir"] is None


def test_workdir_follows_latest_record(client, isolated_store):
    """追加式设计: 多条 workdir 记录取最新一条。"""
    sid = client.post("/api/sessions").json()["id"]
    isolated_store.save_message(sid, Message.user_text("你好"), None)
    isolated_store.set_workdir(sid, r"D:\old\project")
    isolated_store.set_workdir(sid, r"D:\new\project")

    sessions = client.get("/api/sessions").json()["sessions"]
    mine = [s for s in sessions if s["id"] == sid]
    assert mine[0]["workdir"] == r"D:\new\project"


def test_clear_workdir_via_null_record(client, isolated_store):
    """解绑: 追加 workdir=None 的记录后, get_workdir/列表均回落 None。"""
    sid = client.post("/api/sessions").json()["id"]
    isolated_store.save_message(sid, Message.user_text("你好"), None)
    isolated_store.set_workdir(sid, r"D:\workplace\demo")
    isolated_store.set_workdir(sid, None)          # "移除项目"的解绑动作

    assert isolated_store.get_workdir(sid) is None
    sessions = client.get("/api/sessions").json()["sessions"]
    mine = [s for s in sessions if s["id"] == sid]
    assert mine[0]["workdir"] is None


def test_api_set_workdir_unbind_and_rebind(client, isolated_store, tmp_path):
    """workdir 接口: 解绑 → 列表归零; 改绑 → 校验目录存在; 未知会话 404。"""
    sid = client.post("/api/sessions").json()["id"]
    isolated_store.save_message(sid, Message.user_text("你好"), None)
    isolated_store.set_workdir(sid, r"D:\workplace\demo")

    # 解绑: workdir=null
    r = client.post(f"/api/sessions/{sid}/workdir", json={"workdir": None})
    assert r.status_code == 200
    assert r.json()["workdir"] is None
    sessions = client.get("/api/sessions").json()["sessions"]
    mine = [s for s in sessions if s["id"] == sid]
    assert mine[0]["workdir"] is None

    # 改绑: 目录不存在 → 400; 存在 → resolve 后写入
    r = client.post(f"/api/sessions/{sid}/workdir", json={"workdir": r"D:\no\such\dir"})
    assert r.status_code == 400
    target = tmp_path / "proj"
    target.mkdir()
    r = client.post(f"/api/sessions/{sid}/workdir", json={"workdir": str(target)})
    assert r.status_code == 200
    assert isolated_store.get_workdir(sid) == str(target.resolve())

    # 未知会话 → 404
    r = client.post("/api/sessions/20990101-000000/workdir", json={"workdir": None})
    assert r.status_code == 404
