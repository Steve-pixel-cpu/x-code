"""浏览器自动化工具（Playwright headless Chromium）: 让 Agent 能实测 Web 系统。

场景: 打开被测页面 → 填表单/点按钮 → 读 aria snapshot 与 console 报错 →
截图存证。与 agent_tools.py 同构: spec（给模型的 JSON Schema）+ handler
（(params: dict, workdir) -> str）+ register_browser_tools（链式注册进
ToolRegistry）。CLI 与 Web 共用同一份——两者都走 main.build_registry()。

线程模型: Playwright 的 sync API 非线程安全，而 runtime 的工具池是多线程
（ThreadPoolExecutor），所以所有浏览器操作经单线程 executor 串行化——
外层 handler 只做参数整理与结果拼装，真正碰 playwright 对象的代码都在
_BROWSER_POOL 那一个线程里跑。

降级: playwright 未安装/未 download 浏览器时返回安装指引的 ERROR 文本，
不抛异常——模型看到文本能把指引转述给用户，异常只会变成裸 ToolError。
截图 PNG 落在用户目录（~/.x-code/screenshots/），不污染项目 git。
"""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

# 与 tools.py 相同的换行约定
NL = chr(10)

# 截图输出目录: 用户级, 与会话存储平级
def screenshots_dir() -> Path:
    from config import USER_DIR
    return USER_DIR / "screenshots"


# --- 浏览器单例状态（只在 _BROWSER_POOL 线程里触碰） ---

