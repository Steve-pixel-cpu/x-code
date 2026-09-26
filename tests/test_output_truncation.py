"""上下文卫生测试: 工具输出截断 + 系统提示动作经济性指令。

运行方式（在 x-code 目录下）:
    uv run pytest tests/test_output_truncation.py -v
"""

from tools import (
    MAX_TOOL_OUTPUT_CHARS,
    ToolError,
    ToolRegistry,
    truncate_tool_output,
)
from prompt import SystemPromptBuilder


# ------------------------------------------------------------
# truncate_tool_output — 契约: 短输出原样; 超限保留首尾 + 截断标记
# ------------------------------------------------------------

def test_short_output_passthrough():
    assert truncate_tool_output("hello") == "hello"
    assert truncate_tool_output("") == ""


def test_long_output_truncated_with_marker():
    head = "HEAD-" + "a" * 100
    tail = "b" * 100 + "-TAIL"
    output = head + "x" * (MAX_TOOL_OUTPUT_CHARS * 2) + tail

    result = truncate_tool_output(output)

    assert len(result) < MAX_TOOL_OUTPUT_CHARS + 500       # 总量有界
    assert result.startswith(head)                          # 开头保留
    assert result.endswith(tail)                            # 结尾保留（报错常在尾部）
    assert "truncated" in result                            # 模型得知道被掐了
    assert "characters omitted" in result                   # 且知道掐了多少


def test_boundary_not_truncated():
    output = "x" * MAX_TOOL_OUTPUT_CHARS
    assert truncate_tool_output(output) is output


# ------------------------------------------------------------
# ToolRegistry — 截断做在 execute 唯一入口，所有工具统一生效
# handler 签名是 (params, workdir)，与内置工具一致
# ------------------------------------------------------------

def test_registry_applies_truncation():
    registry = ToolRegistry().register(
        "big", lambda params, workdir: "y" * 100_000)

    result = registry.execute("big", "")

    assert len(result) < MAX_TOOL_OUTPUT_CHARS + 500
    assert "truncated" in result


def test_registry_error_passthrough_untouched():
    """ToolError 走异常通道，不被截断逻辑波及。"""
    registry = ToolRegistry().register(
        "boom",
        lambda params, workdir: (_ for _ in ()).throw(ValueError("炸了")))

    try:
        registry.execute("boom", "")
        raised = False
    except ToolError as e:
        raised = "炸了" in str(e)
    assert raised


# ------------------------------------------------------------
# 系统提示 — 动作经济性指令必须进静态区，每次 build 都在
# ------------------------------------------------------------

def test_prompt_contains_economy_instruction():
    sections = SystemPromptBuilder().with_os("Windows", "11").build()
    joined = "\n".join(sections)

    assert "Act economically" in joined


# ------------------------------------------------------------
# 二段式截断（落盘）: bash/grep 等超限全文写盘, 会话里回首尾 + 路径;
# read_file 与未知工具维持纯截断; 落盘失败静默退化
# ------------------------------------------------------------

import pytest
import time as _time
from pathlib import Path

import tools
from tools import resumable_spill_path


@pytest.fixture
def spill_dir(tmp_path, monkeypatch):
    d = tmp_path / "tool-results"
    d.mkdir(parents=True)
    monkeypatch.setattr(tools, "TOOL_RESULTS_DIR", d)
    return d


def test_within_spill_limit_untouched(spill_dir):
    out = "x" * 25_000
    assert truncate_tool_output(out, "bash") is out      # 25k < 30k: 不截不落盘
    assert list(spill_dir.glob("*.txt")) == []


def test_bash_overflow_spills_full_output(spill_dir):
    head, tail = "HEAD " + "a" * 100, "b" * 100 + " TAIL"
    output = head + "x" * 40_000 + tail

    result = truncate_tool_output(output, "bash")

    assert result.startswith(head) and result.endswith(tail)
    assert "Full output saved to" in result
    path = resumable_spill_path(result)
    assert path is not None
    assert Path(path).read_text(encoding="utf-8") == output   # 全文可找回


def test_grep_limit_is_100k(spill_dir):
    out = "x" * 50_000
    assert truncate_tool_output(out, "grep") is out
    assert "Full output saved to" in truncate_tool_output("y" * 120_000, "grep")


def test_unknown_tool_keeps_pure_truncation(spill_dir):
    out = "x" * (MAX_TOOL_OUTPUT_CHARS + 5_000)
    result = truncate_tool_output(out)                    # 旧调用口径: 不落盘
    assert "Full output saved to" not in result
    assert "characters omitted" in result
    assert list(spill_dir.glob("*.txt")) == []


def test_registry_passes_tool_name_to_truncation(spill_dir):
    registry = ToolRegistry().register(
        "bash", lambda params, workdir: "y" * 40_000)
    result = registry.execute("bash", "")
    assert "Full output saved to" in result


def test_resumable_spill_path_no_marker():
    assert resumable_spill_path("普通输出") is None


def test_spill_failure_degrades_to_pure_truncation(spill_dir, monkeypatch):
    monkeypatch.setattr(tools, "spill_tool_output", lambda out: None)  # 落盘失败
    out = "x" * 40_000
    result = truncate_tool_output(out, "bash")
    assert "characters omitted" in result                 # 退化为纯截断, 不炸
    assert "Full output saved to" not in result


def test_spill_cleanup_deletes_expired(spill_dir):
    old = spill_dir / "old.txt"
    old.write_text("stale", encoding="utf-8")
    past = _time.time() - 8 * 86400
    import os
    os.utime(old, (past, past))
    fresh = spill_dir / "fresh.txt"
    fresh.write_text("keep", encoding="utf-8")

    tools._cleanup_spilled_outputs(_time.time())

    assert not old.exists()
    assert fresh.exists()
