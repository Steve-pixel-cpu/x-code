# --- Slash command 解析 ---

import json
import os
import platform
import sys
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from api_client import ApiClient, ClaudeApiClient, THINKING_LEVELS
from config import RuntimeConfig, ConfigLoader, USER_DIR
from hooks import HookRunner
from models import Message, Session, TextContentBlock, ToolContentBlock
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
        "stdout is returned as-is; stderr is appended if present. Use this "
        "for Windows-specific tasks: services, registry, scheduled "
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


# --- 终端视觉规范: 调色板 + 版式 ---
# 规则: ANSI 码只允许出现在 _paint 一处，所有颜色经 c_xxx() 成对包裹/重置，
# 任何异常路径都不可能把颜色泄漏到后续输出。

_ANSI_RESET = "\033[0m"
_ANSI_DIM = "\033[2m"
_ANSI_RED = "\033[31m"
_ANSI_YELLOW = "\033[33m"
_ANSI_CYAN = "\033[36m"


def _paint(code: str, text: str) -> str:
    """成对包裹: code + text + reset。颜色只在这一处拼接。"""
    return f"{code}{text}{_ANSI_RESET}"


def c_dim(s: str) -> str:
    """暗灰: 工具结果预览 / 分隔线。"""
    return _paint(_ANSI_DIM, s)


def c_red(s: str) -> str:
    """红色: 错误信息（配 ✗ 前缀）。"""
    return _paint(_ANSI_RED, s)


def c_yellow(s: str) -> str:
    """黄色: 权限询问。"""
    return _paint(_ANSI_YELLOW, s)


def c_cyan(s: str) -> str:
    """青色: 工具调用提示（配 '⚙ 工具' 前缀）。"""
    return _paint(_ANSI_CYAN, s)


SEPARATOR = "─" * 40        # 每轮对话开始的细分隔线
PREVIEW_MAX_LINES = 3       # 工具结果预览: 最多行数
PREVIEW_MAX_CHARS = 160     # 工具结果预览: 最多字符数
LINE_MAX_CHARS = 80         # 单行内容（权限面板/工具提示）截断宽度
FIELD_LABEL_WIDTH = 9       # 对齐字段的标签列宽（按终端显示宽度计）


def one_line(s: str) -> str:
    """压成单行: 所有空白（含换行）折叠为单个空格。"""
    return " ".join(s.split())


def truncate_line(s: str, width: int = LINE_MAX_CHARS) -> str:
    return s if len(s) <= width else s[: width - 1] + "…"


def _display_width(s: str) -> int:
    """终端显示宽度: CJK 全角按 2 列计。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in s)


def field_line(label: str, value: str) -> str:
    """对齐字段行: `  标签:    值`，值列按显示宽度对齐（权限面板与 /status 共用）。"""
    pad = FIELD_LABEL_WIDTH - _display_width(label) - 1  # -1 给冒号
    return f"  {label}:{' ' * max(pad, 1)}{value}"


def indent_block(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.split("\n"))


def format_preview(output: str) -> str:
    """工具结果预览: 最多 3 行 / 160 字符，超出加 '...(已截断，共 N 行)'。"""
    if not output.strip():
        return "(无输出)"
    text = output.replace("\r\n", "\n").rstrip("\n")
    all_lines = text.split("\n")
    total = len(all_lines)
    preview = "\n".join(all_lines[:PREVIEW_MAX_LINES])
    if len(preview) > PREVIEW_MAX_CHARS:
        preview = preview[:PREVIEW_MAX_CHARS]
    if total > PREVIEW_MAX_LINES or len(text) > len(preview):
        preview += f"\n...(已截断，共 {total} 行)"
    return preview


_JSON_KEY_PRIORITY = ("command", "path", "file_path", "url", "content")


def describe_tool_input(raw: str) -> str:
    """权限面板/工具提示的『内容』: JSON 先解析取关键信息，解析失败才展示原文。"""
    if not raw.strip():
        return "(空)"
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict):
        for key in _JSON_KEY_PRIORITY:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return truncate_line(one_line(value))
        pairs = " ".join(f"{k}={one_line(str(v))}" for k, v in data.items())
        return truncate_line(pairs)
    return truncate_line(one_line(raw))


def setup_console() -> None:
    """启动兜底: UTF-8 输出 + 启用 VT，Windows GBK 控制台不因 emoji/颜色码崩溃。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
    # Windows 控制台启用 ANSI(VT) 转义；跑一次空命令即触发，其他平台无害
    os.system("")

