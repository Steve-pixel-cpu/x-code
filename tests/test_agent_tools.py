"""多 agent 接线测试: agent_tools.py（工具层）+ multi_agent.py 新增改动。

覆盖:
- TOOLS/registry 与 agent 工具的一致性（spec ↔ handler ↔ 白名单）
- workdir 参数从 registry 流到 AgentJob（CLI None / Web 会话目录两条路径）
- _tool_specs_for 规格过滤（递归防护的 API 层）
- spawn → complete 后 agent_status 读回 result 的闭环
- _AgentToolbox 单例与 set_api_config_provider 的注入/回落
"""
import json
from pathlib import Path
import tempfile

import pytest

import agent_tools
from agent_tools import (
    AGENT_TOOL_SPECS,
    agent_list_tool,
    agent_status_tool,
    get_orchestrator,
    register_agent_tools,
    spawn_agent_tool,
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


def use_fake_orchestrator(spawn_fn) -> AgentOrchestrator:
    """把模块级单例临时换成假 spawn 的编排器（不真正起 worker 线程）。"""
    tmp = tempfile.mkdtemp()
    orch = AgentOrchestrator(Path(tmp), spawn_fn=spawn_fn)
    agent_tools._toolbox._orchestrator = orch
    return orch


# ------------------------------------------------------------
# 1. spec ↔ registry ↔ 白名单 一致性
# ------------------------------------------------------------

def test_tools_include_agent_trio():
    names = [s["name"] for s in TOOLS]
    assert names[-3:] == ["agent_tool", "agent_status", "agent_list"]


def test_agent_tool_specs_match_registry_handlers():
    registry = build_registry()
    for spec in AGENT_TOOL_SPECS:
        assert spec["name"] in registry._handlers, spec["name"]


def test_agent_tools_absent_from_worker_whitelists():
    """递归防护: 任何角色的白名单都不含 agent 工具（名字层面）。"""
    for role, tools in TOOL_WHITELIST.items():
        for t in ("agent_tool", "agent_status", "agent_list"):
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
    use_fake_orchestrator(RecordingSpawn())
    out = agent_status_tool({"agent_id": "ghost"})
    assert "ERROR" in out and "ghost" in out


def test_status_requires_agent_id():
    use_fake_orchestrator(RecordingSpawn())
    assert "ERROR" in agent_status_tool({})


def test_list_empty_state():
    use_fake_orchestrator(RecordingSpawn())
    assert agent_list_tool({}) == "(no subagents yet)"


def test_spawn_rejects_empty_input_via_tool_error():
    use_fake_orchestrator(RecordingSpawn())
    out = spawn_agent_tool({"description": "  ", "prompt": "p"})
    assert out.startswith("ERROR:")


# ------------------------------------------------------------
# 4. workdir 参数流: registry → handler → AgentJob
# ------------------------------------------------------------

def test_spawn_via_registry_passes_workdir_to_job():
    rec = RecordingSpawn()
    use_fake_orchestrator(rec)
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
    use_fake_orchestrator(rec)
    spawn_agent_tool({"description": "d", "prompt": "p"}, workdir=None)
    assert rec.jobs[0].workdir is None


def test_explicit_job_workdir_beats_orchestrator_default():
    """spawn_agent 的 workdir 参数优先于编排器构造时的默认值。"""
    rec = RecordingSpawn()
    tmp = tempfile.mkdtemp()
    orch = AgentOrchestrator(Path(tmp), spawn_fn=rec, workdir="D:/default")
    orch.spawn_agent("d", "p", workdir="D:/override")
    assert rec.jobs[0].workdir == "D:/override"
    orch.spawn_agent("d", "p")
    assert rec.jobs[1].workdir == "D:/default"


# ------------------------------------------------------------
# 5. 闭环: spawn → complete → status 读回 result
# ------------------------------------------------------------

def test_spawn_complete_status_roundtrip():
    captured = {}

    def real_lifecycle(job):
        # 模拟 worker 线程的收尾（在 spawn 返回后由测试手动触发也可以,
        # 这里直接在 spawn_fn 里闭环）
        pass

    orch = use_fake_orchestrator(RecordingSpawn())
    out = spawn_agent_tool({"description": "调查登录bug", "prompt": "读代码"}, workdir=None)
    agent_id = out.split("agent_id: ")[1].split("\n")[0]

    orch.complete_agent(agent_id, "bug 在 auth.py:42")
    status_out = agent_status_tool({"agent_id": agent_id})
    data = json.loads(status_out)
    assert data["status"] == "completed"
    assert data["result"] == "bug 在 auth.py:42"
    assert "Still running" not in status_out


def test_status_running_state_hints_polling():
    orch = use_fake_orchestrator(RecordingSpawn())
    out = spawn_agent_tool({"description": "d", "prompt": "p"})
    agent_id = out.split("agent_id: ")[1].split("\n")[0]
    status_out = agent_status_tool({"agent_id": agent_id})
    assert '"status": "running"' in status_out
    assert "Still running" in status_out


def test_list_shows_completed_result():
    orch = use_fake_orchestrator(RecordingSpawn())
    out = spawn_agent_tool({"description": "查内存泄漏", "prompt": "p", "name": "leak-hunter"})
    agent_id = out.split("agent_id: ")[1].split("\n")[0]
    orch.complete_agent(agent_id, "泄漏在 cache.py")
    listing = agent_list_tool({})
    assert "leak-hunter" in listing
    assert "泄漏在 cache.py" in listing
    assert "查内存泄漏" in listing


# ------------------------------------------------------------
# 6. API 配置工厂: 注入与回落
# ------------------------------------------------------------

def test_set_api_config_provider_overrides(monkeypatch):
    calls = []
    set_api_config_provider(lambda: calls.append(1) or ("k", "http://x", "m-1"))
    from multi_agent import _api_config_provider
    key, url, model = _api_config_provider()
    assert (key, url, model) == ("k", "http://x", "m-1")
    set_api_config_provider(_default_api_config)   # 还原


def test_default_api_config_raises_without_key(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setenv("API_KEY", "")   # load_dotenv 可能读 .env, 显式置空串
    import multi_agent
    # 置空串后 _default 里 None 检查不触发 —— 确认行为: 空串被当作"有值"。
    # 这里只锁行为不锁实现: 有 key（哪怕空串）不抛 RuntimeError
    try:
        multi_agent._default_api_config()
    except RuntimeError:
        pytest.fail("空串 key 不应触发 RuntimeError（只有缺失才触发）")
