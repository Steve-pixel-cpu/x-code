# --- Slash command 解析 ---

import json
import os
import platform
import re
import sys
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from api_client import (
    ApiClient,
    make_api_client,
    normalize_protocol,
    DEFAULT_PROTOCOL,
    THINKING_LEVELS,
)
from config import (RuntimeConfig, ConfigLoader, USER_DIR, load_command_allowlist,
                    load_additional_directories, save_command_allowlist,
                    load_command_denylist, load_sensitive_paths)
from hooks import HookRunner
from models import Message, Session, TextContentBlock, ToolContentBlock
from permissions import (
    DANGER_FULL_ACCESS_MODE,
    READ_ONLY_MODE, WORKSPACE_WRITE_MODE,
    ALLOW_MODE, MODE_TO_NAME, NAME_TO_MODE,
)
from permissions import PermissionRequest, PermissionResult, PermissionMode, PermissionPolicy, PermissionDecision, \
    PermissionPrompter
from prompt import ProjectContext, SystemPromptBuilder
from runtime import ConversationRuntime
from storage import SessionStore
from tools import (ToolRegistry, bash_tool, edit_file_tool, glob_tool,
                   grep_tool, read_tool,
                   write_tool, present_plan_tool, powershell_tool,
                   task_output_tool, task_stop_tool, todo_tool,
                   web_fetch_tool, web_search_tool,
                   git_bash_unavailable_reason)
from agent_tools import AGENT_TOOL_SPECS, get_orchestrator, register_agent_tools
from browser_tools import BROWSER_TOOL_SPECS, register_browser_tools
from music import MUSIC_PLAY_SPEC, music_play_tool
from memory.tools import (MEMORY_TOOL_SPECS, memory_write_tool,
                          memory_update_tool, memory_delete_tool,
                          get_memory_store)
from mcp_client import (MCP_TOOL_PREFIX, get_mcp_manager, mcp_tool_name,
                        _safe_segment)
from skills import (SkillError, discover_skills, render_skills_section,
                    sync_skill_tools, register_skill_tools)

