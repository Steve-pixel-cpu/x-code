
import json
import os
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
    # 弹问原因补充（"敏感路径"/"写出 workspace 根"）: CLI 面板与 Web 审批卡
    # 原样展示, 用户知道这次为什么弹。空 = 常规越权升级。
    detail: Optional[str] = None
    # 结构化分级标记: "outside-write"（写出 workspace 根, 记住目录后免问）/
    # "sensitive"（敏感路径, 任何记忆机制都不豁免, 每次都问）。
    # Web 审批卡据此决定给不给"允许并记住该目录"按钮。
    escalation: Optional[str] = None

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

# powershell 白名单: Get-* 惯例只读 + 少数纯计算 cmdlet + 只读 cmdlet 的
# 常用别名。别名必须逐个收录且只收"映射到只读 cmdlet"的——rm/del/ri/mv/
# ni 这些别名映射的是 Remove/New 系, 绝不能进。
PS_READONLY_CMDLETS = frozenset({
    "test-path", "get-item", "get-childitem", "get-content", "get-date",
    "get-location", "get-command", "get-help", "get-member", "get-process",
    "get-service", "get-filehash", "get-psdrive", "get-alias", "get-random",
    "measure-object", "measure-command", "select-object", "sort-object",
    "out-string", "write-output", "write-host", "select-string",
    "compare-object", "split-path", "resolve-path",
    # 只读别名: cat/gc=Get-Content, ls/dir/gci=Get-ChildItem, gi=Get-Item,
    # pwd=Get-Location, echo=Write-Output, gm=Get-Member, gps=Get-Process,
    # sls=Select-String, measure=Measure-Object, sort=Sort-Object(PS 版
    # 不落盘, 与 bash sort -o 不同), ft/fl 纯格式化
    "ls", "dir", "gci", "cat", "gc", "gi", "pwd", "echo", "gm", "gps",
    "sls", "measure", "ft", "fl", "sort", "where", "?",
})

_FIND_MUTATING_ACTIONS = frozenset({
    "-delete", "-exec", "-execdir", "-ok", "-okdir",
    "-fprint", "-fprintf", "-fls",
})

# 纯版本/帮助查询标志: 命令行只有这些标志(不带任何位置参数)时命令
# "无事可做"——只打印版本或用法就退出。node --version / git --version /
# rg --help / cargo -V 这类探查因此免弹窗。不含位置参数是关键:
# `curl -v https://...`、`rm --version file` 这类带操作对象的构造不会命中。
_INFO_FLAGS = frozenset({
    "--version", "--help", "-h", "-v", "-?", "/?",
})


def _all_info_flags(tokens: list) -> bool:
    """所有参数都是版本/帮助标志(至少要有一个参数, 空参数列表不算)。"""
    return bool(tokens) and all(t.lower() in _INFO_FLAGS for t in tokens)


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
    if _all_info_flags(tokens[1:]):   # 纯版本/帮助查询, 命令无事可做
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


def _segment_matches_rule(seg: str, rule_words: list, powershell: bool) -> bool:
    """单段命令是否命中单条规则: 按词对齐前缀匹配（大小写不敏感）。
    规则 "git push" 命中 "git push origin main"; "git" 不命中 "gitpush"。"""
    if "$(" in seg or "`" in seg or "<(" in seg:
        return False   # 命令替换可内嵌任意命令: 一票否决
    # 重定向口径与只读判定一致: 2>&1 / >/dev/null 无害剔除, 之余有 > 即写文件
    cleaned = re.sub(r"\d?[<>]&\d", "", seg)
    cleaned = re.sub(r"\d*>>?\s*/dev/null", "", cleaned)
    if ">" in cleaned:
        return False
    try:
        tokens = shlex.split(cleaned, posix=True)
    except ValueError:
        return False
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        tokens = tokens[1:]   # 剥前缀环境变量赋值, 与规则词对齐
    if len(tokens) < len(rule_words):
        return False
    return all(t.lower() == w for t, w in zip(tokens, rule_words))


