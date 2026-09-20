import contextvars
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

from runtime import ToolError

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
        f"Repeat the call more narrowly (specific file range / filtered command) "
        f"if you need the omitted part ...]\n\n"
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

def read_tool(params: dict, workdir: Optional[str] = None) -> str:
    path = resolve_path(params.get('path', ''), workdir)
    try:
        with open(path, 'r', encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return f'ERROR: file not found {path}'
    return content

def write_tool(params: dict, workdir: Optional[str] = None) -> str:
    path = resolve_path(params.get('path', ''), workdir)
    content = params.get('content', '')
    try:
        with open(path, 'w', encoding="utf-8") as f:
            f.write(content)
    except FileNotFoundError:
        return f'ERROR: directory not found {path}'
    return f'OK: wrote to {path}'


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
