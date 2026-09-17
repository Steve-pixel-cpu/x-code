# --- x-code Web UI 后端 (FastAPI) ---
# 复用现有 agent 内核（runtime / api_client / permissions / storage），这里只做"皮":
# 把同步阻塞的 run_turn 丢进工作线程，内核事件经事件循环推回 WebSocket。
#
# 线程模型:
#   事件循环线程 —— WS 收发、REST、把队列里的 JSON 发给浏览器
#   工作线程     —— runtime.run_turn（同步阻塞），内部跑完整工具循环
# 跨线程通道:
#   emit(payload) 用 loop.call_soon_threadsafe 把事件塞进该连接的 asyncio.Queue，
#   sender 协程专职消费队列发送；权限审批回传走 queue.Queue（decide 阻塞等待）。
# 内核零改动挂点（三个代理，全部在事件发生处镜像一份给浏览器）:
#   _LiveClientProxy  — 包住 anthropic 客户端，SSE 流逐事件镜像（真流式正文）
#   EmittingToolRegistry — 工具执行完镜像 tool_result
#   WebPermissionPrompter — 权限询问转发成弹窗，阻塞等浏览器审批

import asyncio
import json
import os
import platform
import queue
import sys
import threading
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from api_client import ClaudeApiClient, THINKING_LEVELS
from config import ConfigLoader, RuntimeConfig
from main import (
    DEFAULT_MODEL,
    TOOLS,
    build_registry,
    build_runtime,
    repair_interrupted_turn,
    resolve_permission_mode,
    setup_console,
)
from models import (
    Message,
    Session,
    TextContentBlock,
    ToolContentBlock,
    ToolResultContentBlock,
)
from permissions import (
    ALLOW_MODE,
    MODE_TO_NAME,
    NAME_TO_MODE,
    PermissionDecision,
    PermissionMode,
    PermissionRequest,
    PermissionResult,
)
from prompt import SystemPromptBuilder
from storage import SessionStore
from tools import ToolRegistry

setup_console()  # Windows 控制台 UTF-8 兜底（服务器日志不乱码，与 CLI 同一入口）

# --- 启动检查: .env → API_KEY，缺失直接退出（配置错就别起服务） ---
load_dotenv()
API_KEY = os.getenv("API_KEY")
if API_KEY is None:
    print("✗ API_KEY not set! (检查 .env)")
    sys.exit(1)

# --- 与 CLI 同源的装配: 同一份存储、同一套工具、同一个默认模型 ---
STORAGE_DIR = Path.home() / ".x-code" / "sessions"
store = SessionStore(storage_dir=STORAGE_DIR)

runtime_config: RuntimeConfig = ConfigLoader(cwd=Path.cwd(), config_home=Path.home()).load()
system_prompt = (
    SystemPromptBuilder()
    .with_os(platform.system(), platform.release())
    .build()
)
api_client = ClaudeApiClient(
    api_key=str(API_KEY),
    model=runtime_config.model() or DEFAULT_MODEL,
    tools=TOOLS,
    emit_output=False,  # Web 模式不打印终端，事件改推给浏览器
    thinking_level=runtime_config.thinking_level(),
)

app = FastAPI(title="x-code web")
STATIC_DIR = Path(__file__).parent / "static"

PERMISSION_TIMEOUT = 120  # 权限审批等待秒数，超时朝安全侧自动 DENY
UNTITLED = "(未命名)"
_CANCEL_SENTINEL = "__cancelled__"


# ============================================================================
# 按线程路由事件: 内核挂点 → 当前会话的 emit
# ============================================================================

class TurnDispatch:
    """按线程号路由事件。

    工作线程开跑一轮前 bind(emit)，结束后 unbind()。内核侧三个挂点
    （SSE 流代理 / 工具注册表 / 权限桥）在事件发生时用 current() 拿到
    本线程绑定的 emit——多个会话各开各的线程，互不串线。
    """

    def __init__(self):
        self._emit_by_thread: dict[int, Callable] = {}
        self._lock = threading.Lock()

    def bind(self, emit: Callable) -> None:
        with self._lock:
            self._emit_by_thread[threading.get_ident()] = emit

    def unbind(self) -> None:
        with self._lock:
            self._emit_by_thread.pop(threading.get_ident(), None)

    def current(self) -> Optional[Callable]:
        with self._lock:
            return self._emit_by_thread.get(threading.get_ident())


dispatch = TurnDispatch()