def shell_command_matches_allowlist(tool_name: str, tool_input: str,
                                    rules: list) -> Optional[str]:
    """bash/powershell 命令是否命中用户白名单。返回命中的规则原文（供
    授权理由/toast 展示）, 未命中返回 None。

    保守口径（与只读白名单同一方向）:
    - 规则按词对齐前缀匹配, 规则取分词后前缀, 命令须以相同词序开头;
    - 组合命令 && || ; | 换行切成段, **每一段**都必须命中某条规则——
      "git push && rm -rf /" 只要有一段没被覆盖就不放行;
    - 命令替换/写文件重定向的段一律不命中。
    解析失败/空命令/空规则列表返回 None。"""
    valid = [r for r in (str(x).strip() for x in (rules or [])) if r]
    if not valid:
        return None
    try:
        params = json.loads(tool_input)
        cmd = str(params.get("command") or "")
    except Exception:
        return None
    if not cmd.strip():
        return None
    powershell = tool_name == "powershell"
    compiled = []
    for r in valid:
        try:
            words = [w.lower() for w in shlex.split(r, posix=True)]
        except ValueError:
            continue
        if words:
            compiled.append((r, words))
    if not compiled:
        return None
    matched: Optional[str] = None
    for seg in _split_shell_segments(cmd):
        hit = next((r for r, words in compiled
                    if _segment_matches_rule(seg, words, powershell)), None)
        if hit is None:
            return None   # 有一段没被覆盖: 整条命令不放行
        matched = matched or hit
    return matched


def shell_command_covered_by_rules(tool_name: str, tool_input: str,
                                   sources: list) -> Optional[tuple]:
    """组合命令逐段并集判定(P1): 一段"确定性只读"或"命中某条规则"即视为
    该段被覆盖, 全段覆盖且至少一段走了规则才放行。

    背景: 只读降档要求每段都只读、规则匹配要求每段都命中, 二者是全称
    判定——`git status && uv run pytest`(已有 pytest 规则)会因 status
    段不命中规则而白弹一次。安全并集: 各自可信的段取并集仍可信。

    返回 (label, 命中规则原文) 供授权理由展示; 未覆盖/无规则/解析失败
    返回 None。纯只读命令不会走到这里(authorize 里只读降档先放行)。"""
    compiled = []   # [(label, rule_text, rule_words)]
    for label, rules in sources:
        for r in (str(x).strip() for x in (rules or [])):
            if not r:
                continue
            try:
                words = [w.lower() for w in shlex.split(r, posix=True)]
            except ValueError:
                continue
            if words:
                compiled.append((label, r, words))
    if not compiled:
        return None
    try:
        params = json.loads(tool_input)
        cmd = str(params.get("command") or "")
    except Exception:
        return None
    if not cmd.strip():
        return None
    powershell = tool_name == "powershell"
    first_hit: Optional[tuple] = None
    for seg in _split_shell_segments(cmd):
        if _segment_is_read_only(seg, powershell):
            continue   # 只读段天然可信
        seg_hit = next(((label, r) for label, r, words in compiled
                        if _segment_matches_rule(seg, words, powershell)), None)
        if seg_hit is None:
            return None   # 有一段既不只读也无规则覆盖: 整体不放行
        first_hit = first_hit or seg_hit
    return first_hit


def shell_command_hits_any_rule(tool_name: str, tool_input: str,
                                rules: list) -> Optional[str]:
    """deny 规则匹配(P3): **任一**命令段命中**任一**规则即命中——与
    allowlist 的"每段全覆盖"相反, deny 取最小命中即整体拒绝
    ("git push" deny 拦下 `git status && git push --force` 的第二段)。
    返回命中的规则原文, 未命中返回 None。"""
    valid = [r for r in (str(x).strip() for x in (rules or [])) if r]
    if not valid:
        return None
    try:
        params = json.loads(tool_input)
        cmd = str(params.get("command") or "")
    except Exception:
        return None
    if not cmd.strip():
        return None
    powershell = tool_name == "powershell"
    compiled = []
    for r in valid:
        try:
            words = [w.lower() for w in shlex.split(r, posix=True)]
        except ValueError:
            continue
        if words:
            compiled.append((r, words))
    if not compiled:
        return None
    for seg in _split_shell_segments(cmd):
        hit = next((r for r, words in compiled
                    if _segment_matches_rule(seg, words, powershell)), None)
        if hit is not None:
            return hit
    return None


