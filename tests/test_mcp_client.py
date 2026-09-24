"""MCP 客户端端到端测试: 真实拉起 stdio 服务器进程并走完整链路。

覆盖: 连接/列工具/经 ToolRegistry 调用/错误路径/失败隔离/热重载换绑。
"""

import json
import sys
from pathlib import Path

import pytest

import config as config_mod
from config import ConfigError, ConfigLoader, McpServerConfig
from mcp_client import (MCP_TOOL_PREFIX, McpManager, mcp_tool_name)
from runtime import ToolError
from tools import ToolRegistry

TESTS_DIR = Path(__file__).parent
ECHO_SERVER = TESTS_DIR / "mcp_echo_server.py"


def _stdio_server(name: str = "test", timeout: int = 60) -> McpServerConfig:
    return McpServerConfig(
        name=name, transport="stdio",
        command=sys.executable, args=[str(ECHO_SERVER)], timeout=timeout)


@pytest.fixture
def manager():
    m = McpManager()
    yield m
    m.disconnect_all()


@pytest.fixture(autouse=True)
def _restore_global_tools():
    """build_registry 会把 mcp spec 原地写进全局 TOOLS(与其他用例共享
    同一列表对象)——用例结束后还原, 防跨用例污染。"""
    from main import TOOLS
    before = list(TOOLS)
    yield
    TOOLS[:] = before


def _register(m: McpManager, registry: ToolRegistry) -> None:
    for conn in m.connected():
        for t in conn.list_tools():
            registry.register(
                name=mcp_tool_name(conn.server.name, t["name"]),
                handler=m.handler_for(mcp_tool_name(conn.server.name, t["name"])))


# --- 连接与工具发现 ---------------------------------------------------------

def test_connect_and_list_tools(manager):
    statuses = manager.connect_all([_stdio_server()])
    (st,) = statuses
    assert st.status == "connected", st.error
    assert sorted(st.tools) == ["echo", "fail"]


def test_tool_specs_have_prefix_and_schema(manager):
    manager.connect_all([_stdio_server("srv1")])
    specs = manager.tool_specs()
    names = {s["name"] for s in specs}
    assert names == {"mcp__srv1__echo", "mcp__srv1__fail"}
    for s in specs:
        assert s["input_schema"].get("type") == "object"
        assert "properties" in s["input_schema"]
        assert s["description"]   # 描述透传(带来源前缀)


def test_unsafe_server_name_is_sanitized(manager):
    """server 名里的点/空格被清洗, 工具名仍是安全段。"""
    manager.connect_all([_stdio_server("my server.v2")])
    specs = manager.tool_specs()
    assert {s["name"] for s in specs} == \
        {"mcp__my_server_v2__echo", "mcp__my_server_v2__fail"}


# --- 经 ToolRegistry 的完整调用链 -------------------------------------------

def test_call_via_registry(manager):
    registry = ToolRegistry()
    manager.connect_all([_stdio_server()])
    _register(manager, registry)
    out = registry.execute("mcp__test__echo",
                           json.dumps({"text": "hello 世界"}))
    assert out == "echo: hello 世界"


def test_tool_error_surface(manager):
    """isError=true → ToolError → registry.execute 抛 ToolError。"""
    registry = ToolRegistry()
    manager.connect_all([_stdio_server()])
    _register(manager, registry)
    with pytest.raises(ToolError, match="boom-reason"):
        registry.execute("mcp__test__fail",
                         json.dumps({"reason": "boom-reason"}))


def test_registry_truncates_mcp_output(manager):
    """MCP 工具输出同样过 registry 的统一截断（保首尾、掐中段）。"""
    from tools import MAX_TOOL_OUTPUT_CHARS
    registry = ToolRegistry()
    manager.connect_all([_stdio_server()])
    _register(manager, registry)
    big = "x" * (MAX_TOOL_OUTPUT_CHARS + 5000)
    out = registry.execute("mcp__test__echo", json.dumps({"text": big}))
    # 截断后的输出远小于原始长度, 且带说明标记
    assert len(out) < len(big)
    assert "truncated" in out and "omitted" in out


# --- 失败隔离 ----------------------------------------------------------------

def test_failed_server_does_not_block_others(manager):
    """连不上的服务器标记 failed, 其他服务器照常工作。"""
    bad = McpServerConfig(name="bad", transport="stdio",
                          command="definitely-not-a-real-binary-xyz")
    good = _stdio_server("good")
    statuses = manager.connect_all([bad, good])
    by_name = {s.name: s for s in statuses}
    assert by_name["bad"].status == "failed"
    assert by_name["bad"].error
    assert by_name["good"].status == "connected"
    assert {s["name"] for s in manager.tool_specs()} == \
        {"mcp__good__echo", "mcp__good__fail"}


