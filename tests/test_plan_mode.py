# -*- coding: utf-8 -*-
"""计划模式（PLAN）端到端语义。

链路: plan 模式下模型调 present_plan → policy 判为"可升级"→ prompter
弹问（Web=计划卡, CLI=终端面板）→ 批准后回调把会话升级为
workspace-write, 模型继续实施。

钉住的设计决定:
- present_plan 的 required 档位是 WORKSPACE_WRITE(2): plan(1) 下弹问,
  workspace-write 及以上直接放行（批准过一次不再重复弹）。
- 批准回调把 WebSession + runtime 都切到 workspace-write 并广播
  mode_changed; 拒绝只回理由, 不动模式。
- plan 模式 run_turn 时 system_prompt 末尾带 PLAN_MODE_INSTRUCTION,
  其他模式不带（缓存边界之后, 不影响静态前缀缓存）。
"""
import json

import pytest

from permissions import (
    PermissionDecision,
    PermissionMode,
    PermissionPolicy,
    PermissionRequest,
)
from main import TOOL_REQUIREMENTS, build_registry
from tests.test_permission_prompt import RecordingPrompter


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    import server
    return TestClient(server.app)


@pytest.fixture()
def isolated_store(monkeypatch, tmp_path):
    """server.store 指到临时目录（与 test_server.py 同名夹具同语义）。"""
    import server
    from storage import SessionStore
    st = SessionStore(tmp_path)
    monkeypatch.setattr(server, "store", st, raising=False)
    return st

PLAN = PermissionMode.PLAN
WORKSPACE_WRITE = PermissionMode.WORKSPACE_WRITE
DANGER = PermissionMode.DANGER_FULL_ACCESS


def _policy(mode):
    p = PermissionPolicy(mode)
    for name, req in TOOL_REQUIREMENTS.items():
        p.with_tool_requirement(name, req)
    return p


# ------------------------------------------------------------
# 授权层: present_plan 的档位语义
# ------------------------------------------------------------

def test_present_plan_registered_at_workspace_write():
    assert TOOL_REQUIREMENTS["present_plan"] == WORKSPACE_WRITE
    assert "present_plan" in build_registry()._handlers


def test_plan_mode_prompts_for_present_plan_but_denies_other_writes():
    """plan 模式: present_plan 弹问（升级通道）; write_file/bash 无 prompter
    时 fail-closed——研究期连写文件都不许。"""
    p = _policy(PLAN)
    prompter = RecordingPrompter(approved=True)
    r = p.authorize("present_plan", '{"plan": "# 步骤"}', prompter)
    assert r.decision == PermissionDecision.ALLOW
    assert len(prompter.requests) == 1
    assert prompter.requests[0].required_mode == WORKSPACE_WRITE

    # 其他写工具: plan 模式无 prompter → 拒绝
    r2 = p.authorize("write_file", "{}", None)
    assert r2.decision == PermissionDecision.DENY
    assert "requires" in r2.reason


def test_plan_mode_denies_shell_even_with_prompter():
    """bash 默认 required=DANGER, plan 模式(1) 走不了"相邻升级"弹问:
    与 workspace-write(2)→DANGER(3) 不同, 1→3 差两档, 设计上直接拒绝。
    研究期只许读, 命令执行属于实施阶段。"""
    p = _policy(PLAN)
    prompter = RecordingPrompter(approved=True)
    r = p.authorize("bash", "ls", prompter)
    assert r.decision == PermissionDecision.DENY
    assert prompter.requests == []   # 根本不弹


def test_after_approval_present_plan_no_longer_prompts():
    """批准升级到 workspace-write 后, 修订计划再次 present_plan 不再弹
    （2<=2 快速放行）——重复弹窗会打断实施流。"""
    p = _policy(WORKSPACE_WRITE)
    r = p.authorize("present_plan", '{"plan": "v2"}', None)   # 无 prompter 也不该走到问
    assert r.decision == PermissionDecision.ALLOW


# ------------------------------------------------------------
# runtime: plan 模式注入计划指令段
# ------------------------------------------------------------

