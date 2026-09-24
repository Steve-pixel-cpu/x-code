"""MCP (Model Context Protocol) 客户端: 把外部 MCP 服务器的工具接入 Agent 工具循环。

设计要点:

- 官方 mcp SDK 是 asyncio 的, 而 x-code 的工具循环/registry 是同步线程模型。
  每台服务器起一个 daemon 线程独占 event loop(常驻), 同步侧用
  run_coroutine_threadsafe(...).result(timeout) 桥接——不碰宿主循环,
  也不用 anyio from_thread(那要求双方在同一 anyio portal 里, 不现实)。

- 工具名加前缀 mcp__<server>__<tool>: 与内置工具永不冲突, 模型从名字
  就能看出来源; 同名工具在多服务器间也不打架。

- 连接失败 = 该服务器标记 failed + warning, 不挡启动。MCP 是增强能力,
  一台配错不应瘫痪整个 Agent。

- 权限/钩子/输出截断全部复用现有管线: handler 走标准 ToolRegistry 约定
  (params, workdir) -> str; 未在 TOOL_REQUIREMENTS 登记的工具走
  required_mode_for 的 DANGER fallback(plan 拒绝/其余模式审批), 对能力
  未知的外部工具这是正确的保守默认。
"""

import asyncio
import atexit
import contextlib
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from config import McpServerConfig
from runtime import ToolError

# 连接握手超时: stdio 要拉起子进程, http 要建连+initialize, 15s 是宽裕值
CONNECT_TIMEOUT_S = 15.0
# 工具描述进系统提示的 tools 数组, 无界描述是上下文炸弹
MAX_TOOL_DESC_CHARS = 1024

_SAFE_SEG = re.compile(r"[^A-Za-z0-9_-]")


def _safe_segment(name: str) -> str:
    """server/tool 名进工具名前只留安全字符——工具名会进 API 请求与
    registry 键, 不能带点/空格/中文以外的奇怪字节。"""
    return _SAFE_SEG.sub("_", name).strip("_") or "srv"


def mcp_tool_name(server: str, tool: str) -> str:
    return f"mcp__{_safe_segment(server)}__{_safe_segment(tool)}"


MCP_TOOL_NAME_RE = re.compile(r"^mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_-]+$")
MCP_TOOL_PREFIX = "mcp__"


@dataclass
class McpServerStatus:
    name: str
    transport: str
    status: str = "disconnected"   # disconnected / connected / failed
    error: Optional[str] = None
    tools: list[str] = field(default_factory=list)


