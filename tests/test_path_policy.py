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


# ------------------------------------------------------------
# 端到端金丝雀: 危险命令在授权层被拦, 永远到不了执行层
# ------------------------------------------------------------

def _e2e_workspace(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core] canary\n", encoding="utf-8")
    (root / "notes.txt").write_text("canary\n", encoding="utf-8")
    return root


def _e2e_call(policy, tool_name, tool_input, ws, handlers, prompter=None):
    """复刻 runtime._authorize_tool_use 的真实顺序: 先授权, 放行才执行。"""
    from tools import bash_tool
    r = policy.authorize(tool_name, tool_input, prompter=prompter)
    if r.decision == PermissionDecision.DENY:
        return "DENIED", r.reason
    handler = bash_tool if tool_name == "bash" else handlers["write"]
    return "EXECUTED", handler(json.loads(tool_input), str(ws))


def test_dangerous_commands_never_execute_without_approval(tmp_path, monkeypatch):
    """金丝雀证明: 各种权限档位下, 危险命令全部止步于授权层。
    任何一条真的执行了, 金丝雀文件就会变——这个测试直接断言文件内容。"""
    from tools import bash_tool, write_tool

    handlers = {"write": write_tool}
    monkeypatch.chdir(tmp_path)   # 根外目录断言不受 cwd 漂移影响

    ws = _e2e_workspace(tmp_path / "ws")
    policy = PermissionPolicy(PermissionMode.WORKSPACE_WRITE)
    for name, required in TOOL_REQUIREMENTS_FIXTURE().items():
        policy.with_tool_requirement(name, required)
    policy.set_workspace_roots([str(ws)])

    # 无人批准(无 prompter = 自动化/subagent 场景): 危险命令全拒
    for command in ("rm -rf .git", "mv .git /tmp/stolen",
                    "echo evil > .git/hooks/pre-commit",
                    "git push --force && rm -rf .git"):
        verdict, reason = _e2e_call(policy, "bash",
                                    json.dumps({"command": command}), ws, handlers)
        assert verdict == "DENIED", command
    # 根外写拒 + 敏感路径写拒
    verdict, _ = _e2e_call(policy, "write_file", _w(str(tmp_path / "outside" / "x.txt")), ws, handlers)
    assert verdict == "DENIED"
    verdict, _ = _e2e_call(policy, "write_file", _w(".git/config", "hacked"), ws, handlers)
    assert verdict == "DENIED"
    # 金丝雀完好 = 命令真的没被执行, 而不只是"声称拒绝"
    assert (ws / ".git" / "config").read_text(encoding="utf-8") == "[core] canary\n"
    assert (ws / "notes.txt").read_text(encoding="utf-8") == "canary\n"
    assert not (tmp_path / "outside" / "x.txt").exists()


def test_allowlist_cannot_exempt_sensitive_targets(tmp_path):
    """用户把 rm 加进白名单 + allow 模式: rm .git 仍被 bypass-immune 拦下;
    白名单内且目标不敏感的命令照常放行(不误伤)。"""
    from tools import bash_tool

    ws = _e2e_workspace(tmp_path / "ws")
    policy = (PermissionPolicy(PermissionMode.ALLOW)
              .with_tool_requirement("bash", PermissionMode.DANGER_FULL_ACCESS)
              .set_workspace_roots([str(ws)])
              .set_command_allowlist(["rm"]))
    verdict, _ = _e2e_call(policy, "bash", json.dumps({"command": "rm -rf .git"}),
                           ws, {"write": None})
    assert verdict == "DENIED"
    verdict, _ = _e2e_call(policy, "bash", json.dumps({"command": "rm notes.txt"}),
                           ws, {"write": None})
    assert verdict == "EXECUTED"           # 白名单内的合法删除放行
    assert (ws / ".git" / "config").read_text(encoding="utf-8") == "[core] canary\n"
    assert not (ws / "notes.txt").exists()


def TOOL_REQUIREMENTS_FIXTURE():
    """与 main.TOOL_REQUIREMENTS 同源的最小档位登记(避免测试依赖 main 导入)。"""
    return {"write_file": PermissionMode.WORKSPACE_WRITE,
            "edit_file": PermissionMode.WORKSPACE_WRITE,
            "read_file": PermissionMode.PLAN}


# ------------------------------------------------------------
# 工具层/存储层的健壮性(事故: read_file 读 PNG 把会话 JSONL 撕出坏行)
# ------------------------------------------------------------

def test_read_tool_refuses_binary_file(tmp_path):
    from tools import read_tool
    png = tmp_path / "x.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00" + b"\xff" * 100)
    out = read_tool({"path": str(png)}, str(tmp_path))
    assert out.startswith("ERROR:")
    assert "binary" in out
    # 文本文件(含 CJK/emoji, 无 NUL)照常可读
    txt = tmp_path / "a.txt"
    txt.write_text("你好 world\n", encoding="utf-8")
    assert "你好 world" in read_tool({"path": str(txt)}, str(tmp_path))


def test_store_append_survives_hostile_content(tmp_path):
    """工具结果带裸换行/控制字符/代理字符时, JSONL 每条记录仍恰好一行:
    json.dumps 转义控制字符, 代理字符就地转 U+FFFD, 落盘永不吐裸字节。"""
    from storage import SessionStore
    from models import Message
    store = SessionStore(tmp_path)
    hostile = ("line1\nline2\r\n\x00null \x1f ctrl \ufffd replacement "
               "\udc80 surrogate \udcff pair")
    store.save_message("s-x", Message.tool_result(
        id="t1", name="bash", output=hostile, is_error=False), parent_uuid=None)
    raw = (tmp_path / "s-x.jsonl").read_bytes()
    assert raw.count(b"\n") == 1                       # 单条记录单行, 无裸字节
    lines = raw.decode("utf-8").splitlines()           # 能按 utf-8 解码
    loaded = json.loads(lines[0])                      # 且是合法 JSON
    out = loaded["message"]["content"][0]["output"]
    assert "line1" in out and "line2" in out           # 内容保真(转义形态)