class _LiveStreamProxy:
    """anthropic SSE 流代理: 事件原样透传给内核的同时镜像一份给浏览器。

    - text_delta 逐段转发（真流式打字效果）
    - tool_use 在 content_block_stop 时拼装完整事件转发（与内核同样的拼装规则）
    - thinking_delta 刻意不转发: UI 只显示"思考中"指示，不展示思考内容
    """

    def __init__(self, real, sink: Optional[Callable]):
        self._real = real
        self._sink = sink
        self._tools: dict[int, dict] = {}

    def __enter__(self):
        # 管理器的 __enter__ 才返回可迭代的流对象，必须存下来给 __next__ 用；
        # 直接在管理器上取下一个事件会报 'MessageStreamManager' has no __next__
        self._stream = self._real.__enter__()
        return self

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)

    def __iter__(self):
        return self

    def __next__(self):
        event = self._stream.__next__()
        if self._sink is not None:
            self._mirror(event)
        return event

    def _mirror(self, event) -> None:
        etype = getattr(event, "type", None)
        if etype == "content_block_start":
            cb = event.content_block
            if cb.type == "tool_use":
                self._tools[event.index] = {"id": cb.id, "name": cb.name, "json": ""}
        elif etype == "content_block_delta":
            delta = event.delta
            if delta.type == "text_delta":
                self._sink({"type": "text_delta", "text": delta.text})
            elif delta.type == "input_json_delta":
                info = self._tools.get(event.index)
                if info is not None:
                    info["json"] += delta.partial_json
        elif etype == "content_block_stop":
            info = self._tools.pop(event.index, None)
            if info is not None:
                self._sink({
                    "type": "tool_use",
                    "id": info["id"],
                    "name": info["name"],
                    "input": info["json"] or "{}",
                })


class _LiveMessagesProxy:
    """messages 入口代理: 每次开流时取当前线程绑定的 emit 作为镜像去向。"""

    def __init__(self, real_messages, dispatch_ref: TurnDispatch):
        self._real = real_messages
        self._dispatch = dispatch_ref

    def stream(self, **kwargs):
        return _LiveStreamProxy(self._real.stream(**kwargs), self._dispatch.current())


class _LiveClientProxy:
    """包住 anthropic 客户端: messages 换成代理，其余属性原样透传。"""

    def __init__(self, real):
        self._real = real
        self.messages = _LiveMessagesProxy(real.messages, dispatch)

    def __getattr__(self, name):
        return getattr(self._real, name)


api_client.client = _LiveClientProxy(api_client.client)


class EmittingToolRegistry(ToolRegistry):
    """委托真实 registry 执行；由工作线程调用时顺带把 tool_result 推给浏览器。

    被权限拒绝的工具到不了这里（runtime 直接生成 error result），不会产生假结果。
    """

    def __init__(self, inner: ToolRegistry):
        super().__init__()
        self._inner = inner

    def execute(self, name: str, tool_input_json: str) -> str:
        emit = dispatch.current()
        try:
            result = self._inner.execute(name, tool_input_json)
        except Exception as e:
            if emit:
                emit({"type": "tool_result", "name": name, "input": tool_input_json,
                      "output": str(e), "is_error": True})
            raise
        if emit:
            emit({"type": "tool_result", "name": name, "input": tool_input_json,
                  "output": result, "is_error": False})
        return result


registry = EmittingToolRegistry(build_registry())


# ============================================================================
# 事件出口包装 + 权限桥接
# ============================================================================

class TurnEmitter:
    """每轮事件出口: 线程安全转发 + 补齐 tool_use/tool_result 的配对 id。

    runtime 串行处理 tool_use（逐个授权→执行），tool_result 与 tool_use
    严格 FIFO 对应——开工具卡时记下 id，结果到达时弹出最老的一个补进去，
    前端就能按 id 精确配对卡片。
    """

    def __init__(self, sink: Callable):
        self._sink = sink
        self._pending_tool_ids: list[str] = []

    def __call__(self, payload: dict) -> None:
        ptype = payload.get("type")
        if ptype == "tool_use":
            if payload.get("id"):
                self._pending_tool_ids.append(payload["id"])
        elif ptype == "tool_result":
            payload["id"] = (
                self._pending_tool_ids.pop(0) if self._pending_tool_ids else None
            )
        self._sink(payload)


def _deny(tool_name: str, reason: str) -> PermissionResult:
    return PermissionResult(decision=PermissionDecision.DENY, reason=reason)


