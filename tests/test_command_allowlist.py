"""命令前缀白名单: 匹配语义 / 策略行为 / 配置存取 / REST 端点。

事故驱动: 权限审批体验差——每条 shell 命令都要手点"允许"。加"总是允许
（前缀入白名单）": 规则存 ~/.x-code/settings.json 的 commandAllowlist,
授权层按 shlex 词对齐前缀匹配, 组合命令每段都必须命中, 带命令替换或
写文件重定向的命令一律不命中（保守方向, 与只读白名单同一口径）。

运行: uv run pytest tests/test_command_allowlist.py -v
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import permissions
import server
from permissions import (
    NAME_TO_MODE,
    PermissionDecision,
    PermissionMode,
    PermissionPolicy,
    PermissionRequest,
    shell_command_matches_allowlist,
)


# ------------------------------------------------------------
# 匹配语义: shell_command_matches_allowlist
# ------------------------------------------------------------

def _cmd(command: str, tool: str = "bash") -> str:
    return json.dumps({"command": command})


RULES = ["git push", "uv run pytest", "npm test"]


@pytest.mark.parametrize("command,want", [
    ("git push origin main", "git push"),        # 词对齐前缀命中
    ("GIT PUSH origin main", "git push"),        # 大小写不敏感
    ("FOO=1 git push", "git push"),              # 前缀环境变量赋值剥掉
    ("uv run pytest tests/ -x", "uv run pytest"),
    ("git push && git status", None),            # 组合命令: 第二段没覆盖
    ("git status && git push", None),            # 第一段没覆盖
    ("git push; rm -rf /", None),                # 分号段未覆盖
    ("git push | cat", None),                    # 管道段未覆盖
    ("git push > out.txt", None),                # 写文件重定向: 一票否决
    ("git push 2>&1 | cat", None),               # 管道第二段没覆盖
    ("git push >/dev/null", "git push"),         # /dev/null 丢弃输出: 无害豁免, 命中
    ("git push 2>&1", "git push"),               # 2>&1 只重定向流: 无害豁免, 命中
    ("echo hi > /dev/null", None),               # 段本身没被任何规则覆盖
    ("git push $(echo hi)", None),               # 命令替换: 一票否决
    ("git push `echo hi`", None),                # 反引号: 一票否决
    ("gitpush", None),                           # 非词边界: "git" 不命中 "gitpush"
    ("git push --force", "git push"),            # 规则之外的尾部参数不管
])
def test_match_semantics(command, want):
    assert shell_command_matches_allowlist("bash", _cmd(command), RULES) == want


def test_match_powershell_case_insensitive():
    assert shell_command_matches_allowlist(
        "powershell", _cmd("Git Push -Force origin"), ["git push"]) == "git push"


def test_match_non_shell_tool_never_matches():
    # 白名单是 shell 语义: edit_file 等其他工具不吃这套
    assert shell_command_matches_allowlist(
        "edit_file", json.dumps({"path": "x", "old_string": "git push"}), RULES) is None


def test_match_empty_or_broken_input():
    assert shell_command_matches_allowlist("bash", "not json", RULES) is None
    assert shell_command_matches_allowlist("bash", _cmd("   "), RULES) is None
    assert shell_command_matches_allowlist("bash", _cmd("git push"), []) is None
    assert shell_command_matches_allowlist("bash", _cmd("git push"), ["", "  "]) is None


# ------------------------------------------------------------
# 策略行为: PermissionPolicy.authorize 接线
# ------------------------------------------------------------

@pytest.fixture()
def policy_workspace_write():
    return PermissionPolicy(PermissionMode.WORKSPACE_WRITE)


def test_policy_allowlist_hit_allows(policy_workspace_write):
    policy_workspace_write.set_command_allowlist(["git push"])
    r = policy_workspace_write.authorize("bash", _cmd("git push origin main"))
    assert r.decision == PermissionDecision.ALLOW
    assert "git push" in (r.reason or "")


def test_policy_allowlist_miss_still_prompt(policy_workspace_write):
    policy_workspace_write.set_command_allowlist(["git push"])
    r = policy_workspace_write.authorize("bash", _cmd("python x.py"))
    assert r.decision != PermissionDecision.ALLOW   # 走原档位逻辑


def test_policy_allowlist_applies_in_prompt_mode():
    # PROMPT 模式（弹问档）同样先过白名单——这是本功能的主场景
    policy = PermissionPolicy(PermissionMode.PROMPT)
    policy.set_command_allowlist(["uv run pytest"])
    r = policy.authorize("bash", _cmd("uv run pytest -q"))
    assert r.decision == PermissionDecision.ALLOW


def test_policy_empty_allowlist_noop(policy_workspace_write):
    policy_workspace_write.set_command_allowlist([])
    r = policy_workspace_write.authorize("bash", _cmd("git push"))
    assert r.decision != PermissionDecision.ALLOW


def test_policy_allowlist_not_for_other_tools(policy_workspace_write):
    policy_workspace_write.set_command_allowlist(["git push"])
    r = policy_workspace_write.authorize(
        "write_file", json.dumps({"path": "x", "content": "git push"}))
    assert r.decision != PermissionDecision.ALLOW


# ------------------------------------------------------------
# 会话级白名单: add_session_allow_rule（不落盘, 会话结束失效）
# ------------------------------------------------------------

def test_policy_session_rule_allows(policy_workspace_write):
    # 只写会话规则, 未配置全局规则——PROMPT/workspace-write 下同前缀放行
    policy_workspace_write.add_session_allow_rule("uv run pytest")
    r = policy_workspace_write.authorize("bash", _cmd("uv run pytest -q"))
    assert r.decision == PermissionDecision.ALLOW
    assert "session allowlist" in (r.reason or "")


def test_policy_session_rule_independent_from_global(policy_workspace_write):
    policy_workspace_write.set_command_allowlist(["git push"])
    policy_workspace_write.add_session_allow_rule("uv run pytest")
    # 两边规则各自命中, 理由标注来源
    r1 = policy_workspace_write.authorize("bash", _cmd("git push origin"))
    assert "user allowlist" in r1.reason
    r2 = policy_workspace_write.authorize("bash", _cmd("uv run pytest -q"))
    assert "session allowlist" in r2.reason
    # 没被任何一侧覆盖的命令照旧走原档位逻辑
    r3 = policy_workspace_write.authorize("bash", _cmd("python x.py"))
    assert r3.decision != PermissionDecision.ALLOW


def test_policy_session_rule_dedup_and_clean(policy_workspace_write):
    policy_workspace_write.add_session_allow_rule("  git\t push ")
    policy_workspace_write.add_session_allow_rule("git push")   # normcase 后重复
    assert policy_workspace_write._session_allowlist == ["git push"]


def test_policy_session_rule_empty_noop(policy_workspace_write):
    policy_workspace_write.add_session_allow_rule("   ")
    r = policy_workspace_write.authorize("bash", _cmd("git push"))
    assert r.decision != PermissionDecision.ALLOW


# ------------------------------------------------------------
# 配置存取: config.load/save_command_allowlist
# ------------------------------------------------------------

@pytest.fixture()
def settings_file(tmp_path, monkeypatch):
    f = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", f)
    yield f


def test_config_roundtrip_preserves_other_keys(settings_file):
    settings_file.write_text(json.dumps(
        {"providers": [{"id": "default"}], "permissionMode": "plan"}),
        encoding="utf-8")
    saved = config.save_command_allowlist(["git push", "git push", "  uv run pytest  "])
    assert saved == ["git push", "uv run pytest"]           # 去重 + 清洗
    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert data["providers"] == [{"id": "default"}]          # 其他 key 原样
    assert data["permissionMode"] == "plan"
    assert config.load_command_allowlist() == ["git push", "uv run pytest"]


def test_config_missing_or_corrupt_file(settings_file):
    assert config.load_command_allowlist() == []
    settings_file.write_text("{broken", encoding="utf-8")
    assert config.load_command_allowlist() == []
    config.save_command_allowlist(["git push"])              # 损坏文件上也能写
    assert config.load_command_allowlist() == ["git push"]


def test_config_rejects_bad_shapes(settings_file):
    settings_file.write_text(json.dumps({"commandAllowlist": "nope"}), encoding="utf-8")
    assert config.load_command_allowlist() == []
    saved = config.save_command_allowlist(["ok", 123, None, "", "x" * 500])
    assert saved == ["ok"]                                   # 非字符串/空/超长剔除


# ------------------------------------------------------------
# REST 端点: /api/settings/allowlist
# ------------------------------------------------------------

@pytest.fixture()
def isolated_settings(tmp_path, monkeypatch):
    """server 与 config 共用同一 SETTINGS_FILE 常量引用, 一并隔离。"""
    f = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", f)
    monkeypatch.setattr(server, "SETTINGS_FILE", f)
    yield f


def test_allowlist_api_crud(isolated_settings):
    tc = TestClient(server.app)
    assert tc.get("/api/settings/allowlist").json() == {"rules": []}

    r = tc.post("/api/settings/allowlist", json={"rule": "git push"})
    assert r.status_code == 200
    assert r.json()["rules"] == ["git push"]

    # 幂等: 重复添加不产生重复项
    r = tc.post("/api/settings/allowlist", json={"rule": "git push"})
    assert r.json()["rules"] == ["git push"]

    r = tc.post("/api/settings/allowlist", json={"rule": "uv run pytest"})
    assert r.json()["rules"] == ["git push", "uv run pytest"]

    r = tc.request("DELETE", "/api/settings/allowlist",
                   json={"rule": "git push"})
    assert r.json()["rules"] == ["uv run pytest"]
    assert json.loads(isolated_settings.read_text(encoding="utf-8"))[
        "commandAllowlist"] == ["uv run pytest"]


def test_allowlist_api_validation(isolated_settings):
    tc = TestClient(server.app)
    assert tc.post("/api/settings/allowlist", json={"rule": "  "}).status_code == 400
    assert tc.post("/api/settings/allowlist", json={}).status_code == 400
    assert tc.request("DELETE", "/api/settings/allowlist",
                      json={"rule": ""}).status_code == 400


def test_permission_respond_session_not_found(isolated_settings):
    tc = TestClient(server.app)
    r = tc.post("/api/permissions/respond",
                json={"session_id": "nope", "request_id": "x", "approved": True})
    assert r.status_code == 404


# ------------------------------------------------------------
# CLI prompter: y/a/s/N 记忆选项（审批疲劳的 CLI 端出口）
# ------------------------------------------------------------

def _bash_request() -> PermissionRequest:
    return PermissionRequest(
        tool_name="bash",
        input=json.dumps({"command": "git push origin main"}),
        current_mode=PermissionMode.WORKSPACE_WRITE,
        required_mode=PermissionMode.DANGER_FULL_ACCESS)


def test_cli_prompter_always_adds_global_rule(monkeypatch):
    from main import CliPermissionPrompter
    seen: dict = {}
    prompter = CliPermissionPrompter(
        on_always=lambda rule: seen.setdefault("always", rule),
        on_session=lambda rule: seen.setdefault("session", rule))
    monkeypatch.setattr("builtins.input", lambda *a: "a")
    r = prompter.decide(_bash_request())
    assert r.decision == PermissionDecision.ALLOW
    assert seen["always"] == "git push"          # 前 ≤2 词规则（与 Web 同口径）
    assert "session" not in seen


def test_cli_prompter_session_option(monkeypatch):
    from main import CliPermissionPrompter
    seen: dict = {}
    prompter = CliPermissionPrompter(
        on_always=lambda rule: seen.setdefault("always", rule),
        on_session=lambda rule: seen.setdefault("session", rule))
    monkeypatch.setattr("builtins.input", lambda *a: "s")
    r = prompter.decide(_bash_request())
    assert r.decision == PermissionDecision.ALLOW
    assert seen["session"] == "git push"
    assert "always" not in seen


def test_cli_prompter_default_and_non_shell(monkeypatch):
    from main import CliPermissionPrompter
    prompter = CliPermissionPrompter()
    # 回车 = 拒绝（朝安全侧）
    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert prompter.decide(_bash_request()).decision == PermissionDecision.DENY
    # 非 shell 工具没有记忆选项: "a" 不识别, 落到拒绝
    monkeypatch.setattr("builtins.input", lambda *a: "a")
    req = PermissionRequest(
        tool_name="write_file", input=json.dumps({"path": "x", "content": "y"}),
        current_mode=PermissionMode.WORKSPACE_WRITE,
        required_mode=PermissionMode.WORKSPACE_WRITE)
    assert prompter.decide(req).decision == PermissionDecision.DENY