class SlashCommand(Enum):
    HELP = "help"
    STATUS = "status"
    COMPACT = "compact"
    MODE = "mode"
    THINKING = "thinking"
    RENAME = "rename"
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
    """多行权限面板（黄色），y/yes 放行，其余与 Ctrl+C 一律朝安全侧拒绝。"""

    def decide(self, request: PermissionRequest) -> PermissionResult:
        deny_reason = f"User denied permission to run {request.tool_name}!"
        print()
        print(c_yellow("⚠ 需要授权"))
        print(c_yellow(field_line("工具", request.tool_name)))
        print(c_yellow(field_line(
            "权限",
            f"{request.required_mode.as_str()}（当前 {request.current_mode.as_str()}）",
        )))
        print(c_yellow(field_line("内容", describe_tool_input(request.input))))
        try:
            user_input = input(c_yellow("批准? [y/N] "))
        except KeyboardInterrupt:
            print()
            print(c_yellow("已拒绝。"))
            return PermissionResult(
                decision=PermissionDecision.DENY,
                reason=deny_reason
            )
        if user_input.strip().lower() in ["y", "yes"]:
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                reason= "user said yes!"
            )
        print(c_yellow("已拒绝。"))
        return PermissionResult(
            decision=PermissionDecision.DENY,
            reason=deny_reason
        )


# --- CLI 工具执行 ---
class CliToolExecutor:
    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    def execute(self, tool_name: str, input: str) -> str:
        desc = describe_tool_input(input)
        print()
        print(c_cyan(f"⚙ 工具 {tool_name}" + (f"  {desc}" if desc else "")))
        print()
        try:
            output = self.registry.execute(tool_name, input)
        except Exception as e:
            # 终端看红 ✗；异常继续上抛，由 runtime 转 tool_result 给模型
            print(c_red(f"✗ {tool_name} 失败: {truncate_line(one_line(str(e)))}"))
            print()
            raise
        print(c_dim(indent_block(format_preview(output))))
        print()
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
    # 循环层预算接线: maxIterations / tokenBudget(=auto-compact 阈值) /
    # turnTokenBudget 此前只是被解析, 从未生效
    return (running_time
            .with_max_iterations(hooks_config.max_iterations())
            .with_auto_compact_threshold(hooks_config.token_budget())
            .with_turn_output_budget(hooks_config.turn_token_budget()))

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
        print(c_red(f"✗ 配置里的权限模式无效: {mode_name!r}, 回退到 danger-full-access"))
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
        print(c_red(f"✗ 未知模式: {mode_name}"))
        print(f"可选: {' | '.join(MODE_TO_NAME.values())}")
        return
    runtime.set_permission_mode(mode)
    print(f"权限模式已切换: {mode.as_str()}")

def switch_thinking(runtime: ConversationRuntime, level_name: str) -> None:
    """切换思考等级: /thinking 不带参数 = 打印当前等级与可选值; /thinking <level> = 切换。"""
    if not level_name:
        print(f"当前思考等级: {runtime.thinking_level()}")
        print(f"可选: {' | '.join(THINKING_LEVELS)}")
        return
    level = level_name.strip().lower()
    if level not in THINKING_LEVELS:
        print(c_red(f"✗ 未知思考等级: {level_name}"))
        print(f"可选: {' | '.join(THINKING_LEVELS)}")
        return
    runtime.set_thinking_level(level)
    print(f"思考等级已切换: {level}")

def print_status(runtime: "ConversationRuntime") -> None:
    """打印当前会话的用量收据（与权限面板同一套对齐风格）。"""
    usage = runtime.usage().cumulative_usage()

    input_tokens  = usage.input_tokens
    output_tokens = usage.output_tokens
    total_tokens  = usage.total_tokens()

    turns = runtime.usage().turns()
    messages = len(runtime.session().messages)

    print(c_dim(SEPARATOR))
    print("会话状态".center(40))
    print(c_dim(SEPARATOR))
    print(field_line("输入", f"{input_tokens:,} tokens"))
    print(field_line("输出", f"{output_tokens:,} tokens"))
    print(field_line("合计", f"{total_tokens:,} tokens"))
    print(c_dim(SEPARATOR))
    print(field_line("轮数", f"{turns:,}"))
    print(field_line("消息数", f"{messages:,}"))
    print(field_line("权限模式", runtime.permission_mode().as_str()))
    print(field_line("思考等级", runtime.thinking_level()))
    latest = runtime.usage().current_turn_usage()
    print(field_line("最近一轮", f"{latest.input_tokens:,} 入 / {latest.output_tokens:,} 出"))
    print(c_dim(SEPARATOR))

def do_compact(runtime: ConversationRuntime):
    try:
        msg = runtime.compact()
        print(msg)
    except Exception as e:
        print(c_red(f"✗ compact 失败: {e}"))



# --- 会话命名 (prompt_dev/session_title.md) ---
TITLE_MAX_LEN = 50          # /rename 名字上限
AUTO_TITLE_LEN = 30         # 自动命名截取长度
UNTITLED = "(未命名)"