# ============================================================================
# 写路径分级（应用层策略沙箱）。workspace-write 档从此名副其实:
# write_file/edit_file 的目标路径 resolve 后与 workspace 根比对——
#   inside    落在根内   → 维持 WORKSPACE_WRITE（行为不变, 零新增弹窗）
#   outside   越出所有根 → 升 DANGER, 走既有相邻档升级弹问
#   sensitive 命中敏感根 → bypass-immune: 任何模式（含 danger/allow）都
#                          强制人工裁决, 不能被模式/白名单短路
# 这是约定式闸门（policy gate）, 没有内核强制力——用户批准的 shell 命令
# 仍以完整用户权限执行。shell 侧只做"显式破坏"最小扫描（见下）。
# ============================================================================

PATH_SCOPED_WRITE_TOOLS = frozenset({"write_file", "edit_file"})

# 敏感路径（bypass-immune 清单）:
# - 路径里任何一段精确叫 ".git"（版本库元数据; .gitignore 等正常文件不中招）
# - ~/.ssh、~/.x-code（本应用自身配置——白名单/设置就在里面, 防自逃脱）
# - ~/.bashrc/.zshrc/.profile/.gitconfig（shell 配置, 写它们=持久化任意命令）
SENSITIVE_HOME_DIRS = frozenset({".ssh", ".x-code"})
SENSITIVE_HOME_FILES = frozenset({".bashrc", ".zshrc", ".profile", ".gitconfig"})


def _is_within(child: Path, parent: Path) -> bool:
    """child 是否等于 parent 或落在 parent 之内。normcase 归一——
    Windows 大小写不敏感（.GIT 与 .git 是同一目录）; POSIX 上 normcase
    是恒等变换, 保持大小写敏感语义。"""
    nc = os.path.normcase
    c, p = nc(str(child)), nc(str(parent))
    return c == p or c.startswith(p + os.sep)


def _path_is_sensitive(p: Path) -> bool:
    if any(os.path.normcase(part) == ".git" for part in p.parts):
        return True
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        return False
    return (any(_is_within(p, home / d) for d in SENSITIVE_HOME_DIRS)
            or any(_is_within(p, home / f) for f in SENSITIVE_HOME_FILES))


def _resolved_roots(workspace_roots: list) -> list:
    out = []
    for r in workspace_roots or []:
        try:
            out.append(Path(str(r)).expanduser().resolve())
        except (OSError, ValueError, RuntimeError):
            continue
    return out


def _entry_is_sensitive(entry: str, roots: list, target: Path) -> bool:
    """一条用户声明的敏感路径是否覆盖 target: 绝对路径直接比对;
    相对路径按第一个 workspace 根解析(与写目标同一基准)。条目是文件
    时按精确匹配(_is_within 的相等分支), 是目录时整棵子树命中。"""
    e = str(entry or "").strip()
    if not e:
        return False
    try:
        ep = Path(e).expanduser()
        if not ep.is_absolute() and roots:
            ep = roots[0] / ep
        return _is_within(target, ep.resolve())
    except (OSError, ValueError, RuntimeError):
        return False


def classify_write_path(path: str, workspace_roots: list,
                        sensitive_paths: Optional[list] = None) -> str:
    """写工具目标路径分级: 'inside' | 'outside' | 'sensitive'。
    相对路径按第一个 workspace 根（会话工作目录）解析, 与执行层
    tools.resolve_path 同口径; 坏路径按越界处理（升级审批兜底）。
    未配置根时退为进程 cwd 作隐式根——对齐执行层 workdir=None 时
    Popen/相对路径落到进程 cwd 的实际行为。
    sensitive_paths: 用户声明的额外敏感路径(P5, 绝对或相对根), 与
    内置敏感清单同一语义——命中即 sensitive, 任何模式强制裁决。"""
    raw = str(path or "").strip()
    if not raw:
        return "outside"
    roots = _resolved_roots(workspace_roots)
    if not roots:
        try:
            roots = [Path.cwd()]
        except (OSError, RuntimeError):
            return "outside"
    try:
        p = Path(raw).expanduser()
        if not p.is_absolute() and roots:
            p = roots[0] / p
        p = p.resolve()
    except (OSError, ValueError, RuntimeError):
        return "outside"
    if _path_is_sensitive(p):
        return "sensitive"
    if any(_entry_is_sensitive(e, roots, p) for e in (sensitive_paths or [])):
        return "sensitive"
    if any(_is_within(p, r) for r in roots):
        return "inside"
    return "outside"


