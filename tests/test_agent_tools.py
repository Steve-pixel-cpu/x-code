"""多 agent 接线测试: agent_tools.py（工具层）+ multi_agent.py 新增改动。

覆盖:
- TOOLS/registry 与 agent 工具的一致性（spec ↔ handler ↔ 白名单）
- workdir 参数从 registry 流到 AgentJob（CLI None / Web 会话目录两条路径）
- _tool_specs_for 规格过滤（递归防护的 API 层）
- spawn → complete → status/reap 读回 result 的闭环
- 收割机制: reap_ready / mark_delivered / reconcile_orphans
- 会话隔离: A 会话不收 B 会话的结果
- _AgentToolbox 单例与 set_api_config_provider 的注入/回落
"""
import json
import shutil
from pathlib import Path
import tempfile

import pytest

import agent_tools
from agent_tools import (
    AGENT_TOOL_SPECS,
    DEFAULT_SESSION,
    agent_list_tool,
    agent_status_tool,
    bind_agent_to_session,
    get_orchestrator,
    reap_ready_tool,
    register_agent_tools,
    spawn_agent_tool,
    _session_bindings,
)
from main import TOOLS, build_registry
from multi_agent import (
    TOOL_WHITELIST,
    AgentOrchestrator,
    _default_api_config,
    _tool_specs_for,
    set_api_config_provider,
)


class RecordingSpawn:
    def __init__(self, error: Exception | None = None):
        self.jobs = []
        self._error = error

    def __call__(self, job) -> None:
        if self._error is not None:
            raise self._error
        self.jobs.append(job)


@pytest.fixture(autouse=True)
def fake_orchestrator():
    """每个用例: 单例换成假 spawn 的编排器 + 清空会话归属（不真正起线程）。

    spawn_fn 必须注入 no-op: 缺省会走 _default_spawn_fn——起真实 worker
    线程打真实 LLM, 且线程晚到的 FAILED 终态会覆盖用例刚写入的
    COMPLETED（随机翻转状态, 就是这批用例偶发红掉的第二个根因）。
    这里所有用例都是自己调 complete_agent 模拟 worker 收尾, 不需要真线程。"""
    tmp = tempfile.mkdtemp()
    orch = AgentOrchestrator(Path(tmp), spawn_fn=lambda job: None)
    agent_tools._toolbox._orchestrator = orch
    _session_bindings.clear()
    yield orch
    _session_bindings.clear()
    shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------
# 1. spec ↔ registry ↔ 白名单 一致性
# ------------------------------------------------------------

def test_tools_include_agent_quartet():
    names = [s["name"] for s in TOOLS]
    assert names[-4:] == ["agent_tool", "agent_status", "agent_reap", "agent_list"]


def test_agent_tool_specs_match_registry_handlers():
    registry = build_registry()
    for spec in AGENT_TOOL_SPECS:
        assert spec["name"] in registry._handlers, spec["name"]


def test_agent_tools_absent_from_worker_whitelists():
    """递归防护: 任何角色的白名单都不含 agent 工具（名字层面）。"""
    for role, tools in TOOL_WHITELIST.items():
        for t in ("agent_tool", "agent_status", "agent_reap", "agent_list"):
            assert t not in tools, f"{role}/{t}"


# ------------------------------------------------------------
# 2. 规格过滤（worker 请求里看不到白名单外的工具）
# ------------------------------------------------------------

def test_tool_specs_for_filters_by_whitelist():
    specs = _tool_specs_for(TOOL_WHITELIST["explore"])
    assert [s["name"] for s in specs] == ["read_file"]


def test_tool_specs_for_general_has_no_agent_tools():
    names = [s["name"] for s in _tool_specs_for(TOOL_WHITELIST["general"])]
    assert "agent_tool" not in names
    assert set(names) == set(TOOL_WHITELIST["general"])


def test_tool_specs_for_unknown_tool_is_skipped():
    assert _tool_specs_for({"not-a-real-tool"}) == []


# ------------------------------------------------------------
# 3. handler 的错误路径与空态
# ------------------------------------------------------------

def test_status_unknown_id_returns_error_string():
    out = agent_status_tool({"agent_id": "ghost"})
    assert "ERROR" in out and "ghost" in out


def test_status_requires_agent_id():
    assert "ERROR" in agent_status_tool({})


def test_list_empty_state():
    assert agent_list_tool({}) == "(no subagents yet)"


