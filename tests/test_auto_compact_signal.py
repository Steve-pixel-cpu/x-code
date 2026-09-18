"""规格钉子: auto_compacted 语义 = "实际压缩了"。

历史背景: 曾经 _maybe_auto_compact() 在"上下文过阈值但 removed_count==0"时
也返回 True, 信号亮了但会话根本没被压缩（假阳性）。现已改为: 过阈值但
消息数 <= preserve_recent(4)（如单条超大粘贴）时返回 False。

场景: 单条 user 消息携带超大输入 (in_tokens 远超阈值), 消息总数 <= preserve_recent(4),
compact_session 的 should_compact 因条数不足返回 removed_count=0。

运行: uv run pytest tests/test_auto_compact_signal.py -v
"""

from api_client import MessageStopEvent, TextDeltaEvent, UsageInfo
from models import Message, Session
from permissions import ALLOW_MODE, PermissionPolicy
from runtime import ConversationRuntime
from tools import ToolRegistry


def make_events(text: str, out_tokens: int, in_tokens: int) -> list:
    return [
        TextDeltaEvent(text=text),
        MessageStopEvent(usage=UsageInfo(input_tokens=in_tokens, output_tokens=out_tokens)),
    ]


class ScriptedClient:
    def __init__(self, script: list):
        self.script = list(script)
        self.calls = 0
        self.thinking_level = "medium"

    def stream(self, system_prompt, messages, thinking_level=None) -> list:
        events = self.script[self.calls] if self.calls < len(self.script) else self.script[-1]
        self.calls += 1
        return events


class NoopExecutor:
    def execute(self, tool_name, input, tool_use_id=None) -> str:
        return ""


def make_runtime(session, client, threshold: int) -> ConversationRuntime:
    return ConversationRuntime(
        session=session,
        api_client=client,
        tool_executor=NoopExecutor(),
        permission_policy=PermissionPolicy(active_mode=ALLOW_MODE),
        system_prompt=["助手"],
    ).with_auto_compact_threshold(threshold)


# ------------------------------------------------------------
# 对照组: 消息数充足时, 真·压缩发生
# ------------------------------------------------------------

def test_auto_compact_true_positive_actually_compacts():
    session = Session(messages=[Message.user_text(f"旧消息{i} " + "x" * 40) for i in range(6)])
    client = ScriptedClient([make_events("ok", out_tokens=1, in_tokens=500_000)])
    runtime = make_runtime(session, client, threshold=10_000)

    summary = runtime.run_turn("hi")

    assert summary.auto_compacted is True
    assert len(runtime.session().messages) == 5          # 摘要 + 保留 4 条
    assert "continued from a previous conversation" in runtime.session().messages[0].content[0].text


# ------------------------------------------------------------
# 假阳性已修: 消息数不足 (<= preserve_recent) 时, removed_count=0
# → auto_compacted 为 False, 信号不再空转
# ------------------------------------------------------------

def test_auto_compact_over_threshold_but_nothing_removable_is_false():
    session = Session()                                   # 空会话, run_turn 后只有 2 条消息
    client = ScriptedClient([make_events("ok", out_tokens=1, in_tokens=500_000)])
    runtime = make_runtime(session, client, threshold=200_000)

    summary = runtime.run_turn("hi")

    # 过了阈值但没东西可压 → 不亮信号
    assert summary.auto_compacted is False
    # 会话一条没压: 仍是原始的 user + assistant, 没有摘要消息
    roles = [m.role for m in runtime.session().messages]
    assert roles == ["user", "assistant"]
    assert all(
        "continued from a previous conversation" not in b.text
        for m in runtime.session().messages
        for b in m.content
        if hasattr(b, "text")
    )
