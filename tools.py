import contextvars
import fnmatch
import json
import os
import platform
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, List, Optional, Self
from pydantic import BaseModel

from runtime import ToolError, ToolOutput

NL = chr(10)   # LF: splitlines/endswith 的换行判定用, 避免源码里写裸转义

# 用户级配置目录（todo 清单等）: 与 config.USER_DIR 同一处, 但 tools.py
# 不 import config（避免引入配置加载副作用）, 直接推导
USER_CONFIG_HOME = Path.home() / ".x-code"

# 工具输出进入会话历史前的硬上限。模型的思考长度随上下文膨胀，无界的
# 工具输出（大文件、长命令输出）是透支上下文、诱发过度思考的根源，所以
# 在 registry 这个唯一入口统一截断，而不是散在各 handler 里。
MAX_TOOL_OUTPUT_CHARS = 20_000
_KEEP_HEAD = 14_000  # 开头: 结构、表头、命令回显
_KEEP_TAIL = 6_000   # 结尾: 报错和最终状态通常在这里


def truncate_tool_output(output: str) -> str:
    """超限时保留首尾、掐掉中段，并留标记让模型知道去拿哪部分。"""
    if len(output) <= MAX_TOOL_OUTPUT_CHARS:
        return output
    omitted = len(output) - _KEEP_HEAD - _KEEP_TAIL
    return (
        output[:_KEEP_HEAD]
        + f"\n\n[... output truncated: {omitted} characters omitted. "
        f"Do NOT repeat this same call unchanged — it will truncate the same "
        f"way again. Narrow it instead: read_file with offset/limit, a more "
        f"specific grep pattern/path, or a filtered command ...]\n\n"
        + output[-_KEEP_TAIL:]
    )


class ToolRegistry():
    def __init__(self):
        self._handlers = {}


    def register(self, name: str, handler: Callable) -> Self:
        if name in self._handlers:
            raise ValueError(f"Tool already registered: {name}")
        self._handlers[name] = handler
        return self

    def execute(self, name: str, tool_input_json: str,
                workdir: Optional[str] = None) -> str:
        if name not in self._handlers:
            raise ToolError(f"Unknown tool: {name}")

        try:
            params = json.loads(tool_input_json) if tool_input_json else {}
        except json.JSONDecodeError:
            raise ToolError(f"Invalid JSON input: {tool_input_json}")

        try:
            result = self._handlers[name](params, workdir)
            return truncate_tool_output(result)
        except Exception as e:
            raise ToolError(f"Tool execution error: {e}")

def resolve_path(path: str, workdir: Optional[str]) -> Path:
    """相对路径基于会话工作目录解析；绝对路径原样使用。无 workdir 时同旧行为。"""
    p = Path(path)
    if p.is_absolute() or not workdir:
        return p
    return Path(workdir) / p


# --- 进程执行内核: Popen + 读线程 + 杀整棵进程树 ---
#
# 为什么不用 subprocess.run(timeout=...): 它超时只 kill 直接子进程
# (bash.exe), 命令拉起的孙进程(如 `java -jar server.jar` 的 java)不会死,
# 且继承着 stdout 管道句柄——run() 在 kill 后还会再调一次无超时的
# communicate() 收尾输出, 管道永远等不到 EOF, 工具调用永久阻塞,
# turn 工作线程随之卡死(会话 busy 不解、排队消息永不接力)。
# 这里改成: 读线程持续收集输出 + 主线程分片等待, 超时/打断时杀整棵
# 进程树, 收尾只带宽限地 join 读线程——任何路径都不无超时等管道 EOF。

# 工具级取消检查: 宿主(Web 端)在每轮工作线程里 set 成 should_stop,
# contextvars 随 runtime 的 copy_context() 传进并行工具池线程。
# 长命令的等待循环里轮询它, 触发即杀树返回。CLI 不设置, 行为不变。
TOOL_CANCEL_CHECK: contextvars.ContextVar[Optional[Callable[[], bool]]] = \
    contextvars.ContextVar("xcode_tool_cancel", default=None)

DEFAULT_CMD_TIMEOUT = 30
MAX_CMD_TIMEOUT = 600
_KILL_JOIN_GRACE = 2.0     # 杀树后收尸读线程的宽限
_EXIT_JOIN_GRACE = 5.0     # 正常退出后等剩余输出排干的宽限
_WAIT_SLICE = 0.2          # 等待循环的轮询步长


def _cancelled() -> bool:
    check = TOOL_CANCEL_CHECK.get()
    return bool(check and check())


def _clamp_timeout(raw) -> int:
    """timeout 参数: 默认 30s, 限 1~600s。模型传坏值时兜底而非报错。"""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CMD_TIMEOUT
    return max(1, min(n, MAX_CMD_TIMEOUT))