def test_spawn_rejects_empty_input_via_tool_error():
    out = spawn_agent_tool({"description": "  ", "prompt": "p"})
    assert out.startswith("ERROR:")


# ------------------------------------------------------------
# 4. workdir 参数流: registry → handler → AgentJob
# ------------------------------------------------------------

def test_spawn_via_registry_passes_workdir_to_job():
    rec = RecordingSpawn()
    orch = AgentOrchestrator(Path(tempfile.mkdtemp()), spawn_fn=rec)
    agent_tools._toolbox._orchestrator = orch
    registry = build_registry()
    out = registry.execute(
        "agent_tool",
        json.dumps({"description": "d", "prompt": "p", "subagent_type": "verification"}),
        workdir="D:/some/project",
    )
    assert "agent_id" in out
    assert rec.jobs[0].workdir == "D:/some/project"
    assert rec.jobs[0].allowed_tools == set(TOOL_WHITELIST["verification"])


def test_spawn_without_workdir_defaults_to_none():
    rec = RecordingSpawn()
    agent_tools._toolbox._orchestrator = AgentOrchestrator(
        Path(tempfile.mkdtemp()), spawn_fn=rec)
    spawn_agent_tool({"description": "d", "prompt": "p"}, workdir=None)
    assert rec.jobs[0].workdir is None


def test_explicit_job_workdir_beats_orchestrator_default():
    """spawn_agent 的 workdir 参数优先于编排器构造时的默认值。"""
    rec = RecordingSpawn()
    orch = AgentOrchestrator(Path(tempfile.mkdtemp()),
                             spawn_fn=rec, workdir="D:/default")
    orch.spawn_agent("d", "p", workdir="D:/override")
    assert rec.jobs[0].workdir == "D:/override"
    orch.spawn_agent("d", "p")
    assert rec.jobs[1].workdir == "D:/default"


# ------------------------------------------------------------
# 5. 闭环: spawn → complete → status / reap 读回 result
# ------------------------------------------------------------

def _spawn_and_complete(description="调查登录bug", prompt="读代码", result="bug 在 auth.py:42"):
    """spawn（经 handler, 会登记归属）→ 直接标记 completed（模拟 worker 收尾）。"""
    out = spawn_agent_tool({"description": description, "prompt": prompt})
    agent_id = out.split("agent_id: ")[1].split("\n")[0]
    fake_orchestrator_fixture = agent_tools._toolbox._orchestrator
    fake_orchestrator_fixture.complete_agent(agent_id, result)
    return agent_id


def test_status_roundtrip_after_complete():
    agent_id = _spawn_and_complete()
    data = json.loads(agent_status_tool({"agent_id": agent_id}))
    assert data["status"] == "completed"
    assert data["result"] == "bug 在 auth.py:42"
    assert data["delivered"] is False   # 还没收割


def test_status_running_state_hints_polling():
    out = spawn_agent_tool({"description": "d", "prompt": "p"})
    agent_id = out.split("agent_id: ")[1].split("\n")[0]
    status_out = agent_status_tool({"agent_id": agent_id})
    assert '"status": "running"' in status_out
    assert "Still running" in status_out


def test_list_shows_undelivered_marker():
    _spawn_and_complete()
    listing = agent_list_tool({})
    assert "(undelivered)" in listing


def test_list_after_reap_has_no_undelivered_marker():
    _spawn_and_complete()
    reap_ready_tool({})
    assert "(undelivered)" not in agent_list_tool({})


# ------------------------------------------------------------
# 6. 收割机制
# ------------------------------------------------------------

def test_reap_returns_result_and_marks_delivered():
    agent_id = _spawn_and_complete(result="结论: 内存泄漏在 cache.py")
    out = reap_ready_tool({})
    assert agent_id in out
    assert "内存泄漏在 cache.py" in out
    # 再收一次: 已标记 delivered, 不重复注入
    out2 = reap_ready_tool({})
    assert "no pending results" in out2
    # manifest 落盘状态正确
    data = json.loads(agent_status_tool({"agent_id": agent_id}))
    assert data["delivered"] is True


def test_reap_empty_state_message():
    assert "no pending results" in reap_ready_tool({})


def test_reap_skips_running_agents():
    spawn_agent_tool({"description": "慢任务", "prompt": "p"})   # 保持 running
    out = reap_ready_tool({})
    assert "no pending results" in out


