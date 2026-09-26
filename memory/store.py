"""记忆存储: MemoryStore 抽象接口 + JsonFileStore 实现。

存储形态是结构化 JSON（~/.x-code/memory.json, 路径见 config.MEMORY_FILE）,
而非 CC 式 MEMORY.md —— id/category/hits 等字段是 RAG 检索与 Web API 的地基。
单用户桌面应用, 与 settings.json 同读写约定: 读-改-写, 不加锁。

1.0 版零参数遗忘: 容量触发淘汰, 保留分 = hits 为主 + 新近度加权,
source:user 永不自动淘汰; 淘汰条目归档进同文件 evicted 数组（FIFO 兜底）。
"""
from abc import ABC, abstractmethod
import hashlib
import json
import shutil
import time
from typing import Any, Optional

from fsatomic import atomic_write_text

# 容量常量。集中在此供测试引用; 日后要做成配置项时从这里搬。
MEMORY_MAX = 200       # 记忆条数硬上限, add 时超限触发淘汰
EVICTED_MAX = 500      # 淘汰归档上限, FIFO 覆盖


def _now() -> str:
    """ISO 本地时间戳, 与 music-library 等本机文件精度一致（秒级够用）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _norm_key(content: str) -> str:
    """去重键: 空白归一化后取 hash。全角/标点差异不做（交给模型判断）。"""
    return hashlib.sha256(" ".join(content.split()).encode("utf-8")).hexdigest()


class MemoryStore(ABC):
    """记忆存储接口。1.0 由 JsonFileStore 实现; RAG 时代新增实现 + search()。"""

    @abstractmethod
    def list_memories(self) -> list[dict]: ...

    @abstractmethod
    def add(self, content: str, category: str, source: str) -> dict: ...

    @abstractmethod
    def update(self, memory_id: str, content: str) -> Optional[dict]: ...

    @abstractmethod
    def remove(self, memory_id: str) -> bool: ...

    @abstractmethod
    def touch_hits(self, memory_ids: list[str]) -> None: ...

    @abstractmethod
    def clear(self) -> None: ...


class JsonFileStore(MemoryStore):
    """memory.json 文件存储。损坏文件备份为 .bak 后按空库重建。"""

    def __init__(self, path, max_memories: int = MEMORY_MAX,
                 max_evicted: int = EVICTED_MAX):
        self.path = path
        self.max_memories = max_memories
        self.max_evicted = max_evicted
        self._data: Optional[dict] = None   # 惰性加载, 实例内缓存

    # ---------- 内部 ----------

    def _load(self) -> dict:
        if self._data is None:
            self._data = self._read_and_repair()
        return self._data

    def _read_and_repair(self) -> dict:
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except FileNotFoundError:
            return {"version": 1, "memories": [], "evicted": []}
        except Exception:
            # 损坏/非法: 备份原文件后按空库重建, 不让坏文件阻塞主流程。
            try:
                shutil.copyfile(self.path, str(self.path) + ".bak")
            except OSError:
                pass
            return {"version": 1, "memories": [], "evicted": []}
        if not isinstance(data, dict):
            try:
                shutil.copyfile(self.path, str(self.path) + ".bak")
            except OSError:
                pass
            return {"version": 1, "memories": [], "evicted": []}
        data.setdefault("version", 1)
        data.setdefault("memories", [])
        data.setdefault("evicted", [])
        return data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path,
                          json.dumps(self._data, ensure_ascii=False, indent=2))

    # ---------- MemoryStore 接口 ----------

    def list_memories(self) -> list[dict]:
        """按 updated_at 降序（最新改动在前）。"""
        return sorted(self._load()["memories"],
                      key=lambda m: m["updated_at"], reverse=True)

    def add(self, content: str, category: str, source: str) -> dict:
        data = self._load()
        key = _norm_key(content)
        for m in data["memories"]:
            if m["key"] == key:
                return m                      # 精确去重: 返回已有条目
        self.evict_if_over_capacity()
        m = {
            "id": f"mem_{hashlib.sha256(f'{key}{time.time_ns()}'.encode()).hexdigest()[:12]}",
            "content": content,
            "category": category,
            "source": source,
            "created_at": _now(),
            "updated_at": _now(),
            "hits": 0,
            "key": key,                       # 内部去重键, 持久化但不对外展示
        }
        data["memories"].append(m)
        self._save()
        return m

    def update(self, memory_id: str, content: str) -> Optional[dict]:
        data = self._load()
        for m in data["memories"]:
            if m["id"] == memory_id:
                m["content"] = content
                m["key"] = _norm_key(content)
                m["updated_at"] = _now()
                self._save()
                return m
        return None

    def remove(self, memory_id: str) -> bool:
        data = self._load()
        before = len(data["memories"])
        data["memories"] = [m for m in data["memories"] if m["id"] != memory_id]
        if len(data["memories"]) == before:
            return False
        self._save()
        return True

    def touch_hits(self, memory_ids: list[str]) -> None:
        data = self._load()
        ids = set(memory_ids)
        changed = False
        for m in data["memories"]:
            if m["id"] in ids:
                m["hits"] += 1
                changed = True
        if changed:
            self._save()

    def clear(self) -> None:
        data = self._load()
        data["memories"] = []
        self._save()

    def evict_if_over_capacity(self) -> list[dict]:
        """超限时按保留分淘汰 agent 记忆, user 记忆豁免。返回本轮淘汰条目。

        保留分: hits 主权重, 相同则 updated_at 新者优先（零参数, 无衰减公式）。
        淘汰进 evicted 归档（FIFO, 上限 max_evicted）, 不物理消失。
        """
        data = self._load()
        mems = data["memories"]
        overflow = len(mems) + 1 - self.max_memories   # add 前调用, +1 给新条目
        if overflow <= 0:
            return []
        # 可淘汰池: agent 来源。保留分升序 = hits 升序, 平手取 updated_at 最旧。
        pool = sorted(
            (m for m in mems if m["source"] != "user"),
            key=lambda m: (m["hits"], m["updated_at"]))
        victims, keep = pool[:overflow], pool[overflow:]
        user_mems = [m for m in mems if m["source"] == "user"]
        data["memories"] = list(keep) + user_mems
        if victims:
            data["evicted"] = (data["evicted"] + victims)[-self.max_evicted:]
            self._save()
        return victims