def _kill_tree(proc: subprocess.Popen) -> None:
    """杀整棵进程树。Windows 用 taskkill /T; POSIX 建会话后 killpg。
    只杀直接子进程会留下继承管道的孙进程, 是卡死的根源。"""
    if proc.poll() is None:
        try:
            if platform.system() == "Windows":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=10,
                )
            else:
                os.killpg(proc.pid, signal.SIGKILL)   # start_new_session 下 pgid=pid
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _pump(pipe, sink: List[bytes]) -> None:
    """读线程: 持续把管道收进 sink 直到 EOF。EOF 被第三方(孙进程)拖住时
    线程停在 read 上, 由调用方带宽限 join——绝不无限等。"""
    try:
        for chunk in iter(pipe.readline, b""):
            sink.append(chunk)
    except OSError:
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _join_pumps(threads: List[threading.Thread], grace: float) -> None:
    deadline = time.monotonic() + grace
    for t in threads:
        t.join(timeout=max(0.0, deadline - time.monotonic()))


def _assemble(out_chunks: List[bytes], err_chunks: List[bytes]) -> str:
    output = b"".join(out_chunks).decode("utf-8", errors="replace")
    err = b"".join(err_chunks).decode("utf-8", errors="replace")
    if err:
        output += f"\nSTDERR: {err}"
    return output


def _popen_kwargs() -> dict:
    """POSIX 下独立会话启动, 让 killpg 能覆盖整棵树; Windows 靠 taskkill /T。"""
    if platform.system() == "Windows":
        return {}
    return {"start_new_session": True}


def _run_command(argv: List[str], cwd: Optional[str], timeout: int) -> str:
    """前台执行: 收集输出直到进程退出 / 超时 / 打断。三条路都保证返回。"""
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            **_popen_kwargs(),
        )
    except OSError as e:
        return f"ERROR: failed to start command: {e}"

    out_chunks: List[bytes] = []
    err_chunks: List[bytes] = []
    pumps = [
        threading.Thread(target=_pump, args=(proc.stdout, out_chunks), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, err_chunks), daemon=True),
    ]
    for t in pumps:
        t.start()

    interrupted = False
    timed_out = False
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if _cancelled():
            interrupted = True
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(_WAIT_SLICE)

    if interrupted or timed_out:
        _kill_tree(proc)
    _join_pumps(pumps, _KILL_JOIN_GRACE if (interrupted or timed_out)
                else _EXIT_JOIN_GRACE)
    output = _assemble(out_chunks, err_chunks)
    if interrupted:
        output += "\n(用户中断了本轮对话)"
    elif timed_out:
        output += f"\nERROR: timeout for {timeout}s"
    return output


# --- Windows 执行器: Git Bash 优先, PowerShell 兜底 ---
#
# 为什么是 Git Bash: 中文乱码的根子不在终端而在"转码"。MSYS2 的 coreutils
# (cat/grep/ls) 对文件内容是字节直通, UTF-8 文件原样输出; PowerShell 5.1 的
# cmdlet (Get-Content/Select-String) 会按 ANSI(GBK) 主动转码, 读 UTF-8 源码
# 必乱。Claude Code 在 Windows 上同样走 Git Bash。
#
# 选壳顺序: XCODE_BASH_HOME(打包场景指向资源目录) → ~/.x-code/git-bash(内置
# 副本) → 系统安装的 Git(从 git.exe 推导) → PATH 里的 bash(排除 System32 的
# WSL 假 bash) → 都没有才退回 PowerShell。
# 打包集成: 把 PortableGit 解压目录带进安装包, 首发放到 ~/.x-code/git-bash,
# 或让桌面壳以 XCODE_BASH_HOME 指向资源目录即可, 三个位置都会被探测到。
BASH_HOME_ENV = "XCODE_BASH_HOME"

_git_bash_exe: Optional[str] = None    # 进程内缓存: 外壳在会话中途不会变
_powershell_exe: Optional[str] = None


def _git_bash_candidates() -> list[str]:
    """Git Bash 的候选路径, 按优先级排列。"""
    candidates: list[str] = []
    env_home = os.environ.get(BASH_HOME_ENV)
    if env_home:
        candidates += [str(Path(env_home) / "bin" / "bash.exe"),
                       str(Path(env_home) / "bash.exe")]
    bundled = Path.home() / ".x-code" / "git-bash" / "bin" / "bash.exe"
    candidates.append(str(bundled))
    git = shutil.which("git")
    if git:
        root = Path(git).parent.parent      # <root>\cmd\git.exe → <root>
        candidates += [str(root / "bin" / "bash.exe"),
                       str(root / "usr" / "bin" / "bash.exe")]
    path_bash = shutil.which("bash")
    if path_bash and "system32" not in path_bash.lower():
        candidates.append(path_bash)        # System32\bash.exe 是 WSL 启动器, 排除
    return candidates


