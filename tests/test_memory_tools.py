# 记忆 Agent 工具测试: 行为 / 反馈文本 / 错误文本 / 截断 / 单例注入
import pytest

from memory.store import JsonFileStore
from memory.tools import (memory_write_tool, memory_update_tool,
                          memory_delete_tool, set_memory_store,
                          get_memory_store, MEMORY_CONTENT_MAX_CHARS)


@pytest.fixture
def store(tmp_path):
    s = JsonFileStore(tmp_path / "memory.json")
    set_memory_store(s)
    yield s
    set_memory_store(None)


def test_write_returns_confirmation(store):
    out = memory_write_tool({"content": "用户偏好简洁回复", "category": "preference"})
    assert out.startswith("已记住（id=mem_")
    assert "用户偏好简洁回复" in out
    assert store.list_memories()[0]["category"] == "preference"


def test_write_dedupe_returns_existing(store):
    memory_write_tool({"content": "喜欢  周杰伦"})
    out = memory_write_tool({"content": "喜欢 周杰伦 "})   # 仅空白差异
    assert "已记住" in out
    assert len(store.list_memories()) == 1


def test_write_default_category_fact(store):
    memory_write_tool({"content": "x"})
    assert store.list_memories()[0]["category"] == "fact"


def test_write_invalid_category_falls_back(store):
    memory_write_tool({"content": "x", "category": "junk"})
    assert store.list_memories()[0]["category"] == "fact"


def test_write_empty_content_error(store):
    assert memory_write_tool({"content": "  "}).startswith("ERROR:")


def test_write_clips_long_content(store):
    out = memory_write_tool({"content": "长" * 2000})
    assert len(store.list_memories()[0]["content"]) == MEMORY_CONTENT_MAX_CHARS


def test_update_success_and_missing(store):
    m = store.add(content="旧", category="fact", source="agent")
    out = memory_update_tool({"memory_id": m["id"], "content": "新"})
    assert out.startswith("已更新")
    assert store.list_memories()[0]["content"] == "新"
    out2 = memory_update_tool({"memory_id": "mem_nope", "content": "x"})
    assert out2.startswith("ERROR:")
    assert "mem_nope" in out2


def test_delete_success_and_missing(store):
    m = store.add(content="待删", category="fact", source="agent")
    assert memory_delete_tool({"memory_id": m["id"]}).startswith("已忘记")
    assert memory_delete_tool({"memory_id": m["id"]}).startswith("ERROR:")


def test_missing_params_error(store):
    assert memory_update_tool({"memory_id": "", "content": "x"}).startswith("ERROR:")
    assert memory_delete_tool({}).startswith("ERROR:")
