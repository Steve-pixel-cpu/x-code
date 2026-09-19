# -*- coding: utf-8 -*-
"""权限策略: PROMPT 模式必须逐工具弹问, 而非被 >= 数值比较放行。

背景 bug: 工具 required_mode 默认是 DANGER_FULL_ACCESS(3), 而
PROMPT(4)/ALLOW(5) 数值更大, 旧逻辑 `current >= required` 把 prompt
模式的所有请求都当"权限足够"直接放行——用户选了"每次询问"却一次
也不会被问。修复后: ALLOW 仍是唯一跳过一切的档位; PROMPT 一律走
prompter（无 prompter 时 fail-closed 拒绝）。
"""
import pytest

from permissions import (
    PermissionDecision,
    PermissionMode,
    PermissionPolicy,
    PermissionRequest,
    PermissionResult,
)

PROMPT = PermissionMode.PROMPT
ALLOW = PermissionMode.ALLOW
READ_ONLY = PermissionMode.READ_ONLY
WORKSPACE_WRITE = PermissionMode.WORKSPACE_WRITE
DANGER = PermissionMode.DANGER_FULL_ACCESS


class RecordingPrompter:
    """记录收到的请求, 按预设决定放行/拒绝。"""

    def __init__(self, approved=True):
        self.approved = approved
        self.requests: list[PermissionRequest] = []

    def decide(self, request: PermissionRequest) -> PermissionResult:
        self.requests.append(request)
        decision = PermissionDecision.ALLOW if self.approved else PermissionDecision.DENY
        return PermissionResult(decision=decision, reason="")


def test_prompt_mode_asks_for_every_tool_even_low_requirement():
    """prompt 模式下连读文件也要问（required 只是 read-only 级也别放行）。"""
    policy = PermissionPolicy(PROMPT).with_tool_requirement("read_file", READ_ONLY)
    p = RecordingPrompter(approved=True)
    r = policy.authorize("read_file", "a.txt", p)
    assert r.decision == PermissionDecision.ALLOW
    assert len(p.requests) == 1
    assert p.requests[0].required_mode == READ_ONLY


def test_prompt_mode_default_requirement_still_asks():
    """未登记的工具（默认 required=DANGER）同样要问——旧 bug 正是放过了它。"""
    policy = PermissionPolicy(PROMPT)
    p = RecordingPrompter(approved=True)
    assert policy.authorize("bash", "ls", p).decision == PermissionDecision.ALLOW
    assert len(p.requests) == 1


def test_prompt_mode_deny_and_no_prompter_fail_closed():
    policy = PermissionPolicy(PROMPT)
    p = RecordingPrompter(approved=False)
    assert policy.authorize("bash", "rm", p).decision == PermissionDecision.DENY
    # 无 prompter: 无人可问 → fail-closed, 不能悄悄放行
    r = policy.authorize("bash", "rm", None)
    assert r.decision == PermissionDecision.DENY
    assert "prompt" in r.reason


def test_prompt_mode_each_call_asks_again():
    """逐工具逐次: 两次调用弹两次, 上次的批准不延续。"""
    policy = PermissionPolicy(PROMPT)
    p = RecordingPrompter(approved=True)
    policy.authorize("bash", "ls", p)
    policy.authorize("write_file", "x", p)
    assert len(p.requests) == 2


def test_allow_mode_still_skips_everything():
    policy = PermissionPolicy(ALLOW)
    p = RecordingPrompter(approved=True)
    assert policy.authorize("bash", "anything", p).decision == PermissionDecision.ALLOW
    assert p.requests == []   # 从不打扰


def test_workspace_write_still_escalates_to_prompter():
    """原有语义保持: workspace-write 遇 DANGER 级工具仍弹问。"""
    policy = PermissionPolicy(WORKSPACE_WRITE).with_tool_requirement("bash", DANGER)
    p = RecordingPrompter(approved=True)
    assert policy.authorize("bash", "ls", p).decision == PermissionDecision.ALLOW
    assert len(p.requests) == 1
    # read-only 级工具在 workspace-write 下直接放行, 不打扰
    policy2 = PermissionPolicy(WORKSPACE_WRITE).with_tool_requirement("read_file", READ_ONLY)
    p2 = RecordingPrompter(approved=True)
    assert policy2.authorize("read_file", "a", p2).decision == PermissionDecision.ALLOW
    assert p2.requests == []


def test_read_only_denies_danger_tools_without_prompter():
    policy = PermissionPolicy(READ_ONLY).with_tool_requirement("bash", DANGER)
    r = policy.authorize("bash", "rm", None)
    assert r.decision == PermissionDecision.DENY