def _git_bash() -> Optional[str]:
    """找到可用的 Git Bash 就返回路径并缓存; 找不到返回 None。"""
    global _git_bash_exe
    if _git_bash_exe is None:
        _git_bash_exe = next(
            (c for c in _git_bash_candidates() if Path(c).exists()), "")
    return _git_bash_exe or None


GIT_DOWNLOAD_URL = "https://git-scm.com/download/win"

def git_bash_unavailable_reason() -> Optional[str]:
    """Git Bash 不可用时返回给用户看的说明; 可用返回 None。

    启动入口调用: 没有它就没有默认执行器（也没有中文 UTF-8 直通保证）,
    宁可拒绝启动也不静默降级——降级后的乱码输出用户看不懂, 更难排查。
    """
    if _git_bash():
        return None
    return ("未检测到 Git Bash（Git for Windows）, x-code 的命令执行器依赖它, "
            "并靠它保证中文输出不乱码。\n"
            f"请安装 Git 后重启本程序: {GIT_DOWNLOAD_URL}\n"
            "安装选项全部默认即可, 装完无需任何配置。")


def _windows_powershell() -> str:
    """PowerShell 兜底外壳: 系统里有 pwsh 7 就优先（cmdlet 默认 UTF-8）,
    否则 5.1。结果进程内缓存, PATH 中途变化不影响。"""
    global _powershell_exe
    if _powershell_exe is None:
        _powershell_exe = shutil.which("pwsh") or "powershell"
    return _powershell_exe


def _is_pwsh7(shell: str) -> bool:
    return Path(shell).stem.lower() == "pwsh"

def _shell_preamble(shell: str) -> str:
    """PowerShell 的编码前导, 各补各的缺。

    7 系 cmdlet 默认已是 UTF-8, 只需对齐与外部程序之间的两层管道编码;
    5.1 追加 $OutputEncoding 与常用写文件 cmdlet 的默认值——但读无 BOM
    文件仍按 ANSI 且无全局开关, 所以 5.1 只是兜底, 不是答案。
    """
    common = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
              "$OutputEncoding=[System.Text.Encoding]::UTF8;")
    if _is_pwsh7(shell):
        return common
    return common + ("$PSDefaultParameterValues['Out-File:Encoding']='utf8';"
                     "$PSDefaultParameterValues['Set-Content:Encoding']='utf8';"
                     "$PSDefaultParameterValues['Add-Content:Encoding']='utf8';")


def bash_tool(params: dict, workdir: Optional[str] = None) -> str:
    cmd = params.get('command', "")
    cwd = str(Path(workdir)) if workdir else None
    if platform.system() == "Windows":
        bash = _git_bash()
        if bash is None:
            # 连 Git Bash 都没有才退回 PowerShell（编码行为见 _shell_preamble）
            return powershell_tool(params, workdir)
        # -l 登录壳: 拿到 MSYS2 的完整 PATH（/usr/bin 下的 coreutils）
        argv = [bash, "-lc", cmd]
    else:
        argv = ["sh", "-lc", cmd]
    if params.get("background"):
        return _start_background(argv, cwd)
    return _run_command(argv, cwd, _clamp_timeout(params.get("timeout")))

def powershell_tool(params: dict, workdir: Optional[str] = None) -> str:
    cmd = params.get('command', "")
    cwd = str(Path(workdir)) if workdir else None
    shell = _windows_powershell()
    argv = [
        shell,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        _shell_preamble(shell) + cmd,
    ]
    if params.get("background"):
        return _start_background(argv, cwd)
    return _run_command(argv, cwd, _clamp_timeout(params.get("timeout")))

# read_file 整读分页窗口: 超过就只回首页 + 翻页指令, 不交给通用截断。
# head+tail 掐中段对大文件是灾难: 目标代码恰在中段时, 模型会把同一个
# 整读调用原样重发（实测 24 次）也看不见目标——分页协议让它照抄 offset。
_READ_WINDOW_CHARS = 12_000


def _window_line_count(lines: list[str], budget: int) -> int:
    """预算字符数内能装下的行数（至少 1 行, 防单行超长的死循环）。"""
    total = 0
    for i, line in enumerate(lines):
        total += len(line)
        if total > budget:
            return max(1, i)
    return len(lines)


