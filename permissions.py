
import json
import re
import shlex
from enum import IntEnum, Enum
from pathlib import Path
from typing import Protocol, Dict, Optional

from pydantic import BaseModel
from typing import Self

"""权限模式层级 — 源码 permissions.rs:4-10

  从最严格到最宽松:
  - ReadOnly: 只能读，不能写任何东西
  - WorkspaceWrite: 可以写工作目录内的文件
  - DangerFullAccess: 可以做任何事（包括 rm -rf /）
  - Prompt: 总是询问用户
  - Allow: 跳过所有检查（最危险）

  为什么 Prompt 比 DangerFullAccess 更"高"？
  因为 Prompt 模式的意思不是"更有权限"，而是
  "这个模式下，需要升级的操作会触发用户提示"。
  在源码中 Prompt 模式会拦截所有需要确认的操作。
  """
class PermissionMode(IntEnum):
    PLAN = 1
    WORKSPACE_WRITE = 2
    DANGER_FULL_ACCESS = 3
    PROMPT = 4
    ALLOW = 5

    def as_str(self) -> str:
        return {
            self.PLAN: "plan",
            self.WORKSPACE_WRITE: "workspace-write",
            self.DANGER_FULL_ACCESS: "danger-full-access",
            self.PROMPT: "prompt",
            self.ALLOW: "allow",
        }[self]

# --- 模式名 <-> 枚举: /mode 命令的参数解析与显示用 ---
PLAN_MODE = PermissionMode.PLAN
# 兼容别名: 旧代码/旧配置里的只读模式 = 计划模式
READ_ONLY_MODE = PLAN_MODE
WORKSPACE_WRITE_MODE = PermissionMode.WORKSPACE_WRITE
DANGER_FULL_ACCESS_MODE = PermissionMode.DANGER_FULL_ACCESS
PROMPT_MODE = PermissionMode.PROMPT
ALLOW_MODE = PermissionMode.ALLOW

MODE_TO_NAME = {
    PLAN_MODE: "plan",
    WORKSPACE_WRITE_MODE: "workspace-write",
    DANGER_FULL_ACCESS_MODE: "danger-full-access",
    PROMPT_MODE: "prompt",
    ALLOW_MODE: "allow",
}

NAME_TO_MODE = {name: mode for mode, name in MODE_TO_NAME.items()}

class PermissionDecision(Enum):
    ALLOW = "allow"
    DENY = "deny"

class PermissionResult(BaseModel):
    decision: PermissionDecision
    reason: str


class PermissionRequest(BaseModel):
    tool_name: str
    input: str
    current_mode: PermissionMode
    required_mode: PermissionMode
    # 镜像方（如 Web 端）配对工具卡用: 授权询问/拒绝时知道结果该落到哪张卡
    tool_use_id: Optional[str] = None

# Prompter 接口 — 用 Protocol 不用 ABC
# Protocol 不需要继承，只要有 decide() 方法就行（鸭子类型）
class PermissionPrompter(Protocol):

    def decide(self, request: PermissionRequest) -> PermissionResult:
        ...


# ============================================================================
# shell 只读判定。权限层与 runtime 的重复只读护栏共用同一套判定:
# 权限层用它放行只读探查（plan/workspace-write 下 pwd/ls/tail/git log
# 不再被硬拒/弹问）; 护栏用它决定 bash 是否推进变异序号。
# 判定必须保守: 白名单 + 危险构造一票否决, 拿不准一律视为可能变异
# （权限层误判"只读"的代价是放行了一条写命令——所以白名单刻意排除
# 解释器(python/node/awk/sed 可执行任意逻辑)、网络(curl/wget)、
# 归档与构建(tar/make 有写入面)）。
# ============================================================================

MUTATING_SHELL_TOOLS = frozenset({"bash", "powershell"})

# bash 白名单: 文件系统/系统信息 + 文本检索处理（管道常客）。
READONLY_SHELL_COMMANDS = frozenset({
    "ls", "pwd", "cat", "head", "tail", "wc", "file", "stat", "du", "df",
    "find", "tree", "which", "where", "whereis", "type", "realpath",
    "readlink", "basename", "dirname", "env", "printenv", "id", "whoami",
    "hostname", "uname", "date", "sleep",
    "grep", "egrep", "fgrep", "rg", "strings", "cut", "uniq", "tr", "jq",
    "diff", "cmp", "comm", "nl", "tac", "rev", "fold", "fmt", "xxd", "od",
    "md5sum", "sha1sum", "sha256sum", "cksum",
})

