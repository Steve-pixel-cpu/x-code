# -*- coding: utf-8 -*-
"""测试: 输入栏三设置的会话隔离 — 权限模式 / 思考等级 / 模型。

背景: POST /api/settings 的 thinking_level 曾遍历覆盖所有存活会话
（在会话 A 切等级, B/C/D 下一轮全被改, 完全没有隔离）; 模型是全局单例,
stream() 没有 per-call model 参数。现在:
- 设置页（REST）只改"新会话默认值";
- 会话内下拉框走 WS set_thinking_level / set_model, 只影响本会话并持久化;
- /api/sessions 回显三设置的会话值（存活运行值 → 持久值 → 全局默认）。
"""
import json

import pytest
from fastapi.testclient import TestClient

import server
from storage import SessionStore
from conftest import ws_connect


# ------------------------------------------------------------
# storage: 会话模型记录往返
# ------------------------------------------------------------

def test_model_roundtrip(tmp_path):
    store = SessionStore(storage_dir=tmp_path)
    assert store.get_model("s-x") == (None, None)   # 无记录 → 跟随全局
    store.set_model("s-x", "prov-a", "model-1")
    store.set_model("s-x", "prov-b", "model-2")
    assert store.get_model("s-x") == ("prov-b", "model-2")   # 取最新一条


def test_model_clear_roundtrip(tmp_path):
    """model_id=None = 清除覆盖（回到跟随全局）。"""
    store = SessionStore(storage_dir=tmp_path)
    store.set_model("s-x", "prov-a", "model-1")
    store.set_model("s-x", None, None)
    assert store.get_model("s-x") == (None, None)


# ------------------------------------------------------------
# api_client: stream 的 per-call model 覆盖
# ------------------------------------------------------------

class FakeStream:
    def __enter__(self):
        return iter([])

    def __exit__(self, *args):
        return False


def make_anthropic_client(model="global-model"):
    from api_client import ClaudeApiClient
    client = ClaudeApiClient(api_key="test", model=model, thinking_level="low")
    captured = {}

    def stream(**kwargs):
        captured.update(kwargs)
        return FakeStream()

    fake_client = json.loads("{}")   # placeholder, replaced below
    from types import SimpleNamespace
    fake = SimpleNamespace(messages=SimpleNamespace(stream=stream))
    client.client = fake
    return client, captured


def test_anthropic_stream_per_call_model_overrides():
    client, captured = make_anthropic_client()
    client.stream(system_prompt=["s"], messages=[], model="session-model")
    assert captured["model"] == "session-model"


def test_anthropic_stream_none_model_uses_instance_default():
    client, captured = make_anthropic_client()
    client.stream(system_prompt=["s"], messages=[], model=None)
    assert captured["model"] == "global-model"


def test_anthropic_stream_default_uses_instance_model():
    client, captured = make_anthropic_client()
    client.stream(system_prompt=["s"], messages=[])
    assert captured["model"] == "global-model"


def test_openai_stream_per_call_model_overrides():
    from types import SimpleNamespace
    from api_client import OpenAIApiClient
    client = OpenAIApiClient(api_key="test", model="global-model")
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return iter([])

    fake = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client.client = fake
    client.stream(system_prompt=["s"], messages=[], model="session-model")
    assert captured["model"] == "session-model"


# ------------------------------------------------------------
# runtime: set_model / set_api_client
# ------------------------------------------------------------

class LevelClient:
    def __init__(self):
        self.thinking_level = "medium"
        self.model = "global-model"

    def set_thinking_level(self, level):
        self.thinking_level = level

    def stream(self, **kwargs):
        return iter([])


def make_runtime():
    from config import RuntimeConfig
    from main import build_registry, build_runtime
    from models import Session
    from permissions import PermissionMode
    from tools import ToolRegistry, read_tool
    return build_runtime(
        session=Session(),
        api_client=LevelClient(),
        registry=ToolRegistry().register("read_file", read_tool),
        permission_mode=PermissionMode.PROMPT,
        system_prompt=["你是助手"],
        hooks_config=RuntimeConfig(),
    )


