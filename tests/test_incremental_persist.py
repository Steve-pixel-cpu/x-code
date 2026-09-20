"""增量落盘的规格钉子: runtime 一致点钩子 + 只追加存储。

输出中强杀进程时, 落盘只在"历史一致点"发生——不再丢整轮。
压缩是"给模型的请求期视图": 历史不被改写, 存储永远只追加,
on_compacted 只是纯通知(旧版会原地替换历史并重写会话文件, 已退役)。

运行方式（在 x-code 目录下）:
    uv run pytest tests/test_incremental_persist.py -v
"""

import pytest

from api_client import MessageStopEvent, TextDeltaEvent, UsageInfo
from models import Message, Session
from permissions import ALLOW_MODE, PermissionPolicy
from runtime import ConversationRuntime
from storage import SessionStore


class ScriptedClient:
    def __init__(self, script: list):
        self.script = list(script)
        self.calls = 0
        self.thinking_level = "low"

    def stream(self, system_prompt, messages, thinking_level=None) -> list:
        events = self.script[self.calls] if self.calls < len(self.script) else self.script[-1]
        self.calls += 1
        return events


class NoopExecutor:
    def execute(self, tool_name, input, tool_use_id=None) -> str:
        return ""


def make_events(text: str, out_tokens: int = 1, in_tokens: int = 1_000) -> list:
    return [
        TextDeltaEvent(text=text),
        MessageStopEvent(usage=UsageInfo(input_tokens=in_tokens, output_tokens=out_tokens)),
    ]


# ------------------------------------------------------------
# runtime 钩子触发时机
# ------------------------------------------------------------

def make_runtime(session, client) -> ConversationRuntime:
    return ConversationRuntime(
        session=session,
        api_client=client,
        tool_executor=NoopExecutor(),
        permission_policy=PermissionPolicy(active_mode=ALLOW_MODE),
        system_prompt=["助手"],
    )


def test_on_iterate_用户消息后与工具结果后各触发一次():
    from api_client import ToolUseEvent
    client = ScriptedClient([
        [ToolUseEvent(id="t1", name="bash", input="ls"), MessageStopEvent()],
        make_events("done"),
    ])
    session = Session()
    runtime = make_runtime(session, client)
    marks = []
    runtime.set_on_iterate(lambda: marks.append(len(session.messages)))

    runtime.run_turn("hi")

    # 触发点1: 用户消息落定(1条) 触发点2: 工具结果回填(3条); 最终回复由
    # turn 收尾的 persist 覆盖, 不在迭代钩子里
    assert marks == [1, 3]


def test_on_compacted_压缩视图激活时触发_历史不被改写():
    """压缩激活 = 纯通知: 历史原样保留（含完整早期消息, 无摘要消息),
    存储不需要重写——磁盘与内存天然一致。"""
    session = Session(messages=[Message.user_text(f"旧{i} " + "x" * 40) for i in range(6)])
    client = ScriptedClient([make_events("ok", in_tokens=500_000)])
    runtime = make_runtime(session, client).with_auto_compact_threshold(10_000)
    fired = []
    runtime.set_on_compacted(lambda: fired.append(list(session.messages)))

    runtime.run_turn("hi")

    assert len(fired) == 1
    # 通知携带的是当时的完整历史快照, 不含续接摘要
    assert "continued from a previous conversation" not in fired[0][0].content[0].text
    # 轮次结束后历史依旧原样: 6 旧 + user"hi" + assistant
    assert len(session.messages) == 8
    assert all(
        "continued from a previous conversation" not in b.text
        for m in session.messages
        for b in m.content
        if hasattr(b, "text")
    )
    assert runtime._compact_active is True       # 粘性: 后续请求继续使用压缩视图


def test_钩子抛异常不炸对话():
    client = ScriptedClient([make_events("ok")])
    runtime = make_runtime(Session(), client)
    runtime.set_on_iterate(lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    summary = runtime.run_turn("hi")   # 不应抛出

    assert summary.iterations == 1
