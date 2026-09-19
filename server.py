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
import base64
import json
import os
import platform
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from api_client import ClaudeApiClient, THINKING_LEVELS
import anthropic
from config import USER_DIR, SETTINGS_FILE, ConfigLoader, RuntimeConfig, load_providers, save_providers
from main import (
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
    ImageContentBlock,
    FileContentBlock,
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
from tools import ToolRegistry, git_bash_unavailable_reason
from multi_agent import set_api_config_provider
from agent_tools import get_orchestrator

setup_console()  # Windows 控制台 UTF-8 兜底（服务器日志不乱码，与 CLI 同一入口）

# --- 配置来源: 只有 ~/.x-code/settings.json 的 providers/activeProvider
#     （设置页/初始化页写入, 读写逻辑在 config.py）。
#     没有 .env 兜底——未配置时 api_key 为空串照常起服务,
#     前端检测到(/api/settings.configured=false)会弹初始化页引导填写 ---

# --- 与 CLI 同源的装配: 同一份存储、同一套工具、同一个默认模型 ---
STORAGE_DIR = USER_DIR / "sessions"
store = SessionStore(storage_dir=STORAGE_DIR)

# 已创建但尚未落盘的会话 id: POST /api/sessions 只生成 id，首条消息落盘才建
# 文件（与 CLI 一致）。列表/历史接口必须认得它们，否则"新建会话"在侧栏
# 不出现、历史接口 404，前端渲染成空白。
_pending_sessions: set[str] = set()

runtime_config: RuntimeConfig = ConfigLoader(
    cwd=Path.cwd(), config_home=USER_DIR   # x-code 自己的用户配置目录
).load()

# --- 连接门禁: 桌面壳与后端共享 ~/.x-code/token 里的随机令牌 ---
# 所有请求必须携带 x-xcode-token 头 / cookie / query 之一, 否则 403 拒绝——
# 浏览器直接访问 127.0.0.1:8000 因此被挡在门外, 只有桌面壳能进来
_TOKEN_FILE = USER_DIR / "token"


def _ensure_api_token() -> str:
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        t = _TOKEN_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    except OSError:
        pass
    t = secrets.token_hex(32)
    _TOKEN_FILE.write_text(t, encoding="utf-8")
    return t


API_TOKEN = _ensure_api_token()
system_prompt = (
    SystemPromptBuilder()
    .with_os(platform.system(), platform.release())
    .build()
)
def _mirror_rate_limit_retry(attempt: int, max_retries: int,
                             delay_s: float, error) -> None:
    """限流退避镜像: 长退避期间告知前端"还活着、正在重试", 不再静默卡住。
    dispatch 在模块后段才定义, 回调运行于 turn 工作线程, 取到时必然已就绪;
    取不到出口（CLI/无连接）静默。仅 429 触发——其余错误的短退避
    （<1s）不值得打扰界面。"""
    if getattr(error, "status_code", None) != 429:
        return
    sink = dispatch.current()
    if sink is not None:
        sink({"type": "rate_limited_retry", "attempt": attempt,
              "max_retries": max_retries, "delay_s": round(delay_s, 1)})


api_client = ClaudeApiClient(
    api_key="",   # 未配置时为空串: 服务照常起, 由初始化页引导填写
    model=runtime_config.model() or "",   # 不设默认模型: 由用户显式添加
    tools=TOOLS,
    emit_output=False,  # Web 模式不打印终端，事件改推给浏览器
    thinking_level=runtime_config.thinking_level(),
    on_retry=_mirror_rate_limit_retry,
)

app = FastAPI(title="x-code web")


@app.middleware("http")
async def _token_gate(request: Request, call_next):
    """连接门禁: 缺少有效令牌的请求一律 403（API_TOKEN 为空 = 门禁关闭, 供测试）。"""
    if API_TOKEN:
        provided = (request.headers.get("x-xcode-token")
                    or request.cookies.get("xcode_token")
                    or request.query_params.get("token"))
        if provided != API_TOKEN:
            return JSONResponse(
                status_code=403,
                content={"detail": "请通过 x-code 桌面应用打开"},
            )
    return await call_next(request)
STATIC_DIR = Path(__file__).parent / "static"
# 静态资源 (app.css / app.js): index.html 拆分后由这里托管
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def no_cache_shell(request: Request, call_next):
    """页面壳与静态资源禁缓存: 前端迭代频繁, 保证刷新即最新（ETag 未变时仍 304）。"""
    response = await call_next(request)
    p = request.url.path
    if p == "/" or p.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache"
    return response

MAX_CONCURRENT_TURNS = 4  # 全局并发上限: 同时跑的轮次超过这个数就排队
UNTITLED = "(未命名)"
_CANCEL_SENTINEL = "__cancelled__"


# ============================================================================
# 按线程路由事件: 内核挂点 → 当前会话的 emit
# ============================================================================

@dataclass
class _TurnBinding:
    """一轮对话挂在工作线程上的绑定四件套。"""
    emit: Callable
    workdir: Optional[str] = None
    should_stop: Optional[Callable[[], bool]] = None
    session_id: Optional[str] = None


_binding_var: ContextVar[Optional[_TurnBinding]] = ContextVar(
    "xcode_turn_binding", default=None)


class TurnDispatch:
    """按上下文路由事件。

    工作线程开跑一轮前 bind(emit)，结束后 unbind()。内核侧三个挂点
    （SSE 流代理 / 工具注册表 / 权限桥）在事件发生时用 current() 拿到
    本轮绑定的 emit——多个会话各开各的线程，互不串线。
    同时绑定本轮的会话工作目录，工具执行时取 current_workdir()；
    绑定 should_stop 勾子，流式代理逐事件检查以支持即时打断。

    旧实现按线程号存 emit——runtime 串行执行工具时成立；工具并行执行
    进线程池后，挂点可能运行在池线程上, 线程号字典查不到绑定, tool_result
    会被静默吞掉。改用 contextvars：runtime 提交并行任务时带 copy_context()
    快照, 池内线程读到本轮的绑定; turn 工作线程各设各的上下文, 并发轮次
    依然互不串线。
    """

    def bind(self, emit: Callable, workdir: Optional[str] = None,
             should_stop: Optional[Callable[[], bool]] = None,
             session_id: Optional[str] = None) -> None:
        _binding_var.set(_TurnBinding(emit=emit, workdir=workdir,
                                      should_stop=should_stop,
                                      session_id=session_id))

    def unbind(self) -> None:
        _binding_var.set(None)

    def current(self) -> Optional[Callable]:
        binding = _binding_var.get()
        return binding.emit if binding else None

    def current_workdir(self) -> Optional[str]:
        binding = _binding_var.get()
        return binding.workdir if binding else None

    def current_should_stop(self) -> Optional[Callable[[], bool]]:
        binding = _binding_var.get()
        return binding.should_stop if binding else None

    def current_session_id(self) -> Optional[str]:
        binding = _binding_var.get()
        return binding.session_id if binding else None


dispatch = TurnDispatch()


class TurnInterrupted(Exception):
    """用户主动打断: 流式代理在下一个 SSE 事件到达时抛出, 快速收束本轮。"""


class _LiveStreamProxy:
    """anthropic SSE 流代理: 事件原样透传给内核的同时镜像一份给浏览器。

    - text_delta 逐段转发（真流式打字效果）
    - tool_use 在 content_block_stop 时拼装完整事件转发（与内核同样的拼装规则）
    - thinking 块只发起止信号（thinking_start / thinking_end + 耗时），
      思考内容本身（thinking_delta）仍不转发: UI 展示"思考 · 持续了X秒"行
    - 每取一个事件前检查 should_stop: 用户点停止后, 下一个事件到达即刻
      抛 TurnInterrupted 断流, 不必等模型说完（内核同步阻塞, 这是唯一
      能从外部安全掐断的位置）
    """

    def __init__(self, real, sink: Optional[Callable],
                 should_stop: Optional[Callable[[], bool]] = None):
        self._real = real
        self._sink = sink
        self._should_stop = should_stop
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
        if self._should_stop is not None and self._should_stop():
            raise TurnInterrupted()
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
        sink = self._dispatch.current()
        # 每次向模型发起调用就广播一次: 工具跑完到下一个 token 之间有一段
        # prefill 空窗, 界面全静会像已经结束——前端据此显示等待转圈
        if sink is not None:
            sink({"type": "await_output"})
        return _LiveStreamProxy(self._real.stream(**kwargs), sink,
                                self._dispatch.current_should_stop())


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


# ============================================================================
# 模型供应商配置: 读写归口 config.py（~/.x-code/settings.json 的
# providers / activeProvider 两个 key）, 这里只保留运行态副本
# ============================================================================


def _provider_ready(cfg: dict) -> bool:
    """active 指向的供应商是否可用（启用 + 有接口地址 + 有 key）, 即是否已初始化。
    模型不作为就绪条件: 由用户在「设置 → 模型」里显式添加, 不设默认值。"""
    active = cfg.get("active") or {}
    prov = next((p for p in cfg.get("providers", [])
                 if p.get("id") == active.get("provider")), None)
    return bool(prov and prov.get("enabled") and prov.get("api_key")
                and str(prov.get("base_url") or "").strip())


def _apply_provider_config(cfg: dict) -> None:
    """把 active 指向的启用供应商应用到 api_client, 并重挂镜像代理与 _real_messages。
    active 缺失/指向不存在或被禁用的供应商时: 保持未配置空 key（初始化页接管）。
    active 无 model: 只应用连接信息, 模型留待用户显式添加。"""
    global _real_messages
    if _provider_ready(cfg):
        active = cfg.get("active") or {}
        prov = next((p for p in cfg.get("providers", [])
                     if p.get("id") == active.get("provider")), None)
        api_client.configure(
            base_url=prov.get("base_url") or None,
            api_key=prov.get("api_key"),
            model=active.get("model"),   # None/缺失 = 保持当前模型
        )
    else:
        api_client.reset_to(api_key="", model="", base_url=None)
    api_client.client = _LiveClientProxy(api_client.raw_client)
    _real_messages = api_client.raw_client.messages
    # subagent worker 跟随同一份供应商配置: 每次 spawn 时实时读 api_client
    # 的连接信息（apply 在运行期可反复发生, 工厂闭包引用而非快照）
    set_api_config_provider(_subagent_api_config)


def _subagent_api_config() -> tuple[str, Optional[str], str]:
    """multi_agent worker 工厂: 直接镜像 Leader 的 api_client 连接信息。"""
    return api_client.api_key, api_client.base_url, api_client.model


_provider_cfg = load_providers()
_apply_provider_config(_provider_cfg)


def _reconcile_orphan_agents() -> None:
    """启动对账: 上次进程死亡遗留的 running 孤儿标记为 failed。

    挂 startup 事件而非 import 时执行: 测试的 TestClient 不进 lifespan,
    跑测试不会动真实的 agents 目录。"""
    n = get_orchestrator().reconcile_orphans()
    if n:
        print(f"[x-code] 启动对账: {n} 个上次进程遗留的 running agent 已标记为 failed")


app.router.add_event_handler("startup", _reconcile_orphan_agents)


class EmittingToolRegistry(ToolRegistry):
    """委托真实 registry 执行；执行完把 tool_result 推给浏览器。

    被权限拒绝的工具到不了这里（runtime 直接生成 error result），不会产生假结果。
    执行时从 dispatch 取本轮绑定的会话工作目录注入工具（bash 的 cwd、
    读写文件的相对路径解析基点）。并行执行时本方法跑在池线程上, 依靠
    runtime 提交任务时的 contextvars 快照拿到本轮绑定。
    """

    def __init__(self, inner: ToolRegistry):
        super().__init__()
        self._inner = inner

    def execute(self, name: str, tool_input_json: str,
                tool_use_id: Optional[str] = None) -> str:
        emit = dispatch.current()
        try:
            result = self._inner.execute(
                name, tool_input_json, workdir=dispatch.current_workdir())
        except Exception as e:
            if emit:
                emit({"type": "tool_result", "id": tool_use_id, "name": name,
                      "input": tool_input_json, "output": str(e), "is_error": True})
            raise
        if emit:
            emit({"type": "tool_result", "id": tool_use_id, "name": name,
                  "input": tool_input_json, "output": result, "is_error": False})
        return result


registry = EmittingToolRegistry(build_registry())


# ============================================================================
# 事件出口包装 + 权限桥接
# ============================================================================

class TurnEmitter:
    """每轮事件出口: 线程安全转发 + 补齐 tool_use/tool_result 的配对 id。

    runtime 直传 tool_use_id 时（并行执行后结果按完成序到达, FIFO 不可靠）
    按显式 id 配对并从待配队列摘除; 旧式无 id 的事件（权限拒绝路径）退回
    FIFO——弹出最老的一个补进去, 前端按 id 精确配对卡片。
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
            if payload.get("id"):
                # 并行下结果乱序到达: 按真实 id 摘除, 不按到达序猜
                if payload["id"] in self._pending_tool_ids:
                    self._pending_tool_ids.remove(payload["id"])
            else:
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
        # 排队区: 本轮进行中用户追加的后续消息（事件循环线程读写）,
        # 当前轮结束后由 _start_pending_turn 接力开跑。
        # 项为 {qid, text, attachments}——qid 由前端生成、随消息透传,
        # 排队操作（立即/删除/接力开跑）都按 qid 配对, 不再按文本匹配
        self.pending: list[dict] = []
        # 事件出口集合: 同一会话可能被多个窗口/标签打开, 事件广播给所有连接,
        # 连接断开时自动移除（key 为连接序号）
        self.emits: dict[int, Callable] = {}
        self._conn_seq = 0
        self.loop = None                       # 事件循环（worker 用它调度接力）

    def add_emit(self, emit: Callable) -> int:
        self._conn_seq += 1
        self.emits[self._conn_seq] = emit
        return self._conn_seq

    def remove_emit(self, token: int) -> None:
        self.emits.pop(token, None)

    def broadcast(self, payload: dict) -> None:
        for em in list(self.emits.values()):
            try:
                em(payload)
            except Exception:
                pass


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
    # 增量落盘: 历史一致点即写盘, 输出中强杀/崩溃最多丢最后一次一致点
    # 之后的内容, 不再是整轮。压缩会重写内存历史使追加式存储失准,
    # 此时原子重写会话文件让磁盘与内存重新对齐。
    web_session.runtime.set_on_iterate(lambda: persist_turn(web_session))
    web_session.runtime.set_on_compacted(
        lambda: _rewrite_after_compact(web_session))


def _rewrite_after_compact(web_session: WebSession) -> None:
    messages = web_session.runtime.session().messages
    count, last_uuid = store.rewrite_session(web_session.session_id, messages)
    web_session.persisted_count = count
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
# 附件（图片 / 文本文件）校验
# ============================================================================

# 图片: 白名单 media_type + 张数/单张/总量上限; 文本附件: 个数/单内容上限
ATTACH_IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
MAX_IMAGES = 8
MAX_IMAGE_B64_BYTES = 5 * 1024 * 1024      # 单张 base64 ≤5MB
MAX_TOTAL_ATTACH_BYTES = 20 * 1024 * 1024  # 全部附件总量 ≤20MB
MAX_FILES = 8
MAX_FILE_TEXT_BYTES = 512 * 1024           # 单个文本附件内容 ≤512KB


def _parse_attachments(raw) -> tuple[Optional[list[dict]], Optional[str]]:
    """校验 WS user 消息里的 attachments 数组, 规整成可入库的形状。

    返回 (attachments, None) = 合法（可能是空列表）; (None, 错误信息) = 超限,
    错误信息经现有 error 事件发给前端。只做形状与限额校验, 不解压图片、
    不识别内容——传输信任前端压缩结果, 视觉理解归服务端模型。
    """
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return None, "attachments 必须是数组"
    images: list[dict] = []
    files: list[dict] = []
    total_bytes = 0
    for att in raw:
        if not isinstance(att, dict):
            return None, "attachments 项必须是对象"
        kind = att.get("kind")
        if kind == "image":
            name = str(att.get("name") or "").strip()
            media_type = str(att.get("media_type") or "").strip().lower()
            data = att.get("data")
            if media_type not in ATTACH_IMAGE_TYPES:
                return None, f"不支持的图片类型: {media_type or '(缺失)'}（仅支持 png/jpeg/webp/gif）"
            if not isinstance(data, str) or not data:
                return None, f"图片 {name or media_type} 缺少 data"
            b64_len = len(data)
            total_bytes += b64_len
            if b64_len > MAX_IMAGE_B64_BYTES:
                return None, f"图片 {name or media_type} 过大（base64 超 5MB）"
            images.append({"kind": "image", "name": name,
                           "media_type": media_type, "data": data})
            if len(images) > MAX_IMAGES:
                return None, f"图片最多 {MAX_IMAGES} 张"
        elif kind == "file":
            name = str(att.get("name") or "").strip()
            text = att.get("text")
            if not isinstance(text, str):
                return None, f"文本附件 {name or '(未命名)'} 缺少 text"
            total_bytes += len(text.encode("utf-8", "replace"))
            if len(text) > MAX_FILE_TEXT_BYTES:
                return None, f"文本附件 {name or '(未命名)'} 过大（内容超 512KB）"
            files.append({"kind": "file", "name": name, "text": text})
            if len(files) > MAX_FILES:
                return None, f"文本附件最多 {MAX_FILES} 个"
        else:
            return None, f"未知附件类型: {kind!r}"
    if total_bytes > MAX_TOTAL_ATTACH_BYTES:
        return None, "附件总量超过 20MB"
    return images + files, None


# ============================================================================
# 工作线程: 同步 run_turn + 事件桥接
# ============================================================================

_turn_slots = threading.BoundedSemaphore(MAX_CONCURRENT_TURNS)


def _start_turn(web_session: WebSession, text: str, emit: Callable,
                attachments: Optional[list[dict]] = None) -> None:
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
        _queued_turns.append((web_session, text, attachments, emitter, prompter))
        return

    _spawn_turn_thread(web_session, text, attachments, emitter, prompter)


# (web_session, text, attachments, emitter, prompter) 五元组队列;
# 事件循环线程独占读写
_queued_turns: list = []


def _spawn_turn_thread(web_session: WebSession, text: str,
                       attachments: Optional[list[dict]],
                       emitter: TurnEmitter, prompter: WebPermissionPrompter) -> None:
    """真正起工作线程跑一轮。槽位已由调用方持有。"""

    def worker():
        # 必须在工作线程内绑定（按线程号路由）; should_stop 让流式代理逐事件检查打断;
        # session_id 给 agent 工具做收割的会话隔离（A 会话不收 B 会话的结果）
        dispatch.bind(emitter, web_session.workdir,
                      lambda: web_session.stop_requested,
                      session_id=web_session.session_id)
        try:
            summary = web_session.runtime.run_turn(text, prompter,
                                                   attachments=attachments)
        except TurnInterrupted:
            # 用户主动打断: 修补悬空 tool_use 后照常落盘（朝安全侧, 与 CLI Ctrl+C 同路径）
            repair_interrupted_turn(web_session.runtime.session())
            persist_turn(web_session)
            # 插队与手动停止同语义: 被打断的任务就地收束, 不自动续跑
            # （被打断的进度留在历史里, 是否继续由用户下一次消息决定）
            emitter({"type": "turn_done", "interrupted": True, "iterations": 0,
                     "budget_exhausted": False, "iterations_exhausted": False})
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
            # AI 命名放在 turn_done 之后: 前端先收尾，标题好了再单独广播。
            # 排队区非空 = 用户正在连续驱动: 跳过命名请求, 避免与接力的下一轮
            # 撞同一账户限流窗口（本次拿不到 AI 标题, 截断回退仍在, UI 无感）
            if needs_title and not web_session.pending and maybe_auto_title(web_session):
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
            # 本会话还有排队的后续消息: 回事件循环线程接力开跑下一轮
            if (web_session.pending and web_session.emits
                    and web_session.loop is not None):
                web_session.loop.call_soon_threadsafe(_start_pending_turn, web_session)

    threading.Thread(target=worker, name=f"turn-{web_session.session_id}", daemon=True).start()


def _drain_queued_turns() -> None:
    """槽位释放后按 FIFO 唤醒排队轮次。事件循环线程独占调用。

    排队期间被叫停（request_stop / 立即发送插队）或已断连的会话直接跳过:
    prompter.cancel 已经把它的等待权限请求全部 DENY, 轮次起了也会立刻
    收束, 不如不起。跳过时给会话收尾——排队区还有消息（如插队消息）就归队
    被跳过的轮次文本并调度接力开跑; 否则复位忙碌并补发 turn_done,
    不然前端永远停在忙碌态。
    """
    while _queued_turns and _turn_slots.acquire(blocking=False):
        web_session, text, attachments, emitter, prompter = _queued_turns.pop(0)
        if web_session.stop_requested or not web_session.busy:
            if web_session.busy:
                if web_session.pending:
                    # 被跳过的轮次归队, 不丢失
                    web_session.pending.append(
                        {"qid": str(uuid.uuid4()), "text": text,
                         "attachments": attachments})
                    if web_session.emits and web_session.loop is not None:
                        web_session.loop.call_soon_threadsafe(
                            _start_pending_turn, web_session)
                else:
                    web_session.busy = False
                    emitter({"type": "turn_done", "interrupted": True, "iterations": 0,
                             "budget_exhausted": False, "iterations_exhausted": False})
            continue
        _spawn_turn_thread(web_session, text, attachments, emitter, prompter)


def _start_pending_turn(web_session: WebSession) -> None:
    """事件循环线程: 取出该会话排队的下一条后续消息, 接力开跑新一轮。

    由上一轮工作线程在 finally 里经 call_soon_threadsafe 调度——
    此时槽位已释放, _start_turn 拿不到槽位会自行进入全局排队。
    """
    if not web_session.pending:
        return
    item = web_session.pending.pop(0)
    if not web_session.emits:
        return
    text = str(item.get("text") or "")
    attachments = item.get("attachments") or []
    # 前端把"已排队"气泡转正（按 qid 配对撤卡片）, 并重新进入忙碌态
    web_session.broadcast({
        "type": "turn_started",
        "qid": item.get("qid"),
        "text": text,
        "attachments": attachments,
    })
    _start_turn(web_session, text, web_session.broadcast, attachments=attachments)


def request_stop(web_session: WebSession) -> None:
    """stop / 断连: 朝安全侧叫停——解除权限等待，后续工具调用全部自动拒绝。

    说明: 内核的流式调用是同步阻塞的, 打断由流式代理在下一个 SSE 事件
    到达时抛 TurnInterrupted 实现——正文/思考流式期间通常毫秒级生效,
    只有等首包或工具执行中的长命令仍要等它自然结束。
    同时清空排队区: 用户叫停的意图是整轮停下, 排队的后续消息一并撤回。
    """
    if not web_session.busy:
        return
    web_session.stop_requested = True
    if web_session.pending:
        web_session.pending.clear()
        web_session.broadcast({"type": "turn_queue_cleared"})
    if web_session.prompter is not None:
        web_session.prompter.cancel()


def promote_pending(web_session: WebSession, qid: str) -> bool:
    """「立即」插队: 把待发送区里的这条提到最前并叫停当前轮。

    回落后它作为下一棒立刻接力开跑（朝安全侧, 同 request_stop 但不清空
    待发送区）。qid 不在待发送区时静默忽略（返回 False）——它可能已经
    开跑, 此刻叫停只会误杀当前轮。
    与手动停止一致: 被打断的任务就地收束不自动续跑, 是否继续由用户
    下一次消息决定。
    """
    if not (qid and web_session.busy):
        return False
    idx = next((i for i, it in enumerate(web_session.pending)
                if it.get("qid") == qid), -1)
    if idx < 0:
        return False
    web_session.pending.insert(0, web_session.pending.pop(idx))
    web_session.stop_requested = True
    if web_session.prompter is not None:
        web_session.prompter.cancel()
    return True


# ============================================================================
# REST: 会话列表 / 新建 / 历史回放
# ============================================================================

def _message_to_dict(msg: Message) -> dict:
    """历史回放: 按块类型摊平成前端易消费的形状。

    image 输出 {type, media_type, data}: 前端拼 data URI 渲染缩略图;
    file 输出 {type, name, text}: 前端渲染成文件 chip。"""
    blocks = []
    for b in msg.content:
        if isinstance(b, TextContentBlock):
            blocks.append({"type": "text", "text": b.text})
        elif isinstance(b, ImageContentBlock):
            source = b.source or {}
            blocks.append({
                "type": "image",
                "media_type": source.get("media_type"),
                "data": source.get("data"),
            })
        elif isinstance(b, FileContentBlock):
            blocks.append({"type": "file", "name": b.name, "text": b.text})
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


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # 浏览器/工具的默认图标请求路径兜底（页面里已用 <link> 指到 /static/icon.png）
    return FileResponse(STATIC_DIR / "icon.png")


# --- 应用图标: 设置 → 外观 可上传替换; icon-default.png 是出厂副本 ---
_ICON_DEFAULT = STATIC_DIR / "icon-default.png"
_ICON_RE = re.compile(r"^data:image/(png|jpeg|webp);base64,(.+)$", re.S)


def _icon_ver() -> int:
    """图标文件 mtime 当版本号: 前端拿它做缓存穿透 (?v=ver)。"""
    try:
        return int((STATIC_DIR / "icon.png").stat().st_mtime)
    except OSError:
        return 0


@app.post("/api/icon")
async def api_post_icon(request: dict):
    """data 为 dataURL 时覆盖应用图标, null 恢复出厂; 返回新版本号。"""
    data = request.get("data")
    live = STATIC_DIR / "icon.png"
    if data is None:
        if not _ICON_DEFAULT.exists():
            raise HTTPException(status_code=400, detail="出厂图标缺失，无法恢复")
        shutil.copyfile(_ICON_DEFAULT, live)
    else:
        m = _ICON_RE.match(str(data))
        if not m:
            raise HTTPException(status_code=400, detail="图标必须是 PNG/JPEG/WebP 的 dataURL")
        raw = base64.b64decode(m.group(2))
        if len(raw) > 512 * 1024:
            raise HTTPException(status_code=400, detail="图标过大（解码后限 512KB）")
        live.write_bytes(raw)
    return {"ok": True, "ver": _icon_ver()}


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
async def api_create_session(payload: Optional[dict] = Body(None)):
    """新建会话: 与 CLI 相同的 %Y%m%d-%H%M%S 时间戳 id（UTC）。

    文件在首条消息落盘时才创建，与 CLI 行为一致；id 记入 _pending_sessions，
    让列表/历史接口在落盘前就能认出它。可选携带 workdir: 侧栏项目行"新建任务"
    进入时预选的目录，创建即绑定，列表立刻归组（WS 首条消息的绑定仍是兜底）。
    """
    existing = set(store.list_sessions()) | _pending_sessions
    sid = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    while sid in existing:  # 同秒重建撞 id → 追加后缀区分
        sid += "w"
    workdir = None
    raw_wd = str((payload or {}).get("workdir") or "").strip()
    if raw_wd:
        wd = Path(raw_wd)
        if not wd.is_dir():
            raise HTTPException(status_code=400, detail=f"工作目录不存在: {raw_wd}")
        workdir = str(wd.resolve())
        store.set_workdir(sid, workdir)
    _pending_sessions.add(sid)
    return {"id": sid, "workdir": workdir}


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
# REST: 设置（思考等级 + 权限模式 + 激活模型）
# ============================================================================

@app.get("/api/ping")
async def api_ping():
    """探测端点: 桌面壳用它确认"这是 x-code 后端"。
    8000 端口可能被 C-Lodop 打印服务等程序抢占, 不能只看 200 就当作就绪。"""
    return {"app": "x-code"}


@app.get("/api/settings")
async def api_get_settings():
    active = _provider_cfg.get("active") or {}
    return {
        # 思考等级已按会话隔离, 这里返回的是"新会话的默认值"
        "thinking_level": api_client.thinking_level,
        "permission_mode": MODE_TO_NAME[app_state.permission_mode],
        # 前端展示用: 输入栏的模型名 + 顶栏面包屑的工作区名
        "model": api_client.model,
        "provider_id": active.get("provider"),
        "model_id": active.get("model"),
        "configured": _provider_ready(_provider_cfg),   # false → 前端弹初始化页
        "workspace": Path.cwd().name,
        "icon_ver": _icon_ver(),
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
        if mode == ALLOW_MODE:
            # 与 CLI 配置口径一致: allow 连将来需要问的工具也一并放行,
            # 不允许从设置界面进入（REPL /mode allow 临时开启不受影响）
            raise HTTPException(status_code=400, detail="allow 模式不允许从设置进入")
        app_state.set_permission_mode(mode)
        _save_permission_mode(mode)
        # 存活中的会话 runtime 也一并切换（与 CLI /mode 即时生效对齐）
        for web_session in _sessions.values():
            if web_session.runtime is not None:
                web_session.runtime.set_permission_mode(mode)

    # 切换激活模型（来自输入框模型下拉）
    provider_id = request.get("provider_id")
    model_id = request.get("model_id")
    if provider_id is not None and model_id is not None:
        _provider_cfg["active"] = {"provider": str(provider_id), "model": str(model_id)}
        save_providers(_provider_cfg)
        _apply_provider_config(_provider_cfg)

    return await api_get_settings()


def _save_permission_mode(mode: PermissionMode) -> None:
    """权限模式持久化到 ~/.x-code/settings.json 的 permissionMode key。

    值用 resolve_permission_mode / config mode_map 认的规范名, 重启后能原样
    读回; 不写 "allow"（同 POST 入口, 配置口径拒绝它）。读写都走
    config.SETTINGS_FILE, 测试 monkeypatch 该路径即可隔离。
    失败只降级为不持久化（本轮内存里仍生效）, 不打断设置请求。
    """
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data["permissionMode"] = MODE_TO_NAME[mode]
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


# ============================================================================
# REST: 模型供应商配置（设置页"模型"分区）
# ============================================================================

@app.get("/api/providers")
async def api_get_providers():
    return _provider_cfg


@app.post("/api/providers")
async def api_save_providers(request: dict):
    providers = request.get("providers")
    active = request.get("active")
    if not isinstance(providers, list):
        raise HTTPException(status_code=400, detail="providers 必须是数组")
    ids = [p.get("id") for p in providers]
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=400, detail="供应商 id 重复")
    for p in providers:
        if not p.get("id") or not p.get("name"):
            raise HTTPException(status_code=400, detail="供应商缺少 id 或名称")
        if not isinstance(p.get("models"), list):
            raise HTTPException(status_code=400, detail=f"供应商 {p.get('name')} 缺少模型列表")
        # 接口地址必填: 留空会让 SDK 回退到 Anthropic 官方地址, 智谱 key 必被 403
        if p.get("enabled") is not False and not str(p.get("base_url") or "").strip():
            raise HTTPException(
                status_code=400,
                detail=f"供应商 {p.get('name')} 缺少接口地址 Base URL",
            )
    cfg = {"active": active if isinstance(active, dict) else _provider_cfg.get("active"),
           "providers": providers}
    if not isinstance(cfg["active"], dict):
        cfg["active"] = {}
    _provider_cfg.clear()
    _provider_cfg.update(cfg)
    save_providers(_provider_cfg)
    _apply_provider_config(_provider_cfg)
    return _provider_cfg


@app.post("/api/providers/test")
async def api_test_provider(request: dict):
    """用给定配置发一次最小请求, 验证供应商连通性。"""
    base_url = request.get("base_url") or None
    api_key = request.get("api_key") or ""
    model = request.get("model") or ""
    if not api_key or not model:
        raise HTTPException(status_code=400, detail="缺少 api_key 或 model")
    try:
        probe = anthropic.Anthropic(api_key=api_key, base_url=base_url, timeout=30.0)
        resp = probe.messages.create(
            model=model, max_tokens=16,
            messages=[{"role": "user", "content": "hi"}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return {"ok": True, "detail": text.strip()[:50] or "(空回复)"}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:200]}


@app.post("/api/open-config")
async def api_open_config():
    """设置页「打开配置文件」: 用系统默认程序打开 ~/.x-code/settings.json。
    文件不存在时先创建空配置, 保证每次都能打开。"""
    path = SETTINGS_FILE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    try:
        if sys.platform == "win32":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"打开失败: {e}")
    return {"ok": True}


# ============================================================================
# WebSocket: 双向通道（服务端推事件 + 浏览器回审批/停止）
# ============================================================================

@app.websocket("/ws/{session_id}")
async def ws_endpoint(websocket: WebSocket, session_id: str):
    # WS 握手同样过门禁: 令牌可在 query 或 cookie（页面已种入）
    provided = (websocket.query_params.get("token")
                or websocket.cookies.get("xcode_token"))
    if API_TOKEN and provided != API_TOKEN:
        await websocket.close(code=1008)
        return
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

    # 注册本连接的事件出口: 会话事件广播给所有连接（多窗口/标签同时打开同一会话）
    token = web_session.add_emit(emit)
    web_session.loop = loop

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
                attachments, att_err = _parse_attachments(raw.get("attachments"))
                if att_err:
                    emit_error(att_err)
                    continue
                # text 与 attachments 同时为空才丢弃（允许只发图不打字）
                if not text and not attachments:
                    continue
                if web_session.busy:
                    # 本轮还在跑: 静默追加进会话级排队区, 当前轮结束后自动接力;
                    # 前端在待发送气泡上提供「立即」按钮, 需要插队时发 queue_promote。
                    # qid 由前端生成（本地排队卡片与后端排队区对齐）, 缺失时兜底生成
                    if len(web_session.pending) >= 10:
                        emit_error("待发送消息过多（上限 10 条），请等当前轮次结束")
                        continue
                    web_session.pending.append({
                        "qid": str(raw.get("qid") or uuid.uuid4()),
                        "text": text,
                        "attachments": attachments,
                    })
                    emit({"type": "turn_queued_user", "position": len(web_session.pending)})
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
                _start_turn(web_session, text, web_session.broadcast,
                            attachments=attachments)

            elif msg_type == "queue_promote":
                # 「立即」: 把待发送区里的这条提到最前, 并叫停当前轮——
                # 回落后它作为下一棒立刻接力开跑。被打断的当前任务就地
                # 收束, 不自动续跑（与手动停止一致）。按 qid 配对
                qid = str(raw.get("qid") or "").strip()
                promote_pending(web_session, qid)

            elif msg_type == "queue_remove":
                # 编辑/删除待发送卡片: 按 qid 从待发送区移除, 静默无回执
                qid = str(raw.get("qid") or "").strip()
                if qid:
                    web_session.pending = [
                        it for it in web_session.pending
                        if it.get("qid") != qid
                    ]

            elif msg_type == "permission_response":
                prompter = web_session.prompter
                if prompter is None:
                    emit_error("当前没有待审批的请求")
                else:
                    prompter.resolve(str(raw.get("request_id")), bool(raw.get("approved")))

            elif msg_type == "stop":
                request_stop(web_session)

            else:
                emit_error(f"未知消息类型: {msg_type!r}（已知: user / queue_promote / queue_remove / permission_response / stop）")

    except WebSocketDisconnect:
        # 断连但一轮对话可能还在跑: 朝安全侧叫停；落盘由工作线程完成
        request_stop(web_session)
    finally:
        web_session.remove_emit(token)   # 本连接注销: 不再接收广播
        sender_task.cancel()
        with suppress(asyncio.CancelledError):
            await sender_task


if __name__ == "__main__":
    import socket
    import uvicorn

    # 命令执行器依赖 Git Bash: 没有就拒绝启动。原因落盘到 ~/.x-code/,
    # 桌面壳只显示通用的"后端未就绪", 具体原因以这里为准
    reason = git_bash_unavailable_reason()
    if reason:
        print(f"✗ {reason}")
        try:
            USER_DIR.mkdir(parents=True, exist_ok=True)
            (USER_DIR / "startup-error.log").write_text(reason, encoding="utf-8")
        except OSError:
            pass
        sys.exit(1)

    args = sys.argv[1:]
    port = int(args[args.index("--port") + 1]) if "--port" in args \
        else int(os.getenv("XCODE_PORT") or 8000)

    # 父进程看门狗: 桌面壳拉起后端时把自己的 PID 传进来。壳无论怎么死
    # （正常退出/崩溃/被任务管理器强杀, RunEvent 清理都来不及跑）, OS 都会
    # 关闭它持有的内核句柄 → WaitForSingleObject 返回 → 后端立刻自杀,
    # 端口随之释放。没有它, 壳被强杀时后端孤儿化, 端口占用一直挂着。
    if "--parent-pid" in args:
        ppid = int(args[args.index("--parent-pid") + 1])

        def _watch_parent(pid: int) -> None:
            if os.name != "nt":
                return                      # 非 Windows 暂无对应实现, 行为同旧版
            import ctypes
            SYNCHRONIZE, INFINITE = 0x00100000, 0xFFFFFFFF
            handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not handle:
                os._exit(0)                 # 父进程已不存在: 拉起即失联, 直接退出
            ctypes.windll.kernel32.WaitForSingleObject(handle, INFINITE)
            os._exit(0)                     # 父进程死亡: 立刻退出, 释放端口

        threading.Thread(target=_watch_parent, args=(ppid,), daemon=True).start()

    def _port_free(p: int) -> bool:
        # connect_ex 探测: 已有进程监听时返回 0
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", p)) != 0

    # C-Lodop 等程序会抢占 8000: 被占则自动向后避让（8010–8019）,
    # 实际端口写 ~/.x-code/port, 桌面壳由此得知该访问哪个端口
    candidates = [port] + [q for q in range(8010, 8020) if q != port]
    chosen = next((q for q in candidates if _port_free(q)), None)
    if chosen is None:
        print("✗ 8000–8019 端口全部被占用（如 C-Lodop 打印服务）, 请释放后重试")
        sys.exit(1)
    USER_DIR.mkdir(parents=True, exist_ok=True)
    (USER_DIR / "port").write_text(str(chosen), encoding="utf-8")
    (USER_DIR / "startup-error.log").unlink(missing_ok=True)   # 启动成功: 旧原因作废
    print(f"✓ x-code 服务: http://127.0.0.1:{chosen}")
    uvicorn.run(app, host="127.0.0.1", port=chosen)