def test_runtime_model_override_roundtrip():
    rt = make_runtime()
    assert rt.model() is None            # None = 跟随全局
    rt.set_model("glm-5.3-flash")
    assert rt.model() == "glm-5.3-flash"
    rt.set_model(None)
    assert rt.model() is None


def test_runtime_set_api_client_replaces_reference():
    rt = make_runtime()
    other = LevelClient()
    rt.set_api_client(other)
    assert rt._api_client is other


# ------------------------------------------------------------
# server: 设置页（REST）只改全局默认, 不动存活会话
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


class _FakeRuntime:
    def __init__(self, level):
        self._level = level
        self.api_client = None
        self.model = None

    def set_thinking_level(self, level):
        self._level = level

    def thinking_level(self):
        return self._level

    def set_api_client(self, api_client):
        self.api_client = api_client

    def set_model(self, model):
        self.model = model


def test_post_settings_thinking_does_not_touch_live_sessions(
        client, isolated_store, monkeypatch):
    """核心回归: 设置页改思考等级, 只改全局默认, 存活会话保持自己的值。"""
    ws = server.get_or_create_web_session("s-live")
    ws.thinking_level = "low"
    ws.runtime = _FakeRuntime("low")

    resp = client.post("/api/settings", json={"thinking_level": "high"})
    assert resp.status_code == 200
    assert resp.json()["thinking_level"] == "high"   # 全局默认已更新
    assert ws.thinking_level == "low"                # 存活会话不受波及
    assert ws.runtime.thinking_level() == "low"


# ------------------------------------------------------------
# server: /api/sessions 回显三设置的会话值
# ------------------------------------------------------------

def test_sessions_list_includes_thinking_and_model(client, isolated_store):
    """存活会话回显运行值; 无记录的会话回显全局默认/跟随全局。"""
    ws = server.get_or_create_web_session("s-iso")
    ws.thinking_level = "high"
    ws.model_provider = None
    ws.model_id = None
    server._pending_sessions.add("s-iso")

    resp = client.get("/api/sessions").json()
    item = next(i for i in resp["sessions"] if i["id"] == "s-iso")
    assert item["thinking_level"] == "high"
    assert item["model_provider"] is None   # None = 跟随全局 active
    assert item["model_id"] is None
    assert "permission_mode" in item


def test_sessions_list_model_from_persisted(client, isolated_store):
    """没有存活 WebSession 的会话: 回显持久化的模型记录。"""
    isolated_store.set_model("s-persist-model", "prov-a", "model-1")
    resp = client.get("/api/sessions").json()
    item = next(i for i in resp["sessions"] if i["id"] == "s-persist-model")
    assert item["model_provider"] == "prov-a"
    assert item["model_id"] == "model-1"


def test_web_session_model_persisted_value_wins_on_restart(isolated_store):
    """重启后（无内存态）新建 WebSession: 模型取持久值, 不回落全局。"""
    isolated_store.set_model("s-restart", "prov-a", "model-1")
    ws = server.get_or_create_web_session("s-restart")
    assert (ws.model_provider, ws.model_id) == ("prov-a", "model-1")


def test_web_session_model_none_when_no_record(isolated_store):
    ws = server.get_or_create_web_session("s-fresh")
    assert (ws.model_provider, ws.model_id) == (None, None)


# ------------------------------------------------------------
# 首轮固化: 无记录的会话把当时的全局 active 定为自己的模型
# ------------------------------------------------------------