DEFAULT_MODEL = "glm-5.3-flash"
bash_spec = {
    "name": "bash",
    "description": (
        "Execute a shell command in the terminal and return its output. "
        "stdout is returned as-is; stderr is appended if present. "
        "Use this for listing files, running scripts, git operations, "
        "installing dependencies, and other command-line tasks. "
        "Commands are killed (whole process tree) after 'timeout' seconds "
        "(default 30, max 600), so avoid interactive commands. "
        "For long-running services (game/database servers, dev servers, "
        "watchers), set 'background': true: the command starts detached "
        "and returns a task id + log path immediately, keeping the service "
        "alive across turns; read recent output with task_output and stop "
        "it with task_stop. "
        "On Windows this runs in Git Bash (MSYS2), so standard bash/GNU "
        "syntax works: '&&' chains, pipes, grep/sed/awk are available. "
        "Prefer this tool over powershell for portability. "
        "Note: output is UTF-8; prefer bash text tools (cat/grep) over "
        "cmdlets for reading files."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "The shell command to execute, e.g. 'ls -la' or "
                    "'python script.py'. Must be non-interactive. "
                    "Use bash syntax on all platforms."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": (
                    "Seconds before the command and its children are "
                    "killed. Default 30, max 600. Use only as large as "
                    "needed (builds, installs)."
                ),
            },
            "background": {
                "type": "boolean",
                "description": (
                    "Run detached as a background task: returns a task id "
                    "and log path immediately instead of waiting. Use for "
                    "servers and other never-exiting commands; read output "
                    "with task_output, stop with task_stop."
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
        "PowerShell cmdlets. Commands are killed (whole process tree) "
        "after 'timeout' seconds (default 30, max 600), so avoid "
        "interactive commands. 'background': true is also supported "
        "(same semantics as the bash tool)."
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
            "timeout": {
                "type": "integer",
                "description": (
                    "Seconds before the command and its children are "
                    "killed. Default 30, max 600."
                ),
            },
            "background": {
                "type": "boolean",
                "description": (
                    "Run detached as a background task (see bash tool). "
                    "Read output with task_output, stop with task_stop."
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
        "or any text-based file before editing it. Files larger than a few "
        "hundred lines are truncated in the middle when read whole — for "
        "large files pass offset/limit to read a specific line range "
        "instead of shelling out to sed."
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
            "offset": {
                "type": "integer",
                "description": (
                    "1-based line number to start reading from. Use for "
                    "large files instead of reading whole and getting a "
                    "truncated middle."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    "Maximum number of lines to return, starting at offset. "
                    "Omit (with no offset) to read the whole file."
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
        "create new files or fully rewrite a file after reading it. "
        "Overwriting an existing file requires a prior read_file of the "
        "same path in this session — for targeted changes prefer the "
        "edit_file tool instead. Parent directories must already exist."
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

edit_file_spec = {
    "name": "edit_file",
    "description": (
        "Edit a file with an exact string replacement: replaces the first "
        "occurrence of old_string with new_string (all occurrences if "
        "'replace_all' is true). This is the preferred tool for targeted "
        "changes — it touches nothing outside the replaced span, unlike "
        "write_file which rewrites the whole file. old_string must be "
        "copied verbatim from the file, including indentation and "
        "whitespace; if it matches multiple places, extend it with more "
        "surrounding context or set replace_all. The file must have been "
        "read this session and must not have changed since."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path to the file to edit, e.g. 'src/main.py'. "
                    "Supports relative and absolute paths."
                ),
            },
            "old_string": {
                "type": "string",
                "description": (
                    "The exact text to replace, copied verbatim from the "
                    "file (including whitespace/indentation). Must match "
                    "exactly one place unless replace_all is true."
                ),
            },
            "new_string": {
                "type": "string",
                "description": "The replacement text.",
            },
            "replace_all": {
                "type": "boolean",
                "description": (
                    "Replace every occurrence of old_string instead of "
                    "just the first. Default false."
                ),
            },
        },
        "required": ["path", "old_string", "new_string"],
    },
}

present_plan_spec = {
    "name": "present_plan",
    "description": (
        "Present your implementation plan to the user for review and "
        "approval. In plan mode, ALWAYS present your implementation plan "
        "via this tool before attempting any changes. Call this after you "
        "have finished researching the codebase (plan mode). The plan is "
        "shown to the user as a card "
        "with approve/reject buttons; while waiting, do not make any "
        "changes. If rejected, revise the plan based on the feedback "
        "and present again."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "plan": {
                "type": "string",
                "description": (
                    "The full implementation plan in markdown, structured "
                    "with sections: '## 目标' (goal), '## 改动' (step-by-step "
                    "changes as a nested checklist, file paths in backticks), "
                    "'## 验证' (how to verify), and '## 假设' (assumptions, "
                    "if any)."
                ),
            },
        },
        "required": ["plan"],
    },
}

task_output_spec = {
    "name": "task_output",
    "description": (
        "Read recent output of a background task started with the bash "
        "or powershell tool ('background': true). Returns whether the "
        "process is still running plus the last N lines of its log. "
        "Poll this instead of re-running the command to check progress."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task id returned when starting the "
                               "background task, e.g. 'bg-1'.",
            },
            "tail_lines": {
                "type": "integer",
                "description": "How many trailing log lines to return "
                               "(default 50, max 500).",
            },
        },
        "required": ["task_id"],
    },
}

task_stop_spec = {
    "name": "task_stop",
    "description": (
        "Stop a background task started with the bash or powershell "
        "tool ('background': true). Kills the whole process tree "
        "(the server and any children). Use when the service is no "
        "longer needed or must be restarted."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task id returned when starting the "
                               "background task, e.g. 'bg-1'.",
            },
        },
        "required": ["task_id"],
    },
}

grep_spec = {
    "name": "grep",
    "description": (
        "Search file contents with a regular expression (pure-Python ripgrep-"
        "like search; no shell involved). Prefer this over running `grep` via "
        "bash. Parameters: pattern (required regex), path (file or directory, "
        "default workdir), glob (filename filter like '*.py'), output_mode: "
        "'files_with_matches' (default) | 'content' (file:line: text) | "
        "'count'. Skips node_modules/.git and binary files."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regular expression to search for, e.g. r'def \\w+' or 'TODO'.",
            },
            "path": {
                "type": "string",
                "description": "File or directory to search in. Defaults to the workdir.",
            },
            "glob": {
                "type": "string",
                "description": "Optional filename filter, e.g. '*.py' or '*.ts'.",
            },
            "output_mode": {
                "type": "string",
                "enum": ["files_with_matches", "content", "count"],
                "description": (
                    "'files_with_matches' = matching file paths (default); "
                    "'content' = file:line: text; 'count' = matches per file."
                ),
            },
        },
        "required": ["pattern"],
    },
}

