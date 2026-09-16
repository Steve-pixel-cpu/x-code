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
from models import Session
from permissions import (
    DANGER_FULL_ACCESS_MODE,
    ALLOW_MODE, MODE_TO_NAME, NAME_TO_MODE,
)
from permissions import PermissionRequest, PermissionResult, PermissionMode, PermissionPolicy, PermissionDecision, \
    PermissionPrompter
from prompt import SystemPromptBuilder
from runtime import ConversationRuntime
from storage import SessionStore
from tools import ToolRegistry, bash_tool, read_tool, write_tool, powershell_tool

DEFAULT_MODEL = "glm-5.3-flash"
bash_spec = {
    "name": "bash",
    "description": (
        "Execute a shell command in the terminal and return its output. "
        "stdout is returned as-is; stderr is appended if present. "
        "Use this for listing files, running scripts, git operations, "
        "installing dependencies, and other command-line tasks. "
        "Commands time out after 30 seconds, so avoid long-running or "
        "interactive commands. "
        "Note: on Windows this runs through PowerShell (there is no sh), "
        "so use PowerShell-compatible syntax; bash-only constructs such as "
        "'&&' chains, subshells, or GNU grep/sed flags may not work."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "The shell command to execute, e.g. 'ls -la' or "
                    "'python script.py'. Must be non-interactive. "
                    "On Windows, write PowerShell-compatible commands."
                ),
            },
        },
        "required": ["command"],
    },
}

powershell_spec = {
    "name": "powershell",
    "description": (
        "Execute a command in Windows PowerShell and return its output. "
        "stdout is returned as-is; stderr is appended if present. "
        "Use this for Windows-specific tasks: services, registry, scheduled "
        "tasks, ACLs, WMI/CIM queries, Get-ChildItem -Recurse, and other "
        "PowerShell cmdlets. Commands time out after 30 seconds, so avoid "
        "long-running or interactive commands."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "The PowerShell command to execute, e.g. "
                    "'Get-Process | Select-Object -First 5' or "
                    "'Get-Service'. Must be non-interactive."
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
TOOLS = [bash_spec, powershell_spec, read_file_spec, write_file_spec]

class SlashCommand(Enum):
    HELP = "help"
    STATUS = "status"
    COMPACT = "compact"
    MODE = "mode"
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
    body = input[1:].strip()
    # 命令后可带参数（如 /mode read-only）: 只取第一个 token 匹配命令名
    name = body.split()[0] if body else ""
    return command_map.get(name, SlashCommand.UNKNOWN)


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
                  system_prompt: list[str],
                  hooks_config: RuntimeConfig,
                  permission_mode: PermissionMode = DANGER_FULL_ACCESS_MODE,
                 ) -> ConversationRuntime:
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

def resolve_permission_mode(runtime_config: RuntimeConfig) -> PermissionMode:
    """决定启动时的权限模式。

    默认 danger-full-access; 配置里设置了 permissionMode 就用配置值。

    注意: 这里故意不认 "allow" — allow 会连将来注册为需要
    prompt/allow 的工具也一并放行, 所以只允许在 REPL 里用
    /mode allow 临时开启, 不允许从配置文件进入。
    (config.py 的 mode_map 本就不接受 "allow", 这里再兜底一次,
    防止将来改动配置解析时把洞重新引入。)
    """
    mode_name = runtime_config.permission_mode()
    if mode_name:
        mode = NAME_TO_MODE.get(mode_name)
        if mode is not None and mode != ALLOW_MODE:
            return mode
        print(f"配置里的权限模式无效: {mode_name!r}, 回退到 danger-full-access")
    return DANGER_FULL_ACCESS_MODE

BANNER_ART = r"""
 __  __        ____ ___  ____  _____
 \ \/ /       / ___/ _ \|  _ \| ____|
  \  /  _____| |  | | | | | | |  _|
  /  \ |_____| |__| |_| | |_| | |___|
 /_/\_\       \____\___/|____/|_____|
"""
def print_banner(name: str = "X-CODE", width: int = 40) -> None:
    """Print an ASCII-art startup banner with the app name and help hint."""
    print(BANNER_ART)
    print(name)
    print("/help 看命令")
    print("=" * width)

def switch_mode(runtime: ConversationRuntime, mode_name: str) -> None:
    """切换权限模式: /mode 不带参数 = 打印当前模式与可选值; /mode <name> = 切换。"""
    if not mode_name:
        print(f"当前权限模式: {runtime.permission_mode().as_str()}")
        print(f"可选: {' | '.join(MODE_TO_NAME.values())}")
        return
    mode = NAME_TO_MODE.get(mode_name.strip().lower())
    if mode is None:
        print(f"未知模式: {mode_name}")
        print(f"可选: {' | '.join(MODE_TO_NAME.values())}")
        return
    runtime.set_permission_mode(mode)
    print(f"权限模式已切换: {mode.as_str()}")

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

def do_compact(runtime: ConversationRuntime):
    try:
        msg = runtime.compact()
        print(msg)
    except Exception as e:
        print(f"Compact failed! Error: {str(e)}")



# --- REPL ---
def run_repl(runtime: ConversationRuntime,
             prompter: PermissionPrompter,
             store: SessionStore,
             session_id: str,
             last_uuid: Optional[str]):
    print_banner()
    print(f"权限模式: {runtime.permission_mode().as_str()} (切换: /mode <name>)")
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
                do_compact(runtime)
            elif cmd == SlashCommand.MODE:
                switch_cmd_len = len(SlashCommand.MODE.value) + 1
                print(SlashCommand.MODE.value)
                mode_name = text[switch_cmd_len:].strip()
                switch_mode(runtime, mode_name)

        else:
            print("--------------------------------------")
            try:
                runtime.run_turn(text, prompter)

                for msg in runtime.session().messages[idx_before + 1:]:
                    last_uuid = store.save_message(
                        session_id=session_id,
                        message=msg,
                        parent_uuid=last_uuid,
                    )
                idx_before = len(runtime.session().messages) - 1

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
    registry.register(name="bash", handler=bash_tool).register(
        name="powershell", handler=powershell_tool).register(
        name="read_file", handler=read_tool).register(
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
        permission_mode=resolve_permission_mode(runtime_config),
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
        return
    elif args[0] == "--list":
        print(session_store.list_sessions())
    elif args[0] == "--resume":
        session_id = args[1]
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
