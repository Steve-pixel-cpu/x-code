"""把 AgentOrchestrator 包装成 Leader 可调用的工具（接线层）。

multi_agent.py 提供编排能力，但 Leader（LLM）只能通过工具调它 —— 本模块
就是中间那根线: agent_tool / agent_status / agent_reap / agent_list 四个
工具的 spec、handler 与注册函数。CLI 和 Web 共用同一份。

命名说明: 工具名是 agent_tool / agent_status / agent_reap / agent_list
（不是 spawn_agent），Leader 视角这是"操作 subagent 的工具"，动词放前缀
更一致; test_no_whitelist_contains_agent_tool 检查白名单里不含 "Agent"
字面量，与这里的命名不冲突 —— 递归防护靠"白名单不含 agent 工具 + 规格过
滤"（multi_agent._tool_specs_for），不靠名字。

并发模型: spawn 只起 daemon 线程立刻返回，Leader 可在对话中途并行派多个
worker，稍后用 agent_status 轮询；没轮到的由 agent_reap 在收尾时收割。
"""
import threading
from pathlib import Path
from typing import Optional

from multi_agent import AgentOrchestrator

# 状态字段在 spec 的 enum 里写死（pydantic 枚举不进 schema）
_AGENT_STATUSES = ("pending", "running", "completed", "failed")
_AGENT_TYPES = ("explore", "plan", "verification", "general")

# 子 agent 产出文件的存放位置（用户目录下, 与会话存储平级）
def default_agents_dir() -> Path:
    from config import USER_DIR
    return USER_DIR / "agents"


spawn_agent_spec = {
    "name": "agent_tool",
    "description": (
        "Spawn a subagent worker to handle a self-contained subtask "
        "asynchronously, then continue your own work. The worker runs in "
        "an isolated context (it cannot see this conversation) with a "
        "tool whitelist chosen by subagent_type:\n"
        "- explore: read-only investigation (read_file only)\n"
        "- plan: read-only design/analysis (read_file only)\n"
        "- verification: run checks without editing (bash, read_file)\n"
        "- general: full abilities (bash, powershell, read_file, write_file)\n"
        "The worker cannot spawn further subagents. Returns immediately "
        "with the agent id; poll agent_status to collect the result."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "Short human-readable summary of the task "
                               "(shown in status listings).",
            },
            "prompt": {
                "type": "string",
                "description": "The full self-contained task instructions "
                               "for the worker. It shares your working "
                               "directory but NOT your conversation, so "
                               "include every needed detail: file paths, "
                               "expected output, constraints.",
            },
            "subagent_type": {
                "type": "string",
                "enum": list(_AGENT_TYPES),
                "description": "Worker capability profile. Defaults to general.",
            },
            "name": {
                "type": "string",
                "description": "Optional short name for the worker.",
            },
        },
        "required": ["description", "prompt"],
    },
}

agent_status_spec = {
    "name": "agent_status",
    "description": (
        "Get the current status of a spawned subagent by id. Returns "
        "status (pending/running/completed/failed) and, once completed, "
        "the worker's final report text. Use this to poll for results."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "The agent id returned by agent_tool.",
            },
        },
        "required": ["agent_id"],
    },
}

