import os
import platform
import shutil
from pathlib import Path
from typing import Callable, Optional, Self
from pydantic import BaseModel
import subprocess
import json

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
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
        )
        output = result.stdout
        if result.stderr:
            output += f'\nSTDERR: {result.stderr}'
        return output
    except subprocess.TimeoutExpired:
        return 'ERROR: timeout for 30s'

def powershell_tool(params: dict, workdir: Optional[str] = None) -> str:
    cmd = params.get('command', "")
    cwd = str(Path(workdir)) if workdir else None
    shell = _windows_powershell()
    try:
        result = subprocess.run(
            [
                shell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _shell_preamble(shell) + cmd,
            ],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
        )
        output = result.stdout
        if result.stderr:
            output += f'\nSTDERR: {result.stderr}'
        return output
    except subprocess.TimeoutExpired:
        return 'ERROR: timeout for 30s'

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