def read_tool(params: dict, workdir: Optional[str] = None) -> str:
    """读文本文件。大文件必须用 offset/limit 按行取窗口: 整读超窗口时
    返回首页 + 明确翻页指令（"continue with offset=N"）, 而不是掐中段——
    分页是照抄参数就能走的确定路径, 不依赖模型自己想出绕路方案。
    errors="replace": 不可解码字节就地变 U+FFFD, 不再整读报错。"""
    path = resolve_path(params.get('path', ''), workdir)
    try:
        with open(path, 'r', encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return f'ERROR: file not found {path}'
    except OSError as e:
        return f'ERROR: cannot read {path}: {e}'
    total = len(lines)
    try:
        offset = int(params.get("offset") or 1)
        limit = int(params.get("limit") or 0)
    except (TypeError, ValueError):
        return "ERROR: offset/limit must be integers"
    offset = max(1, offset)
    if offset == 1 and limit <= 0:
        # 整读: 小文件全文原样（不加任何头）; 大文件切首页窗口
        if sum(len(line) for line in lines) <= _READ_WINDOW_CHARS:
            return "".join(lines)
        limit = _window_line_count(lines, _READ_WINDOW_CHARS)
    start = offset - 1
    end = start + limit if limit > 0 else total
    chunk = lines[start:end]
    if not chunk:
        return f"ERROR: offset {offset} is beyond the end of file ({total} lines)"
    next_off = min(end, total) + 1
    header = (f"[{path} lines {offset}-{min(end, total)} of {total} total"
              + (f"; continue with offset={next_off}"
                 + (f" limit={limit}" if limit else "")
                 if next_off <= total else "; this is the end of the file")
              + "]")
    return header + "\n" + "".join(chunk)

def _unified_diff(old_text: str, new_text: str, path: str) -> str:
    """两版文本的 unified diff（无 trailing 换行噪音）。"""
    import difflib
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    return "".join(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{path}", tofile=f"b/{path}"))


def write_tool(params: dict, workdir: Optional[str] = None) -> str:
    path = resolve_path(params.get('path', ''), workdir)
    content = params.get('content', '')
    existed = path.exists()
    old_text = ""
    if existed:
        try:
            old_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            old_text = None   # 二进制/不可解码旧内容: diff 不可靠, 置 None 跳过
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding="utf-8") as f:
            f.write(content)
    except FileNotFoundError:
        return f'ERROR: directory not found {path}'
    except OSError as e:
        return f'ERROR: cannot write {path}: {e}'

    new_lines = len(content.splitlines())
    if content and not content.endswith(NL):
        new_lines += 1   # 末行无换行符也算一行
    summary = (f"OK: wrote to {path} ({new_lines} lines, "
               + ("updated" if existed and old_text is not None else
                  "overwritten (old content undecodable)" if existed else "new file")
               + ")")
    meta: dict = {"path": str(path), "created": not existed,
                  "lines": new_lines, "chars": len(content)}
    if old_text is not None and old_text != content:
        diff = _unified_diff(old_text, content, str(path))
        if diff:
            meta["diff"] = diff[:8000]
    return ToolOutput(summary).with_meta(meta)


# --- 任务清单 (todo): 会话级进度追踪, 与 Claude Code 的 TodoWrite 同构 ---
#
# 为什么做进工具而不是提示词: 模型对"多步任务"的进度管理一旦只靠脑内
# 记忆, 长会话压缩后必丢。落一份结构化清单在 ~/.x-code/todos/, 既给模型
# 一个"当前该干什么"的外部记忆, 也给前端一份可渲染的任务面板数据源。
# 单会话单清单: 文件名 = session_id, 线程锁串行化写入。

_TODOS_DIR = USER_CONFIG_HOME / "todos"
_todo_lock = threading.Lock()


def todo_tool(params: dict, workdir: Optional[str] = None,
              session_id: Optional[str] = None) -> str:
    """读/写当前会话的任务清单。写时全量替换（模型每次提交完整列表）。

    session_id 归属与 agent_tools.current_session_id 同一策略: Web 端由
    dispatch 绑定提供（每会话一份清单）, CLI 无绑定落 "cli"。"""
    action = str(params.get("action") or "read")
    if session_id is None:
        try:
            from agent_tools import current_session_id
            session_id = current_session_id() or "cli"
        except Exception:
            session_id = "cli"

    def _load() -> list[dict]:
        try:
            data = json.loads((_TODOS_DIR / f"{session_id}.json").read_text(encoding="utf-8"))
            return data.get("items", []) if isinstance(data, dict) else []
        except (OSError, ValueError):
            return []

    def _render(items: list[dict]) -> str:
        if not items:
            return "(empty)"
        marks = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
        return NL.join(
            f"{marks.get(it.get('status'), '[ ]')} {it.get('content', '')}"
            for it in items)

    with _todo_lock:
        if action == "write":
            raw = params.get("items")
            if not isinstance(raw, list) or len(raw) > 50:
                return "ERROR: items must be a list (max 50)"
            items = []
            for it in raw:
                if not isinstance(it, dict):
                    continue
                content = str(it.get("content") or "").strip()
                if not content:
                    continue
                status = str(it.get("status") or "pending")
                if status not in ("pending", "in_progress", "completed"):
                    status = "pending"
                items.append({"content": content[:200], "status": status})
            _TODOS_DIR.mkdir(parents=True, exist_ok=True)
            (_TODOS_DIR / f"{session_id}.json").write_text(
                json.dumps({"items": items}, ensure_ascii=False, indent=2),
                encoding="utf-8")
            return f"OK: {len(items)} todos saved.{NL}{_render(items)}"
        return _render(_load())


todo_spec = {
    "name": "todo",
    "description": (
        "Maintain a task checklist for the current session so the user can "
        "track progress on multi-step work. Two actions: 'read' returns the "
        "current list; 'write' replaces the whole list (always submit the "
        "complete list, one item per step, status = pending / in_progress / "
        "completed). Keep exactly one item in_progress at a time; mark "
        "completed immediately after finishing a step."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["read", "write"],
                "description": "'read' = fetch current todos; "
                               "'write' = replace the whole list.",
            },
            "items": {
                "type": "array",
                "description": "Full list for action=write. Each item: "
                               "{'content': str, 'status': "
                               "'pending'|'in_progress'|'completed'}.",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {"type": "string",
                                   "enum": ["pending", "in_progress",
                                            "completed"]},
                    },
                    "required": ["content"],
                },
            },
        },
        "required": ["action"],
    },
}


