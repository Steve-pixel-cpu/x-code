"""Windows 执行器选择的规格钉子: Git Bash 优先, PowerShell 兜底。

选壳顺序: XCODE_BASH_HOME(打包资源目录) → ~/.x-code/git-bash(内置副本) →
系统安装的 Git(从 git.exe 推导根目录) → PATH 里的 bash(排除 System32 的
WSL 启动器); 找不到 Git Bash 才退回 PowerShell。PowerShell 自身 pwsh 7 优先。

运行方式（在 x-code 目录下）:
    uv run pytest tests/test_windows_shell.py -v
"""

import sys
import time
from pathlib import Path

import pytest

import tools


def reset(monkeypatch, tmp_path, env_home=None, which=None, home=None):
    """重置进程内缓存, 固定环境变量 / PATH 探测 / 用户目录。"""
    monkeypatch.setattr(tools, "_git_bash_exe", None)
    monkeypatch.setattr(tools, "_powershell_exe", None)
    monkeypatch.delenv(tools.BASH_HOME_ENV, raising=False)
    if env_home is not None:
        monkeypatch.setenv(tools.BASH_HOME_ENV, env_home)
    which = which or {}
    monkeypatch.setattr(tools.shutil, "which",
                        lambda name: which.get(name))
    if home is not None:
        monkeypatch.setattr(tools.Path, "home", lambda: home)


def _mk_bash(root: Path, sub: str = "bin") -> str:
    exe = root / sub / "bash.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("")
    return str(exe)


# ------------------------------------------------------------
# Git Bash 选壳顺序
# ------------------------------------------------------------

def test_环境变量资源目录优先级最高(monkeypatch, tmp_path):
    env_bash = _mk_bash(tmp_path / "resources")
    sys_bash = _mk_bash(tmp_path / "Git")
    reset(monkeypatch, tmp_path, env_home=str(tmp_path / "resources"),
          which={"git": str(tmp_path / "Git" / "cmd" / "git.exe")})

    assert tools._git_bash() == env_bash


def test_内置副本次之(monkeypatch, tmp_path):
    home = tmp_path / "home"
    bundled = _mk_bash(home / ".x-code" / "git-bash")
    reset(monkeypatch, tmp_path, home=home)

    assert tools._git_bash() == bundled


def test_从git_exe推导系统安装的GitBash(monkeypatch, tmp_path):
    home = tmp_path / "home"
    sys_bash = _mk_bash(tmp_path / "Git")             # <root>\bin\bash.exe
    reset(monkeypatch, tmp_path, home=home,
          which={"git": str(tmp_path / "Git" / "cmd" / "git.exe")})

    assert tools._git_bash() == sys_bash


def test_最后才看PATH里的bash(monkeypatch, tmp_path):
    home = tmp_path / "home"
    path_bash = _mk_bash(tmp_path / "Git" / "usr", sub="bin")  # usr\bin\bash.exe
    reset(monkeypatch, tmp_path, home=home,
          which={"bash": path_bash})

    assert tools._git_bash() == path_bash


def test_System32的bash是WSL启动器必须排除(monkeypatch, tmp_path):
    home = tmp_path / "home"
    wsl = _mk_bash(tmp_path / "System32")             # 路径含 system32 → 排除
    reset(monkeypatch, tmp_path, home=home,
          which={"bash": wsl})

    assert tools._git_bash() is None


def test_检测结果进程内缓存(monkeypatch, tmp_path):
    home = tmp_path / "home"
    path_bash = _mk_bash(tmp_path / "Git")
    reset(monkeypatch, tmp_path, home=home, which={"bash": path_bash})
    first = tools._git_bash()
    # PATH 探测结果变化也不影响本进程（外壳在会话中途不会变）
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)

    assert tools._git_bash() is first