glob_spec = {
    "name": "glob",
    "description": (
        "List files matching a glob pattern (pure-Python, no shell). Prefer "
        "this over `find`/`ls -R` via bash. Supports recursive '**'. Returns "
        "absolute paths you can pass to read_file. Skips node_modules/.git "
        "and other noise directories."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern, e.g. '**/*.py', 'src/*.ts', '*.json'.",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in. Defaults to the workdir.",
            },
        },
        "required": ["pattern"],
    },
}

from tools import (todo_spec as _todo_spec,     # noqa: E402  (spec 与实现同源)
                   web_fetch_spec, web_search_spec)

TOOLS = [bash_spec, powershell_spec, read_file_spec, write_file_spec,
         edit_file_spec, grep_spec, glob_spec, task_output_spec, task_stop_spec,
         present_plan_spec, _todo_spec,
         web_search_spec, web_fetch_spec, MUSIC_PLAY_SPEC] + MEMORY_TOOL_SPECS \
        + BROWSER_TOOL_SPECS + AGENT_TOOL_SPECS


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


_JSON_KEY_PRIORITY = ("command", "path", "file_path", "url", "content", "plan")


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
    SKILLS = "skills"
    MEMORY = "memory"
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


# 包管理器的 run/exec/test 子命令本身不是完整语义: "uv run pytest -q"
# 若只取前 2 词得到 "uv run", 会连带放行 "uv run python 任意脚本"——
# 比用户直觉宽。这类首词取 3 词更贴近所批准的东西。
RUNNER_FIRST_WORDS = frozenset({"uv", "npm", "pnpm", "yarn", "bun", "deno"})
RUNNER_SECOND_WORDS = frozenset({"run", "exec", "test", "x"})


def command_allow_rule(tool_input: str) -> Optional[str]:
    """从 bash/powershell 入参提取前缀白名单规则: 命令前 ≤2 个词（剥
    VAR=val 前缀、去引号）; 包管理器 run/exec/test 类取 3 词——与 Web 端
    allowlistRuleOf 同口径, 服务端保存时再清洗, 授权层按 shlex 词对齐
    匹配。取不出词返回 None。"""
    try:
        params = json.loads(tool_input)
        cmd = str(params.get("command") or "")
    except Exception:
        return None
    words = [w for w in cmd.split()
             if w and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)]
    n = 2
    if (len(words) >= 3 and words[0].lower() in RUNNER_FIRST_WORDS
            and words[1].lower() in RUNNER_SECOND_WORDS):
        n = 3
    words = [w.strip("\"'") for w in words[:n]]
    return " ".join(words)[:80].strip() or None