def present_plan_tool(params: dict, workdir: Optional[str] = None) -> str:
    """present_plan 的直通 handler: 正常流程在授权层就被拦截（plan 模式
    弹计划卡, 用户批准后才放行到这里; 其余模式直接放行）。能执行到这一步
    就意味着计划已获批准——返回文案必须明确告诉模型开始实施, 否则它会
    再次提交计划、停在"等待批准"。"""
    plan = str(params.get("plan", "")).strip()
    n = len(plan.splitlines()) if plan else 0
    return (f"Plan received ({n} lines). The user has APPROVED your plan - "
            "start implementing it right away. Do not call present_plan again.")


# --- 后台任务: 常驻命令(服务器/监听进程)脱离轮次生命周期 ---
#
# 服务器类命令前台跑只有两种结局: 30s 超时被杀(服务起不来), 或模型没配
# timeout 时拖着轮次干等。background=true 让命令脱离会话立即返回, 输出
# 进日志文件, 模型按需 task_output 看日志、task_stop 停进程。
# 注册表是进程内存 dict: 服务进程生命周期 = x-code 服务进程生命周期,
# 重启后残留的日志文件无害, 条目丢失只影响对旧任务的查询/停止。

_BG_DIR = Path(tempfile.gettempdir()) / "xcode-bg"
_bg_tasks: dict[str, dict] = {}
_bg_lock = threading.Lock()
_bg_seq = 0


def _bg_entry(task_id: str) -> Optional[dict]:
    with _bg_lock:
        return _bg_tasks.get(task_id)


def _start_background(argv: List[str], cwd: Optional[str]) -> str:
    """后台启动: stdout/stderr 合流进日志文件, 立即返回 task_id + 日志路径。"""
    _BG_DIR.mkdir(parents=True, exist_ok=True)
    with _bg_lock:
        global _bg_seq
        _bg_seq += 1
        task_id = f"bg-{_bg_seq}"
    log_path = _BG_DIR / f"{task_id}-{uuid.uuid4().hex[:8]}.log"
    log_file = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            argv,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            **_popen_kwargs(),
        )
    except OSError as e:
        log_file.close()
        return f"ERROR: failed to start background command: {e}"
    finally:
        # 子进程已持有自己的句柄, 父侧随即关闭, 不阻塞日志文件被删除
        log_file.close()
    with _bg_lock:
        _bg_tasks[task_id] = {"proc": proc, "log": str(log_path)}
    return (f"Background task started: id={task_id} pid={proc.pid}\n"
            f"log: {log_path}\n"
            f"Read recent output with task_output; stop it with task_stop.")


def _bg_status(task: dict) -> str:
    code = task["proc"].poll()
    if code is None:
        return "running"
    return f"exited (code {code})"


