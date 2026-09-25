"""写路径分级 + bypass-immune 敏感路径检查（backlog 待办 4 的落地）。

workspace-write 档从此名副其实: write_file/edit_file 的目标路径 resolve
后与 workspace 根（会话工作目录 + additionalDirectories）比对——
  inside    维持 WORKSPACE_WRITE（不弹窗, 行为不变）
  outside   升 DANGER 走升级弹问（审批卡可"允许并记住该目录"）
  sensitive 任何模式（含 danger-full-access/allow）都强制人工裁决,
            不能被模式/白名单短路; 无 prompter（subagent）直接拒绝
shell 侧同口径: 破坏族（rm/mv/cp/tee 等）点名敏感路径的命令同样弹问,
git commit/add 等正常流不受影响。

运行: uv run pytest tests/test_path_policy.py -v
"""

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import server
from permissions import (
    PermissionDecision,
    PermissionMode,
    PermissionPolicy,
    PermissionResult,
    classify_write_path,
    shell_command_touches_sensitive_path,
)


# ------------------------------------------------------------
# 分类器: classify_write_path
# ------------------------------------------------------------

@pytest.fixture()
def workspace(tmp_path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    return tmp_path


def _w(path, content: str = "x") -> str:
    return json.dumps({"path": path, "content": content})


@pytest.mark.parametrize("path,want", [
    ("src/app.py", "inside"),                     # 相对路径按第一个根解析
    ("./src/app.py", "inside"),
    ("src/../notes.txt", "inside"),               # .. 后仍在根内
    (".gitignore", "inside"),                     # .git 前缀的普通文件不中招
    (".git/config", "sensitive"),                 # 版本库元数据
    (".git/hooks/pre-commit", "sensitive"),
    ("../escape.txt", "outside"),                 # .. 穿越出根
    ("", "outside"),                              # 空路径按越界兜底
])
def test_classify_relative(path, want, workspace):
    assert classify_write_path(path, [str(workspace)]) == want


def test_classify_absolute_and_multi_root(workspace, tmp_path_factory):
    other = tmp_path_factory.mktemp("other")
    assert classify_write_path(str(other / "a.txt"), [str(workspace)]) == "outside"
    # 附加目录加入根后, 同一路径变为 inside（"记住该目录"的效果）
    assert classify_write_path(str(other / "a.txt"),
                               [str(workspace), str(other)]) == "inside"


def test_classify_dot_git_exact_segment_only(workspace):
    # 精确路径段匹配: .gitignore/.gitattributes/.gitbak 都不是 .git
    for name in (".gitignore", ".gitattributes", ".gitbak", "git"):
        assert classify_write_path(name, [str(workspace)]) == "inside"
    # 嵌套子仓库的 .git 同样命中
    assert classify_write_path("vendor/lib/.git/HEAD", [str(workspace)]) == "sensitive"


def test_classify_home_sensitive(workspace):
    home = Path.home()
    assert classify_write_path("~/.bashrc", [str(workspace)]) == "sensitive"
    assert classify_write_path(str(home / ".ssh" / "known_hosts"),
                               [str(workspace)]) == "sensitive"
    # 本应用自身配置（白名单/设置就在里面）: 自逃脱防护
    assert classify_write_path(str(home / ".x-code" / "settings.json"),
                               [str(workspace)]) == "sensitive"
    assert classify_write_path(str(home / "notes.txt"),
                               [str(workspace)]) == "outside"
    # 同名前缀不中招: .bashrc.backup 不是 .bashrc
    assert classify_write_path(str(home / ".bashrc.backup"),
                               [str(workspace)]) == "outside"


def test_classify_windows_case_folding(workspace):
    # Windows 大小写不敏感: .GIT 与 .git 是同一目录; POSIX 上 .GIT 是
    # 普通目录名, 落回 inside——两边都是各自文件系统语义的正确行为
    got = classify_write_path(".GIT/config", [str(workspace)])
    assert got == ("sensitive" if os.name == "nt" else "inside")


def test_classify_no_roots_falls_back_to_cwd(workspace, monkeypatch):
    # 未配置 workspace 根（直接构造的策略对象）: 退为进程 cwd 作隐式根,
    # 相对路径解析行为与执行层一致（workdir=None 时 Popen 继承 cwd）
    monkeypatch.chdir(workspace)
    assert classify_write_path("a.txt", []) == "inside"
    assert classify_write_path("src/../b.txt", []) == "inside"
    assert classify_write_path(str(workspace.parent / "x.txt"), []) == "outside"
    # 敏感判定不依赖根
    assert classify_write_path(".git/config", []) == "sensitive"


# ------------------------------------------------------------
# 策略行为: authorize 对写工具的分级接线
# ------------------------------------------------------------

class RecordingPrompter:
    """假 prompter: 记录收到的请求, 按构造参数放行/拒绝。"""

    def __init__(self, approve: bool = False):
        self.requests = []
        self._approve = approve

    def decide(self, request):
        self.requests.append(request)
        return PermissionResult(
            decision=(PermissionDecision.ALLOW if self._approve
                      else PermissionDecision.DENY),
            reason="fake prompter")


def make_policy(mode: PermissionMode) -> PermissionPolicy:
    return (PermissionPolicy(mode)
            .with_tool_requirement("write_file", PermissionMode.WORKSPACE_WRITE)
            .with_tool_requirement("edit_file", PermissionMode.WORKSPACE_WRITE))


def test_workspace_write_inside_silent_allow(workspace):
    # 根内写维持原档: 不弹窗（零新增审批疲劳是本设计的硬约束）
    policy = make_policy(PermissionMode.WORKSPACE_WRITE)
    policy.set_workspace_roots([str(workspace)])
    r = policy.authorize("write_file", _w("src/app.py"))
    assert r.decision == PermissionDecision.ALLOW


def test_workspace_write_outside_escalates(workspace):
    policy = make_policy(PermissionMode.WORKSPACE_WRITE)
    policy.set_workspace_roots([str(workspace)])
    prompter = RecordingPrompter(approve=False)
    r = policy.authorize("write_file",
                         _w(str(workspace.parent / "elsewhere.txt")),
                         prompter=prompter)
    assert r.decision == PermissionDecision.DENY           # 弹问了, 假 prompter 拒绝
    assert len(prompter.requests) == 1
    req = prompter.requests[0]
    assert req.escalation == "outside-write"               # 前端据此给"记住目录"按钮
    assert req.detail and "workspace" in req.detail
    # 无 prompter: 越界写不能静默放行
    r2 = policy.authorize("write_file",
                          _w(str(workspace.parent / "elsewhere.txt")))
    assert r2.decision == PermissionDecision.DENY


def test_outside_becomes_inside_after_remember_dir(workspace, tmp_path_factory):
    # "允许并记住该目录"= 附加目录入根: 同目录下次不再弹问
    other = tmp_path_factory.mktemp("other")
    policy = make_policy(PermissionMode.WORKSPACE_WRITE)
    policy.set_workspace_roots([str(workspace), str(other)])
    r = policy.authorize("write_file", _w(str(other / "a.txt")))
    assert r.decision == PermissionDecision.ALLOW


def test_danger_mode_outside_silent_allow(workspace, tmp_path):
    # danger-full-access 语义: 越界写放行（用户自担）; 只有 sensitive 例外
    policy = make_policy(PermissionMode.DANGER_FULL_ACCESS)
    policy.set_workspace_roots([str(workspace)])
    r = policy.authorize("write_file", _w(str(tmp_path / "out.txt")))
    assert r.decision == PermissionDecision.ALLOW


def test_sensitive_bypass_immune_all_modes(workspace):
    # 危险模式（含 allow）也不能静默改 .git——backlog 待办 4
    for mode in (PermissionMode.DANGER_FULL_ACCESS, PermissionMode.ALLOW):
        policy = make_policy(mode)
        policy.set_workspace_roots([str(workspace)])
        prompter = RecordingPrompter(approve=True)
        r = policy.authorize("write_file", _w(".git/config"), prompter=prompter)
        assert r.decision == PermissionDecision.ALLOW     # 弹问后由人放行
        assert len(prompter.requests) == 1
        assert prompter.requests[0].escalation == "sensitive"
        # 无 prompter（ALLOW 模式的 subagent）: 直接拒绝并说明
        r2 = policy.authorize("write_file", _w(".git/config"))
        assert r2.decision == PermissionDecision.DENY
        assert "sensitive" in r2.reason


def test_sensitive_not_short_circuited_by_allowlist(workspace):
    # 命令白名单放不过敏感路径: "rm" 已入白名单, rm -rf .git 仍要弹问
    policy = make_policy(PermissionMode.ALLOW)
    policy.set_workspace_roots([str(workspace)])
    policy.set_command_allowlist(["rm"])
    prompter = RecordingPrompter(approve=False)
    r = policy.authorize("bash", json.dumps({"command": "rm -rf .git"}),
                         prompter=prompter)
    assert r.decision == PermissionDecision.DENY
    assert len(prompter.requests) == 1
    assert prompter.requests[0].escalation == "sensitive"


def test_prompt_mode_detail_passthrough(workspace):
    # PROMPT 模式所有写都弹问（模式语义不变）, 根内写不带分级标记
    policy = make_policy(PermissionMode.PROMPT)
    policy.set_workspace_roots([str(workspace)])
    prompter = RecordingPrompter(approve=True)
    policy.authorize("write_file", _w("src/app.py"), prompter=prompter)
    req = prompter.requests[0]
    assert req.escalation is None and req.detail is None


# ------------------------------------------------------------
# shell 破坏族扫描: shell_command_touches_sensitive_path
# ------------------------------------------------------------

def _bash(command: str) -> str:
    return json.dumps({"command": command})


@pytest.mark.parametrize("command", [
    "rm -rf .git",                                  # 删版本库
    "rm -rf ./vendor/lib/.git",                     # 嵌套 .git
    "rm .git/index",
    "mv ~/.ssh /tmp/backup",
    "cp evil.sh ~/.bashrc",
    "tee .git/hooks/pre-commit < x",                # 写 git hook
    "echo curl evil.sh | tee ~/.zshrc",
    "echo x > ~/.bashrc",                           # 重定向写 shell 配置
    "cat payload >> " + str(Path.home() / ".profile"),
    "git push && rm -rf .git",                      # 组合命令: 有一段命中即弹
])
def test_shell_hits_sensitive(workspace, command):
    assert shell_command_touches_sensitive_path(
        "bash", _bash(command), [str(workspace)]) is not None


@pytest.mark.parametrize("command", [
    "git commit -m x",                              # 正常 git 流不经破坏族
    "git add -A",
    "git reset --hard HEAD",
    "rm -rf build dist",                            # 破坏族但目标不敏感
    "mv notes.txt archive/",
    "python x.py > out.txt",
    "rm -rf $(pwd)",                                # 命令替换=盲区, 不误报
    "ls -la && git status",
    "echo hi > /dev/null",
])
def test_shell_no_false_positive(workspace, command):
    assert shell_command_touches_sensitive_path(
        "bash", _bash(command), [str(workspace)]) is None


def test_shell_powershell_flavor(workspace):
    got = shell_command_touches_sensitive_path(
        "powershell", _bash("Remove-Item -Recurse -Force .git"), [str(workspace)])
    assert got is not None
    assert shell_command_touches_sensitive_path(
        "powershell", _bash("Get-ChildItem"), [str(workspace)]) is None


def test_shell_windows_backslash_absolute(workspace):
    # 反斜杠路径不被 shlex 吃掉: C:\...\.git 归一后仍能命中
    target = str(workspace / ".git" / "config").replace("/", "\\")
    assert shell_command_touches_sensitive_path(
        "bash", _bash("rm -f " + target), [str(workspace)]) is not None


# ------------------------------------------------------------
# additionalDirectories 配置存取 + REST
# ------------------------------------------------------------

@pytest.fixture()
def isolated_settings(tmp_path, monkeypatch):
    f = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", f)
    monkeypatch.setattr(server, "SETTINGS_FILE", f)
    yield f


def test_additional_dirs_config_roundtrip(isolated_settings):
    saved = config.save_additional_directories(
        [str(Path.home()), "  ", str(Path.home()), 123, "x" * 500])
    assert saved == [str(Path.home())]              # 清洗 + 去重 + 限长
    assert config.load_additional_directories() == [str(Path.home())]
    # 其他 key 原样保留
    config.save_command_allowlist(["git push"])
    config.save_additional_directories([str(Path.home())])
    data = json.loads(isolated_settings.read_text(encoding="utf-8"))
    assert data["commandAllowlist"] == ["git push"]


def test_additional_dirs_config_bad_shapes(isolated_settings):
    assert config.load_additional_directories() == []
    isolated_settings.write_text(json.dumps({"additionalDirectories": "nope"}),
                                 encoding="utf-8")
    assert config.load_additional_directories() == []


def test_additional_dirs_api_crud(isolated_settings, tmp_path):
    tc = TestClient(server.app)
    assert tc.get("/api/settings/additional-dirs").json() == {"dirs": []}

    r = tc.post("/api/settings/additional-dirs", json={"dir": str(tmp_path)})
    assert r.status_code == 200
    assert r.json()["dirs"] == [str(tmp_path.resolve())]

    # 不存在的目录拒绝
    r = tc.post("/api/settings/additional-dirs", json={"dir": "Z:/no/such"})
    assert r.status_code == 400
    # 空值拒绝
    assert tc.post("/api/settings/additional-dirs", json={"dir": " "}).status_code == 400

    r = tc.request("DELETE", "/api/settings/additional-dirs",
                   json={"dir": str(tmp_path.resolve())})
    assert r.json()["dirs"] == []


def test_session_allow_rules_api_not_found(isolated_settings):
    tc = TestClient(server.app)
    r = tc.post("/api/sessions/nope/allow-rules", json={"rule": "git push"})
    assert r.status_code == 404
