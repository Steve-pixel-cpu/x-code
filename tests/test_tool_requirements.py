# -*- coding: utf-8 -*-
"""工具权限档位分级（main.TOOL_REQUIREMENTS → build_runtime 的 policy）。

背景: with_tool_requirement 从前从未接线, 所有工具默认 DANGER_FULL_ACCESS,
workspace-write 与 prompt 模式行为完全一样。现在只读工具有低档位,
让"档位"真正有区别; prompt 模式依旧逐工具弹问（与档位正交）。
"""
from permissions import (
    PermissionDecision,
    PermissionMode,
    PermissionPolicy,
)
from main import TOOL_REQUIREMENTS, build_registry


def _policy(mode):
    p = PermissionPolicy(mode)
    for name, req in TOOL_REQUIREMENTS.items():
        p.with_tool_requirement(name, req)
    return p


def test_registered_requirements_cover_all_registry_tools_except_shell():
    registry = build_registry()
    names = set(registry._handlers)
    assert {"read_file", "write_file", "bash", "powershell",
            "agent_tool", "agent_status", "agent_list"} <= names
    registered = set(TOOL_REQUIREMENTS)
    assert "bash" not in registered and "powershell" not in registered   # 保持默认 DANGER
    assert registered <= names   # 登记的工具都真实存在（拼写错误早暴露）


def test_workspace_write_allows_reads_without_prompt():
    p = _policy(PermissionMode.WORKSPACE_WRITE)
    r = p.authorize("read_file", "a.txt", None)   # 无 prompter: 不该走到问
    assert r.decision == PermissionDecision.ALLOW


def test_workspace_write_still_prompts_for_writes_and_shell():
    p = _policy(PermissionMode.WORKSPACE_WRITE)
    assert p.required_mode_for("write_file") == PermissionMode.WORKSPACE_WRITE
    # workspace-write 遇 DANGER(bash) 仍弹问/拒绝
    r = p.authorize("bash", "ls", None)
    assert r.decision == PermissionDecision.DENY and "approval" in r.reason


def test_plan_mode_denies_writes_but_allows_reads():
    p = _policy(PermissionMode.PLAN)
    assert p.authorize("read_file", "a.txt", None).decision == PermissionDecision.ALLOW
    r = p.authorize("write_file", "{}", None)
    assert r.decision == PermissionDecision.DENY


def test_prompt_mode_ignores_levels_and_asks_anyway():
    """prompt 模式语义 = 逐次都问, 低档位也不例外（正交性）。"""
    from tests.test_permission_prompt import RecordingPrompter
    p = _policy(PermissionMode.PROMPT)
    prompter = RecordingPrompter(approved=True)
    assert p.authorize("read_file", "a.txt", prompter).decision == PermissionDecision.ALLOW
    assert len(prompter.requests) == 1
