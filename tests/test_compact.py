"""compact_session 切割边界的规格钉子: 切割点绝不落在 tool_use 与它的
tool_result 之间——悬空 tool_result 会让下一轮请求被 API 400 掉整轮。

运行方式（在 x-code 目录下）:
    uv run pytest tests/test_compact.py -v
"""

from compact import CompactionConfig, compact_session
from models import Message, ToolContentBlock


def _assistant_tool_use(tool_id: str) -> Message:
    return Message(role="assistant",
                   content=[ToolContentBlock(id=tool_id, name="bash", input="ls")])


def _no_dangling_tool_result(messages) -> bool:
    """不变量: 每个 tool_result 引用的 tool_use 必须在同一段历史里出现过。"""
    tool_use_ids = set()
    for m in messages:
        for b in m.content:
            if b.type == "tool_use":
                tool_use_ids.add(b.id)
        if m.role == "tool":
            for b in m.content:
                if b.type == "tool_result" and b.id not in tool_use_ids:
                    return False
    return True


def _exchange_chain() -> list[Message]:
    """两组完整的工具交换 + 文本, 共 8 条。"""
    return [
        Message.user_text("任务"),
        _assistant_tool_use("t1"),
        Message.tool_result(id="t1", name="bash", output="out1", is_error=False),
        Message.user_text("继续"),
        Message.user_text("再来"),
        _assistant_tool_use("t2"),
        Message.tool_result(id="t2", name="bash", output="out2", is_error=False),
        Message.user_text("总结一下"),
    ]


def test_切割点避开工具交换块():
    # 8 条保 2 条: 朴素切割落在 t2 的 tool_use(5) 和 result(6) 之间,
    # 必须回退到交换块起点(5), 让 t2 的 tool_use/result 同生共死
    messages = _exchange_chain()
    result = compact_session(messages, CompactionConfig(
        preserve_recent_messages=2, max_estimated_tokens=0))

    assert result.removed_count == 5
    preserved = result.compacted_messages[1:]          # [0] 是摘要消息
    assert preserved[0].role == "assistant"
    assert preserved[0].content[0].id == "t2"          # 交换块整体进保留区
    assert _no_dangling_tool_result(result.compacted_messages)


def test_安全边界不受调整影响():
    messages = [Message.user_text(f"消息{i}") for i in range(6)]
    result = compact_session(messages, CompactionConfig(
        preserve_recent_messages=4, max_estimated_tokens=0))

    assert result.removed_count == 2                   # 纯文本: 条数切割照旧
    assert len(result.compacted_messages) == 5         # 摘要 + 保留 4 条
    assert _no_dangling_tool_result(result.compacted_messages)


def test_回退到头时宁可不压():
    # 整段历史就是一个工具交换链: 回退到 0 意味着没有可安全切割的位置,
    # removed 为空 → removed_count=0, 调用方按"没压掉东西"处理
    messages = [
        _assistant_tool_use("t1"),
        Message.tool_result(id="t1", name="bash", output="out1", is_error=False),
        _assistant_tool_use("t2"),
        Message.tool_result(id="t2", name="bash", output="out2", is_error=False),
        Message.user_text("总结"),
    ]
    result = compact_session(messages, CompactionConfig(
        preserve_recent_messages=4, max_estimated_tokens=0))

    assert result.removed_count == 0
    assert result.compacted_messages == messages