def task_output_tool(params: dict, workdir: Optional[str] = None) -> str:
    """读后台任务的日志尾部 + 存活状态。只读文件尾部, 服务日志再大也不撑上下文。"""
    task_id = str(params.get("task_id", "")).strip()
    task = _bg_entry(task_id)
    if task is None:
        return f"ERROR: unknown task_id: {task_id}"
    try:
        tail = max(1, min(int(params.get("tail_lines", 50)), 500))
    except (TypeError, ValueError):
        tail = 50
    try:
        with open(task["log"], "rb") as f:
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError as e:
        return f"ERROR: cannot read log: {e}"
    recent = "\n".join(lines[-tail:])
    header = f"[{task_id}] {_bg_status(task)}"
    if not recent.strip():
        return f"{header}\n(no output yet)"
    return f"{header}\n{recent}"


def task_stop_tool(params: dict, workdir: Optional[str] = None) -> str:
    """停后台任务: 杀整棵进程树(服务常带子进程, 只杀主进程会漏)。"""
    task_id = str(params.get("task_id", "")).strip()
    task = _bg_entry(task_id)
    if task is None:
        return f"ERROR: unknown task_id: {task_id}"
    if task["proc"].poll() is not None:
        return f"[{task_id}] already exited (code {task['proc'].poll()})."
    _kill_tree(task["proc"])
    return f"[{task_id}] stopped."


# --- grep / glob: 纯 Python 只读搜索工具 ---
#
# 为什么不直接用 bash grep/find: (1) 无 Git Bash 的机器上 bash 工具降级,
# 模型只能拼 PowerShell 语法, 易翻车; (2) 模式含引号/正则元字符时经 shell
# 转义是常见失败源, 专用工具收 JSON 参数零 shell 解析; (3) 只读语义让它
# 们归 READ_ONLY 权限档, 免弹窗且可安全并发。
# glob 遍历时跳过 node_modules/.git/__pycache__ 等噪声目录与隐藏目录。

_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "dist",
    "build", ".idea", ".vscode", ".pytest_cache", ".mypy_cache",
    "site-packages", ".tox", "target", ".next", "vendor",
}
_BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip",
    ".gz", ".tar", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib",
    ".woff", ".woff2", ".ttf", ".eot", ".mp3", ".mp4", ".mov", ".avi",
    ".sqlite", ".db", ".pyc", ".class", ".wasm", ".bin",
}
_MAX_FILE_BYTES = 2_000_000      # 单文件读入上限: 超过按二进制跳过
_MAX_RESULTS = 200               # grep 单模式匹配数上限（files_with_matches 是文件数）
_MAX_LIST = 500                  # glob 返回路径数上限


def _iter_files(base: Path):
    """递归遍历 base 下的文件。跳过噪声目录/隐藏目录, 不跟随符号链接。"""
    stack = [base]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name)
        except (PermissionError, OSError):
            continue
        for e in entries:
            if e.is_dir():
                if e.name.startswith(".") or e.name in _SKIP_DIRS:
                    continue
                stack.append(e)
            elif e.is_file():
                yield e


def glob_tool(params: dict, workdir: Optional[str] = None) -> str:
    """按 glob 模式列出文件路径。pattern 相对 workdir, 支持递归 `**`。"""
    pattern = str(params.get("pattern", "")).strip()
    if not pattern:
        return "ERROR: pattern is required"
    base = resolve_path(str(params.get("path") or "."), workdir)
    if not base.is_dir():
        return f"ERROR: directory not found: {base}"

    pats = pattern if isinstance(pattern, list) else [pattern]
    seen: dict[str, None] = {}
    truncated = False
    for pat in pats:
        # ** 跨目录递归; Path.glob 在 Windows 上大小写不敏感, 与系统一致
        pat = pat.replace("\\\\", "/")
        try:
            matches = list(base.glob(pat))
        except (ValueError, OSError) as e:
            return f"ERROR: bad pattern '{pat}': {e}"
        for m in matches:
            if len(seen) >= _MAX_LIST:
                truncated = True
                break
            # 目录本身不进结果（模型要的是文件）; 保留先见顺序, 去重
            if not m.is_file():
                continue
            # Path.glob 不认 _SKIP_DIRS, 结果按相对路径补过滤
            # （Path.glob 总是从 base 起, 不必担心 m 在 base 之外）
            rel_parts = m.relative_to(base).parts[:-1]
            if any(p.startswith(".") or p in _SKIP_DIRS for p in rel_parts):
                continue
            seen.setdefault(str(m), None)
        if truncated:
            break
    if not seen:
        return (
            f"No files found for pattern(s): {pattern}.\n"
            "[System note] Zero hits means nothing under this root matches. "
            "Do NOT retry near-identical patterns. Widen the glob "
            "('--/**/*.ext'), point path at a parent directory, or list the "
            "directory to see what is actually there."
        )
    lines = list(seen.keys())
    if truncated:
        lines.append(f"... (truncated at {_MAX_LIST} files, refine the pattern)")
    # 摘要头帮助模型一眼看到规模, 正文是可直接传给 read_file 的路径
    return f"{len(lines) - (1 if truncated else 0)} file(s):" + NL + NL.join(lines)