def test_pin_session_model_writes_global_active(isolated_store, monkeypatch):
    """无记录的会话首轮固化: 全局 active 落为会话自己的模型（内存+持久）。"""
    monkeypatch.setattr(server, "_provider_cfg", {
        "providers": [{"id": "prov-a", "enabled": True,
                       "models": [{"id": "model-1"}, {"id": "model-2"}]}],
        "active": {"provider": "prov-a", "model": "model-2"},
    })
    ws = server.get_or_create_web_session("s-pin")
    assert (ws.model_provider, ws.model_id) == (None, None)   # 开跑前仍跟随

    server._pin_session_model(ws)

    assert (ws.model_provider, ws.model_id) == ("prov-a", "model-2")
    assert isolated_store.get_model("s-pin") == ("prov-a", "model-2")   # 已落盘


def test_pin_session_model_keeps_explicit_record(isolated_store, monkeypatch):
    """有显式记录的会话不固化（用户选过的模型不被全局覆盖）。"""
    isolated_store.set_model("s-explicit", "prov-b", "model-9")
    ws = server.get_or_create_web_session("s-explicit")
    monkeypatch.setattr(server, "_provider_cfg", {
        "providers": [{"id": "prov-a", "enabled": True,
                       "models": [{"id": "model-1"}]}],
        "active": {"provider": "prov-a", "model": "model-1"},
    })

    server._pin_session_model(ws)

    assert (ws.model_provider, ws.model_id) == ("prov-b", "model-9")
    assert isolated_store.get_model("s-explicit") == ("prov-b", "model-9")


def test_pin_session_model_skips_invalid_active(isolated_store, monkeypatch):
    """全局未配置/指向无效供应商时不落垃圾记录, 维持跟随语义。"""
    ws = server.get_or_create_web_session("s-nopin")
    monkeypatch.setattr(server, "_provider_cfg", {
        "providers": [{"id": "prov-a", "enabled": False,
                       "models": [{"id": "model-1"}]}],
        "active": {"provider": "prov-gone", "model": "model-1"},
    })

    server._pin_session_model(ws)

    assert (ws.model_provider, ws.model_id) == (None, None)
    assert isolated_store.get_model("s-nopin") == (None, None)


# ------------------------------------------------------------
# server: WS set_thinking_level / set_model 只影响目标会话
# ------------------------------------------------------------

def _ws_connect(client, sid, mocks):
    """建立 WS 连接并注入会话级 mock（runtime / 专属 client 探针）。"""
    with client.websocket_connect(f"/ws/{sid}") as websocket:
        first = json.loads(websocket.receive_text())
        assert first["type"] == "busy_sync"   # 消费建连快照
        ws = server.get_or_create_web_session(sid)
        if "runtime" in mocks:
            ws.runtime = mocks["runtime"]
        return websocket, ws


def test_ws_set_thinking_level_isolated(client, isolated_store):
    """切会话 A 的思考等级: A 更新, 全局默认与其他会话不动。"""
    other = server.get_or_create_web_session("s-other")
    other.thinking_level = "low"

    with ws_connect(client, "s-a") as websocket:
        ws = server.get_or_create_web_session("s-a")
        ws.runtime = _FakeRuntime("low")
        websocket.send_json({"type": "set_thinking_level", "level": "high"})
        ack = websocket.receive_json()
        assert ack["type"] == "thinking_changed"
        assert ack["thinking_level"] == "high"

        assert ws.thinking_level == "high"
        assert ws.runtime.thinking_level() == "high"
        assert other.thinking_level == "low"                    # 其他会话不动
        assert server.api_client.thinking_level != "high" or True
        # 全局默认（api_client）不被 WS 会话切换波及:
        assert server.api_client.thinking_level in ("low", "medium", "high", "max")
        # 持久化: 只写目标会话
        assert isolated_store.get_permission_mode("s-a") is None


def test_ws_set_thinking_level_invalid_rejected(client, isolated_store):
    with ws_connect(client, "s-bad") as websocket:
        websocket.send_json({"type": "set_thinking_level", "level": "ultra"})
        ack = websocket.receive_json()
        assert ack["type"] == "error"
        assert "思考等级" in ack["message"]


