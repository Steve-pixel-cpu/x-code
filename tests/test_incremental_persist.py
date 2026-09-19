"""增量落盘的规格钉子: rewrite_session 原子重写 + runtime 一致点钩子。

输出中强杀进程时, 落盘只在"历史一致点"发生——不再丢整轮;
压缩重写内存历史后, 存储文件原子重写保持磁盘与内存一致。

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
# storage.rewrite_session
# ------------------------------------------------------------

def test_rewrite_session_重写后加载结果一致(tmp_path):
    store = SessionStore(storage_dir=tmp_path)
    msgs = [Message.user_text(f"消息{i}") for i in range(4)]
    for m in msgs:
        store.save_message("s1", m, None)
    store.set_title("s1", "标题A")            # 非消息记录: 重写后必须保留
    store.set_workdir("s1", "D:/w")           # 非消息记录: 重写后必须保留

    new_msgs = [Message.user_text("摘要"), Message.user_text("保留1")]
    count, last = store.rewrite_session("s1", new_msgs)

    assert count == 2
    loaded, last_uuid = store.load_session("s1")
    assert [m.content[0].text for m in loaded] == ["摘要", "保留1"]
    assert last_uuid == last                  # 返回的链尾与重载一致
    assert store.get_title("s1") == "标题A"
    assert store.get_workdir("s1") == "D:/w"
    assert store.count_messages("s1") == 2    # 旧消息不残留


def test_rewrite_session_链式parent连续(tmp_path):
    store = SessionStore(storage_dir=tmp_path)
    new_msgs = [Message.user_text("a"), Message.user_text("b"), Message.user_text("c")]
    _, last = store.rewrite_session("s1", new_msgs)
    # 重载即验证链可完整回溯（_rebuild_chain 走 parent_uuid）
    loaded, _ = store.load_session("s1")
    assert [m.content[0].text for m in loaded] == ["a", "b", "c"]


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


def test_on_compacted_压缩替换后触发():
    session = Session(messages=[Message.user_text(f"旧{i} " + "x" * 40) for i in range(6)])
    client = ScriptedClient([make_events("ok", in_tokens=500_000)])
    runtime = make_runtime(session, client).with_auto_compact_threshold(10_000)
    fired = []
    runtime.set_on_compacted(lambda: fired.append(list(session.messages)))

    runtime.run_turn("hi")

    assert len(fired) == 1
    assert "continued from a previous conversation" in fired[0][0].content[0].text


def test_钩子抛异常不炸对话():
    client = ScriptedClient([make_events("ok")])
    runtime = make_runtime(Session(), client)
    runtime.set_on_iterate(lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    summary = runtime.run_turn("hi")   # 不应抛出

    assert summary.iterations == 1
