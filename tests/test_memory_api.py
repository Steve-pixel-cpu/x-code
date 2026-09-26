# 记忆 Web API 测试: CRUD / 校验 / 404 / user 来源
# 隔离口径: monkeypatch memory.tools 的 store 单例（不触真实 ~/.x-code）,
# server 端点在请求路径上惰性导入 get_memory_store, 单例替换即全链路生效。
import pytest

from fastapi.testclient import TestClient

from memory.store import JsonFileStore
from memory.tools import set_memory_store
import server  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "")   # 关门禁（同 test_server 口径）
    s = JsonFileStore(tmp_path / "memory.json")
    set_memory_store(s)
    yield s
    set_memory_store(None)


@pytest.fixture()
def client():
    with TestClient(server.app) as c:
        yield c


def test_get_empty(store, client):
    r = client.get("/api/memory")
    assert r.status_code == 200
    assert r.json() == {"memories": [], "evicted_count": 0}


def test_add_and_get(store, client):
    r = client.post("/api/memory",
                    json={"content": "项目用 uv", "category": "context"})
    assert r.status_code == 200
    assert r.json()["memory"]["source"] == "user"   # API 手动添加 = user 来源
    r = client.get("/api/memory")
    assert r.json()["memories"][0]["content"] == "项目用 uv"


def test_add_empty_content_400(store, client):
    assert client.post("/api/memory", json={"content": "  "}).status_code == 400


def test_update_and_delete(store, client):
    m = store.add(content="旧", category="fact", source="agent")
    r = client.patch(f"/api/memory/{m['id']}", json={"content": "新"})
    assert r.status_code == 200
    assert r.json()["memory"]["content"] == "新"
    assert client.delete(f"/api/memory/{m['id']}").status_code == 200
    assert client.get("/api/memory").json()["memories"] == []


def test_update_delete_missing_404(store, client):
    assert client.patch("/api/memory/mem_nope",
                        json={"content": "x"}).status_code == 404
    assert client.delete("/api/memory/mem_nope").status_code == 404