def grep_tool(params: dict, workdir: Optional[str] = None) -> str:
    """在文件内容里搜正则。模式: files_with_matches(默认)/content/count。
    glob 参数过滤文件名（如 '*.py'）, path 参数限定目录或单文件。"""
    import re
    pattern = params.get("pattern")
    if not pattern or not str(pattern).strip():
        return "ERROR: pattern is required"
    try:
        rx = re.compile(str(pattern))
    except re.error as e:
        return f"ERROR: invalid regex: {e}"

    mode = str(params.get("output_mode") or "files_with_matches")
    if mode not in ("files_with_matches", "content", "count"):
        return f"ERROR: output_mode must be files_with_matches | content | count"
    file_glob = params.get("glob")
    globs = [str(g) for g in file_glob] if isinstance(file_glob, list) else (
        [str(file_glob)] if file_glob else None)

    target = resolve_path(str(params.get("path") or "."), workdir)
    if target.is_file():
        files = [target]
    elif target.is_dir():
        files = _iter_files(target)
    else:
        return f"ERROR: path not found: {target}"

    hits: list[str] = []       # 输出行
    matched_files = 0
    total_matches = 0
    truncated = False
    for f in files:
        if globs:
            # '*.py' 对文件名匹配; 'src/*.py' 这类带斜杠的按相对路径匹配
            rel = f.relative_to(target) if target.is_dir() else Path(f.name)
            if not any(fnmatch.fnmatch(str(rel), g) or fnmatch.fnmatch(f.name, g)
                       for g in globs):
                continue
        if f.suffix.lower() in _BINARY_EXT:
            continue
        try:
            if f.stat().st_size > _MAX_FILE_BYTES:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
        except (OSError, PermissionError):
            continue
        count = 0
        content_lines: list[str] = []
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                count += 1
                total_matches += 1
                if mode == "content":
                    content_lines.append(f"{f}:{i}: {line.strip()[:300]}")
        if count:
            matched_files += 1
            if mode == "files_with_matches":
                hits.append(str(f))
                if len(hits) >= _MAX_RESULTS:
                    truncated = True
            elif mode == "count":
                hits.append(f"{f}: {count} match(es)")
                if len(hits) >= _MAX_RESULTS:
                    truncated = True
            else:
                hits.extend(content_lines)
                if len(hits) >= _MAX_RESULTS:
                    truncated = True
        if truncated:
            break

    if not hits:
        return (
            f"No matches for /{pattern}/ in {target}.\n"
            "[System note] Zero matches means this pattern does not exist in "
            "this scope. Do NOT re-issue the same search with cosmetic keyword "
            "variations — that is how turns get burned. Change the approach "
            "instead: widen or narrow the path/glob, search for an identifier "
            "you already saw in earlier output, or open the most likely file "
            "with read_file and navigate it."
        )
    if truncated:
        hits.append(f"... (truncated at {_MAX_RESULTS} results, narrow the search)")
    if mode == "files_with_matches":
        head = f"{matched_files} file(s) match /{pattern}/:"
        return head + NL + NL.join(hits)
    if mode == "count":
        head = f"{total_matches} match(es) of /{pattern}/ in {matched_files} file(s):"
        return head + NL + NL.join(hits)
    return NL.join(hits)

# ---------------------------------------------------------------------------
# Web 工具: web_search（websearch-py 多引擎免 key 搜索, 引擎自动回退）+
# web_fetch（trafilatura 抓取正文）。定位: 只读外部信息, 输出纯文本进上下文。
# ---------------------------------------------------------------------------

_WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# trafilatura 惰性导入缓存: 失败时缓存错误, 避免每轮调用重复 import 失败
_trafilatura_state: dict = {}

# websearch_py 惰性导入缓存（同上, 失败只报一次）
_websearch_state: dict = {}

# 引擎回退顺序: 实测部分网络只可达 Bing, 故 bing 优先, 其余做异地回退
_WEB_ENGINES = ["bing", "duckduckgo", "brave", "qwant"]


def _get_websearcher(timeout: int):
    """惰性构建 WebSearcher(多引擎回退)。返回 (searcher 或 None, 错误信息)。"""
    if "err" in _websearch_state:
        return None, _websearch_state["err"]
    try:
        import warnings
        from websearch_py import WebSearcher
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            searcher = WebSearcher(engines=list(_WEB_ENGINES), timeout=timeout)
        return searcher, None
    except Exception as e:
        _websearch_state["err"] = f"websearch-py not available: {e}"
        return None, _websearch_state["err"]


