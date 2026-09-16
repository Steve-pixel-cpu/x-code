import json
import os
import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from textwrap import dedent
from typing import Optional, Callable

from dotenv import load_dotenv
from pydantic import BaseModel

from api_client import ClaudeApiClient
from models import Session, TextContentBlock
from permissions import ALLOW_MODE, PermissionPolicy
from runtime import ConversationRuntime
from tools import ToolRegistry, bash_tool, read_tool, write_tool, powershell_tool

TOOL_WHITELIST: dict[str, set[str]] = {
    "explore": {"read_file"},
    "plan": {"read_file"},
    "verification": {"bash", "read_file"},
    "general": {"bash", "read_file", "write_file"},
}

class AgentStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class AgentManifest(BaseModel):
    agent_id: str
    name: Optional[str]
    description: str
    subagent_type: str
    status: str  # "pending" / "running" / "completed" / "failed"
    output_file: str
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]
    error: Optional[str]

class AgentJob(BaseModel):
    manifest: AgentManifest
    prompt: str
    allowed_tools: set[str]


def allowed_tools_for_subagent(subagent_type: str) -> set[str]:
    default_set = TOOL_WHITELIST["general"]
    return TOOL_WHITELIST.get(subagent_type, default_set)


# --- 第 12 课: worker 的 runtime 组装 ---

# 工具名 → 处理函数。worker 只注册 job.allowed_tools 里出现的名字，
# 没注册的工具在 ToolRegistry 层直接抛 ToolError（白名单的执行层兜底）。
SUBAGENT_TOOL_HANDLERS: dict[str, Callable] = {
    "bash": bash_tool,
    "powershell": powershell_tool,
    "read_file": read_tool,
    "write_file": write_tool,
}


class SilentToolExecutor:
    """worker 用的安静执行器 — 实现 runtime 的 ToolExecutor Protocol，
    但不像 Leader 的 CliToolExecutor 那样往终端打印（终端是 Leader 的）。"""

    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    def execute(self, tool_name: str, input: str) -> str:
        return self.registry.execute(tool_name, input)


def build_subagent_runtime(job: AgentJob) -> ConversationRuntime:
    """给 worker 组装一个完全独立的 runtime。

    - 空会话: subagent 看不到 Leader 的对话历史（上下文隔离的根基）
    - 安静的 api_client: emit_output=False，流式输出不刷 Leader 的终端
    - 白名单工具: 只注册 allowed_tools，白名单之外的工具调不到
    - ALLOW 权限: 子 agent 的边界是白名单而不是权限模式——没有人类
      可以被询问，权限模式在这里没有意义
    """
    load_dotenv()
    api_key = os.getenv("API_KEY")
    if api_key is None:
        raise RuntimeError("API_KEY not set!")

    # 工具规格和默认模型目前定义在 main.py（组装点）。
    # 延迟导入: 将来第 13 课 main 接线 multi_agent（Leader 派活）时，
    # 模块级 import 会变成循环导入，函数内 import 不会。
    from main import DEFAULT_MODEL, TOOLS

    registry = ToolRegistry()
    for tool_name in sorted(job.allowed_tools):
        handler = SUBAGENT_TOOL_HANDLERS.get(tool_name)
        if handler is not None:
            registry.register(name=tool_name, handler=handler)

    return ConversationRuntime(
        session=Session(),
        api_client=ClaudeApiClient(
            api_key=api_key,
            model=os.getenv("CLAUDE_MODEL") or DEFAULT_MODEL,
            tools=TOOLS,
            emit_output=False,
        ),
        tool_executor=SilentToolExecutor(registry=registry),
        permission_policy=PermissionPolicy(active_mode=ALLOW_MODE),
        system_prompt=[
            "You are a focused subagent worker. Complete exactly the task "
            "you were given, then report the result as text. Do nothing "
            "beyond the task.",
        ],
    )


def final_text_of(summary) -> str:
    """从 TurnSummary 提取最后一条带文本的 assistant 消息，作为 agent 的结果。"""
    for msg in reversed(summary.assistant_messages):
        texts = [b.text for b in msg.content if isinstance(b, TextContentBlock)]
        if texts:
            return "\n".join(texts)
    return "(无文本输出)"


