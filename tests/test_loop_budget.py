"""循环层预算测试: usage 计量打通、单轮输出预算、迭代上限优雅收束、auto-compact 信号、配置接线。

运行方式（在 x-code 目录下）:
    uv run pytest tests/test_loop_budget.py -v
"""

import pytest

from api_client import MessageStopEvent, TextDeltaEvent, ToolUseEvent, UsageInfo
from config import ConfigLoader, RuntimeConfig, RuntimeFeatureConfig
from main import build_runtime
from models import Session
from permissions import ALLOW_MODE, PermissionMode, PermissionPolicy
from runtime import ConversationRuntime, build_assistant_message
from tools import ToolRegistry


@pytest.fixture(autouse=True)
def clean_budget_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_TURN_TOKEN_BUDGET", raising=False)


# ------------------------------------------------------------
# 测试替身
# ------------------------------------------------------------

def make_events(text: str, out_tokens: int, in_tokens: int = 1_000) -> list:
    """一轮纯文本回复的事件流，带指定用量。"""
    return [
        TextDeltaEvent(text=text),
        MessageStopEvent(usage=UsageInfo(input_tokens=in_tokens, output_tokens=out_tokens)),
    ]


def make_tool_events(out_tokens: int = 10) -> list:
    """带工具调用的事件流（工具会被 ALLOW 模式放行，NoopExecutor 执行）。"""
    return [
        ToolUseEvent(id="t1", name="bash", input='{"command": "ls"}'),
        MessageStopEvent(usage=UsageInfo(input_tokens=1_000, output_tokens=out_tokens)),
    ]


class ScriptedClient:
    """按剧本逐次返回事件流；剧本耗尽后重复最后一条。记录调用次数。"""

    def __init__(self, script: list):
        self.script = list(script)
        self.calls = 0

    def stream(self, system_prompt, messages) -> list:
        events = self.script[self.calls] if self.calls < len(self.script) else self.script[-1]
        self.calls += 1
        return events


class NoopExecutor:
    def execute(self, tool_name, input) -> str:
        return ""


def make_runtime(client, **budgets) -> ConversationRuntime:
    runtime = ConversationRuntime(
        session=Session(),
        api_client=client,
        tool_executor=NoopExecutor(),
        permission_policy=PermissionPolicy(active_mode=ALLOW_MODE),
        system_prompt=["你是助手"],
    )
    for name, value in budgets.items():
        getattr(runtime, f"with_{name}")(value)
    return runtime


# ------------------------------------------------------------
# 计量打通 — usage 从事件流走到 TokenUsage（此前永远返回 None）
# ------------------------------------------------------------

def test_build_assistant_message_carries_usage():
    events = make_events("你好", out_tokens=50, in_tokens=100)
    events[1].usage.cache_read_input_tokens = 10

    _, usage = build_assistant_message(events)

    assert usage is not None
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.cache_read_input_tokens == 10


def test_build_assistant_message_without_usage_stays_none():
    events = [TextDeltaEvent(text="hi"), MessageStopEvent()]
    _, usage = build_assistant_message(events)
    assert usage is None


# ------------------------------------------------------------
# 单轮输出预算 — 超限在循环顶部收束，历史保持一致
# ------------------------------------------------------------

def test_turn_output_budget_stops_before_next_call():
    # 工具调用让循环想继续，累计输出超限后第三次调用在循环顶部被拦下
    client = ScriptedClient([make_tool_events(out_tokens=60)])
    runtime = make_runtime(client, turn_output_budget=100)

    summary = runtime.run_turn("干活")

    assert summary.budget_exhausted is True
    assert client.calls == 2
    assert summary.iterations == 2
    roles = [m.role for m in runtime.session().messages]
    assert roles == ["user", "assistant", "tool", "assistant", "tool"]  # 无悬空 tool_use


def test_turn_budget_not_triggered_when_under():
    client = ScriptedClient([make_events("ok", out_tokens=10)])
    runtime = make_runtime(client, turn_output_budget=65_536)

    summary = runtime.run_turn("hi")

    assert summary.budget_exhausted is False
    assert client.calls == 1


# ------------------------------------------------------------
# 迭代上限 — 优雅收束（旧实现在这里 raise 炸掉整轮）
# ------------------------------------------------------------

def test_max_iterations_stops_gracefully():
    client = ScriptedClient([make_tool_events()])
    runtime = make_runtime(client, max_iterations=3)

    summary = runtime.run_turn("hi")            # 旧实现: RuntimeError

    assert summary.iterations_exhausted is True
    assert summary.budget_exhausted is False
    assert client.calls == 3


# ------------------------------------------------------------
# auto-compact 信号 — 用最近一次调用的 input（≈上下文占用），
# 而不是只增不减的累计 input
# ------------------------------------------------------------

def test_auto_compact_triggers_on_latest_input():
    client = ScriptedClient([make_events("ok", out_tokens=1, in_tokens=500_000)])
    runtime = make_runtime(client, auto_compact_threshold=200_000)

    assert runtime.run_turn("hi").auto_compacted is True


def test_auto_compact_not_triggered_under_threshold():
    client = ScriptedClient([make_events("ok", out_tokens=1, in_tokens=100)])
    runtime = make_runtime(client, auto_compact_threshold=200_000)

    assert runtime.run_turn("hi").auto_compacted is False


# ------------------------------------------------------------
# build_runtime 接线 — maxIterations / tokenBudget / turnTokenBudget 不再是死配置
# ------------------------------------------------------------

def test_build_runtime_wires_loop_budgets():
    config = RuntimeConfig(feature_config=RuntimeFeatureConfig(
        max_iterations=7,
        token_budget=123_456,
        turn_token_budget=777,
    ))

    runtime = build_runtime(
        session=Session(),
        api_client=ScriptedClient([]),
        registry=ToolRegistry(),
        system_prompt=[],
        hooks_config=config,
    )

    assert runtime._max_iterations == 7
    assert runtime._auto_compact_threshold == 123_456
    assert runtime._turn_output_budget == 777


# ------------------------------------------------------------
# turnTokenBudget 配置解析
# ------------------------------------------------------------

def test_config_turn_token_budget_default(tmp_path):
    assert ConfigLoader(cwd=tmp_path, config_home=tmp_path).load().turn_token_budget() == 65_536


def test_config_turn_token_budget_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_TURN_TOKEN_BUDGET", "999")
    assert ConfigLoader(cwd=tmp_path, config_home=tmp_path).load().turn_token_budget() == 999
