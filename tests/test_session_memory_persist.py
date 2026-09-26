"""Session Memory 持久化的验收测试: storage 记录 + runtime 回调 + 两端闭环。

校验语义（宁可弃用, 不劣于没有持久化）:
- 记录存在且 summary 非空、digested 在界、尾部消息哈希一致 → 恢复;
- 消息链被修补/改写（哈希不齐）→ 弃用, 回落现场摘要。

运行: uv run pytest tests/test_session_memory_persist.py -v
"""

import pytest

from models import Message
from runtime import ConversationRuntime
from storage import SessionStore

from test_llm_summary import SummaryFakeClient, make_session
from test_session_memory import make_runtime


# ------------------------------------------------------------
# storage 层: 记录 + 对齐校验
# ------------------------------------------------------------

def _messages(n=5):
    return [Message.user_text(f"m{i} " + "x" * 20) for i in range(n)]


def test_roundtrip(tmp_path):
    store = SessionStore(storage_dir=tmp_path)
    msgs = _messages()
    store.save_session_memory("s1", "滚动摘要", 4, msgs)
    assert store.load_session_memory("s1", msgs) == ("滚动摘要", 4)


def test_latest_record_wins(tmp_path):
    store = SessionStore(storage_dir=tmp_path)
    msgs = _messages()
    store.save_session_memory("s1", "v1", 2, msgs)
    store.save_session_memory("s1", "v2", 4, msgs)
    assert store.load_session_memory("s1", msgs) == ("v2", 4)


def test_no_record_returns_none(tmp_path):
    store = SessionStore(storage_dir=tmp_path)
    assert store.load_session_memory("s1", _messages()) is None


def test_tampered_chain_rejected(tmp_path):
    """消息链被修补/改写（哈希不齐）→ 弃用, 宁可回落现场摘要。"""
    store = SessionStore(storage_dir=tmp_path)
    msgs = _messages()
    store.save_session_memory("s1", "滚动摘要", 4, msgs)

    tampered = _messages()
    tampered[3] = Message.user_text("被中断修补换过的消息")
    assert store.load_session_memory("s1", tampered) is None


def test_out_of_bounds_digested_rejected(tmp_path):
    store = SessionStore(storage_dir=tmp_path)
    msgs = _messages()
    store.save_session_memory("s1", "摘要", 10, msgs)      # digested 越界
    assert store.load_session_memory("s1", msgs) is None
    store.save_session_memory("s1", "摘要", 0, msgs)       # digested=0 无意义
    assert store.load_session_memory("s1", msgs) is None


# ------------------------------------------------------------
# runtime 层: 恢复、消化回调、失败不炸消化
# ------------------------------------------------------------

def test_load_session_memory_seeds_state():
    rt = make_runtime(SummaryFakeClient())
    rt.load_session_memory("S", 7)
    assert rt._session_memory.summary == "S"
    assert rt._session_memory.digested == 7


def test_digest_fires_persist_callback():
    rt = make_runtime(SummaryFakeClient("滚动摘要 v1"))
    rt._session.messages = make_session(20).messages
    seen = []
    rt.set_on_session_memory(lambda s, d: seen.append((s, d)))

    rt._run_session_memory_update()

    assert seen == [("滚动摘要 v1", 20)]


def test_persist_failure_does_not_break_digest():
    rt = make_runtime(SummaryFakeClient("v1"))
    rt._session.messages = make_session(20).messages

    def boom(summary, digested):
        raise OSError("disk full")

    rt.set_on_session_memory(boom)
    rt._run_session_memory_update()

    assert rt._session_memory.digested == 20     # 消化不受影响
    assert rt._memory_busy is False              # 不卡死, 下轮可继续


# ------------------------------------------------------------
# 闭环: 消化回调写 storage → 恢复读回
# ------------------------------------------------------------

def test_roundtrip_through_store(tmp_path):
    store = SessionStore(storage_dir=tmp_path)
    rt = make_runtime(SummaryFakeClient("滚动摘要"))
    rt._session.messages = make_session(20).messages
    rt.set_on_session_memory(
        lambda s, d: store.save_session_memory("s1", s, d, rt.session().messages))

    rt._run_session_memory_update()

    assert store.load_session_memory("s1", rt.session().messages) == ("滚动摘要", 20)