# git 只读子命令。branch/tag/remote 虽可创建引用, 但不改工作树文件内容;
# checkout/switch/reset/clean/stash(pop) 会动文件, 不在名单。
GIT_READONLY_SUBCOMMANDS = frozenset({
    "log", "show", "diff", "status", "blame", "rev-parse", "ls-files",
    "describe", "shortlog", "reflog", "branch", "tag", "remote", "grep",
    "ls-tree", "cat-file", "worktree", "stash",
})

# powershell 白名单: Get-* 惯例只读 + 少数纯计算 cmdlet。别名(ls/cat 等)
# 落到 bash 名单里天然覆盖。
PS_READONLY_CMDLETS = frozenset({
    "test-path", "get-item", "get-childitem", "get-content", "get-date",
    "get-location", "get-command", "get-help", "get-member", "get-process",
    "get-service", "get-filehash", "get-psdrive", "get-alias", "get-random",
    "measure-object", "measure-command", "select-object", "sort-object",
    "out-string", "write-output", "write-host", "select-string",
    "compare-object", "split-path", "resolve-path",
})

_FIND_MUTATING_ACTIONS = frozenset({
    "-delete", "-exec", "-execdir", "-ok", "-okdir",
    "-fprint", "-fprintf", "-fls",
})


def _split_shell_segments(cmd: str) -> list:
    """按 && || ; | 换行切成命令段——引号内的分隔符不算（"a|b" 是模式）。"""
    segs, buf, quote, i = [], [], None, 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            buf.append(ch)
        elif cmd[i:i + 2] in ("&&", "||"):
            segs.append("".join(buf))
            buf = []
            i += 1
        elif ch in ";|\n":
            segs.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    segs.append("".join(buf))
    return [s.strip() for s in segs if s.strip()]


def _first_word(tokens: list) -> str:
    """剥掉前缀环境变量赋值（VAR=val cmd ...）后的首个命令词（取 basename）。"""
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return ""
    return Path(tokens[0]).name.lower()


def _segment_is_read_only(seg: str, powershell: bool) -> bool:
    # 命令替换/进程替换可内嵌任意命令: 一票否决
    if "$(" in seg or "`" in seg or "<(" in seg:
        return False
    # 重定向清理: fd 拷贝(2>&1)与 /dev/null 弃置无害; 之余还有 > 就是写文件
    cleaned = re.sub(r"\d?[<>]&\d", "", seg)
    cleaned = re.sub(r"\d*>>?\s*/dev/null", "", cleaned)
    if ">" in cleaned:
        return False
    try:
        tokens = shlex.split(cleaned, posix=True)
    except ValueError:
        return False
    word = _first_word(tokens)
    if not word or word == "sudo":
        return False
    if word == "cd":            # cd 不改文件内容; 相对读取按 workdir 解析
        return True
    if powershell:
        return word in PS_READONLY_CMDLETS
    if word == "git":
        return _git_read_only(tokens[1:])
    if word == "find":
        return not any(t.lower() in _FIND_MUTATING_ACTIONS for t in tokens[1:])
    if word == "sort":          # sort -o 写文件; 其余用法只读
        return not any(t.lower().startswith("-o") for t in tokens[1:])
    return word in READONLY_SHELL_COMMANDS


def _git_read_only(rest: list) -> bool:
    if not rest:
        return False
    sub = rest[0].lower()
    if sub == "stash":          # 只有 list/show/stat 只读, push/pop/drop 动树
        return len(rest) > 1 and rest[1].lower() in ("list", "show", "stat")
    return sub in GIT_READONLY_SUBCOMMANDS


def shell_command_is_read_only(tool_name: str, tool_input: str) -> bool:
    """bash/powershell 调用是否确定性只读。解析失败/空命令/任何拿不准的
    构造都返回 False（视为可能变异, 维持原权限档位——保守方向兜底）。"""
    try:
        params = json.loads(tool_input)
        cmd = str(params.get("command") or "")
    except Exception:
        return False
    if not cmd.strip():
        return False
    powershell = tool_name == "powershell"
    return all(_segment_is_read_only(s, powershell)
               for s in _split_shell_segments(cmd))