class WebPermissionPrompter:
    """阻塞式 prompter: decide() 在工作线程挂起等待，浏览器审批结果经 resolve() 送达。

    超时 / stale 响应 / 被打断（stop、断连）一律朝安全侧 DENY。
    每轮对话新建一个实例，同一时刻只有一个 decide 在等（runtime 串行处理 tool_use）。
    """

    def __init__(self, emit: Callable):
        self._emit = emit
        self._responses: "queue.Queue[tuple[str, bool]]" = queue.Queue()
        self._seq = 0
        self._cancelled = False

    def decide(self, request: PermissionRequest) -> PermissionResult:
        if self._cancelled:
            return self._finish_deny(request, "(用户已打断本轮，自动拒绝)")
        self._seq += 1
        request_id = f"perm-{self._seq}"
        # 先推弹窗再阻塞等待——顺序反了浏览器永远收不到弹窗
        self._emit({
            "type": "permission_request",
            "request_id": request_id,
            "tool_name": request.tool_name,
            "input": request.input,
            "current_mode": request.current_mode.as_str(),
            "required_mode": request.required_mode.as_str(),
        })
        deadline = time.monotonic() + PERMISSION_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._finish_deny(
                    request, f"审批超时（{PERMISSION_TIMEOUT}s），自动拒绝 {request.tool_name}")
            try:
                got_id, approved = self._responses.get(timeout=remaining)
            except queue.Empty:
                continue
            if got_id == _CANCEL_SENTINEL:
                return self._finish_deny(request, "(用户已打断本轮，自动拒绝)")
            if got_id != request_id:
                continue  # stale/重放的响应，忽略
            if approved:
                return PermissionResult(
                    decision=PermissionDecision.ALLOW, reason="user said yes!")
            return self._finish_deny(
                request, f"User denied permission to run {request.tool_name}!")

    def resolve(self, request_id: str, approved: bool) -> None:
        """事件循环侧: 浏览器的审批结果送达。"""
        self._responses.put((request_id, approved))

    def cancel(self) -> None:
        """打断等待（stop / 断连）: 立即解除 decide 阻塞并朝安全侧拒绝。"""
        self._cancelled = True
        self._responses.put((_CANCEL_SENTINEL, False))

    def _finish_deny(self, request: PermissionRequest, reason: str) -> PermissionResult:
        # 拒绝结果也推一条 tool_result，让前端工具卡片能闭合显示"已拒绝"
        self._emit({
            "type": "tool_result",
            "name": request.tool_name,
            "input": request.input,
            "output": reason,
            "is_error": True,
            "denied": True,
        })
        return _deny(request.tool_name, reason)


# ============================================================================
# 每连接会话状态 + runtime 装配
# ============================================================================