class McpConnection:
    """一台 MCP 服务器的常驻连接。

    生命周期: start() 起后台 loop 线程并握手; 之后 list_tools/call_tool
    阻塞转发; close() 退出 transport 上下文(stdio 会收到杀子进程)再停 loop。
    """

    def __init__(self, server: McpServerConfig,
                 connect_timeout: float = CONNECT_TIMEOUT_S):
        self.server = server
        self._connect_timeout = connect_timeout
        self.status = McpServerStatus(name=server.name,
                                      transport=server.transport)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session: Any = None            # mcp ClientSession
        self._stack: Optional[contextlib.AsyncExitStack] = None
        self._tools: list[dict] = []         # SDK Tool 的 {name, description, input_schema}
        self._lifecycle_lock = threading.Lock()
        self._loop_ready = threading.Event()   # loop 线程就绪信号

    # ---- 同步侧 API -------------------------------------------------------

    def start(self) -> None:
        """握手并常驻。失败时抛 ToolError 且不留半连接(线程收尾)。"""
        with self._lifecycle_lock:
            if self._thread is not None:
                raise ToolError(f"MCP server '{self.server.name}' already started")
            self._loop_ready.clear()
            self._thread = threading.Thread(
                target=self._run_loop, name=f"mcp-{self.server.name}", daemon=True)
            self._thread.start()
            # 等线程把 loop 建好再投递协程——new_event_loop 与线程启动
            # 之间存在窗口, 直接投会撞上 _loop 为 None
            if not self._loop_ready.wait(timeout=10.0):
                self.status.status = "failed"
                self.status.error = "loop thread failed to start"
                raise ToolError(
                    f"MCP server '{self.server.name}': loop thread failed to start")
            try:
                # 内层 asyncio.timeout 负责精确取消; 外层稍长, 只防 loop
                # 本身卡死(理论不发生, 兜底不无限等)
                self._call(self._connect(), timeout=self._connect_timeout + 10.0)
            except BaseException as e:
                self.status.status = "failed"
                self.status.error = str(e)
                self._shutdown()
                raise ToolError(
                    f"MCP server '{self.server.name}' connect failed: {e}") from e
            self.status.status = "connected"
            self.status.tools = [t["name"] for t in self._tools]

    def list_tools(self) -> list[dict]:
        return list(self._tools)

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """远程调用并转成文本。isError=true 抛 ToolError——runtime 标记
        is_error 后模型把它当反馈读, 而不是当成又一次"成功"的结果。"""
        if self._session is None:
            raise ToolError(f"MCP server '{self.server.name}' is not connected")
        timeout = float(self.server.timeout)
        try:
            result = self._call(
                self._session.call_tool(tool_name, arguments or {}), timeout=timeout)
        except TimeoutError:
            raise ToolError(
                f"MCP tool '{tool_name}' timed out after {self.server.timeout}s")
        except Exception as e:
            raise ToolError(f"MCP tool '{tool_name}' failed: {e}")
        text = self._render_result(result)
        if getattr(result, "is_error", False):
            raise ToolError(f"MCP tool '{tool_name}' returned an error: {text}")
        return text

    def close(self) -> None:
        with self._lifecycle_lock:
            self._shutdown()

    # ---- asyncio 侧 -------------------------------------------------------

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        self._loop.run_forever()
        self._loop.close()

    def _call(self, coro, timeout: float):
        """协程投递到连接线程的 loop 并阻塞等结果。
        asyncio.timeout 在 loop 内取消, AsyncExitStack 正常回滚。"""
        import concurrent.futures as cf
        if self._loop is None or self._loop.is_closed():
            raise ToolError(f"MCP server '{self.server.name}' loop is not running")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout)
        except cf.TimeoutError:
            fut.cancel()
            raise

    async def _connect(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async with asyncio.timeout(self._connect_timeout):
            stack = contextlib.AsyncExitStack()
            try:
                if self.server.transport == "stdio":
                    params = StdioServerParameters(
                        command=self.server.command or "",
                        args=self.server.args,
                        env=self.server.env or None,
                        cwd=self.server.cwd,
                    )
                    streams = await stack.enter_async_context(stdio_client(params))
                else:
                    streams = await stack.enter_async_context(
                        _http_streams(self.server))
                read_stream, write_stream = streams[0], streams[1]
                session = await stack.enter_async_context(
                    ClientSession(read_stream, write_stream,
                                  read_timeout_seconds=self.server.timeout))
                await session.initialize()
            except BaseException:
                await stack.aclose()
                raise
            self._stack = stack
            self._session = session
            self._tools = await self._list_tools()

    async def _list_tools(self) -> list[dict]:
        """带 cursor 翻页拉全量工具。"""
        out: list[dict] = []
        cursor = None
        for _ in range(50):   # 翻页硬上限: 防异常服务器死循环
            params = {"cursor": cursor} if cursor else None
            res = await self._session.list_tools(params=params)
            for t in res.tools:
                desc = t.description or ""
                out.append({
                    "name": t.name,
                    "description": desc[:MAX_TOOL_DESC_CHARS],
                    "input_schema": (t.input_schema
                                     and dict(t.input_schema)) or {
                        "type": "object", "properties": {}},
                })
            cursor = getattr(res, "nextCursor", None)
            if not cursor:
                break
        return out

    @staticmethod
    def _render_result(result: Any) -> str:
        """CallToolResult.content → 文本。多模态块记占位符——历史里要能
        看出"这里有东西", 但不塞 base64 炸上下文。"""
        parts: list[str] = []
        for c in (getattr(result, "content", None) or []):
            ctype = getattr(c, "type", "")
            if ctype == "text":
                parts.append(getattr(c, "text", ""))
            elif ctype == "image":
                parts.append(f"[image: {getattr(c, 'mime_type', '?')}]")
            elif ctype == "audio":
                parts.append(f"[audio: {getattr(c, 'mime_type', '?')}]")
            elif ctype == "resource":
                res = getattr(c, "resource", None)
                uri = getattr(res, "uri", "?") if res is not None else "?"
                parts.append(f"[resource: {uri}]")
            else:
                parts.append(f"[{ctype or 'unknown'} content]")
        return "\n".join(p for p in parts if p) or "(empty result)"

    def _shutdown(self) -> None:
        """退出 transport 栈 → 停 loop → 收线程。幂等, 任意半初始化态可调。"""
        session, stack, loop = self._session, self._stack, self._loop
        self._session, self._stack = None, None
        if stack is not None and loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(stack.aclose(), loop
                                                 ).result(timeout=5.0)
            except Exception:
                pass   # 收尾失败不遮蔽主流程; daemon 线程随进程退出
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5.0)
        self._thread = None
        self._loop = None
        if self.status.status == "connected":
            self.status.status = "disconnected"