def derive_title(text: str) -> str:
    """自动命名: 去除换行后的前 30 字符; 全空白返回空串（跳过命名）。"""
    collapsed = " ".join(text.split())
    return collapsed[:AUTO_TITLE_LEN]


def maybe_auto_title(runtime: "ConversationRuntime", store: SessionStore,
                     session_id: str, titled: bool) -> bool:
    """首轮对话成功后自动命名（一次会话只命名一次）。

    返回新的 titled 状态。已命名 / 首条用户消息全空白 → 跳过。"""
    if titled:
        return True
    messages = runtime.session().messages
    if not messages or messages[0].role != "user":
        return titled
    first_text = "".join(
        b.text for b in messages[0].content if isinstance(b, TextContentBlock)
    )
    title = derive_title(first_text)
    if not title:
        return titled  # 空名字防护: 全空白不命名
    store.set_title(session_id, title)
    print(c_dim(f"✓ 会话已命名: {title}"))
    return True


def rename_usage() -> None:
    print(f"用法: /rename <新名字>（最长 {TITLE_MAX_LEN} 字符）")


def do_rename(runtime: "ConversationRuntime", store: SessionStore,
              session_id: str, arg: str) -> None:
    """/rename: 覆盖式改名（追加新 title 记录, 展示永远取最新）。"""
    name = arg.strip()
    if not name:
        rename_usage()
        return
    if len(name) > TITLE_MAX_LEN:
        print(c_red(f"✗ 名字过长: {len(name)} 字符（上限 {TITLE_MAX_LEN}）"))
        return
    store.set_title(session_id, name)
    print(f"会话已改名: {c_cyan(name)}")


def display_title(store: SessionStore, session_id: str) -> str:
    """展示层取名字: 没有命名记录的旧会话 → "(未命名)"。"""
    return store.get_title(session_id) or UNTITLED


def repair_interrupted_turn(session: Session) -> None:
    """中断后修补会话尾: 若 assistant 带着未答复的 tool_use, 补 error
    result——否则下一次请求(以及 resume)会因悬空 tool_use 被 API 拒绝。
    尾部是 user/tool 的序列本身合法, 不动。"""
    messages = session.messages
    if not messages or messages[-1].role != "assistant":
        return
    for block in messages[-1].content:
        if isinstance(block, ToolContentBlock):
            messages.append(Message.tool_result(
                id=block.id,
                name=block.name,
                output="(用户中断了本轮对话)",
                is_error=True,
            ))


# --- REPL ---
def run_repl(runtime: ConversationRuntime,
             prompter: PermissionPrompter,
             store: SessionStore,
             session_id: str,
             last_uuid: Optional[str]):
    setup_console()  # 幂等兜底: 直接进 REPL 的路径也保证 UTF-8 + VT
    print_banner()
    print(f"权限模式: {runtime.permission_mode().as_str()} (切换: /mode <name>)")
    print(f"会话: {session_id}  名字: {c_cyan(display_title(store, session_id))}")
    titled = store.get_title(session_id) is not None  # 自动命名只做一次

    # 恢复的会话: 提示已加载 + 回放最后一条 assistant 文本(上次聊到哪),
    # 全量历史不刷屏——与真实 Claude Code 的恢复行为一致。
    # idx_before 指向末尾而非 -1: 否则恢复后第一轮会把全部旧消息重复写盘。
    existing = runtime.session().messages
    if existing:
        print(c_dim(f"✓ 已恢复会话 {session_id}（{len(existing)} 条消息）"))
        for msg in reversed(existing):
            if msg.role != "assistant":
                continue
            texts = [b.text for b in msg.content if isinstance(b, TextContentBlock)]
            if texts:
                # 完整回放, 不截断: 恢复时就该看清上次聊到哪
                print(c_dim(indent_block(" ".join(texts))))
                break
    idx_before = len(existing) - 1
    ctrl_c_pending = False  # 连续两次 Ctrl+C 才退出, 第一次只提示
    while True:
        try:
            text = input("x-code> ").strip()
            ctrl_c_pending = False
        except KeyboardInterrupt:
            if ctrl_c_pending:
                print("bye!")
                break
            ctrl_c_pending = True
            print("\n(再按一次 Ctrl+C 退出; 对话中按一次 = 中断本轮)")
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
                mode_name = text[switch_cmd_len:].strip()
                switch_mode(runtime, mode_name)
            elif cmd == SlashCommand.THINKING:
                thinking_cmd_len = len(SlashCommand.THINKING.value) + 1
                level_name = text[thinking_cmd_len:].strip()
                switch_thinking(runtime, level_name)
            elif cmd == SlashCommand.RENAME:
                rename_cmd_len = len(SlashCommand.RENAME.value) + 1
                do_rename(runtime, store, session_id, text[rename_cmd_len:])

        else:
            # 每轮对话开始: 细分隔线；块与块之间靠各视觉块自带的空行隔开
            print(c_dim(SEPARATOR))
            summary = None
            try:
                summary = runtime.run_turn(text, prompter)
            except KeyboardInterrupt:
                # Ctrl+C 只中断本轮, 不退出 REPL; 修补悬空 tool_use 后照常落盘
                print()
                print(c_yellow("⚠ 已中断本轮对话"))
                repair_interrupted_turn(runtime.session())
            except Exception as e:
                print()
                print(c_red(f"✗ {e}"))
                continue

            # 循环层预算的收束是正常返回（不是异常），把触发原因讲给用户
            if summary is not None and summary.budget_exhausted:
                print(c_yellow("⚠ 本轮输出 token 预算已用尽，已提前收束本轮（可调大 turnTokenBudget）"))
            elif summary is not None and summary.iterations_exhausted:
                print(c_yellow(f"⚠ 已达单轮最大迭代次数（{summary.iterations} 次调用），已提前收束本轮"))

            for msg in runtime.session().messages[idx_before + 1:]:
                last_uuid = store.save_message(
                    session_id=session_id,
                    message=msg,
                    parent_uuid=last_uuid,
                )
            idx_before = len(runtime.session().messages) - 1
            titled = maybe_auto_title(runtime, store, session_id, titled)


