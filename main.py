# --- Slash command 解析 ---

import os
import platform
import sys
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from api_client import ApiClient, ClaudeApiClient
from config import RuntimeConfig, ConfigLoader
from hooks import HookRunner
from models import Session, Message
from permissions import PermissionRequest, PermissionResult, PermissionMode, PermissionPolicy, PermissionDecision, \
    PermissionPrompter
from prompt import SystemPromptBuilder
from runtime import ConversationRuntime
from storage import SessionStore
from tools import ToolRegistry, bash_tool, read_tool, write_tool

DEFAULT_MODEL = "claude-opus-4-6"
bash_spec = {
    "name": "bash",
    "description": (
        "Execute a shell command in the terminal and return its output. "
        "stdout is returned as-is; stderr is appended if present. "
        "Use this for listing files, running scripts, git operations, "
        "installing dependencies, and other command-line tasks. "
        "Commands time out after 30 seconds, so avoid long-running or "
        "interactive commands."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "The shell command to execute, e.g. 'ls -la' or "
                    "'python script.py'. Must be non-interactive."
                ),
            },
        },
        "required": ["command"],
    },
}

read_file_spec = {
    "name": "read_file",
    "description": (
        "Read the contents of a text file from the local filesystem and "
        "return it as a string. Use this to inspect source code, configs, "
        "or any text-based file before editing it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path to the file to read, e.g. 'src/main.py' or "
                    "'/home/user/notes.txt'. Supports relative and absolute paths."
                ),
            },
        },
        "required": ["path"],
    },
}

write_file_spec = {
    "name": "write_file",
    "description": (
        "Write text content to a file at the given path. Creates the file "
        "if it does not exist, and overwrites it if it does. Use this to "
        "create or update source code, configs, and other text files. "
        "Parent directories must already exist."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path to the file to write, e.g. 'src/main.py' or "
                    "'/home/user/notes.txt'. Supports relative and absolute paths."
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    "The full text content to write to the file. "
                    "This replaces any existing content."
                ),
            },
        },
        "required": ["path", "content"],
    },
}
TOOLS = [bash_spec, read_file_spec, write_file_spec]

class SlashCommand(Enum):
    HELP = "help"
    STATUS = "status"
    COMPACT = "compact"
    EXIT = "exit"
    UNKNOWN = "unknown"

    @classmethod
    def print_cmd(cls):
        for cmd in cls:
            if cmd is cls.UNKNOWN:
                continue
            print(f"/{cmd.value}")

def parse_slash_command(input: str) -> Optional[SlashCommand]:
    command_map = {cmd.value: cmd for cmd in SlashCommand}
    if not input or not input.startswith("/"):
        return None
    return command_map.get(input[1:].strip(), SlashCommand.UNKNOWN)


class CliPermissionPrompter:
    def decide(self, request: PermissionRequest) -> PermissionResult:
        tool_input = request.input if len(request.input)<200 else request.input[0:200]
        try:
            user_input = input(f"工具{request.tool_name},需要执行:{tool_input},当前模式是{request.current_mode.as_str()},需要的模式是{request.required_mode.as_str()},y/N:")
        except KeyboardInterrupt:
            raw_output = f"User denied permission to run {request.tool_name}!"
            print(raw_output[0:160] if len(raw_output) > 160 else raw_output)
            return PermissionResult(
                decision=PermissionDecision.DENY,
                reason= raw_output
            )
        if user_input.strip().lower() in ["y", "yes"]:
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                reason= "user said yes!"
            )
        raw_output = f"User denied permission to run {request.tool_name}!"
        print(raw_output[0:160] if len(raw_output) > 160 else raw_output)
        return PermissionResult(
            decision=PermissionDecision.DENY,
            reason=raw_output
        )


# --- CLI 工具执行 ---
class CliToolExecutor:
    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    def execute(self, tool_name: str, input: str) -> str:
        print(f"正在执行工具: {tool_name}")
        output = self.registry.execute(tool_name, input)
        print(output[0:160] if len(output) > 160 else output)
        return output

# --- 组装 runtime ---
def build_runtime(session: Session,
                  api_client: ApiClient,
                  registry: ToolRegistry,
                  permission_mode: PermissionMode,
                  system_prompt: list[str],
                  hooks_config: RuntimeConfig) -> ConversationRuntime:
    permission_policy = PermissionPolicy(
        active_mode = permission_mode,
    )
    hook_runner = HookRunner.from_config(hooks_config)

    running_time = ConversationRuntime(
        api_client=api_client,
        tool_executor=CliToolExecutor(registry),
        system_prompt=system_prompt,
        hook_runner=hook_runner,
        permission_policy=permission_policy,
        session=session,
    )
    
    return running_time

