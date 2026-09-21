"""规格钉子: auto_compacted 语义 = "本请求使用了压缩视图"。

压缩是"给模型的请求期视图": 过阈值置粘性标记 _compact_active, stream 收到的
messages 为 [续接摘要] + 保留区; **会话历史本身不被改写**——完整对话原样
保留在内存与磁盘供展示, 摘要不再落盘。

历史背景:
- 曾经 _maybe_auto_compact() 在"过阈值但 removed_count==0"时也返回 True
  （假阳性）。现已改为: 消息数 <= preserve_recent(4)（如单条超大粘贴）时
  返回 False。
- 曾经压缩会原地替换 session.messages 并重写会话文件——展示层被迫跟着
  显示一整面摘要墙。现为请求期视图, 历史原样保留。
- 曾经触发口径读的是"本轮 usage 累加和"（current_turn_usage）——单轮
  工具迭代 N 次就把 N 份上下文加在一起, 真实上下文 15~20k 时迭代几次
  即误触发压缩。现口径 = 最近一次 API 调用的 input+缓存读写
  （latest_call_usage）, 与"此刻上下文多大"对齐。

运行: uv run pytest tests/test_auto_compact_signal.py -v
"""

from api_client import MessageStopEvent, TextDeltaEvent, ToolUseEvent, UsageInfo
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
        self.seen: list[list] = []       # 每次调用收到的 messages 快照
        self.thinking_level = "medium"

    def stream(self, system_prompt, messages, thinking_level=None) -> list:
        self.seen.append(list(messages))
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
# 对照组: 消息数充足时, 请求视图被压缩, 历史原样保留
# ------------------------------------------------------------

def test_auto_compact_view_applies_history_untouched():
    session = Session(messages=[Message.user_text(f"旧消息{i} " + "x" * 40) for i in range(12)])
    client = ScriptedClient([make_events("ok", out_tokens=1, in_tokens=500_000)])
    runtime = make_runtime(session, client, threshold=10_000)

    summary = runtime.run_turn("hi")

    assert summary.auto_compacted is True
    # 历史(内存)不被改写: 12 旧 + user"hi" + assistant 完整保留, 无摘要消息
    assert len(runtime.session().messages) == 14
    assert all(
        "continued from a previous conversation" not in b.text
        for m in runtime.session().messages
        for b in m.content
        if hasattr(b, "text")
    )
    # 跨阈值发生在本轮响应之后(usage 到手才知道), 当轮请求仍是全量视图;
    # 粘性标记已置位 → 自下一次请求起使用压缩视图(见粘性测试)
    assert len(client.seen[0]) == 13
    assert runtime._compact_active is True


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
    # 视图 = 调用时刻的全量历史（仅 user"hi", assistant 尚未产生）
    assert client.seen[0] == runtime.session().messages[:1]


# ------------------------------------------------------------
# 触发口径 — 最近一次调用的上下文体积, 不是本轮 usage 累加和
# （单轮迭代 N 次不能把 N 份上下文加在一起）
# ------------------------------------------------------------

def _tool_events(in_tokens: int) -> list:
    return [
        ToolUseEvent(id="t1", name="bash", input='{"command": "ls"}'),
        MessageStopEvent(usage=UsageInfo(input_tokens=in_tokens, output_tokens=10)),
    ]


def test_单轮多次迭代_累加用量不触发压缩_只看最近一次():
    """回归钉: 旧口径读 current_turn_usage()（按轮累加, 输出预算的正确口径,
    却被压缩判断误用）。8 次迭代 × 每次 15k input, 累计 121k 远超 100k 阈值,
    但每次调用的真实上下文只有 15~16k——正确行为是全程不压缩。旧实现会在
    第 8 次迭代顶部误触发, 视图骤降为 [摘要]+保留区。"""
    session = Session(messages=[Message.user_text(f"旧消息{i} " + "x" * 40) for i in range(12)])
    client = ScriptedClient(
        [_tool_events(15_000)] * 7
        + [make_events("ok", out_tokens=10, in_tokens=16_000)]
    )
    runtime = make_runtime(session, client, threshold=100_000)

    summary = runtime.run_turn("hi")

    # 累计口径已超阈值, 但单次口径从未超 → 全程不压缩
    assert runtime.usage().current_turn_usage().input_tokens == 7 * 15_000 + 16_000
    assert runtime.usage().current_turn_usage().input_tokens >= 100_000
    assert summary.auto_compacted is False
    assert runtime._compact_active is False
    # 每次请求都是当时的全量历史（12 旧 + user + 每迭代 assistant/tool 各一）,
    # 没有任何一次被换成压缩视图
    for i, view in enumerate(client.seen):
        assert len(view) == 13 + 2 * i
        assert all(
            "continued from a previous conversation" not in b.text
            for m in view for b in m.content if hasattr(b, "text")
        )


# ------------------------------------------------------------
# 粘性: 激活一次后持续生效, 不因 usage 回落而恢复全量视图（防振荡）
# ------------------------------------------------------------

def test_auto_compact_激活后粘性生效_不因阈值回落而恢复全量():
    session = Session(messages=[Message.user_text(f"旧消息{i} " + "x" * 40) for i in range(12)])
    client = ScriptedClient([
        make_events("ok", out_tokens=1, in_tokens=500_000),   # 第一次: 跨过阈值, 激活
        make_events("ok", out_tokens=1, in_tokens=100),       # 第二次: usage 远低于阈值
    ])
    runtime = make_runtime(session, client, threshold=10_000)

    runtime.run_turn("第一轮")
    # 第一次请求时 usage 未知, 仍是全量视图; 轮末激活粘性标记
    assert len(client.seen[0]) == 13
    runtime.run_turn("第二轮")            # usage 远低于阈值, 但粘性已激活
    second_view = client.seen[1]

    # 第二轮请求(usage 远低于阈值)仍使用压缩视图: [续接摘要] + 保留 8 条
    # (12旧+第一轮user+第一轮assistant+第二轮user=15 条, 切掉前 7 条),
    # 而不是恢复全量
    assert "continued from a previous conversation" in second_view[0].content[0].text
    assert len(second_view) == 9
    # 历史依旧原样: 14 条 + 第二轮 user + assistant = 16
    assert len(runtime.session().messages) == 16