class PermissionPolicy:
    def __init__(self, active_mode: PermissionMode):
        self._active_mode = active_mode
        self._tool_requirements: Dict[str, PermissionMode] = {}

    def with_tool_requirement(self,tool_name: str, required_mode: PermissionMode) -> Self:
        self._tool_requirements[tool_name] = required_mode
        return self

    def required_mode_for(self, tool_name: str) -> PermissionMode:
        return self._tool_requirements.get(
            tool_name, PermissionMode.DANGER_FULL_ACCESS
        )

    @property
    def active_mode(self) -> PermissionMode:
        return self._active_mode

    def set_mode(self, mode: PermissionMode) -> Self:
        """切换当前权限模式（运行时可随时调用，如 /mode 命令）。"""
        self._active_mode = mode
        return self

    def authorize(self, tool_name: str, input: str, prompter: Optional[PermissionPrompter] = None,
                  tool_use_id: Optional[str] = None) -> PermissionResult:
        current = self.active_mode
        required = self.required_mode_for(tool_name)

        # 只读 shell 白名单: bash/powershell 未显式登记档位（走 DANGER
        # fallback）时, 命令经保守判定确为只读则按最低档评估——
        # plan/workspace-write 下 pwd/ls/tail/git log 等探查直接放行,
        # 不再"每条探查命令多烧一轮拒绝+重思考"。判定拿不准即维持原档。
        if (required == PermissionMode.DANGER_FULL_ACCESS
                and tool_name in MUTATING_SHELL_TOOLS
                and shell_command_is_read_only(tool_name, input)):
            required = PLAN_MODE   # == READ_ONLY_MODE(1): 数值比较即放行

        # 快速路径: Allow 模式跳过一切; 其余模式仅在"当前权限足够"时放行。
        # PROMPT(4) 数值上 >= 大多数 required, 但它的语义是"每次都问",
        # 不是"权限更高"——必须赶在 >= 比较之前拦截, 否则 prompt 模式
        # 形同虚设(所有工具默认 required=DANGER_FULL_ACCESS < 4, 全被放行)。
        if current == PermissionMode.ALLOW:
            return PermissionResult(decision= PermissionDecision.ALLOW, reason= "")
        if current == PermissionMode.PROMPT:
            request = PermissionRequest(tool_name = tool_name,
                                        input = input,
                                        current_mode= current,
                                        required_mode = required,
                                        tool_use_id = tool_use_id )
            if prompter is not None:
                return prompter.decide(request)
            return PermissionResult(decision= PermissionDecision.DENY,
                                    reason= f"tool '{tool_name}' requires approval "
                                    f"(prompt mode) but no prompter is available")
        if current >= required:
            return PermissionResult(decision= PermissionDecision.ALLOW, reason= "")

        request = PermissionRequest(tool_name = tool_name,
                                    input = input,
                                    current_mode= current,
                                    required_mode = required,
                                    tool_use_id = tool_use_id )


        # "可升级弹问"分支（相邻档位）: 当前档差一档且目标可议时交给
        # prompter 裁决——workspace-write→DANGER(危险命令单次放行) 与
        # plan→WORKSPACE_WRITE(present_plan 计划审批/write_file 单次放行)。
        # 批准 present_plan 的同时把模式升级为 workspace-write 由调用方
        # （server 的 on_plan_approved 回调 / CLI 的 /mode）负责, 授权层
        # 只管这一次的决定。
        prompter_decides = (
            prompter is not None
            and (current == PermissionMode.PLAN
                 and required == PermissionMode.WORKSPACE_WRITE)
        ) or (
            prompter is not None
            and current == PermissionMode.WORKSPACE_WRITE
            and required == PermissionMode.DANGER_FULL_ACCESS
        )
        if prompter_decides:
            return prompter.decide(request)
        if (current == PermissionMode.PLAN
                and required == PermissionMode.WORKSPACE_WRITE):
            return PermissionResult(decision= PermissionDecision.DENY,
                                    reason= f"tool '{tool_name}' requires approval to escalate "
                                    f"from {current.as_str()} to {required.as_str()}")
        if current == PermissionMode.WORKSPACE_WRITE and required == PermissionMode.DANGER_FULL_ACCESS:
            return PermissionResult(decision= PermissionDecision.DENY,
                                    reason= f"tool '{tool_name}' requires approval to escalate "
                                    f"from {current.as_str()} to {required.as_str()}")

        # 其他情况: 权限不足，直接拒绝
        # 计划模式下(典型: bash 默认 DANGER 档, PLAN→DANGER 跨两档)附带
        # 教学指引——拒绝本身是对模型的一次纠正, 否则它不知道自己在计划
        # 模式, 只会反复换命令撞墙
        plan_hint = (" Plan mode is active: do not execute or modify anything. "
                     "Research with read_file, then call present_plan with "
                     "your implementation plan.")
        # shell 拒绝附带队内替代方案: 只读查询有专用工具且永不触发权限,
        # 给出 redirect 避免模型换个命令继续撞墙
        shell_redirect = (
            " If you only need information (file contents, search, "
            "directory listing), use the dedicated read_file/grep/glob "
            "tools instead of shell — they never trigger permission checks."
        ) if tool_name in MUTATING_SHELL_TOOLS else ""
        return PermissionResult(
                    decision = PermissionDecision.DENY,
                    reason = f"tool '{tool_name}' requires {required.as_str()} " 
                    f"permission; current mode is {current.as_str()}" + (plan_hint if current == PermissionMode.PLAN else "") + shell_redirect
                )