def test_run_turn_plan_mode_appends_instruction_to_system_prompt():
    from api_client import MessageStopEvent, TextDeltaEvent
    from models import Session
    from runtime import ConversationRuntime, PLAN_MODE_INSTRUCTION
    from tests.test_runtime import ScriptedApiClient, EchoExecutor

    fake = ScriptedApiClient([[TextDeltaEvent(text="ok"), MessageStopEvent()]])
    rt = ConversationRuntime(
        session=Session(),
        api_client=fake,
        tool_executor=EchoExecutor(),
        permission_policy=_policy(PLAN),
        system_prompt=["static-section"],
    )
    rt.run_turn("做个计划")
    # 指令段作为追加 section 出现, 原 sections 原样在前
    assert fake.calls, "stream 未被调用"
    # ScriptedApiClient.stream 记录的是 messages; system_prompt 用属性另存
    # ——没有记录就直查: 换用带记录的替身再跑一次
    class _Spy(ScriptedApiClient):
        def __init__(self, script):
            super().__init__(script)
            self.system_prompts = []
        def stream(self, system_prompt, messages, thinking_level=None):
            self.system_prompts.append(list(system_prompt))
            return super().stream(system_prompt, messages, thinking_level)

    spy = _Spy([[TextDeltaEvent(text="ok"), MessageStopEvent()]])
    rt2 = ConversationRuntime(
        session=Session(), api_client=spy, tool_executor=EchoExecutor(),
        permission_policy=_policy(PLAN), system_prompt=["static-section"],
    )
    rt2.run_turn("做个计划")
    assert spy.system_prompts[0] == ["static-section", PLAN_MODE_INSTRUCTION]

    # 非计划模式: 原样, 不注入
    spy3 = _Spy([[TextDeltaEvent(text="ok"), MessageStopEvent()]])
    rt3 = ConversationRuntime(
        session=Session(), api_client=spy3, tool_executor=EchoExecutor(),
        permission_policy=_policy(WORKSPACE_WRITE), system_prompt=["static-section"],
    )
    rt3.run_turn("直接干")
    assert spy3.system_prompts[0] == ["static-section"]


def test_run_turn_present_plan_approved_upgrades_mode_mid_turn():
    """批准回调把 policy 切到 workspace-write: 同一轮里 present_plan 之后的
    write_file 直接放行（模拟"批准后立刻实施"）。"""
    from api_client import MessageStopEvent, TextDeltaEvent, ToolUseEvent
    from models import Session
    from runtime import ConversationRuntime
    from tests.test_runtime import ScriptedApiClient, EchoExecutor

    upgrades = []
    fake = ScriptedApiClient([
        [ToolUseEvent(id="t1", name="present_plan",
                      input='{"plan": "# 计划"}'), MessageStopEvent()],
        [ToolUseEvent(id="t2", name="write_file",
                      input='{"path": "a.txt", "content": "x"}'), MessageStopEvent()],
        [TextDeltaEvent(text="done"), MessageStopEvent()],
    ])

    class ApprovingPrompter:
        """批准 present_plan 并执行升级（对齐 server._upgrade_after_plan）。"""
        def __init__(self, policy):
            self.policy = policy
            self.requests = []
        def decide(self, request):
            self.requests.append(request)
            if request.tool_name == "present_plan":
                self.policy.set_mode(WORKSPACE_WRITE)
                upgrades.append(request.tool_name)
                from permissions import PermissionResult
                return PermissionResult(decision=PermissionDecision.ALLOW,
                                        reason="Plan approved.")
            from permissions import PermissionResult
            return PermissionResult(decision=PermissionDecision.DENY,
                                    reason="no")

    policy = _policy(PLAN)
    prompter = ApprovingPrompter(policy)
    rt = ConversationRuntime(
        session=Session(), api_client=fake, tool_executor=EchoExecutor(),
        permission_policy=policy, system_prompt=["s"],
    )
    summary = rt.run_turn("计划并实施", prompter)
    assert upgrades == ["present_plan"]
    # write_file 在升级后的模式下执行成功（否则会以 error result 出现）
    err = [b for m in summary.tool_results for b in m.content if b.is_error]
    assert err == []
    assert rt.permission_mode() == WORKSPACE_WRITE


# ------------------------------------------------------------
# server: 批准升级回调 + 计划卡协议
# ------------------------------------------------------------

def test_web_prompter_plan_approval_triggers_upgrade_and_broadcast(client, isolated_store):
    """WebPermissionPrompter: present_plan 批准 → 升级回调跑 + mode_changed
    广播; ALLOW 带实施指令 reason。"""
    import server

    web_session = server.get_or_create_web_session("s-plan-ok")
    received = []
    token = web_session.add_emit(lambda payload: received.append(payload))
    try:
        events = []
        prompter = server.WebPermissionPrompter(
            events.append,
            on_plan_approved=lambda: received.append({"type": "mode_changed"}),
        )
        req = PermissionRequest(
            tool_name="present_plan", input='{"plan": "# t"}',
            current_mode=PLAN, required_mode=WORKSPACE_WRITE,
        )
        import threading
        th = threading.Thread(target=lambda: prompter.decide(req))
        th.start()
        # 等 permission_request 事件推出来
        for _ in range(100):
            if events:
                break
            import time; time.sleep(0.01)
        assert events and events[0]["type"] == "permission_request"
        assert events[0]["tool_name"] == "present_plan"

        prompter.resolve(events[0]["request_id"], True)
        th.join(timeout=2)
        assert not th.is_alive()
        assert received[-1]["type"] == "mode_changed"   # 升级回调跑过
    finally:
        web_session.remove_emit(token)
        server._sessions.clear()