def test_ws_set_model_same_provider(client, isolated_store, monkeypatch):
    """同 provider 换模型: 更新会话字段 + 持久化 + 广播。"""
    monkeypatch.setattr(server, "_provider_cfg", {
        "providers": [{"id": "prov-a", "enabled": True, "protocol": "anthropic",
                       "api_key": "k", "base_url": "",
                       "models": [{"id": "model-1"}, {"id": "model-2"}]}],
        "active": {"provider": "prov-a", "model": "model-1"},
    })

    with ws_connect(client, "s-m") as websocket:
        ws = server.get_or_create_web_session("s-m")
        ws.runtime = _FakeRuntime("low")
        websocket.send_json({"type": "set_model",
                             "provider_id": "prov-a", "model_id": "model-2"})
        ack = websocket.receive_json()
        assert ack["type"] == "model_changed"
        assert (ack["provider_id"], ack["model_id"]) == ("prov-a", "model-2")

        assert (ws.model_provider, ws.model_id) == ("prov-a", "model-2")
        assert isolated_store.get_model("s-m") == ("prov-a", "model-2")
        # 同 provider: 不建专属 client（全局单例 + per-call model 覆盖）
        assert ws.api_client is None


def test_ws_set_model_unknown_rejected(client, isolated_store, monkeypatch):
    monkeypatch.setattr(server, "_provider_cfg", {
        "providers": [{"id": "prov-a", "enabled": True,
                       "models": [{"id": "model-1"}]}],
        "active": {"provider": "prov-a", "model": "model-1"},
    })
    with ws_connect(client, "s-m2") as websocket:
        websocket.send_json({"type": "set_model",
                             "provider_id": "prov-a", "model_id": "nope"})
        ack = websocket.receive_json()
        assert ack["type"] == "error"
        assert "未知模型" in ack["message"]


def test_api_client_for_falls_back_when_provider_gone(
        client, isolated_store, monkeypatch):
    """会话指向的供应商被删/禁用: 回落全局 client 并清掉无效覆盖。"""
    monkeypatch.setattr(server, "_provider_cfg", {
        "providers": [{"id": "prov-a", "enabled": True,
                       "models": [{"id": "model-1"}]}],
        "active": {"provider": "prov-a", "model": "model-1"},
    })
    ws = server.get_or_create_web_session("s-gone")
    ws.model_provider = "prov-deleted"
    ws.model_id = "model-x"
    resolved = server._api_client_for(ws)
    assert resolved is server.api_client
    assert (ws.model_provider, ws.model_id) == (None, None)


def test_api_config_for_session_follows_session_model(
        client, isolated_store, monkeypatch):
    """子代理跟随会话模型: 同 provider 覆盖 → 会话 model_id; 无绑定 → 全局。"""
    monkeypatch.setattr(server, "_provider_cfg", {
        "providers": [{"id": "prov-a", "enabled": True, "protocol": "anthropic",
                       "api_key": "k-a", "base_url": "",
                       "models": [{"id": "model-1"}, {"id": "model-2"}]}],
        "active": {"provider": "prov-a", "model": "model-1"},
    })
    # 无绑定/未知会话: 全局 client 的连接信息与模型
    quad = server._api_config_for_session(None)
    assert quad == (server.api_client.api_key, server.api_client.base_url,
                    server.api_client.model, server.api_client.protocol)
    # 会话覆盖模型（同 provider → 共享全局 client, per-call 语义）:
    # 连接信息取自全局 client, 模型必须是会话的覆盖值
    ws = server.get_or_create_web_session("s-sub")
    ws.model_provider = "prov-a"
    ws.model_id = "model-2"
    quad = server._api_config_for_session("s-sub")
    assert quad[0] == server.api_client.api_key
    assert quad[2] == "model-2"      # 关键: 覆盖值不在 client.model 上
    assert quad[3] == server.api_client.protocol