BANNER_ART = r"""
 __  __        ____ ___  ____  _____
 \ \/ /       / ___/ _ \|  _ \| ____|
  \  /  _____| |  | | | | | | |  _|
  /  \ |_____| |__| |_| | |_| | |___
 /_/\_\       \____\___/|____/|_____|
"""
def print_banner(name: str = "X-CODE", width: int = 40) -> None:
    """Print an ASCII-art startup banner with the app name and help hint."""
    print(BANNER_ART)
    print(name.center(width))
    print("/help 看命令".center(width))
    print("=" * width)

def print_status(runtime: "ConversationRuntime") -> None:
    """打印当前会话的用量收据。"""
    usage = runtime.usage().cumulative_usage()

    input_tokens  = usage.input_tokens
    output_tokens = usage.output_tokens
    total_tokens  = usage.total_tokens()

    turns = runtime.usage().turns()
    messages = len(runtime.session().messages)

    width = 40
    line = "-" * width

    print(line)
    print("会话状态".center(width))
    print(line)
    print(f"  {'input  tokens':<16}{input_tokens:>10,}")
    print(f"  {'output tokens':<16}{output_tokens:>10,}")
    print(f"  {'total  tokens':<16}{total_tokens:>10,}")
    print(line)
    print(f"  {'turns':<16}{turns:>10,}")
    print(f"  {'messages':<16}{messages:>10,}")
    print(line)

# --- REPL ---
def run_repl(runtime: ConversationRuntime,
             prompter: PermissionPrompter,
             store: SessionStore,
             session_id: str,
             last_uuid: Optional[str]):
    print_banner()
    idx_before = -1
    while True:
        try:
            text = input("x-code> ").strip()
        except KeyboardInterrupt:
            print("\n")
            continue
        except EOFError:
            print("bye!")
            break
        if not text:
            continue
        cmd = parse_slash_command(text)
        if cmd is not None:
            if cmd == SlashCommand.EXIT:
                print("bye!")
                break
            elif cmd == SlashCommand.HELP or cmd == SlashCommand.UNKNOWN:
                SlashCommand.print_cmd()
            elif cmd == SlashCommand.STATUS:
                print_status(runtime)
            elif cmd == SlashCommand.COMPACT:
                runtime.compact()


        else:
            print("--------------------------------------")
            try:
                for msg in runtime.session().messages[idx_before+1:]:
                    store.save_message(
                        session_id=session_id,
                        message=msg,
                        parent_uuid=last_uuid,
                    )
                idx_before = len(runtime.session().messages[idx_before+1:]) -1

                runtime.run_turn(text, prompter)
            except Exception as e:
                print("Error: {}".format(e))
                continue


def start(session_store:SessionStore,session_id:str):
    load_dotenv()
    api_key = os.getenv("API_KEY")
    if api_key is None:
        print("API_KEY not set!")
        return

    registry = ToolRegistry()

    session_load = session_store.load_session(session_id)
    session_msgs = session_load[0]
    last_uuid = session_load[1]
    registry.register(name="bash", handler=bash_tool).register(name="read_file", handler=read_tool).register(
        name="write_file", handler=write_tool)

    config_loader = ConfigLoader(
        cwd=Path.cwd(),
        config_home=Path.home(),
    )
    system_prompt = SystemPromptBuilder().with_os(platform.system(), platform.release()).build()
    runtime_config = config_loader.load()
    api_client = ClaudeApiClient(
        api_key=str(api_key),
        model=runtime_config.model() or DEFAULT_MODEL,
        tools=TOOLS
    )
    runtime = build_runtime(
        api_client=api_client,
        system_prompt=system_prompt,
        registry=registry,
        permission_mode=PermissionMode.WORKSPACE_WRITE,
        hooks_config=runtime_config,
        session= Session(
            messages=session_msgs,
        )
    )
    prompter = CliPermissionPrompter()
    run_repl(runtime=runtime, prompter=prompter, store=session_store, session_id=session_id,last_uuid=last_uuid)


# --- 入口 ---
def main():
    session_store = SessionStore(
        storage_dir=Path.home() / ".x-code" / "sessions",
    )
    session_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    args = sys.argv[1:]
    if not args:
        start(session_store=session_store, session_id=session_id)
    if args[1] == "--list":
        print(session_store.list_sessions())
    elif args[1] == "--resume":
        session_id = args[2]
        session_id_list = session_store.list_sessions()
        if session_id in session_id_list:
            start(session_store=session_store,session_id=session_id)
        else:
            print("找不到会话!")
            print("可用列表:\n")
            print("\n".join(session_id_list))

    else:
        start(session_store=session_store,session_id=session_id)

if __name__ == "__main__":
    main()

