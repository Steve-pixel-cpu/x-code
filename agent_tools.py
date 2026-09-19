"""把 AgentOrchestrator 包装成 Leader 可调用的工具（接线层）。

multi_agent.py 提供编排能力，但 Leader（LLM）只能通过工具调它 —— 本模块
就是中间那根线: spawn_agent / agent_status / agent_list 三个工具的 spec、
handler 与注册函数。CLI 和 Web 共用同一份。

命名说明: 工具名是 agent_tool / agent_status / agent_list（不是
spawn_agent），Leader 视角这是"操作 subagent 的工具"，动词放前缀更一致;
test_no_whitelist_contains_agent_tool 检查白名单里不含 "Agent" 字面量，
与这里的命名不冲突 —— 递归防护靠"白名单不含 agent 工具 + 规格过滤"
（multi_agent._tool_specs_for），不靠名字。

并发模型: spawn 只起 daemon 线程立刻返回，Leader 可在对话中途并行派多个
worker，稍后用 agent_status 轮询。未完成就问状态 → manifest 状态是
running；is_done=False 提示 Leader 稍后再查，避免它干等。
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

    workdir 在构建后注入（server 端按会话工作目录解析后再 set_workdir）;
    CLI 端保持 None（工具跟随进程 cwd）。"""
    def __init__(self):
        self._lock = threading.Lock()
        self._orchestrator: Optional[AgentOrchestrator] = None

    def get(self, workdir: Optional[str] = None) -> AgentOrchestrator:
        with self._lock:
            if self._orchestrator is None:
                self._orchestrator = AgentOrchestrator(
                    store_dir=default_agents_dir())
            if workdir:
                self._orchestrator.set_workdir(workdir)
            return self._orchestrator


_toolbox = _AgentToolbox()


def get_orchestrator(workdir: Optional[str] = None) -> AgentOrchestrator:
    """CLI 与 Web 共用同一个编排器（同一个 agent 存储目录）。"""
    return _toolbox.get(workdir)


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
    return (
        f"Subagent spawned.\n"
        f"agent_id: {manifest.agent_id}\n"
        f"type: {manifest.subagent_type}\n"
        f"status: {manifest.status}\n"
        f"Poll agent_status with this agent_id to collect the result."
    )


def agent_status_tool(params: dict, workdir: Optional[str] = None) -> str:
    orch = get_orchestrator(workdir)
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


def agent_list_tool(params: dict, workdir: Optional[str] = None) -> str:
    orch = get_orchestrator(workdir)
    agents = orch.list_agents()
    if not agents:
        return "(no subagents yet)"
    lines = []
    for m in sorted(agents, key=lambda x: x.created_at):
        err = f"  error: {m.error}" if m.error else ""
        result = f"  result: {m.result}" if m.result else ""
        lines.append(
            f"- {m.agent_id} [{m.status}] type={m.subagent_type} "
            f"name={m.name or '-'} :: {m.description}{err}{result}"
        )
    return "\n".join(lines)


AGENT_TOOL_SPECS = [spawn_agent_spec, agent_status_spec, agent_list_spec]


def register_agent_tools(registry):
    """把三个 agent 工具注册进 ToolRegistry（供 main.build_registry 调用）。"""
    return (registry
            .register(name="agent_tool", handler=spawn_agent_tool)
            .register(name="agent_status", handler=agent_status_tool)
            .register(name="agent_list", handler=agent_list_tool))
