import json
import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from textwrap import dedent
from typing import Optional, Callable

from pydantic import BaseModel

TOOL_WHITELIST: dict[str, set[str]] = {
    "explore": {"read_file"},
    "plan": {"read_file"},
    "verification": {"read_file", "bash"},
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


class AgentOrchestrator:
    def __init__(self, store_dir: Path, spawn_fn: Optional[Callable] = None):
        self._store_dir = store_dir
        self._spawn_fn = spawn_fn if spawn_fn else self._default_spawn_fn

    def _default_spawn_fn(self, job: AgentJob):
        def _worker():  # ← 内层：线程的身体
            try:
                pass  # ← TODO：第 12 课换成「建 runtime → 跑一轮 → complete_agent」
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
        with open(json_path, "w", encoding= "utf-8") as f:
            f.write(new_manifest.model_dump_json())


    def complete_agent(self, agent_id: str, result:str):
        manifest = self.get_status(agent_id)
        self._persist_terminal_state(manifest=manifest, status=AgentStatus.COMPLETED.value,result=result, error=None)


    def fail_agent(self, agent_id: str, error: str) :
        manifest = self.get_status(agent_id)
        self._persist_terminal_state(manifest = manifest, status= AgentStatus.FAILED.value,result=None, error= error)
