# 记忆存储层测试: CRUD / 精确去重 / hits / 容量淘汰 / user 豁免 / 归档 / 损坏兜底
import json

import pytest

from memory.store import JsonFileStore, MEMORY_MAX, EVICTED_MAX


@pytest.fixture
def store(tmp_path):
    return JsonFileStore(tmp_path / "memory.json")


def _add(store, content, category="fact", source="agent"):
    return store.add(content=content, category=category, source=source)


# ---------- CRUD ----------

def test_add_and_list(store):
    m = _add(store, "用户习惯自然语言点歌")
    assert m["id"].startswith("mem_")
    assert m["hits"] == 0
    assert m["source"] == "agent"
    assert store.list_memories()[0]["content"] == "用户习惯自然语言点歌"


def test_list_sorted_by_updated_at_desc(store):
    first = _add(store, "旧记忆")
    second = _add(store, "新记忆")
    store.update(first["id"], "旧记忆（改）")
    contents = [m["content"] for m in store.list_memories()]
    assert contents == ["旧记忆（改）", "新记忆"]  # 刚更新的排最前


def test_update_refreshes_content(store):
    m = _add(store, "旧内容")
    updated = store.update(m["id"], "新内容")
    assert updated["content"] == "新内容"
    assert updated["updated_at"] >= m["updated_at"]


def test_update_missing_id_returns_none(store):
    assert store.update("mem_nope", "x") is None


def test_remove(store):
    m = _add(store, "待删")
    assert store.remove(m["id"]) is True
    assert store.list_memories() == []
    assert store.remove(m["id"]) is False


def test_clear_removes_all(store):
    _add(store, "a")
    _add(store, "b")
    store.clear()
    assert store.list_memories() == []


# ---------- 去重 ----------

def test_exact_dedupe_normalized_whitespace(store):
    m1 = _add(store, "用户偏好  简洁\n回复")
    m2 = _add(store, "用户偏好 简洁 回复")   # 仅空白差异, 归一化后相同
    assert m2["id"] == m1["id"]               # 返回已有条目, 不新增
    assert len(store.list_memories()) == 1


def test_different_content_not_deduped(store):
    _add(store, "喜欢周杰伦")
    _add(store, "常点七里香")
    assert len(store.list_memories()) == 2


# ---------- hits ----------

def test_touch_hits(store):
    m1 = _add(store, "a")
    m2 = _add(store, "b")
    store.touch_hits([m1["id"]])
    store.touch_hits([m1["id"], m2["id"]])
    hits = {m["content"]: m["hits"] for m in store.list_memories()}
    assert hits == {"a": 2, "b": 1}


# ---------- 容量淘汰 ----------

def test_eviction_over_capacity_keeps_user_source(store):
    """超限时淘汰 hits 低且旧的 agent 记忆; source:user 永不自动淘汰。"""
    s = JsonFileStore(_f := store_path(), max_memories=5)
    user_mem = s.add(content="用户手写", category="fact", source="user")
    for i in range(5):
        m = s.add(content=f"m{i}", category="fact", source="agent")
    s.touch_hits([m["id"]])  # 只有最后一条有 hits
    s.add(content="触发淘汰的一条", category="fact", source="agent")

    contents = [x["content"] for x in s.list_memories()]
    assert "用户手写" in contents           # user 豁免
    assert "m0" not in contents             # hits=0 且最旧 → 先出
    assert f"m{4}" in contents              # 有 hits 保留
    evicted = s._load()["evicted"]
    assert any(e["content"] == "m0" for e in evicted)


def test_evicted_archive_fifo_capped():
    s = JsonFileStore(_f := store_path(), max_memories=2, max_evicted=3)
    for i in range(8):
        s.add(content=f"m{i}", category="fact", source="agent")
    data = s._load()
    assert len(data["evicted"]) == 3        # FIFO 覆盖, 只留最新 3 条
    assert [e["content"] for e in data["evicted"]] == ["m3", "m4", "m5"]


def store_path(tmp_path=None):
    import tempfile, os
    d = tempfile.mkdtemp()
    import pathlib
    return pathlib.Path(d) / "memory.json"


# ---------- 损坏文件兜底 ----------

def test_corrupt_file_backed_up_and_reset(store, tmp_path):
    f = tmp_path / "memory.json"
    f.write_text("{broken json!!", encoding="utf-8")
    s = JsonFileStore(f)
    assert s.list_memories() == []
    assert (tmp_path / "memory.json.bak").read_text(encoding="utf-8") == "{broken json!!"
    s.add(content="重建后第一条", category="fact", source="agent")
    assert json.loads(f.read_text(encoding="utf-8"))["memories"][0]["content"] == "重建后第一条"


def test_non_dict_json_reset(store, tmp_path):
    f = tmp_path / "memory.json"
    f.write_text("[1,2,3]", encoding="utf-8")   # 合法 JSON 但不是 dict
    s = JsonFileStore(f)
    assert s.list_memories() == []


# ---------- 注入渲染 ----------

from memory.inject import render_memories, MEMORY_BUDGET_CHARS


def test_render_empty_returns_none(store):
    assert render_memories(store) is None


def test_render_basic_format(store):
    _add(store, "喜欢周杰伦", category="preference")
    _add(store, "项目用 uv", category="fact")
    out = render_memories(store)
    assert out.startswith("<user-memory>")
    assert out.endswith("</user-memory>")
    assert "[pref] 喜欢周杰伦" in out
    assert "[fact] 项目用 uv" in out
    assert "共 2 条" in out


def test_render_unknown_category_falls_back(store):
    _add(store, "x", category="weird")
    assert "[weird] x" in render_memories(store)


def test_render_budget_truncation_marks_hidden(store):
    s = JsonFileStore(store_path(), max_memories=MEMORY_MAX)
    for i in range(50):
        s.add(content=f"记忆条目{i}" * 10, category="fact", source="agent")
    out = render_memories(s, budget=400)
    assert "未展示" in out
    assert len(out) < 400 + 200     # 主体在预算内（标签/标题留余量）


def test_render_touches_hits_only_shown(store):
    m1 = _add(store, "a")
    m2 = _add(store, "b")
    render_memories(store, budget=10**9)
    hits = {m["id"]: m["hits"] for m in store.list_memories()}
    assert all(v == 1 for v in hits.values())   # 两条都展示了, 各 +1