def _tool_param_path(tool_input: str) -> str:
    """从工具 JSON 入参里取 path 字段（write_file/edit_file 用）。"""
    try:
        params = json.loads(tool_input)
        return str(params.get("path") or "")
    except Exception:
        return ""


# 显式破坏族: 拦"点名删除/挪动敏感路径"的 shell 命令（rm -rf .git、
# mv ~/.ssh x、echo hi > ~/.bashrc）。只认首词命中 rm/mv/cp 等的命令
# 与显式 > / >> 重定向目标——git commit/add 等正常工作流不经此判定。
DESTRUCTIVE_PATH_COMMANDS = frozenset({
    "rm", "rmdir", "rd", "mv", "cp", "tee",
    "del", "remove-item", "move", "copy-item",
})

_REDIRECT_TOKEN_RE = re.compile(r"^\d*>+")


def _redirect_targets(tokens: list) -> list:
    """shlex 分词后提取 > / >> 的目标词（含 2>err 这类带 fd 前缀的）。"""
    out = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if _REDIRECT_TOKEN_RE.match(t):
            rest = _REDIRECT_TOKEN_RE.sub("", t)
            if rest:
                out.append(rest)
            elif i + 1 < len(tokens) and not _REDIRECT_TOKEN_RE.match(tokens[i + 1]):
                out.append(tokens[i + 1])
                i += 1
        i += 1
    return out


def _candidate_is_sensitive(token: str, base: Optional[Path],
                            sensitive_paths: Optional[list] = None,
                            roots: Optional[list] = None) -> bool:
    try:
        p = Path(token).expanduser()
        if not p.is_absolute():
            p = (base or Path.cwd()) / p
        p = p.resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    if _path_is_sensitive(p):
        return True
    return any(_entry_is_sensitive(e, roots or ([base] if base else []), p)
               for e in (sensitive_paths or []))