class _BrowserState:
    """懒初始化的 playwright/chromium/page 持有者。所有字段只在
    _BROWSER_POOL 线程里读写（goto/click 等调用与 _reset 都在那跑）。"""

    def __init__(self):
        self._pw = None            # playwright 实例（stop 时关闭）
        self._browser = None       # Browser（复用, 工具间不重启）
        self._page = None          # 当前活动 Page（单页面会话）
        self._channel_used = None  # 实际启动成功的渠道（诊断用）
        self.console: list[str] = []   # console 消息 + pageerror 累积
        self.max_console = 200     # 防长会话无限膨胀

    # -- 初始化 / 关闭 --

    def _import_playwright(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError(
                "playwright is not installed. Run: "
                "`uv add playwright` (or pip install playwright), "
                "then `playwright install chromium`") from e
        return sync_playwright

    def _launch(self):
        """起 playwright + headless chromium。渠道顺序: 环境变量 XCODE_BROWSER_CHANNEL
        指定 > chromium（playwright 自带） > 本机 chrome > 本机 msedge（Windows
        全机自带）。桌面端/冻结 exe 没法跑 `playwright install`，靠后面的本机
        浏览器渠道兜底——Win10/11 必有 Edge，等效"零额外下载"。"""
        sync_playwright = self._import_playwright()
        self._pw = sync_playwright().start()
        import os
        forced = os.environ.get("XCODE_BROWSER_CHANNEL", "").strip() or None
        channels = [forced] if forced else [None, "chrome", "msedge"]
        last_err: Exception | None = None
        for channel in channels:
            try:
                self._browser = self._pw.chromium.launch(headless=True,
                                                         channel=channel)
                self._channel_used = channel or "chromium"
                break
            except Exception as e:
                last_err = e
                self._browser = None
        if self._browser is None:
            self._pw.stop()
            self._pw = None
            raise RuntimeError(
                f"failed to launch any Chromium channel ({last_err}). "
                f"Fix: run `playwright install chromium`, or install "
                f"Chrome/Edge, or set XCODE_BROWSER_CHANNEL to a specific "
                f"channel") from last_err
        self._page = None
        self.console = []

    def _ensure(self):
        """当前 thread 里保证 browser 可用。"""
        if self._browser is None:
            self._launch()
        return self._browser

    def _page_ctx(self):
        """当前页面; 没有则建一个并挂 console 收集器。"""
        self._ensure()
        if self._page is None:
            self._page = self._browser.new_page()
            page = self._page
            page.on("console", lambda m: self._push_console(
                    f"[{m.type}] {m.text}"))
            page.on("pageerror", lambda e: self._push_console(
                    f"[pageerror] {e}"))
            page.on("close", lambda _: setattr(self, "_page", None))
        return self._page

    def _push_console(self, line: str):
        self.console.append(line)
        if len(self.console) > self.max_console:
            self.console = self.console[-self.max_console:]

    def _reset(self):
        """全量关闭（_close_tool 与异常兜底共用; 只在 pool 线程跑）。"""
        for attr in ("_page", "_browser", "_pw"):
            obj = getattr(self, attr, None)
            try:
                if obj is not None:
                    obj.close() if attr != "_pw" else obj.stop()
            except Exception:
                pass
            setattr(self, attr, None)
        self.console = []

    # -- 供 handler 经 pool 调用的操作 --

    def navigate(self, url: str, timeout_ms: int) -> str:
        page = self._page_ctx()
        resp = page.goto(url, timeout=timeout_ms, wait_until="load")
        status = resp.status if resp else "?"
        return (f"title: {page.title()}" + NL
                + f"url: {page.url}" + NL
                + f"http status: {status}" + NL + NL
                + _aria_snapshot(page))

    def snapshot(self) -> str:
        page = self._page_ctx()
        return (f"url: {page.url}" + NL + NL
                + _aria_snapshot(page) + NL + NL
                + "-- visible text --" + NL + _visible_text(page))

    def click(self, selector: str, timeout_ms: int) -> str:
        page = self._page_ctx()
        page.click(selector, timeout=timeout_ms)
        page.wait_for_load_state("load", timeout=timeout_ms)
        return (f"clicked: {selector}" + NL + NL + _aria_snapshot(page))

    def type_text(self, selector: str, text: str,
                  submit: bool, timeout_ms: int) -> str:
        page = self._page_ctx()
        page.fill(selector, text, timeout=timeout_ms)
        if submit:
            page.press(selector, "Enter", timeout=timeout_ms)
            page.wait_for_load_state("load", timeout=timeout_ms)
        return f"typed into {selector}: {text!r}" + (
            " + Enter" if submit else "")

    def press_key(self, key: str, timeout_ms: int) -> str:
        page = self._page_ctx()
        page.keyboard.press(key)
        page.wait_for_load_state("load", timeout=timeout_ms)
        return f"pressed: {key}"

    def select_option(self, selector: str, value: str, timeout_ms: int) -> str:
        page = self._page_ctx()
        page.select_option(selector, value, timeout=timeout_ms)
        return f"selected {value!r} in {selector}"

    def screenshot(self, full_page: bool, path: Optional[str]) -> str:
        page = self._page_ctx()
        out = Path(path) if path else (
            screenshots_dir() / f"shot-{time.strftime('%Y%m%d-%H%M%S')}"
            f"-{uuid.uuid4().hex[:6]}.png")
        out.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out), full_page=full_page)
        return (f"screenshot saved: {out.resolve()}" + NL
                + f"full_page: {full_page}")

    def console_text(self, clear: bool) -> str:
        if not self.console:
            body = "(no console messages yet)"
        else:
            body = NL.join(self.console)
        if clear:
            self.console = []
        return body

    def close(self) -> str:
        self._reset()
        return "browser closed"


def _aria_snapshot(page) -> str:
    """页面结构描述。Playwright 的 aria_snapshot 对 LLM 最友好（角色树）;
    版本不支持时退回 outerHTML 片段。"""
    try:
        return page.locator("body").aria_snapshot()
    except Exception:
        html = page.content()
        return f"<body (html fallback)>{html[:4000]}"


def _visible_text(page) -> str:
    try:
        return page.evaluate("() => document.body.innerText") or ""
    except Exception:
        return "(could not read page text)"


_STATE = _BrowserState()
# max_workers=1: 所有 playwright 对象只被这一个线程触碰（sync API 非线程安全）
_BROWSER_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser")