# ------------------------------------------------------------
# 兜底与 PowerShell
# ------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Windows 分支")
def test_无GitBash时bash_tool退回PowerShell(monkeypatch, tmp_path):
    reset(monkeypatch, tmp_path)                      # 什么都不装
    captured = {}

    def fake_run_command(argv, cwd, timeout):
        captured["argv"] = argv
        return "ps-out"

    monkeypatch.setattr(tools, "_run_command", fake_run_command)

    out = tools.bash_tool({"command": "ls"}, None)

    assert captured["argv"][0] == "powershell"        # 无 pwsh → 5.1
    assert captured["argv"][4].endswith("ls")
    assert out == "ps-out"


def test_powershell外壳pwsh7优先(monkeypatch, tmp_path):
    pwsh = tmp_path / "pwsh.exe"
    pwsh.write_text("")
    reset(monkeypatch, tmp_path, which={"pwsh": str(pwsh)})

    assert tools._windows_powershell() == str(pwsh)


def test_powershell外壳无pwsh回退51(monkeypatch, tmp_path):
    reset(monkeypatch, tmp_path)

    assert tools._windows_powershell() == "powershell"


def test_51前导追加写文件cmdlet默认值_pwsh7不用(monkeypatch, tmp_path):
    reset(monkeypatch, tmp_path)
    ps51 = tools._shell_preamble("powershell")
    pwsh7 = tools._shell_preamble(str(tmp_path / "pwsh.exe"))

    assert "$PSDefaultParameterValues['Out-File:Encoding']='utf8'" in ps51
    assert "$PSDefaultParameterValues" not in pwsh7    # 7 系默认已是 UTF-8
    assert "[Console]::OutputEncoding" in ps51 and "[Console]::OutputEncoding" in pwsh7


# ------------------------------------------------------------
# 启动门禁: 没有 Git Bash 就拒绝启动, 说明里带下载入口
# ------------------------------------------------------------

def test_启动检查_git可用时返回None(monkeypatch, tmp_path):
    bash = _mk_bash(tmp_path / "Git")
    reset(monkeypatch, tmp_path, which={"bash": bash})

    assert tools.git_bash_unavailable_reason() is None


def test_启动检查_缺失时给可操作的说明(monkeypatch, tmp_path):
    reset(monkeypatch, tmp_path)                      # 什么都不装

    reason = tools.git_bash_unavailable_reason()

    assert reason is not None
    assert tools.GIT_DOWNLOAD_URL in reason           # 告诉用户去哪装
    assert "重启" in reason                            # 告诉用户装完要重启


# ------------------------------------------------------------
# 执行内核: 超时杀树 / 打断立即返回 / timeout 钳制
# 回归钉: 旧实现 subprocess.run(timeout=30) 超时只 kill 直接子进程,
# 孙进程(如 MC 服务器的 java)继承管道 → kill 后无超时的 communicate()
# 永久阻塞 → turn 工作线程卡死、对话冻结。
# ------------------------------------------------------------

def test_超时后杀树并按时返回_不被孙进程拖死():
    """`sleep 30 & wait`: bash 存活期间孙进程持有 stdout 管道。
    新实现必须整树击杀、按时返回; 旧实现在这里永久挂死。"""
    t0 = time.monotonic()
    out = tools.bash_tool({"command": "sleep 30 & wait", "timeout": 2})

    assert "ERROR: timeout for 2s" in out
    assert time.monotonic() - t0 < 15                 # 真挂死时远超此值


def test_打断检查让长命令立即返回():
    tools.TOOL_CANCEL_CHECK.set(lambda: True)
    try:
        t0 = time.monotonic()
        out = tools.bash_tool({"command": "sleep 30", "timeout": 60})

        assert "用户中断" in out
        assert time.monotonic() - t0 < 10
    finally:
        tools.TOOL_CANCEL_CHECK.set(None)


def test_普通命令正常收集输出():
    out = tools.bash_tool({"command": "echo shell-ok"})

    assert "shell-ok" in out
    assert "ERROR" not in out


def test_timeout参数钳制():
    assert tools._clamp_timeout(None) == 30           # 缺省
    assert tools._clamp_timeout("x") == 30            # 坏值兜底
    assert tools._clamp_timeout(0) == 1               # 下限
    assert tools._clamp_timeout(9999) == 600          # 上限
