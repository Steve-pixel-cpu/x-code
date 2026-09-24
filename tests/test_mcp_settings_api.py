"""MCP 服务器设置 API: GET 合一下发 / POST 校验+落盘+热应用 / 热重载。

读-改-写走 config.SETTINGS_FILE, 测试 monkeypatch 该路径隔离（与
test_providers 同一口径）。热应用路径 _attach_mcp_tools 需要真实连接,
这里用 mcp_echo_server 的 stdio 服务器做一次端到端, 其余用例只打校验与
持久化路径（连不上的服务器降级 failed, 不影响断言落盘结果）。"""

import json

import pytest

from config import SETTINGS_FILE  # noqa: E402
import server  # noqa: E402


@pytest.fixture()
def settings_file(tmp_path, monkeypatch):
    target = tmp_path / "home" / "settings.json"   # 与 discover 的用户级路径一致
    home = target.parent
    monkeypatch.setattr("config.SETTINGS_FILE", target)
    monkeypatch.setattr("server.SETTINGS_FILE", target)
    monkeypatch.setattr("server.USER_DIR", home)   # reload 的 config_home
    monkeypatch.setattr(server, "API_TOKEN", "")   # 关门禁（同 test_server 口径）
    # 热应用会用 manager 换绑, 用例结束清干净, 防污染其他用例
    yield target
    from mcp_client import get_mcp_manager
    get_mcp_manager().disconnect_all()


@pytest.fixture(autouse=True)
def _isolated_mcp_manager():
    """模块内用例共享全局 manager 单例: 前后各清一次, 防跨用例泄漏
    （test_get_empty 断言 status == [], 若前面的用例留了连接就会假失败）。"""
    from mcp_client import get_mcp_manager
    get_mcp_manager().disconnect_all()
    yield
    get_mcp_manager().disconnect_all()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    with TestClient(server.app) as c:
        yield c


def test_get_empty(settings_file, client):
    r = client.get("/api/mcp/servers")
    assert r.status_code == 200
    assert r.json() == {"mcpServers": {}, "status": []}


def test_get_merges_config_and_status(settings_file, client):
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps({
        "theme": "dark",
        "mcpServers": {"x": {"command": "whatever"}},
    }), encoding="utf-8")
    r = client.get("/api/mcp/servers")
    body = r.json()
    assert body["mcpServers"]["x"] == {"command": "whatever"}
    assert body["status"] == []   # 未连接过


def test_save_roundtrip_and_preserves_other_keys(settings_file, client):
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text('{"theme": "dark"}', encoding="utf-8")
    r = client.post("/api/mcp/servers", json={"mcpServers": {
        "demo": {"type": "stdio", "command": "no-such-bin-xyz", "args": ["a"],
                 "timeout": 30}}})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"   # 其他 key 原样保留
    assert data["mcpServers"]["demo"]["command"] == "no-such-bin-xyz"
    # 保存后服务端热应用过: 连不上的服务器降级 failed 而非报错
    assert r.json()["servers"][0]["status"] == "failed"
    assert r.json()["mcp_tool_count"] == 0


def test_save_normalizes_empty_fields(settings_file, client):
    """空值字段剔除; stdio/http 未写 type 时按字段推断。"""
    client.post("/api/mcp/servers", json={"mcpServers": {
        "a": {"type": "stdio", "command": "c", "env": {}, "args": []},
        "b": {"url": "http://x/mcp"},
    }})
    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert data["mcpServers"]["a"] == {"type": "stdio", "command": "c"}
    assert data["mcpServers"]["b"] == {"type": "http", "url": "http://x/mcp"}


@pytest.mark.parametrize("payload", [
    {"mcpServers": []},                                   # 不是对象
    {"mcpServers": {"": {"command": "c"}}},               # 空名
    {"mcpServers": {"x": "not-a-dict"}},                  # 描述不是对象
    {"mcpServers": {"x": {"type": "ws", "url": "u"}}},    # 未知传输
    {"mcpServers": {"x": {}}},                            # stdio 缺 command
    {"mcpServers": {"x": {"type": "http"}}},              # http 缺 url
    {"mcpServers": {"x": {"command": "c", "timeout": 0}}},    # timeout 非正
    {"mcpServers": {"x": {"command": "c", "timeout": True}}}, # bool 混入
    {"mcpServers": {"x": {"command": "c", "args": [1]}}},     # args 非字符串
    {"mcpServers": {"x": {"command": "c", "env": {"k": 1}}}}, # env 值非字符串
])
def test_save_rejects_invalid(settings_file, client, payload):
    r = client.post("/api/mcp/servers", json=payload)
    assert r.status_code == 400, payload
    assert not settings_file.exists()   # 校验失败不动已存配置


def test_save_end_to_end_connects(settings_file, client):
    """UI 保存路径的端到端: 落盘 + 真实连接 echo 服务器 + 工具注入计数。"""
    import sys
    from pathlib import Path
    srv = Path(__file__).parent / "mcp_echo_server.py"
    r = client.post("/api/mcp/servers", json={"mcpServers": {
        "e2e": {"command": sys.executable, "args": [str(srv)]}}})
    assert r.status_code == 200
    body = r.json()
    st = {s["name"]: s for s in body["servers"]}
    assert st["e2e"]["status"] == "connected"
    assert sorted(st["e2e"]["tools"]) == ["echo", "fail"]
    assert body["mcp_tool_count"] == 2
    # 全局 TOOLS 已同步
    names = {t["name"] for t in server.TOOLS}
    assert {"mcp__e2e__echo", "mcp__e2e__fail"} <= names


def test_reload_after_manual_edit(settings_file, client):
    """改完配置文件 → POST reload 生效（设置页「重连全部」的内核语义）。"""
    import sys
    from pathlib import Path
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    srv = Path(__file__).parent / "mcp_echo_server.py"
    settings_file.write_text(json.dumps({"mcpServers": {
        "r": {"command": sys.executable, "args": [str(srv)]}}}),
        encoding="utf-8")
    r = client.post("/api/mcp/reload")
    assert r.status_code == 200
    st = {s["name"]: s for s in r.json()["servers"]}
    assert st["r"]["status"] == "connected"
    assert r.json()["mcp_tool_count"] == 2
