from pathlib import Path
from typing import Optional, Callable

from pydantic import BaseModel

TOOL_WHITELIST: dict[str, set[str]]   # subagent_type → 允许的工具集

class AgentManifest(BaseModel):
    agent_id: str
    name: str
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
    ...

class AgentOrchestrator:
    def __init__(self, store_dir: Path, spawn_fn: Optional[Callable] = None):
        self.store_dir = store_dir
        self.spawn_fn = spawn_fn

    def spawn_agent(self, description: str, prompt: str, subagent_type: str = "general") -> AgentManifest:
        ...

    def get_status(self,agent_id: str) -> AgentManifest:
        ...

    def list_agents(self) -> list[AgentManifest]:
        ...

    def complete_agent(self, agent_id: str, result:str):
        ...

    def fail_agent(self, agent_id: str, error: Optional[str]) :
        ...
