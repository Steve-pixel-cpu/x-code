# --- Slash command 解析 ---
from enum import Enum
from typing import Optional

from api_client import ApiClient
from config import RuntimeConfig
from models import Session
from permissions import PermissionRequest, PermissionResult, PermissionMode
from runtime import ConversationRuntime
from tools import ToolRegistry


class SlashCommand(Enum):
    HELP = "help"
    STATUS = "status"
    COMPACT = "compact"
    EXIT = "exit"
    UNKNOWN = "unknown"

def parse_slash_command(input: str) -> Optional[SlashCommand]:
    ...


class CliPermissionPrompter:
    def decide(self, request: PermissionRequest) -> PermissionResult:
        ...


# --- CLI 工具执行 ---
class CliToolExecutor:
    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    def execute(self, tool_name: str, input: str) -> str:
        ...

# --- 组装 runtime ---
def build_runtime(session: Session,
                  api_client: ApiClient,
                  registry: ToolRegistry,
                  permission_mode: PermissionMode,
                  system_prompt: list[str],
                  hooks_config: RuntimeConfig) -> ConversationRuntime:
    running_time = ConversationRuntime(
        api_client=api_client,
        tool_executor=registry,
    )
    
    return running_time

# --- REPL ---
def run_repl(model, permission_mode):
    ...

# --- 入口 ---
def main():
    ...

if __name__ == "__main__":
    main()