def _segment_sensitive_hit(seg: str, base: Optional[Path],
                           sensitive_paths: Optional[list] = None,
                           roots: Optional[list] = None) -> Optional[str]:
    """单段命令里提取破坏族参数/重定向目标做敏感比对, 命中返回路径词。"""
    if "$(" in seg or "`" in seg or "<(" in seg:
        return None   # 命令替换解析不了: 静态盲区, 靠审批兜底
    cleaned = re.sub(r"\d?[<>]&\d", "", seg)       # 2>&1 类 fd 拷贝
    cleaned = re.sub(r"\d*>>?\s*/dev/null", "", cleaned)
    # 反斜杠归一为 /: shlex posix 模式会把 C:\a\b 吃成 C:ab（未加引号的
    # Windows 路径）, powershell 侧 \ 本来就是分隔符。归一只可能多报
    # （更保守）, 不会漏报敏感命中。
    cleaned = cleaned.replace("\\", "/")
    try:
        tokens = shlex.split(cleaned, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    candidates: list = []
    if _first_word(tokens) in DESTRUCTIVE_PATH_COMMANDS:
        candidates += [t for t in tokens[1:] if not t.startswith("-")]
    candidates += _redirect_targets(tokens)
    return next((c for c in candidates
                 if _candidate_is_sensitive(c, base, sensitive_paths, roots)),
                None)


def shell_command_touches_sensitive_path(tool_name: str, tool_input: str,
                                         workspace_roots: list,
                                         sensitive_paths: Optional[list] = None
                                         ) -> Optional[str]:
    """bash/powershell 命令是否"点名破坏"敏感路径（破坏族参数或显式
    重定向目标落在敏感根内）。返回命中的路径词（供审批提示）, 否则 None。

    只做显式命中的最小切片: 命令替换/解析失败一律视为未命中——shell
    路径静态分析是盲区, 拿不准的场景维持"靠审批兜底"的既有口径。"""
    try:
        params = json.loads(tool_input)
        cmd = str(params.get("command") or "")
    except Exception:
        return None
    roots = _resolved_roots(workspace_roots)
    base = roots[0] if roots else None
    for seg in _split_shell_segments(cmd):
        hit = _segment_sensitive_hit(seg, base, sensitive_paths, roots)
        if hit is not None:
            return hit
    return None


class PermissionPolicy:
    def __init__(self, active_mode: PermissionMode):
        self._active_mode = active_mode
        self._tool_requirements: Dict[str, PermissionMode] = {}
        self._command_allowlist: list = []
        self._session_allowlist: list = []
        self._command_denylist: list = []
        self._sensitive_paths: list = []
        self._workspace_roots: list = []

    def with_tool_requirement(self,tool_name: str, required_mode: PermissionMode) -> Self:
        self._tool_requirements[tool_name] = required_mode
        return self

    def set_command_allowlist(self, rules: list) -> Self:
        """用户自定义命令前缀白名单（保存的原始规则列表, 匹配时逐段对齐）。
        运行时可随时热更新（Web 设置页保存后推给所有活跃 runtime）。"""
        self._command_allowlist = list(rules or [])
        return self

    def set_command_denylist(self, rules: list) -> Self:
        """deny 规则(P3): 任一命令段命中即整体拒绝, 优先级高于一切
        allow(只读降档/白名单/会话规则)。用于表达例外——
        "git push" 已加白但 "git push --force" 永远不许跑。"""
        self._command_denylist = list(rules or [])
        return self

    def set_sensitive_paths(self, paths: list) -> Self:
        """用户声明的敏感路径(P5, 绝对或相对 workspace 根): 写入或被
        破坏族点名即 sensitive, 与内置敏感清单同一语义。热更新。"""
        self._sensitive_paths = [str(p) for p in (paths or [])
                                 if str(p).strip()]
        return self

    def set_workspace_roots(self, roots: list) -> Self:
        """workspace 根（会话工作目录 + additionalDirectories）。写工具的
        路径分级与 shell 敏感路径扫描都以它为基准, 会话改绑目录时热更新。
        第一项约定为会话工作目录——相对路径按它解析（对齐执行层）。"""
        self._workspace_roots = [str(r) for r in (roots or [])
                                 if str(r).strip()]
        return self

    def add_session_allow_rule(self, rule: str) -> Self:
        """本会话临时白名单规则（审批卡"本会话允许"/CLI 的 s 选项）:
        不落盘, 会话结束即失效。与全局规则同一匹配器, 去重追加。"""
        cleaned = " ".join(str(rule or "").split())
        if cleaned and cleaned.lower() not in {r.lower()
                                               for r in self._session_allowlist}:
            self._session_allowlist.append(cleaned)
        return self

    def required_mode_for(self, tool_name: str) -> PermissionMode:
        return self._tool_requirements.get(
            tool_name, PermissionMode.DANGER_FULL_ACCESS
        )

    def _require_sensitive_approval(self, tool_name: str, input: str,
                                    prompter: Optional[PermissionPrompter],
                                    tool_use_id: Optional[str],
                                    detail: str) -> PermissionResult:
        """敏感路径的终局闸门: 有 prompter 交人工裁决（不给出"拒绝后记忆"
        的自动放行路径——每次都问）; 没有（subagent 等）直接拒绝并说明。"""
        if prompter is None:
            return PermissionResult(
                decision=PermissionDecision.DENY,
                reason=f"tool '{tool_name}' touches a sensitive path and "
                       f"always requires explicit approval ({detail}); "
                       f"no interactive prompter is available")
        return prompter.decide(PermissionRequest(
            tool_name=tool_name, input=input,
            current_mode=self.active_mode,
            required_mode=PermissionMode.DANGER_FULL_ACCESS,
            tool_use_id=tool_use_id, detail=detail,
            escalation="sensitive"))

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
        detail: Optional[str] = None
        escalation: Optional[str] = None

        # deny 规则(P3): 先于一切判定——只读降档/白名单/会话规则/敏感
        # 路径弹问都不能越过显式拒绝。任一段命中即整体拒绝。
        if (tool_name in MUTATING_SHELL_TOOLS and self._command_denylist):
            hit = shell_command_hits_any_rule(tool_name, input,
                                              self._command_denylist)
            if hit is not None:
                return PermissionResult(
                    decision=PermissionDecision.DENY,
                    reason=f"command matches denylist rule: {hit}")

        # 只读 shell 白名单: bash/powershell 未显式登记档位（走 DANGER
        # fallback）时, 命令经保守判定确为只读则按最低档评估——
        # plan/workspace-write 下 pwd/ls/tail/git log 等探查直接放行,
        # 不再"每条探查命令多烧一轮拒绝+重思考"。判定拿不准即维持原档。
        if (required == PermissionMode.DANGER_FULL_ACCESS
                and tool_name in MUTATING_SHELL_TOOLS
                and shell_command_is_read_only(tool_name, input)):
            required = PLAN_MODE   # == READ_ONLY_MODE(1): 数值比较即放行

        # 写路径分级 + bypass-immune 敏感路径检查。
        #
        # 分级: workspace-write 档不再"全盘放行"——inside 维持原档（零新增
        # 弹窗）, outside 升 DANGER 走既有升级弹问。
        #
        # sensitive 无模式豁免: 先于 ALLOW 快速路径与一切白名单,
        # danger-full-access/allow 也不能静默改 .git、~/.bashrc、本应用
        # 自身配置（backlog 待办 4 的落地）。无 prompter（如 ALLOW 模式的
        # subagent）直接拒绝并说明, 由主会话代为执行。shell 侧同口径:
        # 破坏族点名敏感路径的命令同样不可被白名单/只读判定短路。
        if tool_name in PATH_SCOPED_WRITE_TOOLS:
            kind = classify_write_path(_tool_param_path(input),
                                       self._workspace_roots,
                                       sensitive_paths=self._sensitive_paths)
            if kind == "sensitive":
                return self._require_sensitive_approval(
                    tool_name, input, prompter, tool_use_id,
                    detail="敏感路径（.git / shell 配置 / 本应用配置）")
            if kind == "outside":
                required = PermissionMode.DANGER_FULL_ACCESS
                detail = "写出 workspace 根（工作目录 + 附加目录之外）"
                escalation = "outside-write"
        elif tool_name in MUTATING_SHELL_TOOLS:
            hit = shell_command_touches_sensitive_path(
                tool_name, input, self._workspace_roots,
                self._sensitive_paths)
            if hit is not None:
                return self._require_sensitive_approval(
                    tool_name, input, prompter, tool_use_id,
                    detail=f"命令点名操作敏感路径: {hit}")

        # 快速路径: Allow 模式跳过一切; 其余模式仅在"当前权限足够"时放行。
        # PROMPT(4) 数值上 >= 大多数 required, 但它的语义是"每次都问",
        # 不是"权限更高"——必须赶在 >= 比较之前拦截, 否则 prompt 模式
        # 形同虚设(所有工具默认 required=DANGER_FULL_ACCESS < 4, 全被放行)。
        if current == PermissionMode.ALLOW:
            return PermissionResult(decision= PermissionDecision.ALLOW, reason= "")
        # 用户命令白名单(P1 并集口径): 全局持久规则 + 本会话临时规则
        # 一起参与逐段判定——一段"确定性只读"或"命中任一规则"即覆盖,
        # 全段覆盖才放行(git status && uv run pytest 不再因 status 段
        # 无规则而白弹)。返回命中规则供理由展示。
        if tool_name in MUTATING_SHELL_TOOLS:
            hit = shell_command_covered_by_rules(tool_name, input, [
                ("user", self._command_allowlist),
                ("session", self._session_allowlist),
            ])
            if hit is not None:
                label, rule = hit
                return PermissionResult(
                    decision=PermissionDecision.ALLOW,
                    reason=f"command matches {label} allowlist rule: {rule}")
        if current == PermissionMode.PROMPT:
            request = PermissionRequest(tool_name = tool_name,
                                        input = input,
                                        current_mode= current,
                                        required_mode = required,
                                        tool_use_id = tool_use_id,
                                        detail = detail,
                                        escalation = escalation )
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
                                    tool_use_id = tool_use_id,
                                    detail = detail,
                                    escalation = escalation )


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