def test_reap_collects_multiple_results():
    _spawn_and_complete(description="任务一", result="结果一")
    _spawn_and_complete(description="任务二", result="结果二")
    out = reap_ready_tool({})
    assert "结果一" in out and "结果二" in out


def test_reap_worker_with_empty_result():
    _spawn_and_complete(description="哑任务", result="")
    out = reap_ready_tool({})
    assert "returned no text" in out


# ------------------------------------------------------------
# 7. 会话隔离
# ------------------------------------------------------------

def test_reap_isolated_by_session(monkeypatch):
    """Web 多会话: A 会话派的任务, B 会话收不到。"""
    monkeypatch.setattr(agent_tools, "current_session_id", lambda: "sess-A")
    agent_id_a = _spawn_and_complete(result="A 的结果")

    monkeypatch.setattr(agent_tools, "current_session_id", lambda: "sess-B")
    out_b = reap_ready_tool({})
    assert "no pending results" in out_b       # B 收不到 A 的
    assert "A 的结果" not in out_b

    monkeypatch.setattr(agent_tools, "current_session_id", lambda: "sess-A")
    out_a = reap_ready_tool({})
    assert "A 的结果" in out_a
    assert agent_id_a in out_a


def test_cli_session_uses_default_bucket(monkeypatch):
    """CLI（current_session_id=None）的 spawn/reap 都走 DEFAULT_SESSION。"""
    monkeypatch.setattr(agent_tools, "current_session_id", lambda: None)
    agent_id = _spawn_and_complete(result="cli 结果")
    assert agent_id in _session_bindings[DEFAULT_SESSION]
    out = reap_ready_tool({})
    assert "cli 结果" in out


def test_current_session_id_reads_real_dispatch_binding():
    """回归钉子: 不 monkeypatch, 走真实 server.dispatch 绑定链路。

    曾经 TurnDispatch 没有 current_session_id 方法, AttributeError 被
    agent_tools 的兜底 except 吞掉恒返回 None——Web 端所有会话的 worker
    都落进 cli 桶, 会话隔离静默失效。"""
    from server import dispatch
    dispatch.bind(emit=lambda p: None, session_id="sess-real")
    try:
        assert agent_tools.current_session_id() == "sess-real"
    finally:
        dispatch.unbind()
    assert agent_tools.current_session_id() is None


# ------------------------------------------------------------
# 8. 孤儿对账（multi_agent.reconcile_orphans）
# ------------------------------------------------------------

def test_reconcile_marks_running_as_failed():
    orch = agent_tools._toolbox._orchestrator
    rec = RecordingSpawn()
    orch2 = AgentOrchestrator(orch._store_dir, spawn_fn=rec)
    m1 = orch2.spawn_agent("孤儿一", "p")
    m2 = orch2.spawn_agent("孤儿二", "p")
    n = orch.reconcile_orphans()
    assert n == 2
    s1 = orch.get_status(m1.agent_id)
    assert s1.status == "failed"
    assert "重启" in s1.error
    assert orch.get_status(m2.agent_id).status == "failed"


def test_reconcile_keeps_terminal_states():
    agent_id = _spawn_and_complete()
    orch = agent_tools._toolbox._orchestrator
    n = orch.reconcile_orphans()
    assert n == 0
    assert orch.get_status(agent_id).status == "completed"


def test_reap_ready_ignores_delivered():
    orch = agent_tools._toolbox._orchestrator
    agent_id = _spawn_and_complete()
    assert len(orch.reap_ready()) == 1
    orch.mark_delivered([agent_id])
    assert orch.reap_ready() == []
    # delivered 重复标记幂等
    assert orch.mark_delivered([agent_id]) == 1


# ------------------------------------------------------------
# 9. API 配置工厂: 注入与回落
# ------------------------------------------------------------

def test_set_api_config_provider_overrides():
    set_api_config_provider(lambda: ("k", "http://x", "m-1"))
    from multi_agent import _api_config_provider
    assert _api_config_provider() == ("k", "http://x", "m-1")
    set_api_config_provider(_default_api_config)   # 还原


def test_default_api_config_tolerates_empty_key(monkeypatch):
    """空串 key（Web 初始化页场景）不抛 RuntimeError, 只有缺失才抛。"""
    monkeypatch.setenv("API_KEY", "")
    from multi_agent import _default_api_config
    key, url, model = _default_api_config()
    assert key == ""