agent_reap_spec = {
    "name": "agent_reap",
    "description": (
        "Harvest results from completed subagents whose outputs have not "
        "been delivered into this conversation yet (e.g. workers you "
        "spawned but never polled, including from before a restart). "
        "Returns each result with its agent id and marks them delivered. "
        "Call this when you have spawned workers whose results you have "
        "not yet collected."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}

agent_list_spec = {
    "name": "agent_list",
    "description": (
        "List all subagents spawned in this project with their ids, "
        "types, statuses and errors. Use it to re-discover agent ids or "
        "check for unfinished workers."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}


class _AgentToolbox:
    """持有一个懒构建的 AgentOrchestrator（线程安全的单例入口）。

    agent 存储在用户级目录（~/.x-code/agents）, 跨会话/跨启动共享;
    workdir 不在这里设置 —— spawn 时按调用参数传入（无共享中间状态）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._orchestrator: Optional[AgentOrchestrator] = None

    def get(self) -> AgentOrchestrator:
        with self._lock:
            if self._orchestrator is None:
                self._orchestrator = AgentOrchestrator(
                    store_dir=default_agents_dir())
            return self._orchestrator


_toolbox = _AgentToolbox()


def get_orchestrator() -> AgentOrchestrator:
    """CLI 与 Web 共用同一个编排器（同一个 agent 存储目录）。"""
    return _toolbox.get()


# --- 会话归属: 每个 worker 记录派它的会话, 收割时按会话隔离 ---
# key = session_id; CLI 恒为 DEFAULT_SESSION（单会话进程）。
# 跨进程不持久: 重启后历史 agent 不在任何会话桶里, 无法按会话收割,
# reconcile（进程启动对账）会把它们的未交付 completed 一并标记交付归档。
_session_bindings: dict[str, set[str]] = {}
DEFAULT_SESSION = "cli"


def bind_agent_to_session(agent_id: str, session_id: str) -> None:
    """spawn 后登记归属。只在 spawn 同步路径调用（worker 线程不碰它）,
    dict 单写者无并发问题。"""
    _session_bindings.setdefault(session_id, set()).add(agent_id)


def current_session_id() -> Optional[str]:
    """当前 turn 绑定的会话 id。server 端由 dispatch 提供绑定; CLI 恒 None。

    延迟导入 + 异常兜底: agent_tools 被 CLI（main）导入时不能反向依赖
    server 模块（FastAPI 导入重, 且 CLI 场景没有 dispatch 绑定）。"""
    try:
        from server import dispatch
        return dispatch.current_session_id()
    except Exception:
        return None


# --- handlers: 签名与其他工具一致 (params: dict, workdir: Optional[str]) ---

def _fmt_manifest(m) -> str:
    import json
    d = json.loads(m.model_dump_json())
    return json.dumps(d, ensure_ascii=False, indent=2)


def spawn_agent_tool(params: dict, workdir: Optional[str] = None) -> str:
    orch = get_orchestrator()
    # workdir 由调用点按次传入: CLI = None（进程 cwd）; Web = 当前会话的项目
    # 目录（EmittingToolRegistry 从 dispatch.current_workdir() 取）。走参数
    # 而非先 set_workdir 再 spawn: orchestrator 跨会话共享, 参数传递无竞态。
    try:
        manifest = orch.spawn_agent(
            description=params.get("description", ""),
            prompt=params.get("prompt", ""),
            name=params.get("name") or None,
            subagent_type=params.get("subagent_type") or "general",
            workdir=workdir,
        )
    except ValueError as e:
        return f"ERROR: {e}"
    # 登记会话归属: 收割按会话隔离（Web 多会话不互抢结果）
    bind_agent_to_session(manifest.agent_id,
                          current_session_id() or DEFAULT_SESSION)
    return (
        f"Subagent spawned.\n"
        f"agent_id: {manifest.agent_id}\n"
        f"type: {manifest.subagent_type}\n"
        f"status: {manifest.status}\n"
        f"Poll agent_status with this agent_id to collect the result."
    )


def agent_status_tool(params: dict, workdir: Optional[str] = None) -> str:
    orch = get_orchestrator()
    agent_id = (params.get("agent_id") or "").strip()
    if not agent_id:
        return "ERROR: agent_id is required"
    try:
        manifest = orch.get_status(agent_id)
    except FileNotFoundError:
        return f"ERROR: no such agent: {agent_id}"
    body = _fmt_manifest(manifest)
    if manifest.status == "running":
        body += "\n(Still running — call agent_status again later.)"
    return body


def reap_ready_tool(params: dict, workdir: Optional[str] = None) -> str:
    """收割: completed 未交付的结果注入对话, 并标记 delivered。

    会话隔离: 只收当前会话派出的 worker——Web 端多会话并存, A 会话不该
    收走 B 会话的结果（B 的 Leader 还要靠 agent_status 轮询它们）。CLI
    是单会话进程, 恒用 DEFAULT_SESSION。"""
    orch = get_orchestrator()
    session_id = current_session_id() or DEFAULT_SESSION
    my_ids = _session_bindings.get(session_id, set())
    ready = [m for m in orch.reap_ready() if m.agent_id in my_ids]
    if not ready:
        return ("(no pending results — all spawned agents in this session "
                "are either still running or already delivered)")
    orch.mark_delivered([m.agent_id for m in ready])
    blocks = []
    for m in ready:
        result = m.result or "(worker completed but returned no text)"
        blocks.append(
            f"[{m.agent_id}] {m.subagent_type} :: {m.description}\n"
            f"result:\n{result}"
        )
    return "\n\n".join(blocks)


def agent_list_tool(params: dict, workdir: Optional[str] = None) -> str:
    orch = get_orchestrator()
    agents = orch.list_agents()
    if not agents:
        return "(no subagents yet)"
    lines = []
    for m in sorted(agents, key=lambda x: x.created_at):
        err = f"  error: {m.error}" if m.error else ""
        result = f"  result: {m.result}" if m.result else ""
        undelivered = "  (undelivered)" if (
            m.status == "completed" and not m.delivered) else ""
        lines.append(
            f"- {m.agent_id} [{m.status}] type={m.subagent_type} "
            f"name={m.name or '-'} :: {m.description}{undelivered}{err}{result}"
        )
    return "\n".join(lines)


AGENT_TOOL_SPECS = [spawn_agent_spec, agent_status_spec,
                    agent_reap_spec, agent_list_spec]


def register_agent_tools(registry):
    """把四个 agent 工具注册进 ToolRegistry（供 main.build_registry 调用）。"""
    return (registry
            .register(name="agent_tool", handler=spawn_agent_tool)
            .register(name="agent_status", handler=agent_status_tool)
            .register(name="agent_reap", handler=reap_ready_tool)
            .register(name="agent_list", handler=agent_list_tool))