def _get_trafilatura():
    """惰性 import trafilatura。返回 (module 或 None, 错误信息或 None)。"""
    if "mod" in _trafilatura_state or "err" in _trafilatura_state:
        return _trafilatura_state.get("mod"), _trafilatura_state.get("err")
    try:
        import trafilatura  # type: ignore
        _trafilatura_state["mod"] = trafilatura
        return trafilatura, None
    except Exception as e:  # ImportError 等加载失败
        _trafilatura_state["err"] = f"trafilatura not available: {e}"
        return None, _trafilatura_state["err"]


def _http_get(url: str, timeout: int):
    """统一 HTTP GET。返回 (body_bytes, final_url, 错误信息)。
    用标准库 urllib: 搜索/抓取不新增重量级 HTTP 依赖。"""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": _WEB_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), resp.geturl(), None
    except Exception as e:
        return b"", url, str(e)


def web_search_tool(params: dict, workdir: Optional[str] = None) -> str:
    """web_search handler: 免 key DuckDuckGo 搜索, 返回标题+链接+摘要。
    定位: 实时信息/训练截止后事件/不确定事实时才用。"""
    query = str(params.get("query") or "").strip()
    if not query:
        return "ERROR: query is required"
    try:
        max_results = max(1, min(10, int(params.get("max_results", 5))))
    except (TypeError, ValueError):
        max_results = 5
    try:
        timeout = max(3, min(30, int(params.get("timeout", 10))))
    except (TypeError, ValueError):
        timeout = 10

    searcher, err = _get_websearcher(timeout)
    if err:
        return f"ERROR: {err}"
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            resp = searcher.search(query, max_results=max_results,
                                   language="en")
        hits = [{"title": r.titre, "url": r.url,
                 "snippet": (r.extrait or "")} for r in resp.results]
    except Exception as e:  # 含 AllEnginesFailedError(各引擎错误已在其消息里)
        return f"ERROR: search failed: {str(e)[:400]}"
    if not hits:
        return f"No results for: {query}"
    hits = hits[:max_results]
    lines = []
    for i, h in enumerate(hits, 1):
        line = f"{i}. [{h['title']}]({h['url']})"
        if h["snippet"]:
            line += NL + "   " + h["snippet"][:200]
        lines.append(line)
    return (f"{len(hits)} result(s) for '{query}':" + NL + NL.join(lines))


def web_fetch_tool(params: dict, workdir: Optional[str] = None) -> str:
    """web_fetch handler: trafilatura 抓 URL 提取正文纯文本, 不落盘。"""
    url = str(params.get("url") or "").strip()
    if not url:
        return "ERROR: url is required"
    if not url.startswith(("http://", "https://")):
        return "ERROR: url must start with http:// or https://"
    try:
        max_chars = max(500, min(30_000, int(params.get("max_chars", 8000))))
    except (TypeError, ValueError):
        max_chars = 8000
    try:
        timeout = max(3, min(60, int(params.get("timeout", 15))))
    except (TypeError, ValueError):
        timeout = 15

    body, final_url, err = _http_get(url, timeout)
    if err:
        return f"ERROR: fetch failed: {err}"
    if not body:
        return "ERROR: empty response"
    trafilatura, terr = _get_trafilatura()
    if terr:
        return f"ERROR: {terr}"
    try:
        text = trafilatura.extract(
            body, url=final_url, include_comments=False,
            favor_recall=True) or ""
    except Exception as e:
        return f"ERROR: extraction failed: {e}"
    if not text:
        return ("ERROR: no main content extracted (page may be JS-rendered "
                "or not an article). Try another URL.")
    if len(text) > max_chars:
        text = text[:max_chars] + f"... [truncated at {max_chars} chars]"
    return final_url + NL + text


web_search_spec = {
    "name": "web_search",
    "description": (
        "Search the web (no API key; multi-engine with automatic fallback: "
        "Bing, DuckDuckGo, Brave, Qwant). Returns title/url/snippet list. "
        "Use ONLY for real-time info, events after your training data, or "
        "uncertain facts - not for everyday questions you can answer directly."),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search keywords"},
            "max_results": {"type": "integer",
                            "description": "1-10 results, default 5"},
            "timeout": {"type": "integer",
                        "description": "Seconds, 3-30, default 10"},
        },
        "required": ["query"],
    },
}

web_fetch_spec = {
    "name": "web_fetch",
    "description": (
        "Fetch a URL and extract main article text with trafilatura. Returns "
        "plain text; writes no files. Pair with web_search: search first, then "
        "fetch the most promising hit for full text."),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "http(s) URL"},
            "max_chars": {"type": "integer",
                          "description": "Truncate text to N chars, default 8000"},
            "timeout": {"type": "integer",
                        "description": "Seconds, 3-60, default 15"},
        },
        "required": ["url"],
    },
}
