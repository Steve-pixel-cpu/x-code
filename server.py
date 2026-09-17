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

# 已创建但尚未落盘的会话 id: POST /api/sessions 只生成 id，首条消息落盘才建
# 文件（与 CLI 一致）。列表/历史接口必须认得它们，否则"新建会话"在侧栏
# 不出现、历史接口 404，前端渲染成空白。
_pending_sessions: set[str] = set()

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

MAX_CONCURRENT_TURNS = 4  # 全局并发上限: 同时跑的轮次超过这个数就排队
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
    同时绑定本轮的会话工作目录，工具执行时取 current_workdir()。
    """

    def __init__(self):
        self._emit_by_thread: dict[int, Callable] = {}
        self._workdir_by_thread: dict[int, Optional[str]] = {}
        self._lock = threading.Lock()

    def bind(self, emit: Callable, workdir: Optional[str] = None) -> None:
        with self._lock:
            self._emit_by_thread[threading.get_ident()] = emit
            self._workdir_by_thread[threading.get_ident()] = workdir

    def unbind(self) -> None:
        with self._lock:
            ident = threading.get_ident()
            self._emit_by_thread.pop(ident, None)
            self._workdir_by_thread.pop(ident, None)

    def current(self) -> Optional[Callable]:
        with self._lock:
            return self._emit_by_thread.get(threading.get_ident())

    def current_workdir(self) -> Optional[str]:
        with self._lock:
            return self._workdir_by_thread.get(threading.get_ident())


dispatch = TurnDispatch()


class _LiveStreamProxy:
    """anthropic SSE 流代理: 事件原样透传给内核的同时镜像一份给浏览器。

    - text_delta 逐段转发（真流式打字效果）
    - tool_use 在 content_block_stop 时拼装完整事件转发（与内核同样的拼装规则）
    - thinking 块只发起止信号（thinking_start / thinking_end + 耗时），
      思考内容本身（thinking_delta）仍不转发: UI 展示"思考 · 持续了X秒"行
    """

    def __init__(self, real, sink: Optional[Callable]):
        self._real = real
        self._sink = sink
        self._tools: dict[int, dict] = {}
        self._thinking: dict[int, float] = {}

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
            elif cb.type == "thinking":
                self._thinking[event.index] = time.monotonic()
                self._sink({"type": "thinking_start"})
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
            t0 = self._thinking.pop(event.index, None)
            if t0 is not None:
                self._sink({
                    "type": "thinking_end",
                    "duration_ms": int((time.monotonic() - t0) * 1000),
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


# 原生 messages 入口先留一份: AI 命名走它，不经过镜像代理（避免标题请求的事件串进对话流）
_real_messages = api_client.client.messages
api_client.client = _LiveClientProxy(api_client.client)


class EmittingToolRegistry(ToolRegistry):
    """委托真实 registry 执行；由工作线程调用时顺带把 tool_result 推给浏览器。

    被权限拒绝的工具到不了这里（runtime 直接生成 error result），不会产生假结果。
    执行时从 dispatch 取本轮绑定的会话工作目录注入工具（bash 的 cwd、
    读写文件的相对路径解析基点）。
    """

    def __init__(self, inner: ToolRegistry):
        super().__init__()
        self._inner = inner

    def execute(self, name: str, tool_input_json: str) -> str:
        emit = dispatch.current()
        try:
            result = self._inner.execute(
                name, tool_input_json, workdir=dispatch.current_workdir())
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
        # 不设超时地等待审批（用户明确要求取消 120s 自动拒绝）:
        # 只被 resolve() / cancel()（stop、断连）解除, 弹窗可见就一直等。
        while True:
            try:
                got_id, approved = self._responses.get()
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
        self.workdir = store.get_workdir(session_id)  # 会话工作目录（项目）
        # 会话级思考等级: 初值取全局默认; 切换只影响本会话（runtime 注入）
        self.thinking_level = runtime_config.thinking_level()
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
    web_session.runtime.set_thinking_level(web_session.thinking_level)
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


def _ai_title(first_text: str) -> Optional[str]:
    """让模型为对话起标题。失败/空结果返回 None，由调用方回退截断。"""
    prompt = (
        "请为下面这段用户与编程助手的对话拟一个简洁标题："
        "不超过 16 个字，概括主题，只输出标题本身，"
        "不要引号、句号或任何解释。\n\n用户消息：" + first_text[:500]
    )
    msg = _real_messages.create(
        model=api_client.model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    out = "".join(
        b.text for b in msg.content if getattr(b, "type", "") == "text"
    )
    title = " ".join(out.split()).strip("　\"'“”「」『』。.!！?？，,；;：:")
    return title[:30] or None


def maybe_auto_title(web_session: WebSession) -> bool:
    """首轮对话成功后自动命名: AI 总结标题，失败回退前 30 字符截断。

    返回是否设置了标题（供工作线程决定是否广播 session_renamed）。
    调用点在工作线程、turn_done 已发出之后——多花几秒不阻塞前端收尾。
    """
    if web_session.titled:
        return False
    messages = web_session.runtime.session().messages
    if not messages or messages[0].role != "user":
        return False
    first_text = " ".join(
        b.text for b in messages[0].content if isinstance(b, TextContentBlock)
    ).strip()
    if not first_text:
        return False
    title = None
    try:
        title = _ai_title(first_text)
    except Exception as e:
        print(f"⚠ AI 命名失败，回退截断标题: {e}")
    if not title:
        title = first_text[:30]
    store.set_title(web_session.session_id, title)
    web_session.titled = True
    return True


# ============================================================================
# 工作线程: 同步 run_turn + 事件桥接
# ============================================================================

_turn_slots = threading.BoundedSemaphore(MAX_CONCURRENT_TURNS)


def _start_turn(web_session: WebSession, text: str, emit: Callable) -> None:
    """开一轮对话: 占坑、准备事件出口、起工作线程。调用方已确认 !busy。

    并发上限: 信号量在事件循环线程 try_acquire——拿不到就把本轮标记为
    queued 后直接 return, 由一个专职协程等槽位再真正起线程。busy 在排队
    期就置位（会话仍不允许并发第二轮）, 队列等价于"每个会话自己的等待室"。
    """
    web_session.busy = True
    web_session.stop_requested = False
    web_session.persisted_count = len(web_session.runtime.session().messages)
    emitter = TurnEmitter(emit)
    prompter = WebPermissionPrompter(emitter)
    web_session.prompter = prompter

    if not _turn_slots.acquire(blocking=False):
        # 全局槽位已满: 前端显示排队中, 等有轮结束释放槽位后再起线程
        emit({"type": "turn_queued", "max_concurrent": MAX_CONCURRENT_TURNS})
        _queued_turns.append((web_session, text, emitter, prompter))
        return

    _spawn_turn_thread(web_session, text, emitter, prompter)


# (web_session, text, emitter, prompter) 三元组队列; 事件循环线程独占读写
_queued_turns: list = []


def _spawn_turn_thread(web_session: WebSession, text: str,
                       emitter: TurnEmitter, prompter: WebPermissionPrompter) -> None:
    """真正起工作线程跑一轮。槽位已由调用方持有。"""

    def worker():
        dispatch.bind(emitter, web_session.workdir)  # 必须在工作线程内绑定（按线程号路由）
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
            needs_title = not web_session.titled
            emitter({
                "type": "turn_done",
                "interrupted": web_session.stop_requested,
                "iterations": summary.iterations,
                "budget_exhausted": summary.budget_exhausted,
                "iterations_exhausted": summary.iterations_exhausted,
            })
            # AI 命名放在 turn_done 之后: 前端先收尾，标题好了再单独广播
            if needs_title and maybe_auto_title(web_session):
                emitter({
                    "type": "session_renamed",
                    "session_id": web_session.session_id,
                    "title": store.get_title(web_session.session_id),
                })
        finally:
            dispatch.unbind()
            web_session.busy = False
            web_session.prompter = None
            # 释放槽位并唤醒排队的会话（FIFO; 断连/停止的排队项被跳过）
            _turn_slots.release()
            _drain_queued_turns()

    threading.Thread(target=worker, name=f"turn-{web_session.session_id}", daemon=True).start()


def _drain_queued_turns() -> None:
    """槽位释放后按 FIFO 唤醒排队轮次。事件循环线程独占调用。

    排队期间被叫停（request_stop）或已断连的会话直接跳过: prompter.cancel
    已经把它的等待权限请求全部 DENY, 轮次起了也会立刻收束, 不如不起。
    """
    while _queued_turns and _turn_slots.acquire(blocking=False):
        web_session, text, emitter, prompter = _queued_turns.pop(0)
        if web_session.stop_requested or not web_session.busy:
            continue
        _spawn_turn_thread(web_session, text, emitter, prompter)


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
    on_disk = set(store.list_sessions())
    # 已落盘的会话由 store 覆盖，pending 里不再需要；未落盘的保持 pending
    _pending_sessions.difference_update(on_disk)
    items = [
        {
            "id": sid,
            "title": store.get_title(sid) or UNTITLED,
            "message_count": store.count_messages(sid),
            # 项目归属: 会话的工作目录(WorkdirRecord, 取最新一条); 未设置时 None
            "workdir": store.get_workdir(sid),
        }
        for sid in on_disk
    ]
    for sid in _pending_sessions:
        items.append({"id": sid, "title": UNTITLED, "message_count": 0,
                      "workdir": None})
    items.sort(key=lambda item: item["id"], reverse=True)  # 时间戳字典序即时间序，最新在前
    return {"sessions": items}


@app.post("/api/sessions")
async def api_create_session():
    """新建会话: 与 CLI 相同的 %Y%m%d-%H%M%S 时间戳 id（UTC）。

    文件在首条消息落盘时才创建，与 CLI 行为一致；id 记入 _pending_sessions，
    让列表/历史接口在落盘前就能认出它。
    """
    existing = set(store.list_sessions()) | _pending_sessions
    sid = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    while sid in existing:  # 同秒重建撞 id → 追加后缀区分
        sid += "w"
    _pending_sessions.add(sid)
    return {"id": sid}


@app.delete("/api/sessions/{session_id}")
async def api_delete_session(session_id: str):
    """删除会话: 删磁盘 JSONL + 清内存态。对话进行中的会话拒删。"""
    web_session = _sessions.get(session_id)
    if web_session is not None and web_session.busy:
        raise HTTPException(status_code=409, detail="会话正在对话中，暂不能删除")
    _pending_sessions.discard(session_id)
    _sessions.pop(session_id, None)
    try:
        store.delete_session(session_id)
    except KeyError:
        # 本就不存在（旧 pending 未落盘等）: 按幂等成功处理，内存态已清
        pass
    return {"ok": True}


@app.post("/api/sessions/{session_id}/rename")
async def api_rename_session(session_id: str, request: dict):
    """手动重命名: 追加一条 title 记录（展示取最新）。未落盘的会话不可重命名。"""
    title = str(request.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    title = title[:60]
    if session_id not in set(store.list_sessions()):
        raise HTTPException(status_code=404, detail="会话不存在（还没有消息，无法命名）")
    store.set_title(session_id, title)
    # 手动命名优先: 标记已命名，首轮 AI 命名不再覆盖它
    web_session = _sessions.get(session_id)
    if web_session is not None:
        web_session.titled = True
    return {"ok": True, "title": title}


@app.get("/api/dirs")
async def api_list_dirs(path: str = ""):
    """列出某目录下的子目录（前端工作目录选择器浏览用）。path 缺省为服务进程 cwd。"""
    target = Path(path) if path.strip() else Path.cwd()
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"目录不存在: {target}")
    try:
        target = target.resolve()
        dirs = sorted(
            (p.name for p in target.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=str.lower,
        )
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"无法读取目录: {e}")
    return {"path": str(target), "dirs": dirs}


@app.get("/api/sessions/{session_id}/messages")
async def api_get_messages(session_id: str):
    if session_id not in set(store.list_sessions()):
        # 新建后尚未落盘的会话: 返回空历史而不是 404——否则前端"新建会话"
        # 会走加载失败分支，渲染成空白
        return {"session_id": session_id, "messages": [],
                "workdir": store.get_workdir(session_id)}
    msgs, _ = store.load_session(session_id)
    return {"session_id": session_id, "messages": [_message_to_dict(m) for m in msgs],
            "workdir": store.get_workdir(session_id)}


# ============================================================================
# REST: 设置（思考等级 + 权限模式）
# ============================================================================

@app.get("/api/settings")
async def api_get_settings():
    return {
        # 思考等级已按会话隔离, 这里返回的是"新会话的默认值"
        "thinking_level": api_client.thinking_level,
        "permission_mode": MODE_TO_NAME[app_state.permission_mode],
        # 前端展示用: 输入栏的模型名 + 顶栏面包屑的工作区名
        "model": api_client.model,
        "workspace": Path.cwd().name,
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
        api_client.set_thinking_level(level)  # 新会话的默认值
        # 存活会话各自持有等级: runtime（本轮立即生效）+ WebSession（下轮注入）
        for web_session in _sessions.values():
            web_session.thinking_level = level
            if web_session.runtime is not None:
                web_session.runtime.set_thinking_level(level)

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
                # 首条消息可携带工作目录（项目的意义）: 只在未设置时落一次
                raw_wd = str(raw.get("workdir") or "").strip()
                if web_session.workdir is None and raw_wd:
                    wd = Path(raw_wd)
                    if not wd.is_dir():
                        emit_error(f"工作目录不存在: {raw_wd}")
                        continue
                    web_session.workdir = str(wd.resolve())
                    store.set_workdir(web_session.session_id, web_session.workdir)
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