def _run_in_browser_thread(fn, *args):
    """把浏览器操作丢进单线程池执行。RuntimeError（安装/启动问题）原样
    返回成 ERROR 文本; 其余异常转简短 ERROR（保 registry 的 ToolError 可读）。"""
    try:
        return _BROWSER_POOL.submit(fn, *args).result(timeout=120)
    except RuntimeError as e:
        return f"ERROR: {e}"
    except Exception as e:
        return f"ERROR: browser operation failed: {e}"


# --- 参数整理（线程池外）: 统一签名 (params, workdir) -> str ---

def _clamp_ms(raw, default=8000, cap=60_000) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1000, min(n, cap))


def browser_navigate_tool(params: dict, workdir: Optional[str] = None) -> str:
    url = str(params.get("url") or "").strip()
    if not url:
        return "ERROR: url is required"
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return _run_in_browser_thread(_STATE.navigate, url,
                                  _clamp_ms(params.get("timeout")))


def browser_snapshot_tool(params: dict, workdir: Optional[str] = None) -> str:
    return _run_in_browser_thread(_STATE.snapshot)


def browser_click_tool(params: dict, workdir: Optional[str] = None) -> str:
    selector = str(params.get("selector") or "").strip()
    if not selector:
        return "ERROR: selector is required"
    return _run_in_browser_thread(_STATE.click, selector,
                                  _clamp_ms(params.get("timeout")))


def browser_type_tool(params: dict, workdir: Optional[str] = None) -> str:
    selector = str(params.get("selector") or "").strip()
    if not selector:
        return "ERROR: selector is required"
    text = str(params.get("text") or "")
    return _run_in_browser_thread(_STATE.type_text, selector, text,
                                  bool(params.get("submit")),
                                  _clamp_ms(params.get("timeout")))


def browser_press_tool(params: dict, workdir: Optional[str] = None) -> str:
    key = str(params.get("key") or "").strip()
    if not key:
        return "ERROR: key is required"
    return _run_in_browser_thread(_STATE.press_key, key,
                                  _clamp_ms(params.get("timeout")))


def browser_select_tool(params: dict, workdir: Optional[str] = None) -> str:
    selector = str(params.get("selector") or "").strip()
    value = str(params.get("value") or "").strip()
    if not selector or not value:
        return "ERROR: selector and value are required"
    return _run_in_browser_thread(_STATE.select_option, selector, value,
                                  _clamp_ms(params.get("timeout")))


def browser_screenshot_tool(params: dict, workdir: Optional[str] = None) -> str:
    return _run_in_browser_thread(_STATE.screenshot,
                                  bool(params.get("full_page")),
                                  params.get("path") or None)


def browser_console_tool(params: dict, workdir: Optional[str] = None) -> str:
    return _run_in_browser_thread(_STATE.console_text,
                                  bool(params.get("clear")))


def browser_close_tool(params: dict, workdir: Optional[str] = None) -> str:
    return _run_in_browser_thread(_STATE.close)


# --- 工具 spec: 描述与参数 schema 给模型看 ---

_COMMON_TIMEOUT = {
    "timeout": {"type": "integer",
                "description": "Milliseconds, 1000-60000, default 8000"},
}

SELECTOR_DOC = (
    "CSS selector or text engine ('text=Login', 'role=button[name=Submit]')")

browser_navigate_spec = {
    "name": "browser_navigate",
    "description": (
        "Open a URL in a headless Chromium browser (Playwright). Returns "
        "page title, final URL, HTTP status and an aria snapshot of the "
        "page structure. First call of a web-testing flow; subsequent "
        "snapshot/click/type operate on the same page."),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "http(s) URL"},
            **_COMMON_TIMEOUT,
        },
        "required": ["url"],
    },
}

browser_snapshot_spec = {
    "name": "browser_snapshot",
    "description": (
        "Capture the current browser page as an aria snapshot (accessibility "
        "tree with roles/names) plus visible text. Use it after navigate or "
        "interactions to inspect what the page shows now — no arguments."),
    "input_schema": {"type": "object", "properties": {}},
}