def test_web_prompter_plan_rejection_returns_revision_reason(client, isolated_store):
    """计划被拒: 回流"修订后再提交"的指引, 不弹升级。"""
    import server

    web_session = server.get_or_create_web_session("s-plan-no")
    try:
        events = []
        ran = []
        prompter = server.WebPermissionPrompter(
            events.append, on_plan_approved=lambda: ran.append(1))
        req = PermissionRequest(
            tool_name="present_plan", input="{}",
            current_mode=PLAN, required_mode=WORKSPACE_WRITE,
        )
        import threading, time
        th = threading.Thread(target=lambda: prompter.decide(req))
        th.start()
        for _ in range(100):
            if events:
                break
            time.sleep(0.01)
        prompter.resolve(events[0]["request_id"], False)
        th.join(timeout=2)
        # decide 返回值在线程里拿不到, 断言拒绝路径的副作用:
        # 升级回调未跑; prompter 本身不再推 denied tool_result——
        # 该镜像统一由 runtime 的 on_tool_finalized 回调发射
        # （server._emit_finalized_tool_result, 见下方专用测试）
        assert ran == []
        assert [e for e in events if e["type"] == "tool_result"] == []
    finally:
        server._sessions.clear()


def test_finalize_callback_emits_plan_rejected_marker(client, isolated_store):
    """present_plan 被拒: 终局回调发的 tool_result 带 plan_rejected 标记
    （前端计划卡自己渲染拒绝态, 不再补失败工具卡）, 理由回流给模型。"""
    import server
    from models import Message, ToolContentBlock

    out = []
    server.dispatch.bind(out.append)
    try:
        block = ToolContentBlock(id="plan-1", name="present_plan",
                                 input="{}")
        result = Message.tool_result(id="plan-1", name="present_plan",
                                     output="Plan rejected by the user. "
                                     "Revise the plan per the feedback.",
                                     is_error=True)
        server._emit_finalized_tool_result(block, result)
    finally:
        server.dispatch.unbind()
    assert len(out) == 1
    payload = out[0]
    assert payload["type"] == "tool_result"
    assert payload["id"] == "plan-1"
    assert payload["plan_rejected"] is True
    assert payload["is_error"] is True
    assert "Revise" in payload["output"] or "revise" in payload["output"]


def test_start_turn_wires_upgrade_callback(client, isolated_store, monkeypatch):
    """_start_turn 构造的 prompter 带升级回调: 回调切 WebSession+runtime
    并广播 mode_changed。"""
    import server

    web_session = server.get_or_create_web_session("s-plan-wire")
    try:
        class _Rt:
            def __init__(self):
                self.modes = []
            def set_permission_mode(self, m):
                self.modes.append(m)
            def session(self):
                from models import Session
                return Session()

        web_session.runtime = _Rt()
        web_session.permission_mode = server.NAME_TO_MODE["plan"]
        # _start_turn 里闭包的装配太重（起线程）, 这里复现同一段装配验证:
        # 升级回调 → WebSession 模式 + runtime 模式 + mode_changed 广播
        broadcasts = []
        web_session.broadcast = lambda payload: broadcasts.append(payload)

        def _upgrade_after_plan():
            web_session.permission_mode = server.WORKSPACE_WRITE_MODE
            if web_session.runtime is not None:
                web_session.runtime.set_permission_mode(server.WORKSPACE_WRITE_MODE)
            web_session.broadcast({
                "type": "mode_changed",
                "session_id": web_session.session_id,
                "permission_mode": server.MODE_TO_NAME[server.WORKSPACE_WRITE_MODE],
            })

        prompter = server.WebPermissionPrompter(
            lambda payload: None, on_plan_approved=_upgrade_after_plan)
        req = PermissionRequest(
            tool_name="present_plan", input="{}",
            current_mode=PLAN, required_mode=WORKSPACE_WRITE)
        import threading, time
        th = threading.Thread(target=lambda: prompter.decide(req))
        th.start()
        th.join(timeout=2)   # 无事件消费也不挂: sink 只是被忽略
        prompter.resolve("perm-1", True)
        th.join(timeout=2)
        assert not th.is_alive()
        assert web_session.permission_mode == server.WORKSPACE_WRITE_MODE
        assert web_session.runtime.modes == [server.WORKSPACE_WRITE_MODE]
        assert broadcasts and broadcasts[-1]["permission_mode"] == "workspace-write"
    finally:
        server._sessions.clear()
