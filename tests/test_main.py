"""main.py 纯逻辑部分的验收测试（REPL 交互本身靠人工冒烟）。

运行方式（在 x-code 目录下）:
    uv run pytest tests/test_main.py -v
"""

import pytest

from config import RuntimeConfig
from main import (
    CliPermissionPrompter,
    CliToolExecutor,
    SlashCommand,
    build_runtime,
    parse_slash_command,
)
from models import Session
from permissions import PermissionDecision, PermissionMode, PermissionRequest
from tools import ToolRegistry, read_tool


# ------------------------------------------------------------
# parse_slash_command — 契约: 无 / → None; /命令 → 枚举; 未知 → UNKNOWN
# ------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("你好，帮我看看这个bug", None),        # 普通文本 → None
    ("hello world", None),
    ("", None),                              # 空输入 → None
    ("/help", SlashCommand.HELP),
    ("/status", SlashCommand.STATUS),
    ("/compact", SlashCommand.COMPACT),
    ("/exit", SlashCommand.EXIT),
    ("/help ", SlashCommand.HELP),           # 尾随空白容忍
    (" /help", None),                        # 前导空格 → 不是命令开头
    ("/foo", SlashCommand.UNKNOWN),          # 未知命令 → UNKNOWN（不是 None）
    ("/HELP", SlashCommand.UNKNOWN),         # 大小写敏感（当前设计如此）
])
def test_parse_slash_command(text, expected):
    assert parse_slash_command(text) == expected


# ------------------------------------------------------------
# CliPermissionPrompter — y/yes 放行, 其余拒绝, Ctrl+C 视为拒绝
# ------------------------------------------------------------

def make_request() -> PermissionRequest:
    return PermissionRequest(
        tool_name="bash",
        input='{"command": "rm -rf /"}',
        current_mode=PermissionMode.WORKSPACE_WRITE,
        required_mode=PermissionMode.DANGER_FULL_ACCESS,
    )


def _decide_with_stdin(monkeypatch, answer) -> PermissionDecision:
    monkeypatch.setattr("builtins.input", lambda *a: answer)
    return CliPermissionPrompter().decide(make_request()).decision


def test_prompter_approves_y(monkeypatch):
    assert _decide_with_stdin(monkeypatch, "y") == PermissionDecision.ALLOW


def test_prompter_approves_yes(monkeypatch):
    assert _decide_with_stdin(monkeypatch, "yes") == PermissionDecision.ALLOW


@pytest.mark.parametrize("answer", ["", "n", "no", "N", "随便"])
def test_prompter_denies_non_yes(monkeypatch, answer):
    # 默认必须朝安全侧倒: 只有 y/yes 放行
    assert _decide_with_stdin(monkeypatch, answer) == PermissionDecision.DENY


def test_prompter_denies_on_ctrl_c(monkeypatch):
    def raise_interrupt(*a):
        raise KeyboardInterrupt
    monkeypatch.setattr("builtins.input", raise_interrupt)
    assert CliPermissionPrompter().decide(make_request()).decision == PermissionDecision.DENY


# ------------------------------------------------------------
# CliToolExecutor — 委托 registry, 完整结果返回, 终端只看预览
# ------------------------------------------------------------

class FakeRegistry:
    def __init__(self):
        self.calls = []

    def execute(self, tool_name, input):
        self.calls.append((tool_name, input))
        return "x" * 500  # 长输出


def test_executor_delegates_and_returns_full_result(capsys):
    fake = FakeRegistry()
    executor = CliToolExecutor(fake)
    result = executor.execute("read_file", '{"path": "a.py"}')

    assert fake.calls == [("read_file", '{"path": "a.py"}')]  # 委托到位
    assert len(result) == 500                                  # 完整结果给 LLM

    printed = capsys.readouterr().out
    assert "read_file" in printed                              # 过程给人看
    assert len(printed) < len(result)                          # 终端只有预览


# ------------------------------------------------------------
# build_runtime — 装配点: 五个零件接上, session 不被偷换
# ------------------------------------------------------------

class FakeApiClient:
    pass


def test_build_runtime_assembles_and_preserves_session():
    registry = ToolRegistry().register("read_file", read_tool)
    session = Session()

    runtime = build_runtime(
        session=session,
        api_client=FakeApiClient(),
        registry=registry,
        permission_mode=PermissionMode.PROMPT,
        system_prompt=["你是助手"],
        hooks_config=RuntimeConfig(),
    )

    assert runtime.session() is session  # 装配点不偷换对象