async def _http_streams(server: McpServerConfig):
    """http/sse 传输的流构造。两个客户端上下文签名不同, 但都产出
    (read, write[, extra]) 三元组, 取前两个即可。
    http 的自定义 headers 经 create_mcp_http_client 注入(2.x 的
    streamable_http_client 只收预构建的 httpx client)。"""
    if server.transport == "http":
        from mcp.client.streamable_http import (create_mcp_http_client,
                                                streamable_http_client)
        client = create_mcp_http_client(headers=server.headers or None)
        return streamable_http_client(server.url or "", http_client=client)
    from mcp.client.sse import sse_client
    return sse_client(server.url or "", headers=server.headers or None)


class McpManager:
    """全部 MCP 连接的持有者: 连接、工具规格、handler 解析、热重载。"""

    def __init__(self):
        self._conns: dict[str, McpConnection] = {}   # server name → connection
        self._lock = threading.Lock()

    def connect_all(self, servers: list[McpServerConfig],
                    connect_timeout: float = CONNECT_TIMEOUT_S) -> list[McpServerStatus]:
        """并行连接所有服务器; 失败记入 status 不抛——MCP 是增强能力。"""
        with self._lock:
            # 全量替换语义: 重载时旧的先关
            for old in self._conns.values():
                try:
                    old.close()
                except Exception:
                    pass
            self._conns.clear()
            conns = [McpConnection(s, connect_timeout) for s in servers]
            for c in conns:
                self._conns[c.server.name] = c

        def _start(c: McpConnection):
            try:
                c.start()
            except ToolError:
                pass   # 状态已记 failed

        if conns:
            with ThreadPoolExecutor(max_workers=len(conns)) as pool:
                list(pool.map(_start, conns))
        return [c.status for c in conns]

    def connected(self) -> list[McpConnection]:
        with self._lock:
            return [c for c in self._conns.values() if c.status.status == "connected"]

    def tool_specs(self) -> list[dict]:
        """全部已连接服务器的工具 → API tools 数组条目(name 已加前缀)。"""
        specs: list[dict] = []
        for conn in self.connected():
            server_seg = _safe_segment(conn.server.name)
            for t in conn.list_tools():
                specs.append({
                    "name": mcp_tool_name(conn.server.name, t["name"]),
                    "description": (
                        f"[MCP:{server_seg}] {t['description']}".strip()),
                    "input_schema": t["input_schema"] or {
                        "type": "object", "properties": {}},
                })
        return specs

    def handler_for(self, prefixed_name: str) -> Optional[Callable]:
        """工具名 → registry 兼容 handler (params, workdir) -> str。"""
        for conn in self.connected():
            server_seg = _safe_segment(conn.server.name)
            prefix = f"mcp__{server_seg}__"
            if prefixed_name.startswith(prefix):
                remote = prefixed_name[len(prefix):]
                for t in conn.list_tools():
                    if _safe_segment(t["name"]) == remote:
                        return self._make_handler(conn, t["name"])
        return None

    @staticmethod
    def _make_handler(conn: McpConnection, remote_name: str) -> Callable:
        def handler(params: dict, workdir: Optional[str] = None) -> str:
            return conn.call_tool(remote_name, params)
        handler.__name__ = f"mcp_{remote_name}"
        return handler

    def sync_tools_list(self, tools: list[dict]) -> list[dict]:
        """把 MCP 工具规格原地并入 tools 列表(先清掉旧 mcp__ 项再追加)。
        api_client / multi_agent 都持有同一列表对象, 原地修改即全链路生效。"""
        kept = [t for t in tools if not t.get("name", "").startswith(MCP_TOOL_PREFIX)]
        tools[:] = kept + self.tool_specs()
        return tools

    def status(self) -> list[dict]:
        with self._lock:
            conns = list(self._conns.values())
        return [{
            "name": c.status.name,
            "transport": c.status.transport,
            "status": c.status.status,
            "error": c.status.error,
            "tools": c.status.tools,
        } for c in conns]

    def disconnect_all(self) -> None:
        with self._lock:
            conns = list(self._conns.values())
            self._conns.clear()
        for c in conns:
            try:
                c.close()
            except Exception:
                pass


_manager: Optional[McpManager] = None
_manager_lock = threading.Lock()


def get_mcp_manager() -> McpManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = McpManager()
            atexit.register(_manager.disconnect_all)
        return _manager