class CliPermissionPrompter:
    """多行权限面板（黄色）。y 本次放行; bash/powershell 能提出前缀规则时
    额外给 a=总是（入全局白名单）/ s=本会话（会话级白名单）——治审批疲劳,
    同类命令不再逐条问; 其余与 Ctrl+C 一律朝安全侧拒绝。"""

    def __init__(self,
                 on_always: Optional[callable] = None,
                 on_session: Optional[callable] = None):
        # a/s 的落地回调（拿得到 runtime 与 config 的闭包, prompter 自身
        # 不持有它们）。缺省时对应选项退化为仅本次放行。
        self._on_always = on_always
        self._on_session = on_session

    def decide(self, request: PermissionRequest) -> PermissionResult:
        deny_reason = f"User denied permission to run {request.tool_name}!"
        rule = (command_allow_rule(request.input)
                if request.tool_name in ("bash", "powershell") else None)
        rememberable = rule is not None
        print()
        print(c_yellow("⚠ 需要授权"))
        print(c_yellow(field_line("工具", request.tool_name)))
        print(c_yellow(field_line(
            "权限",
            f"{request.required_mode.as_str()}（当前 {request.current_mode.as_str()}）",
        )))
        if request.detail:
            print(c_yellow(field_line("原因", request.detail)))
        print(c_yellow(field_line("内容", describe_tool_input(request.input))))
        hint = ("批准? [y]本次 / [a]总是(白名单) / [s]本会话 / [N]拒绝 "
                if rememberable else "批准? [y/N] ")
        try:
            user_input = input(c_yellow(hint))
        except KeyboardInterrupt:
            print()
            print(c_yellow("已拒绝。"))
            return PermissionResult(
                decision=PermissionDecision.DENY,
                reason=deny_reason
            )
        ans = user_input.strip().lower()
        if ans in ("y", "yes"):
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                reason= "user said yes!"
            )
        if ans in ("a", "always") and rememberable:
            if self._on_always is not None:
                try:
                    self._on_always(rule)
                    print(c_yellow(f"已加全局白名单: {rule}"))
                except Exception as e:
                    print(c_yellow(f"白名单保存失败({e}), 仅本次放行"))
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                reason="user said yes! (added to global allowlist)"
            )
        if ans in ("s", "session") and rememberable:
            if self._on_session is not None:
                try:
                    self._on_session(rule)
                    print(c_yellow(f"本会话内该前缀不再询问: {rule}"))
                except Exception as e:
                    print(c_yellow(f"会话规则写入失败({e}), 仅本次放行"))
            return PermissionResult(
                decision=PermissionDecision.ALLOW,
                reason="user said yes! (added to session allowlist)"
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

    def execute(self, tool_name: str, input: str, tool_use_id: str | None = None) -> str:
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
    for tool_name, required in TOOL_REQUIREMENTS.items():
        permission_policy.with_tool_requirement(tool_name, required)
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
    running_time = (running_time
                    .with_max_iterations(hooks_config.max_iterations())
                    .with_auto_compact_threshold(hooks_config.token_budget())
                    .with_turn_output_budget(hooks_config.turn_token_budget()))
    # 用户命令白名单: settings.json 持久规则, 启动即生效（CLI 与 Web 同源）。
    # setter 返回 None, 不能挂进上面的 builder 链尾。
    running_time.set_command_allowlist(load_command_allowlist())
    # deny 规则与用户敏感路径: 同源 settings.json, 启动即生效。
    running_time.set_command_denylist(load_command_denylist())
    running_time.set_sensitive_paths(load_sensitive_paths())
    # workspace 根初值: CLI 执行层不传 workdir（Popen 继承进程 cwd）,
    # 根 = 当前目录 + 全局附加目录。Web 端组装后按会话 workdir 重设。
    running_time.set_workspace_roots(
        [str(Path.cwd())] + load_additional_directories())
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
    name = mode_name.strip().lower()
    if name == "read-only":
        name = "plan"   # 旧名兼容: 归一为 plan
    mode = NAME_TO_MODE.get(name)
    if mode is None:
        print(c_red(f"✗ 未知模式: {mode_name}"))
        print(f"可选: {' | '.join(MODE_TO_NAME.values())}（read-only 是 plan 的旧名）")
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
    print(field_line("缓存写入", f"{usage.cache_creation_input_tokens:,} tokens"))
    print(field_line("缓存读取", f"{usage.cache_read_input_tokens:,} tokens"))
    print(field_line("合计", f"{total_tokens:,} tokens"))
    print(c_dim(SEPARATOR))
    print(field_line("轮数", f"{turns:,}"))
    print(field_line("消息数", f"{messages:,}"))
    print(field_line("权限模式", runtime.permission_mode().as_str()))
    print(field_line("思考等级", runtime.thinking_level()))
    latest = runtime.usage().current_turn_usage()
    print(field_line(
        "最近一轮",
        f"{latest.input_tokens:,} 入 / {latest.output_tokens:,} 出"
        f"（缓存读 {latest.cache_read_input_tokens:,}）"))
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


def notify_done() -> None:
    """一轮对话正常结束的终端提醒: Windows 弹系统提示音, 其他平台响终端铃。
    尽力而为, 任何失败静默吞掉——提醒不该把 REPL 搞挂。"""
    try:
        if sys.platform == "win32":
            import winsound
            # MB_ICONASTERISK = 系统星号提示音, 比 Beep 的蜂鸣柔和
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        else:
            sys.stdout.write("\a")
            sys.stdout.flush()
    except Exception:
        pass


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


def do_memory(arg: str) -> None:
    """/memory: 列出全部记忆; /memory add <内容>: 手动添加（source=user,
    永不被自动淘汰）; /memory rm <id前缀>: 前缀匹配删除（歧义列候选）;
    /memory clear: 清空全部（输入 y 确认）。"""
    from memory.tools import get_memory_store as _store
    store = _store()
    arg = (arg or "").strip()
    action, _, rest = arg.partition(" ")
    rest = rest.strip()

    if action in ("", "ls", "list"):
        mems = store.list_memories()
        if not mems:
            print(c_dim("（还没有记忆。对话中让它记, 或 /memory add <内容> 手动添加）"))
            return
        for m in mems:
            print(f"[{m['id'][4:12]}] [{m['category']}] {m['content']} "
                  f"{c_dim(f'({m['created_at']}, hits={m['hits']})')}")
        return

    if action == "add":
        if not rest:
            print(c_red("✗ 用法: /memory add <内容>"))
            return
        m = store.add(content=rest, category="fact", source="user")
        print(f"✓ 已添加（id={m['id']}）— source=user, 不会被自动淘汰")
        return

    if action == "rm":
        if not rest:
            print(c_red("✗ 用法: /memory rm <id前缀>（先 /memory 查看 id）"))
            return
        hits = [m for m in store.list_memories()
                if m["id"][4:].startswith(rest)]
        if not hits:
            print(c_red(f"✗ 没有匹配前缀 {rest!r} 的记忆"))
            return
        if len(hits) > 1:
            print(c_red(f"✗ 前缀 {rest!r} 匹配到 {len(hits)} 条, 更精确一些:"))
            for m in hits:
                print(f"  [{m['id'][4:12]}] {m['content']}")
            return
        store.remove(hits[0]["id"])
        print(f"✓ 已删除（id={hits[0]['id']}）: {hits[0]['content']}")
        return

    if action == "clear":
        if not store.list_memories():
            print(c_dim("（记忆已为空）"))
            return
        print(c_yellow("确认清空全部记忆？（已淘汰的归档也会一并清掉）[y/N]"))
        if input().strip().lower() in ("y", "yes"):
            store.clear()
            print("✓ 已清空")
        else:
            print(c_dim("已取消"))
        return

    print(c_red(f"✗ 未知子命令: {action}"))
    print("用法: /memory [add <内容> | rm <id前缀> | clear]")


def do_skills(runtime: "ConversationRuntime", name_arg: str) -> None:
    """/skills: 列出已装技能; /skills <name>: 预览该技能的 SKILL.md 开头。
    列表数据从系统提示词无法反解, 这里按同一套发现规则现扫——CLI 会话
    期间装了新技能, 重启会话或直接看这里都能看到最新状态。"""
    from skills import discover_skills as _discover, SKILL_FILE
    name_arg = (name_arg or "").strip()
    skills = _discover(Path.cwd(), USER_DIR)
    if not skills:
        print(c_dim("没有已安装的技能。把 SKILL.md 放进 "
                    "~/.x-code/skills/<name>/ 或 <项目>/.claude/skills/<name>/, "
                    "或用 Web 设置页从 GitHub 仓库安装。"))
        return
    if name_arg:
        skill = next((s for s in skills if s.name == name_arg), None)
        if skill is None:
            print(c_red(f"✗ 找不到技能: {name_arg}"))
            print("可用列表:")
            for s in skills:
                print(f"  {s.name}")
            return
        print(f"{skill.name}  ({skill.source})  {c_dim(str(skill.dir))}")
        print(c_dim(f"描述: {skill.description or '(无)'}"))
        print()
        skill_file = skill.dir / SKILL_FILE
        try:
            lines = skill_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            print(c_red(f"✗ 无法读取: {skill_file}"))
            return
        preview = lines[:40]
        print("\n".join(preview))
        if len(lines) > len(preview):
            print(c_dim(f"…（共 {len(lines)} 行, 完整内容请看 {skill_file}）"))
        return
    print(f"已安装 {len(skills)} 个技能:")
    for s in skills:
        print(f"  {s.name:<24} {c_cyan(s.source):<20} "
              f"{c_dim(s.description[:60] or '(无描述)')}")


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
                # 未知 /词 先当技能命令试: /brainstorming … 命中即展开跑本轮,
                # 未命中维持原来的帮助输出（CLI 与 Web 端同一套匹配规则）
                skill = None
                try:
                    from skills import discover_skills as _ds, \
                        match_skill_command as _match, \
                        expand_skill_command as _expand
                    skill = _match(text, _ds(Path.cwd(), USER_DIR))
                except Exception:
                    skill = None
                if skill is not None:
                    print(c_dim(f"→ 技能命令: /{skill.name}"))
                    try:
                        summary = runtime.run_turn(
                            _expand(skill, text), prompter)
                        notify_done()
                    except KeyboardInterrupt:
                        print()
                        print(c_yellow("⚠ 已中断本轮对话"))
                        repair_interrupted_turn(runtime.session())
                    except Exception as e:
                        print()
                        print(c_red(f"✗ {e}"))
                        continue
                    for msg in runtime.session().messages[idx_before + 1:]:
                        last_uuid = store.save_message(
                            session_id=session_id,
                            message=msg,
                            parent_uuid=last_uuid,
                        )
                    idx_before = len(runtime.session().messages) - 1
                    titled = maybe_auto_title(runtime, store, session_id,
                                              titled)
                    continue
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
            elif cmd == SlashCommand.SKILLS:
                skills_cmd_len = len(SlashCommand.SKILLS.value) + 1
                do_skills(runtime, text[skills_cmd_len:])
            elif cmd == SlashCommand.MEMORY:
                memory_cmd_len = len(SlashCommand.MEMORY.value) + 1
                do_memory(text[memory_cmd_len:])

        else:
            # 每轮对话开始: 细分隔线；块与块之间靠各视觉块自带的空行隔开
            print(c_dim(SEPARATOR))
            summary = None
            try:
                summary = runtime.run_turn(text, prompter)
                notify_done()   # 正常返回即干完: 提示音/终端铃, 异常路径不响
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


# 工具 -> 权限要求档位。分级原则: 只读不落盘 = READ_ONLY; 本地写 =
# WORKSPACE_WRITE; 可执行任意命令/触达共享系统 = DANGER_FULL_ACCESS(默认,
# 不必登记——bash/powershell 走 required_mode_for 的 fallback)。
# 消费点: build_runtime 的 PermissionPolicy.with_tool_requirement。
TOOL_REQUIREMENTS = {
    "read_file": READ_ONLY_MODE,        # 读文件无副作用
    "agent_status": READ_ONLY_MODE,     # 看 subagent 状态
    "agent_list": READ_ONLY_MODE,       # 列 subagent
    "task_output": READ_ONLY_MODE,      # 读后台任务日志, 只读
    "grep": READ_ONLY_MODE,             # 纯只读搜索
    "glob": READ_ONLY_MODE,             # 纯只读列文件
    "todo": READ_ONLY_MODE,             # 会话任务清单（只写 ~/.x-code/todos/ 元数据）
    "web_search": READ_ONLY_MODE,       # 免 key 网页搜索, 纯只读
    "web_fetch": READ_ONLY_MODE,        # 抓 URL 提取正文, 不落盘
    "skill_read": READ_ONLY_MODE,       # 读已装技能目录内文件, 只读且限技能目录
    "browser_navigate": READ_ONLY_MODE,  # 打开页面（不写本地, 副作用在被测站）
    "browser_snapshot": READ_ONLY_MODE,  # 读页面结构/文本
    "browser_console": READ_ONLY_MODE,   # 读 console/JS 报错
    "browser_click": WORKSPACE_WRITE_MODE,     # 改被测系统状态（登录/下单）
    "browser_type": WORKSPACE_WRITE_MODE,      # 填表单
    "browser_press": WORKSPACE_WRITE_MODE,     # 键盘操作可触发表单提交
    "browser_select": WORKSPACE_WRITE_MODE,    # 改下拉选中值
    "browser_screenshot": WORKSPACE_WRITE_MODE,  # 截图 PNG 落盘用户目录
    "write_file": WORKSPACE_WRITE_MODE, # 落盘文件（本地写）
    "edit_file": WORKSPACE_WRITE_MODE,  # 局部编辑文件（本地写）
    "agent_tool": WORKSPACE_WRITE_MODE, # 派生 subagent（写 agents 状态目录）
    # present_plan 走 WORKSPACE_WRITE 档: plan 模式下它触发"可升级弹问"
    # （Web 端渲染成计划卡）, 其余模式下直接放行
    "present_plan": WORKSPACE_WRITE_MODE,
    # 聊天点歌: 上游搜索纯只读, 播放动作在前端电台（副作用不出本机 UI）,
    # 与 browser_navigate 同一档位——只读模式也能点歌
    "music_play": READ_ONLY_MODE,
    # 记忆三件套: 只写 ~/.x-code/memory.json 本机数据, 半自动可见（写入
    # 反馈经工具结果回传）, 与 todo 同档免审批——"顺手记一下"不该打断对话
    "memory_write": READ_ONLY_MODE,
    "memory_update": READ_ONLY_MODE,
    "memory_delete": READ_ONLY_MODE,
}


def build_registry(mcp_servers: Optional[list] = None,
                   skills: Optional[list] = None) -> ToolRegistry:
    """CLI 与 Web 共用的工具注册表: 内置工具 + 后台任务两件套 + 多 agent
    三件套 + browser + MCP 外部工具一次注册到位。

    mcp_servers 非空时: 连接配置里的 MCP 服务器, 注册其工具(handler 走
    mcp__ 前缀), 并把规格同步进 TOOLS(api_client 与 multi_agent 持有同一
    列表对象, 原地修改即全链路生效)。连接失败降级为 failed 状态(查
    get_mcp_manager().status()), registry 永远可用, 不挡启动。"""
    registry = ToolRegistry().register(name="bash", handler=bash_tool).register(
        name="powershell", handler=powershell_tool).register(
        name="read_file", handler=read_tool).register(
        name="write_file", handler=write_tool).register(
        name="edit_file", handler=edit_file_tool).register(
        name="task_output", handler=task_output_tool).register(
        name="task_stop", handler=task_stop_tool).register(
        name="present_plan", handler=present_plan_tool).register(
        name="todo", handler=todo_tool).register(
        name="grep", handler=grep_tool).register(
        name="glob", handler=glob_tool).register(
        name="web_search", handler=web_search_tool).register(
        name="web_fetch", handler=web_fetch_tool).register(
        name="music_play", handler=music_play_tool).register(
        name="memory_write", handler=memory_write_tool).register(
        name="memory_update", handler=memory_update_tool).register(
        name="memory_delete", handler=memory_delete_tool)
    registry = register_agent_tools(registry)
    registry = register_browser_tools(registry)

    if mcp_servers:
        _attach_mcp_tools(registry, mcp_servers)
    # skills 的 skill_read: 只读工具, MCP 之后挂（名字冲突时技能让位）
    register_skill_tools(registry, skills or [])
    sync_skill_tools(TOOLS, skills or [])
    return registry


def _attach_mcp_tools(registry: ToolRegistry, mcp_servers: list) -> None:
    """连接 MCP 服务器并把工具挂进 registry + TOOLS。

    注册前先清掉 mcp__ 前缀旧条目(热重载换绑不换 registry 对象——Web 端
    runtime 持有同一 registry 引用)。连接状态查 get_mcp_manager().status()。

    ⚠ 调用方注意: Web 端的 registry 是 EmittingToolRegistry(壳), execute
    委托 _inner。**必须传真正执行执行的 registry**(server.py 传
    registry._inner)——挂到壳上 handler 永远不会被调到, 而 _inner 里残留
    的旧 handler 闭包引用已被 close 的旧连接, 调用即 "is not connected"。"""
    manager = get_mcp_manager()
    statuses = manager.connect_all(mcp_servers)

    # 换绑: 旧 mcp 工具先注销, 已连接的重新挂 handler
    for st in statuses:
        for t in list(registry._handlers):
            if t.startswith(MCP_TOOL_PREFIX):
                registry.unregister(t)
    for conn in manager.connected():
        server_seg = _safe_segment(conn.server.name)
        for t in conn.list_tools():
            handler = manager.handler_for(
                mcp_tool_name(conn.server.name, t["name"]))
            if handler is not None:
                registry.register(
                    name=mcp_tool_name(conn.server.name, t["name"]),
                    handler=handler)
    manager.sync_tools_list(TOOLS)


def mcp_status_lines() -> list[str]:
    """MCP 连接状态的启动摘要行（CLI 打印用; Web 端走 /api/mcp/status）。"""
    lines = []
    for st in get_mcp_manager().status():
        if st["status"] == "connected":
            lines.append(c_dim(f"  ✓ MCP {st['name']} ({st['transport']}): "
                               f"{len(st['tools'])} tools"))
        else:
            lines.append(c_yellow(f"  ✗ MCP {st['name']} ({st['transport']}): "
                                  f"{st['error']}"))
    return lines


def start(session_store:SessionStore,session_id:str):
    load_dotenv()
    api_key = os.getenv("API_KEY")
    if api_key is None:
        print(c_red("✗ API_KEY not set!"))
        return

    # 启动对账: 上次进程死亡遗留的 running 孤儿标记为 failed
    # （reconcile 假设此刻本进程尚无 running worker, 只能在启动时调一次）
    n = get_orchestrator().reconcile_orphans()
    if n:
        print(c_dim(f"启动对账: {n} 个上次遗留的 running agent 已标记为 failed"))

    # 配置先于工具装配: mcpServers 决定 build_registry 连哪些服务器
    config_loader = ConfigLoader(
        cwd=Path.cwd(),
        config_home=USER_DIR,   # x-code 自己的用户配置目录
    )
    runtime_config = config_loader.load()
    # 技能发现: 项目级覆盖用户级, 解析失败降级为警告行不挡启动
    skill_warnings: list[str] = []
    skills = discover_skills(Path.cwd(), USER_DIR,
                             on_error=lambda msg: skill_warnings.append(msg))
    for w in skill_warnings:
        print(c_yellow(f"  ⚠ {w}"))
    registry = build_registry(mcp_servers=runtime_config.mcp_servers(),
                              skills=skills)
    for line in mcp_status_lines():
        print(line)
    if skills:
        print(c_dim(f"  ✓ Skills: {', '.join(s.name for s in skills)}"))

    session_load = session_store.load_session(session_id)
    session_msgs = session_load[0]
    last_uuid = session_load[1]
    # 项目上下文（cwd/日期/CLAUDE.md）注入系统提示——没有它模型看到
    # "Working directory: unknown", 只能靠 pwd && ls 乱摸探路
    system_prompt = (
        SystemPromptBuilder()
        .with_os(platform.system(), platform.release())
        .with_project_context(ProjectContext.discover(
            Path.cwd(), datetime.now().strftime("%Y-%m-%d")))
        .build()
    )
    # 记忆 section 挂动态边界之后（memory.inject 渲染, 无记忆返回 None
    # 整段省略）; 渲染成功即 touch_hits —— hits 是遗忘淘汰的主权重
    from memory.inject import render_memories
    memory_section = render_memories(get_memory_store())
    if memory_section:
        system_prompt.append(memory_section)
    # 技能清单挂在追加段（动态边界之后）: 只进 name+description, 静态
    # 前缀不变, prompt caching 不受影响; 无技能时是空串, builder 会跳过
    if skills:
        skills_section = render_skills_section(skills)
        if skills_section:
            system_prompt.append(skills_section)
    # CLI 侧协议选择: XCODE_PROTOCOL 环境变量（anthropic 默认; openai 兼容
    # 端点可直接本地起 CLI 用）。非法值回退 anthropic, 不挡启动。
    try:
        cli_protocol = normalize_protocol(os.getenv("XCODE_PROTOCOL"))
    except ValueError:
        cli_protocol = DEFAULT_PROTOCOL
    api_client = make_api_client(
        cli_protocol,
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
    prompter = CliPermissionPrompter(
        # a=总是: 规则入全局白名单（settings.json）并热更当前 runtime;
        # s=本会话: 只写 runtime 的会话级规则, 不落盘。
        on_always=lambda rule: runtime.set_command_allowlist(
            save_command_allowlist(load_command_allowlist() + [rule])),
        on_session=lambda rule: runtime.add_session_allow_rule(rule),
    )
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
    # 命令执行器依赖 Git Bash: 没有就拒绝启动（说明里带下载入口）
    reason = git_bash_unavailable_reason()
    if reason:
        print(c_red("✗ " + reason))
        sys.exit(1)
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
