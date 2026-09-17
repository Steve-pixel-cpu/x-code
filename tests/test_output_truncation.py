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
# ------------------------------------------------------------

def test_registry_applies_truncation():
    registry = ToolRegistry().register("big", lambda params: "y" * 100_000)

    result = registry.execute("big", "")

    assert len(result) < MAX_TOOL_OUTPUT_CHARS + 500
    assert "truncated" in result


def test_registry_error_passthrough_untouched():
    """ToolError 走异常通道，不被截断逻辑波及。"""
    registry = ToolRegistry().register("boom", lambda params: (_ for _ in ()).throw(ValueError("炸了")))

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