browser_click_spec = {
    "name": "browser_click",
    "description": (
        "Click an element in the current page, wait for load, return the "
        "updated aria snapshot. Pair with browser_snapshot to find selectors."),
    "input_schema": {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": SELECTOR_DOC},
            **_COMMON_TIMEOUT,
        },
        "required": ["selector"],
    },
}

browser_type_spec = {
    "name": "browser_type",
    "description": (
        "Clear and type text into an input/textarea in the current page. "
        "Set submit=true to press Enter afterwards (e.g. search boxes, "
        "login forms)."),
    "input_schema": {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": SELECTOR_DOC},
            "text": {"type": "string", "description": "Text to type"},
            "submit": {"type": "boolean",
                       "description": "Press Enter after typing, default false"},
            **_COMMON_TIMEOUT,
        },
        "required": ["selector", "text"],
    },
}

browser_press_spec = {
    "name": "browser_press",
    "description": (
        "Press a keyboard key in the current page (e.g. 'Enter', 'Escape', "
        "'Tab'). Use for key-driven interactions not tied to an input."),
    "input_schema": {
        "type": "object",
        "properties": {
            "key": {"type": "string",
                    "description": "Key name like 'Enter', 'Escape', 'ArrowDown'"},
            **_COMMON_TIMEOUT,
        },
        "required": ["key"],
    },
}

browser_select_spec = {
    "name": "browser_select",
    "description": (
        "Select an option in a <select> dropdown in the current page "
        "(matches by value or label)."),
    "input_schema": {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": SELECTOR_DOC},
            "value": {"type": "string", "description": "Option value/label"},
            **_COMMON_TIMEOUT,
        },
        "required": ["selector", "value"],
    },
}

browser_screenshot_spec = {
    "name": "browser_screenshot",
    "description": (
        "Save a PNG screenshot of the current page. Returns the absolute "
        "file path (default under ~/.x-code/screenshots/). Set full_page=true "
        "to capture beyond the viewport."),
    "input_schema": {
        "type": "object",
        "properties": {
            "full_page": {"type": "boolean",
                          "description": "Full scrollable page, default false"},
            "path": {"type": "string",
                     "description": "Optional output file path (.png)"},
        },
    },
}

browser_console_spec = {
    "name": "browser_console",
    "description": (
        "Read console messages and uncaught JS errors (pageerror) collected "
        "since page open. Essential for web testing: check it after "
        "interactions to catch silent JS failures. Set clear=true to drain."),
    "input_schema": {
        "type": "object",
        "properties": {
            "clear": {"type": "boolean",
                      "description": "Clear messages after reading, default false"},
        },
    },
}

browser_close_spec = {
    "name": "browser_close",
    "description": (
        "Close the browser and release resources. Call when testing is done "
        "for this conversation."),
    "input_schema": {"type": "object", "properties": {}},
}

BROWSER_TOOL_SPECS = [
    browser_navigate_spec, browser_snapshot_spec, browser_click_spec,
    browser_type_spec, browser_press_spec, browser_select_spec,
    browser_screenshot_spec, browser_console_spec, browser_close_spec,
]

# worker（subagent）可用名: 与 SUBAGENT_TOOL_HANDLERS 保持同步
BROWSER_TOOL_NAMES = {s["name"] for s in BROWSER_TOOL_SPECS}


def register_browser_tools(registry):
    """把 9 个浏览器工具注册进 ToolRegistry（main.build_registry 调用）。"""
    return (registry
            .register(name="browser_navigate", handler=browser_navigate_tool)
            .register(name="browser_snapshot", handler=browser_snapshot_tool)
            .register(name="browser_click", handler=browser_click_tool)
            .register(name="browser_type", handler=browser_type_tool)
            .register(name="browser_press", handler=browser_press_tool)
            .register(name="browser_select", handler=browser_select_tool)
            .register(name="browser_screenshot", handler=browser_screenshot_tool)
            .register(name="browser_console", handler=browser_console_tool)
            .register(name="browser_close", handler=browser_close_tool))