def test_call_on_disconnected_raises():
    conn_target = _stdio_server("ghost")
    from mcp_client import McpConnection
    conn = McpConnection(conn_target)
    with pytest.raises(ToolError, match="not connected|loop"):
        conn.call_tool("echo", {})


# --- 热重载换绑（server /api/mcp/reload 走的同一条 _attach_mcp_tools 路径） --

def test_reconnect_replaces_tools_in_registry_and_tools_list():
    """重连: 同一 registry 对象换绑, 旧服务器工具被清除, TOOLS 原地同步。"""
    from main import _attach_mcp_tools
    from mcp_client import get_mcp_manager
    registry = ToolRegistry()
    tools = [{"name": "bash", "description": "d", "input_schema": {}}]
    try:
        _attach_mcp_tools(registry, [_stdio_server("alpha")])
        manager = get_mcp_manager()
        manager.sync_tools_list(tools)
        assert "mcp__alpha__echo" in registry._handlers

        # 重连: 同一 registry 对象换绑, 旧工具先消失
        _attach_mcp_tools(registry, [_stdio_server("beta")])
        assert "mcp__alpha__echo" not in registry._handlers
        assert "mcp__alpha__fail" not in registry._handlers
        assert "mcp__beta__echo" in registry._handlers
        manager.sync_tools_list(tools)
        # 内置工具的 spec 原样保留, mcp 项在后
        assert tools[0]["name"] == "bash"
        assert all(t["name"].startswith(MCP_TOOL_PREFIX) for t in tools[1:])
        assert {t["name"] for t in tools[1:]} == \
            {"mcp__beta__echo", "mcp__beta__fail"}
    finally:
        get_mcp_manager().disconnect_all()


# --- build_registry 装配链（CLI 与 Web 共用路径） ----------------------------

def test_build_registry_with_mcp_end_to_end(tmp_path):
    """main.build_registry(mcp_servers=...) 走通: registry 有 mcp 工具,
    TOOLS 列表同步, 内置工具不受影响。"""
    from main import TOOLS, build_registry, mcp_status_lines
    from mcp_client import get_mcp_manager
    before_builtin = [t["name"] for t in TOOLS
                      if not t["name"].startswith(MCP_TOOL_PREFIX)]
    registry = build_registry(mcp_servers=[_stdio_server()])
    try:
        assert any("✓" in ln for ln in mcp_status_lines())
        names = set(registry._handlers)
        assert "mcp__test__echo" in names and "mcp__test__fail" in names
        spec_names = {t["name"] for t in TOOLS}
        assert {"mcp__test__echo", "mcp__test__fail"} <= spec_names
        # 内置工具一个不少
        assert set(before_builtin) <= spec_names
        out = registry.execute("mcp__test__echo",
                               json.dumps({"text": "via-build"}))
        assert out == "echo: via-build"
    finally:
        get_mcp_manager().disconnect_all()


def test_build_registry_without_mcp_unchanged():
    from main import TOOLS, build_registry
    n_specs = len(TOOLS)
    registry = build_registry()
    assert len(TOOLS) == n_specs   # 原地同步是 no-op
    assert all(not t["name"].startswith(MCP_TOOL_PREFIX) for t in TOOLS)
    assert not any(h.startswith(MCP_TOOL_PREFIX) for h in registry._handlers)


# --- 配置 → 连接全链路（通过 ConfigLoader 真实文件） ------------------------

def test_config_to_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "USER_DIR", tmp_path / "home")
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"mcpServers": {
        "cfgsrv": {"command": sys.executable,
                   "args": [str(ECHO_SERVER)], "timeout": 30}}}),
        encoding="utf-8")
    cfg = ConfigLoader(cwd=tmp_path, config_home=home).load()
    registry = ToolRegistry()
    registry, _ = build_registry_lite(cfg.mcp_servers(), registry)
    assert "mcp__cfgsrv__echo" in registry._handlers
    out = registry.execute("mcp__cfgsrv__echo",
                           json.dumps({"text": "cfg"}))
    assert out == "echo: cfg"


def build_registry_lite(servers, registry):
    """test 专用的极简装配（避免 import main 的重依赖）。"""
    from mcp_client import get_mcp_manager
    m = get_mcp_manager()
    m.connect_all(servers)
    _register(m, registry)
    return registry, []