class AgentOrchestrator:
    def __init__(self, store_dir: Path, spawn_fn: Optional[Callable] = None):
        self._store_dir = store_dir
        self._spawn_fn = spawn_fn if spawn_fn else self._default_spawn_fn

    def _default_spawn_fn(self, job: AgentJob):
        def _worker():  # ← 内层：线程的身体
            # 第 12 课: worker 本体 = 建 runtime → 跑一轮 → complete_agent
            try:
                runtime = build_subagent_runtime(job)
                summary = runtime.run_turn(job.prompt)
                self.complete_agent(job.manifest.agent_id, final_text_of(summary))
            except Exception as exc:
                try:
                    self._persist_terminal_state(job.manifest, status=AgentStatus.FAILED.value, result=None, error=str(exc))
                except Exception:
                    pass

        thread = threading.Thread(target=_worker, name=f"agent-{job.manifest.agent_id}", daemon=True)  # ← 外层：target=_worker（注意没有括号！）
        thread.start()  # ← 点火，立刻返回

    def spawn_agent(self, description: str, prompt: str, name: Optional[str] = None, subagent_type: str = "general") -> AgentManifest:
        if description.strip() == "" or prompt.strip() == "":
            raise ValueError("description or prompt are null")
        self._store_dir.mkdir(parents=True, exist_ok=True)
        agent_id = uuid.uuid4().hex[0:12]
        md_path = self._store_dir / f"{agent_id}.md"
        json_path = self._store_dir / f"{agent_id}.json"

        subagent_type = subagent_type.strip().lower()
        white_tools = allowed_tools_for_subagent(subagent_type)

        now = datetime.now(timezone.utc).isoformat()
        name = name if name else ""
        manifest = AgentManifest(
            agent_id=agent_id,
            name=name,
            description=description,
            subagent_type=subagent_type,
            status=AgentStatus.RUNNING.value,
            output_file=str(md_path),
            started_at=now,
            created_at=now,
            completed_at=None,
            error=None
        )
        md_content = dedent(f"""\
            # Agent Task

            - id: {agent_id}
            - name: {name}
            - subagent_type: {subagent_type}
            - created_at: {now}

            ## Prompt

            {prompt}
        """)
        with open(md_path, "w", encoding= "utf-8") as f:
            f.write(md_content)

        manifest_content = manifest.model_dump_json()
        with open(json_path, "w", encoding= "utf-8") as f:
            f.write(manifest_content)

        job = AgentJob(
            manifest=manifest.model_copy(),
            prompt=prompt,
            allowed_tools=white_tools.copy()
        )
        try:
            self._spawn_fn(job)
        except Exception as e:
            self._persist_terminal_state(manifest=manifest, status=AgentStatus.FAILED.value, result="", error=str(e))
            raise RuntimeError(f"Spawn failed: {e}") from e
        return manifest


    def get_status(self,agent_id: str) -> AgentManifest:
        json_path = self._store_dir / f"{agent_id}.json"
        if not json_path.exists():
            raise FileNotFoundError(f"Agent {agent_id} not found")
        with open(json_path, "r", encoding= "utf-8") as f:
            manifest = AgentManifest.model_validate(json.load(f))

        return manifest

    def list_agents(self) -> list[AgentManifest]:
        result: list[AgentManifest] = []
        for manifest in self._store_dir.glob("*.json"):
            try:
                with open(manifest, "r", encoding="utf-8") as f:
                    result.append(AgentManifest.model_validate(json.load(f)))
            except Exception as e:
                continue

        return result

    def _persist_terminal_state(self, manifest: AgentManifest, status: str, result: Optional[str], error:Optional[str]):
        """写终态。

        .md 追加结果；manifest JSON 用"写临时文件 + 原子替换"覆盖。
        worker 线程写终态的同时，Leader 可能正在轮询 get_status() 读
        同一个文件——直接 open("w") 会先截断再写，读者会撞见半截 JSON。
        原子替换保证读者要么看到完整旧文件、要么看到完整新文件。
        """
        md_path = self._store_dir / f"{manifest.agent_id}.md"
        now = datetime.now(timezone.utc).isoformat()
        md_content = dedent(f"""\
                   ## Result
                   
                   status: {status}
                   result: {result}
                   error: {error}
               """)
        with open(md_path, "a", encoding= "utf-8") as f:
            f.write(md_content)
        new_manifest = manifest.model_copy(update={
            "status": status,
            "completed_at": now,
            "error": error,
        })
        json_path = self._store_dir / f"{manifest.agent_id}.json"
        tmp_path = json_path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding= "utf-8") as f:
            f.write(new_manifest.model_dump_json())
        os.replace(tmp_path, json_path)


    def complete_agent(self, agent_id: str, result:str):
        manifest = self.get_status(agent_id)
        self._persist_terminal_state(manifest=manifest, status=AgentStatus.COMPLETED.value,result=result, error=None)


    def fail_agent(self, agent_id: str, error: str) :
        manifest = self.get_status(agent_id)
        self._persist_terminal_state(manifest = manifest, status = AgentStatus.FAILED.value,result=None, error= error)
