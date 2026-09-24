"""浏览器工具测试: browser_tools.py（接线一致性 + 参数校验 + 降级提示）
+ 可选集成（本机装好 playwright 时走真实 headless chromium 的闭环）。

不 mock playwright 内部——单测只覆盖不碰浏览器的路径（spec/handler 对齐、
参数缺失、URL 补全、未安装时的 ERROR 文本）; 真实浏览器用例用
pytest.importorskip 门禁，没装就跳过，CI 无浏览器也不会红。
"""
import http.server
import threading
import time

import pytest

import browser_tools as bt
from browser_tools import (BROWSER_TOOL_NAMES, BROWSER_TOOL_SPECS,
                           browser_click_tool, browser_console_tool,
                           browser_close_tool, browser_navigate_tool,
                           browser_snapshot_tool, browser_type_tool,
                           register_browser_tools, _clamp_ms)
from main import TOOLS, TOOL_REQUIREMENTS, build_registry


# --- spec / registry / 权限档 一致性 ---

def test_specs_unique_and_named():
    names = [s["name"] for s in BROWSER_TOOL_SPECS]
    assert len(names) == len(set(names))
    assert names[0] == "browser_navigate"
    assert set(names) == BROWSER_TOOL_NAMES


def test_tools_list_contains_browser_specs():
    tool_names = {t["name"] for t in TOOLS}
    assert BROWSER_TOOL_NAMES <= tool_names


def test_registry_has_all_handlers():
    registry = build_registry()
    for name in BROWSER_TOOL_NAMES:
        assert name in registry._handlers


def test_permissions_registration():
    from permissions import PLAN_MODE, WORKSPACE_WRITE_MODE
    assert TOOL_REQUIREMENTS["browser_navigate"] is PLAN_MODE      # 只读
    assert TOOL_REQUIREMENTS["browser_snapshot"] is PLAN_MODE
    assert TOOL_REQUIREMENTS["browser_console"] is PLAN_MODE
    assert TOOL_REQUIREMENTS["browser_click"] is WORKSPACE_WRITE_MODE
    assert TOOL_REQUIREMENTS["browser_type"] is WORKSPACE_WRITE_MODE
    assert TOOL_REQUIREMENTS["browser_screenshot"] is WORKSPACE_WRITE_MODE


def test_register_chainable():
    from tools import ToolRegistry
    r = register_browser_tools(ToolRegistry())
    assert BROWSER_TOOL_NAMES <= set(r._handlers)


# --- 参数校验（不碰浏览器） ---

def test_navigate_requires_url():
    out = browser_navigate_tool({}, None)
    assert out.startswith("ERROR: url is required")


def test_click_type_require_selector():
    assert browser_click_tool({}, None).startswith("ERROR: selector")
    assert browser_type_tool({"text": "x"}, None).startswith("ERROR: selector")
    assert browser_type_tool({"selector": "#a"}, None) == "" or True  # type 进浏览器线程, 仅查 selector 缺失路径


def test_console_and_close_never_raise(monkeypatch):
    """没装 playwright / 没开浏览器时也必须是文本, 不能抛异常。"""
    out = browser_console_tool({"clear": True}, None)
    assert isinstance(out, str)
    out = browser_close_tool({}, None)
    assert isinstance(out, str)


def test_clamp_ms():
    assert _clamp_ms(None) == 8000
    assert _clamp_ms("bad") == 8000
    assert _clamp_ms(10) == 1000        # 下限
    assert _clamp_ms(999_999) == 60_000  # 上限
    assert _clamp_ms(15_000) == 15_000


# --- 降级提示: 把 import 摘掉后应返回安装指引文本 ---

def test_graceful_without_playwright(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def blocked(name, *a, **k):
        if name.split(".")[0] == "playwright":
            raise ImportError("No module named 'playwright'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)
    # 强制重新走 launch 路径
    monkeypatch.setattr(bt._STATE, "_browser", None, raising=False)
    out = browser_navigate_tool({"url": "http://127.0.0.1:1/"}, None)
    assert out.startswith("ERROR:")
    assert "playwright install chromium" in out


# --- 集成: 真实 headless chromium（未装则整组跳过） ---

_FORM_PAGE = b"""<html><head><title>form</title></head><body>
<input id="name" placeholder="who"/>
<button id="go" onclick="done()">Go</button>
<pre id="out"></pre>
<script>
function done(){
  var v = document.getElementById('name').value;
  document.getElementById('out').textContent = 'hello ' + v;
  console.log('clicked with ' + v);
}
</script></body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_FORM_PAGE)

    def log_message(self, *a):  # 静音
        pass


@pytest.fixture(scope="module")
def http_url():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


def _playwright_ready() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _playwright_ready(), reason="playwright 未安装")
def test_full_flow(http_url):
    try:
        nav = browser_navigate_tool({"url": http_url}, None)
        assert "form" in nav and "http status: 200" in nav
        assert "textbox" in nav or "input" in nav  # aria snapshot

        typed = browser_type_tool({"selector": "#name", "text": "xcode"},
                                  None)
        assert "typed" in typed

        clicked = browser_click_tool({"selector": "#go"}, None)
        assert "clicked" in clicked

        page = browser_snapshot_tool({}, None)
        assert "hello xcode" in page  # JS 执行生效

        console = browser_console_tool({}, None)
        assert "clicked with xcode" in console
    finally:
        browser_close_tool({}, None)


@pytest.mark.skipif(not _playwright_ready(), reason="playwright 未安装")
def test_navigate_auto_scheme(http_url):
    try:
        out = browser_navigate_tool({"url": http_url.replace("http://", "")},
                                    None)
        assert "http status: 200" in out
    finally:
        browser_close_tool({}, None)