class WebSession:
    """一个会话的运行态。字段各归各线程读写，无需加锁:

    - 事件循环侧: prompter 登记、busy/stop 标记
    - 工作线程侧: runtime、persisted_count（本轮落盘起点）、last_uuid（落盘链尾）
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.runtime = None            # 懒构建: 首条消息时才组装（恢复历史）
        self.last_uuid: Optional[str] = None
        self.persisted_count = 0       # 已落盘的消息条数（本轮从这之后保存）
        self.busy = False              # 并发守卫: 一轮对话进行中
        self.stop_requested = False
        self.titled = store.get_title(session_id) is not None  # 自动命名一次
        self.prompter: Optional[WebPermissionPrompter] = None


_sessions: dict[str, WebSession] = {}


class AppState:
    """全局生效的设置（Web 顶栏）: 思考等级在 api_client 上，权限模式在这里。"""

    def __init__(self, mode: PermissionMode):
        self._mode = mode

    @property
    def permission_mode(self) -> PermissionMode:
        return self._mode

    def set_permission_mode(self, mode: PermissionMode) -> None:
        self._mode = mode


app_state = AppState(resolve_permission_mode(runtime_config))


def get_or_create_web_session(session_id: str) -> WebSession:
    if session_id not in _sessions:
        _sessions[session_id] = WebSession(session_id)
    return _sessions[session_id]


def load_runtime_for(web_session: WebSession) -> None:
    """组装 runtime: 有历史则先恢复（与 CLI 共享同一份存储）。"""
    if web_session.runtime is not None:
        return
    msgs, last_uuid = store.load_session(web_session.session_id)
    web_session.runtime = build_runtime(
        session=Session(messages=msgs),
        api_client=api_client,
        registry=registry,
        system_prompt=system_prompt,
        hooks_config=runtime_config,
        permission_mode=app_state.permission_mode,
    )
    web_session.last_uuid = last_uuid


# ============================================================================
# 落盘: 与 run_repl 一致 — 每轮结束后按 parent_uuid 链保存新增消息
# ============================================================================

def persist_turn(web_session: WebSession) -> None:
    messages = web_session.runtime.session().messages
    for msg in messages[web_session.persisted_count:]:
        web_session.last_uuid = store.save_message(
            session_id=web_session.session_id,
            message=msg,
            parent_uuid=web_session.last_uuid,
        )
    web_session.persisted_count = len(messages)


def maybe_auto_title(web_session: WebSession) -> None:
    """首轮对话成功后自动命名（与 main.derive_title 同规则: 前 30 字符）。"""
    if web_session.titled:
        return
    messages = web_session.runtime.session().messages
    if not messages or messages[0].role != "user":
        return
    first_text = "".join(
        b.text for b in messages[0].content if isinstance(b, TextContentBlock)
    )
    title = " ".join(first_text.split())[:30]
    if not title:
        return  # 全空白不命名
    store.set_title(web_session.session_id, title)
    web_session.titled = True


# ============================================================================
# 工作线程: 同步 run_turn + 事件桥接
# ============================================================================

def _start_turn(web_session: WebSession, text: str, emit: Callable) -> None:
    """开一轮对话: 占坑、准备事件出口、起工作线程。调用方已确认 !busy。"""
    web_session.busy = True
    web_session.stop_requested = False
    web_session.persisted_count = len(web_session.runtime.session().messages)
    emitter = TurnEmitter(emit)
    prompter = WebPermissionPrompter(emitter)
    web_session.prompter = prompter

    def worker():
        dispatch.bind(emitter)  # 必须在工作线程内绑定（按线程号路由）
        try:
            summary = web_session.runtime.run_turn(text, prompter)
        except Exception as e:
            # 异常中断（网络断 / API 报错）: 修补悬空 tool_use 后照常落盘
            # （朝安全侧，与 CLI Ctrl+C 同路径）
            repair_interrupted_turn(web_session.runtime.session())
            persist_turn(web_session)
            emitter({"type": "error", "message": str(e)})
            emitter({"type": "turn_done", "interrupted": True, "iterations": 0,
                     "budget_exhausted": False, "iterations_exhausted": False})
        else:
            persist_turn(web_session)
            maybe_auto_title(web_session)
            emitter({
                "type": "turn_done",
                "interrupted": web_session.stop_requested,
                "iterations": summary.iterations,
                "budget_exhausted": summary.budget_exhausted,
                "iterations_exhausted": summary.iterations_exhausted,
            })
        finally:
            dispatch.unbind()
            web_session.busy = False
            web_session.prompter = None

    threading.Thread(target=worker, name=f"turn-{web_session.session_id}", daemon=True).start()


def request_stop(web_session: WebSession) -> None:
    """stop / 断连: 朝安全侧叫停——解除权限等待，后续工具调用全部自动拒绝。

    说明: 内核的流式调用是同步阻塞的，无法从外部安全掐死线程，
    stop 对"正在路上的这一次 LLM 调用"不生效，在下一个决策点生效。
    """
    if not web_session.busy:
        return
    web_session.stop_requested = True
    if web_session.prompter is not None:
        web_session.prompter.cancel()


# ============================================================================
# REST: 会话列表 / 新建 / 历史回放
# ============================================================================

def _message_to_dict(msg: Message) -> dict:
    """历史回放: 按块类型摊平成前端易消费的形状（text / tool_use / tool_result）。"""
    blocks = []
    for b in msg.content:
        if isinstance(b, TextContentBlock):
            blocks.append({"type": "text", "text": b.text})
        elif isinstance(b, ToolContentBlock):
            blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        elif isinstance(b, ToolResultContentBlock):
            blocks.append({
                "type": "tool_result",
                "id": b.id,
                "name": b.name,
                "output": b.output,
                "is_error": bool(b.is_error),
            })
    return {"role": msg.role, "blocks": blocks}


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/sessions")
async def api_list_sessions():
    items = []
    for sid in store.list_sessions():
        items.append({
            "id": sid,
            "title": store.get_title(sid) or UNTITLED,
            "message_count": store.count_messages(sid),
        })
    items.reverse()  # 会话 id 是时间戳，字典序即时间序 → 最新在前
    return {"sessions": items}


@app.post("/api/sessions")
async def api_create_session():
    """新建会话: 与 CLI 相同的 %Y%m%d-%H%M%S 时间戳 id（UTC）。

    文件在首条消息落盘时才创建，与 CLI 行为一致。
    """
    existing = set(store.list_sessions())
    sid = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    while sid in existing:  # 同秒重建撞 id → 追加后缀区分
        sid += "w"
    return {"id": sid}


@app.get("/api/sessions/{session_id}/messages")
async def api_get_messages(session_id: str):
    if session_id not in set(store.list_sessions()):
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
    msgs, _ = store.load_session(session_id)
    return {"session_id": session_id, "messages": [_message_to_dict(m) for m in msgs]}


# ============================================================================
# REST: 设置（思考等级 + 权限模式）
# ============================================================================

@app.get("/api/settings")
async def api_get_settings():
    return {
        "thinking_level": api_client.thinking_level,
        "permission_mode": MODE_TO_NAME[app_state.permission_mode],
    }


@app.post("/api/settings")
async def api_post_settings(request: dict):
    thinking = request.get("thinking_level")
    if thinking is not None:
        level = str(thinking).strip().lower()
        if level not in THINKING_LEVELS:
            raise HTTPException(
                status_code=400,
                detail=f"未知思考等级: {thinking}（可选: {' | '.join(THINKING_LEVELS)}）",
            )
        api_client.set_thinking_level(level)  # 下一轮立即生效

    mode_name = request.get("permission_mode")
    if mode_name is not None:
        mode = NAME_TO_MODE.get(str(mode_name).strip().lower())
        if mode is None:
            raise HTTPException(
                status_code=400,
                detail=f"未知权限模式: {mode_name}（可选: {' | '.join(MODE_TO_NAME.values())}）",
            )
        app_state.set_permission_mode(mode)
        # 存活中的会话 runtime 也一并切换（与 CLI /mode 即时生效对齐）
        for web_session in _sessions.values():
            if web_session.runtime is not None:
                web_session.runtime.set_permission_mode(mode)

    return await api_get_settings()


# ============================================================================
# WebSocket: 双向通道（服务端推事件 + 浏览器回审批/停止）
# ============================================================================

@app.websocket("/ws/{session_id}")
async def ws_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()
    web_session = get_or_create_web_session(session_id)
    loop = asyncio.get_running_loop()
    out_queue: asyncio.Queue = asyncio.Queue()

    async def sender():
        # 专职发送协程: 收发分离，避免与 receive 循环交错写同一 socket
        while True:
            payload = await out_queue.get()
            await websocket.send_text(json.dumps(payload, ensure_ascii=False))

    sender_task = asyncio.create_task(sender())

    def emit(payload) -> None:
        """工作线程调用: 事件路由到本连接的发送队列（线程安全、非阻塞）。"""
        loop.call_soon_threadsafe(out_queue.put_nowait, payload)

    def emit_error(message: str) -> None:
        emit({"type": "error", "message": message})

    try:
        while True:
            try:
                raw = await websocket.receive_json()
            except WebSocketDisconnect:
                raise
            except ValueError:  # JSON 解析失败（WebSocketDisconnect 不是 ValueError）
                emit_error("消息不是合法 JSON")
                continue
            msg_type = raw.get("type")

            if msg_type == "user":
                text = str(raw.get("text") or "").strip()
                if not text:
                    continue
                if web_session.busy:
                    emit_error("本轮对话进行中，同一会话同一时刻只允许一轮")
                    continue
                try:
                    load_runtime_for(web_session)
                except Exception as e:
                    emit_error(f"会话加载失败: {e}")
                    continue
                _start_turn(web_session, text, emit)

            elif msg_type == "permission_response":
                prompter = web_session.prompter
                if prompter is None:
                    emit_error("当前没有待审批的请求")
                else:
                    prompter.resolve(str(raw.get("request_id")), bool(raw.get("approved")))

            elif msg_type == "stop":
                request_stop(web_session)

            else:
                emit_error(f"未知消息类型: {msg_type!r}（已知: user / permission_response / stop）")

    except WebSocketDisconnect:
        # 断连但一轮对话可能还在跑: 朝安全侧叫停；落盘由工作线程完成
        request_stop(web_session)
    finally:
        sender_task.cancel()
        with suppress(asyncio.CancelledError):
            await sender_task


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
