"""Session Memory 中间层（三层压缩体系）的验收测试。

三层: MicroCompact 清旧结果 → Session Memory 预建滚动摘要 → 现场全量
摘要兜底。这里钉住中间层的行为:

- 后台消化: 攒够阈值才发调用; 增量合并（只喂新增, 不重喂全史）
- 压缩消费: 摘要覆盖归档区 → 零调用直接用; 覆盖不全 → 当增量起点
- 失败安全: 消化失败指针不动、不卡死, 下轮重试; 主循环不受影响

运行: uv run pytest tests/test_session_memory.py -v
"""

from compact import SessionMemory
from models import Message, Session
from permissions import ALLOW_MODE, PermissionPolicy
from runtime import ConversationRuntime

from test_llm_summary import NoopExecutor, SummaryFakeClient, make_session


def make_runtime(client) -> ConversationRuntime:
    return ConversationRuntime(
        session=Session(messages=[]), api_client=client,
        tool_executor=NoopExecutor(),
        permission_policy=PermissionPolicy(active_mode=ALLOW_MODE),
        system_prompt=["助手"],
    )


# ------------------------------------------------------------
# 阈值与后台消化
# ------------------------------------------------------------

def test_digest_due_threshold():
    m = SessionMemory()
    assert not m.digest_due(15, 16)   # 攒不够不发调用
    assert m.digest_due(16, 16)


def test_background_digest_merges_increment():
    client = SummaryFakeClient("滚动摘要 v1")
    rt = make_runtime(client)
    rt._session.messages = make_session(20).messages

    rt._run_session_memory_update()   # 同步跑线程体

    assert rt._session_memory.summary == "滚动摘要 v1"
    assert rt._session_memory.digested == 20          # 指针推进到位
    assert rt._memory_busy is False                   # 单飞行旗标复位
    assert len(client.summary_calls) == 1
    # 增量口径: 只喂 20 条新消息 + 1 条摘要指令, 不重喂全史
    assert len(client.summary_calls[0]["messages"]) == 21


def test_no_digest_below_threshold():
    client = SummaryFakeClient("不该被调用")
    rt = make_runtime(client)
    rt._session.messages = make_session(10).messages   # 10 < 16

    rt._kick_session_memory_update()

    assert client.summary_calls == []
    assert rt._memory_busy is False


def test_kick_single_flight_skips_when_busy():
    client = SummaryFakeClient()
    rt = make_runtime(client)
    rt._session.messages = make_session(20).messages
    rt._memory_busy = True                             # 模拟上一轮仍在消化

    rt._kick_session_memory_update()

    assert rt._memory_busy is True                     # 不抢跑、不误复位


def test_digest_failure_keeps_pointer_and_releases():
    client = SummaryFakeClient(mode="raise")
    rt = make_runtime(client)
    rt._session.messages = make_session(20).messages

    rt._run_session_memory_update()

    assert rt._session_memory.digested == 0            # 指针不动, 下轮重试
    assert rt._memory_busy is False                    # 不卡死


# ------------------------------------------------------------
# 压缩消费: 零调用路径与增量起点
# ------------------------------------------------------------

def test_compact_uses_memory_zero_call():
    client = SummaryFakeClient("现场摘要（不该发生）")
    rt = make_runtime(client)
    rt._session_memory.summary = "滚动摘要"
    rt._session_memory.digested = 10
    msgs = make_session(14).messages

    result = rt._build_compact_summary(msgs, keep_from=10)

    assert result == "滚动摘要"                        # 直接命中
    assert client.summary_calls == []                  # 压缩时零 LLM 调用


def test_partial_coverage_seeds_incremental_summary():
    client = SummaryFakeClient("合并后摘要")
    rt = make_runtime(client)
    rt._session_memory.summary = "旧段摘要"
    rt._session_memory.digested = 4                    # 覆盖 0..4, 归档区到 10
    msgs = make_session(14).messages

    result = rt._build_compact_summary(msgs, keep_from=10)

    assert result == "合并后摘要"
    call = client.summary_calls[0]
    first = call["messages"][0]                        # prev 包装在首条 user 消息
    assert "旧段摘要" in first.content[0].text
    # 增量 = msgs[4:10]（6 条）+ prev 包装 + 摘要指令 = 8 条
    assert len(call["messages"]) == 8


def test_stale_memory_does_not_override_fresher_cache():
    client = SummaryFakeClient("现场摘要")
    rt = make_runtime(client)
    rt._session_memory.summary = "旧滚动摘要"
    rt._session_memory.digested = 3                    # 比 cache(6) 旧
    rt._compact_cache = (6, "压缩缓存摘要")
    msgs = make_session(14).messages

    rt._build_compact_summary(msgs, keep_from=10)

    call = client.summary_calls[0]
    first = call["messages"][0]
    assert "压缩缓存摘要" in first.content[0].text     # 仍用更更新的 cache 当起点
    assert len(call["messages"]) == 6                  # msgs[6:10] 4 条 + 包装 + 指令
