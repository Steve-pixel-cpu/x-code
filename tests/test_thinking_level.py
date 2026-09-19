"""思考等级（thinking level）功能测试: 配置解析、env 覆盖、请求参数映射、/thinking 命令。

运行方式（在 x-code 目录下）:
    uv run pytest tests/test_thinking_level.py -v
"""

import json
from types import SimpleNamespace

import pytest

from api_client import ClaudeApiClient
from config import ConfigError, ConfigLoader, RuntimeConfig
from main import (
    SlashCommand,
    build_runtime,
    parse_slash_command,
    switch_thinking,
)
from models import Session
from permissions import PermissionMode
from tools import ToolRegistry, read_tool


@pytest.fixture(autouse=True)
def clean_thinking_env(monkeypatch):
    """隔离真实环境的 CLAUDE_THINKING_LEVEL，避免机器上已有配置影响断言。"""
    monkeypatch.delenv("CLAUDE_THINKING_LEVEL", raising=False)


def make_loader(tmp_path: ...) -> ConfigLoader:
    return ConfigLoader(cwd=tmp_path, config_home=tmp_path)


# ------------------------------------------------------------
# 配置解析 — 契约: 默认 high; low/medium/high/max 合法; 非法值报 ConfigError
# ------------------------------------------------------------

def test_default_level_is_high(tmp_path):
    assert make_loader(tmp_path).load().thinking_level() == "high"


@pytest.mark.parametrize("level", ["low", "medium", "high", "max", "HIGH", " low "])
def test_valid_levels_accepted(tmp_path, level):
    (tmp_path / "settings.json").write_text(
        json.dumps({"thinkingLevel": level}), encoding="utf-8")
    assert make_loader(tmp_path).load().thinking_level() == level.strip().lower()


def test_invalid_level_rejected(tmp_path):
    (tmp_path / "settings.json").write_text(
        json.dumps({"thinkingLevel": "ultra"}), encoding="utf-8")
    with pytest.raises(ConfigError):
        make_loader(tmp_path).load()


def test_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_THINKING_LEVEL", "low")
    assert make_loader(tmp_path).load().thinking_level() == "low"


def test_env_override_invalid_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_THINKING_LEVEL", "ultra")
    with pytest.raises(ConfigError):
        make_loader(tmp_path).load()


# ------------------------------------------------------------
# ClaudeApiClient — 等级映射进请求 kwargs; max 与未知值不传 thinking
# ------------------------------------------------------------

class FakeStream:
    def __enter__(self):
        return iter([])

    def __exit__(self, *args):
        return False


def make_client(level):
    client = ClaudeApiClient(api_key="test", model="glm-5.3-flash", thinking_level=level)
    fake = SimpleNamespace()
    captured = {}

    def stream(**kwargs):
        captured.update(kwargs)
        return FakeStream()

    fake.messages = SimpleNamespace(stream=stream)
    client.client = fake
    return client, captured


@pytest.mark.parametrize("level, budget", [
    ("low", 2048),
    ("medium", 8192),
    ("high", 16384),
    ("max", None),
])
def test_kwargs_thinking_mapping(level, budget):
    client, captured = make_client(level)
    client.stream(system_prompt=["s"], messages=[])

    if budget is None:
        assert "thinking" not in captured
    else:
        assert captured["thinking"] == {"type": "enabled", "budget_tokens": budget}


# ------------------------------------------------------------
# /thinking 命令 — 解析、切换、无参打印、非法值保持不变
# ------------------------------------------------------------

def test_parse_thinking_command():
    assert parse_slash_command("/thinking") == SlashCommand.THINKING
    assert parse_slash_command("/thinking low") == SlashCommand.THINKING


class LevelClient:
    def __init__(self):
        self.thinking_level = "medium"

    def set_thinking_level(self, level):
        self.thinking_level = level


def make_runtime():
    return build_runtime(
        session=Session(),
        api_client=LevelClient(),
        registry=ToolRegistry().register("read_file", read_tool),
        permission_mode=PermissionMode.PROMPT,
        system_prompt=["你是助手"],
        hooks_config=RuntimeConfig(),
    )


def test_switch_thinking_switches(capsys):
    runtime = make_runtime()
    switch_thinking(runtime, "low")
    assert runtime.thinking_level() == "low"


def test_switch_thinking_no_arg_prints_current_and_choices(capsys):
    switch_thinking(make_runtime(), "")
    out = capsys.readouterr().out
    assert "medium" in out
    assert "low | medium | high | max" in out


def test_switch_thinking_invalid_keeps_current(capsys):
    runtime = make_runtime()
    switch_thinking(runtime, "ultra")
    assert runtime.thinking_level() == "medium"
    assert "未知思考等级" in capsys.readouterr().out
