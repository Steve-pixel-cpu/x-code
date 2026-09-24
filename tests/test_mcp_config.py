"""mcpServers 配置解析测试: 结构校验 / ${VAR} 展开 / 跨层级深合并。"""

import json

import pytest

from config import ConfigError, ConfigLoader


def _load(tmp_path, user=None, project=None, local=None):
    """按三级布局写配置文件并加载。"""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True, exist_ok=True)

    if user is not None:
        (home / "settings.json").write_text(
            json.dumps(user), encoding="utf-8")
    if project is not None:
        (proj / ".claude" / "settings.json").write_text(
            json.dumps(project), encoding="utf-8")
    if local is not None:
        (proj / ".claude" / "settings.local.json").write_text(
            json.dumps(local), encoding="utf-8")
    return ConfigLoader(cwd=proj, config_home=home).load()


def test_no_mcp_servers_by_default(tmp_path):
    cfg = _load(tmp_path)
    assert cfg.mcp_servers() == []


def test_stdio_server_minimal(tmp_path):
    cfg = _load(tmp_path, user={"mcpServers": {
        "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}})
    servers = cfg.mcp_servers()
    assert len(servers) == 1
    s = servers[0]
    assert (s.name, s.transport, s.command, s.args) == \
        ("fetch", "stdio", "uvx", ["mcp-server-fetch"])
    assert s.env == {} and s.cwd is None and s.timeout == 60


def test_http_server_with_headers_and_timeout(tmp_path):
    cfg = _load(tmp_path, user={"mcpServers": {
        "docs": {"type": "http", "url": "http://localhost:3000/mcp",
                 "headers": {"Authorization": "Bearer x"},
                 "timeout": 30}}})
    (s,) = cfg.mcp_servers()
    assert (s.transport, s.url, s.headers, s.timeout) == \
        ("http", "http://localhost:3000/mcp", {"Authorization": "Bearer x"}, 30)


def test_env_var_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "tok123")
    cfg = _load(tmp_path, user={"mcpServers": {
        "a": {"command": "run", "env": {"K": "v-${MY_TOKEN}"}},
        "b": {"type": "http", "url": "http://x/${MY_TOKEN}/mcp",
              "headers": {"A": "${MY_TOKEN}"}}}})
    servers = {s.name: s for s in cfg.mcp_servers()}
    assert servers["a"].env == {"K": "v-tok123"}
    assert servers["b"].url == "http://x/tok123/mcp"
    assert servers["b"].headers == {"A": "tok123"}


def test_project_overrides_user_same_server(tmp_path):
    """同名服务器: deep_merge 字段级合并——项目级只覆盖它声明的字段,
    其余继承用户级（与整套配置的合并语义一致）。"""
    cfg = _load(tmp_path,
                user={"mcpServers": {
                    "x": {"command": "old-cmd", "args": ["--old"],
                          "env": {"A": "1"}}}},
                project={"mcpServers": {
                    "x": {"command": "new-cmd", "timeout": 90}}})
    (s,) = cfg.mcp_servers()
    assert s.command == "new-cmd"       # 项目级覆盖
    assert s.timeout == 90              # 项目级新增
    assert s.args == ["--old"]          # 用户级继承
    assert s.env == {"A": "1"}


def test_user_and_project_different_servers_merge(tmp_path):
    cfg = _load(tmp_path,
                user={"mcpServers": {"u": {"command": "u-cmd"}}},
                project={"mcpServers": {"p": {"command": "p-cmd"}}})
    assert {s.name for s in cfg.mcp_servers()} == {"u", "p"}


@pytest.mark.parametrize("bad", [
    ["not", "a", "dict"],                       # 顶层不是对象
    {"x": "not-an-object"},                     # 服务器描述不是对象
    {"x": {"type": "websocket", "url": "u"}},   # 未知传输
    {"x": {}},                                  # stdio 缺 command
    {"x": {"command": "  "}},                   # command 空白
    {"x": {"type": "http"}},                    # http 缺 url
    {"x": {"command": "c", "args": [1]}},       # args 非字符串数组
    {"x": {"command": "c", "env": {"k": 1}}},   # env 值非字符串
    {"x": {"command": "c", "timeout": -1}},     # timeout 非正数
    {"x": {"command": "c", "cwd": 3}},          # cwd 非字符串
    {"": {"command": "c"}},                     # 名字为空
])
def test_invalid_configs_raise(tmp_path, bad):
    with pytest.raises(ConfigError):
        _load(tmp_path, user={"mcpServers": bad})