def build_registry() -> ToolRegistry:
    """CLI 与 Web 共用的工具注册表: 四个内置工具一次注册到位。"""
    return ToolRegistry().register(name="bash", handler=bash_tool).register(
        name="powershell", handler=powershell_tool).register(
        name="read_file", handler=read_tool).register(
        name="write_file", handler=write_tool)


def start(session_store:SessionStore,session_id:str):
    load_dotenv()
    api_key = os.getenv("API_KEY")
    if api_key is None:
        print(c_red("✗ API_KEY not set!"))
        return

    registry = build_registry()

    session_load = session_store.load_session(session_id)
    session_msgs = session_load[0]
    last_uuid = session_load[1]

    config_loader = ConfigLoader(
        cwd=Path.cwd(),
        config_home=USER_DIR,   # x-code 自己的用户配置目录
    )
    system_prompt = SystemPromptBuilder().with_os(platform.system(), platform.release()).build()
    runtime_config = config_loader.load()
    api_client = ClaudeApiClient(
        api_key=str(api_key),
        model=runtime_config.model() or DEFAULT_MODEL,
        tools=TOOLS,
        thinking_level=runtime_config.thinking_level(),
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
def usage() -> None:
    """打印所有入口的用法。参数写错时也走这里。"""
    print("用法: python main.py [选项]")
    print()
    print("选项:")
    print("  (无参数)        新会话")
    print("  -c, --continue  恢复最近一次会话")
    print("  --resume <id>   恢复指定会话")
    print("  --list          列出全部会话")


def main():
    setup_console()
    session_store = SessionStore(
        storage_dir=USER_DIR / "sessions",
    )
    session_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    args = sys.argv[1:]

    # 分派原则: 先校验参数数量/取值, 再执行 — 任何分支都不可能越界取 args
    if not args:
        # 设计决定: 默认无参 = 永远开新会话（与真实 Claude Code 行为一致）
        start(session_store=session_store, session_id=session_id)
        return

    head = args[0]
    if head == "--list":
        for sid in session_store.list_sessions():
            title = display_title(session_store, sid)
            n_msgs = session_store.count_messages(sid)
            id_pad = max(len(sid), 16)  # 时间戳 id 16 位, 名字列从 18 列起
            print(f"{sid:<{id_pad}}  {c_cyan(title)}  {c_dim(f'({n_msgs} 条消息)')}")
        return

    if head in ("--continue", "-c"):
        sessions = session_store.list_sessions()
        if not sessions:
            print(c_red("✗ 没有历史会话"))
            return
        # 会话 id 是 %Y%m%d-%H%M%S 时间戳, 字典序即时间序, max() 即最近
        start(session_store=session_store, session_id=max(sessions))
        return

    if head == "--resume":
        if len(args) < 2 or not args[1].strip():
            usage()
            return
        resume_id = args[1].strip()
        if resume_id in session_store.list_sessions():
            start(session_store=session_store, session_id=resume_id)
        else:
            print(c_red(f"✗ 找不到会话: {resume_id}"))
            print("可用列表:")
            for sid in session_store.list_sessions():
                print(f"  {sid}")
        return

    usage()


if __name__ == "__main__":
    main()
