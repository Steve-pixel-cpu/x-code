from datetime import datetime, timezone
import json
import os
import uuid
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ValidationError

from models import ContentBlock, Session, Message

class StorageEntry(BaseModel):
    uuid: str
    parent_uuid: Optional[str] = None
    message: dict  # Message.model_dump() 的结果
    timestamp: str


class SessionStore:

    def __init__(self, storage_dir: Path):
        self._storage_dir = storage_dir

    def  _append_entry(self, path: Path, entry: StorageEntry):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False))
            f.write("\n")


    def save_message(self, session_id: str, message: Message, parent_uuid: Optional[str]) -> str:
        file_path = self._session_path(session_id)
        msg = message.model_dump()
        curr_id = str(uuid.uuid4())
        entry = StorageEntry(
            uuid=curr_id,
            parent_uuid=parent_uuid,
            message=msg,
            timestamp=datetime.now(timezone.utc).isoformat()
        ).model_dump()
        self._append_entry(file_path, entry)
        return curr_id

    def load_session(self, session_id:str) -> tuple[list[Message], Optional[str]]:
        file_path = self._session_path(session_id)
        result : list[Message] = []
        entries = self._read_entries(file_path)
        if not entries:
            return result, None
        chain = self._rebuild_chain(entries)
        for entry in chain:
            result.append(Message.model_validate(entry.message))

        last_uuid = chain[-1].uuid

        return result, last_uuid

    def list_sessions(self) -> list[str]:
        """列出所有会话 ID。"""
        if not self._storage_dir.exists():
            return []
        return sorted(
            p.stem for p in self._storage_dir.glob("*.jsonl")
        )

    def _session_path(self, session_id: str) -> Path:
        return self._storage_dir / f"{session_id}.jsonl"

    def _read_entries(self, file_path: Path) -> list[StorageEntry]:

        result : list[StorageEntry] = []
        if not file_path.exists(): return result
        with open(file_path, "r", encoding='utf-8') as f:

            for line_no, line in enumerate(f, 1):
                if line.strip() == "":
                    continue
                try:

                    entry = StorageEntry.model_validate(json.loads(line))

                    result.append(entry)
                except json.JSONDecodeError as e:
                    print(f"[WARN] line {line_no}: JSON 解析失败 - {e}")
                    continue
                except ValidationError as e:
                    print(f"[WARN] line {line_no}: 校验失败 - {e}")
                    continue
        return result

    def detect_interruption(self, session_id: str) -> Optional[str]:
        """检测中断类型。

               CC 的恢复逻辑: 根据最后一条消息的 role 判断:
               - "user" → 用户发了消息但 AI 没回复
               - "tool" → 工具执行完但 AI 没继续
               - "assistant" → 正常结束，无中断
               - None → 空会话
        """
        path = self._session_path(session_id)
        entries = self._read_entries(path)
        if not entries:
            return None

        chain = self._rebuild_chain(entries)
        if not chain:
            return None

        last_role = chain[-1].message.get("role", "")
        if last_role in ("user", "tool"):
            return last_role
        return None

    @classmethod
    def _rebuild_chain(cls, entries: list[StorageEntry])-> list[StorageEntry]:
        uuid_map = {e.uuid: e for e in entries}
        referenced: set[str] = set()
        chain : list [StorageEntry] = []

        for entry in entries:
            if entry.parent_uuid:
                referenced.add(entry.parent_uuid)

        leaf_set = set(uuid_map.keys()) - referenced
        if not leaf_set:
            return [entries[-1]]

        tip : Optional[StorageEntry] = None
        for entry in reversed(entries):
            if entry.uuid in leaf_set:
                tip = entry
                break


        seen = set()  # 环检测（防止损坏的链指针导致无限循环）
        current_uuid = tip.uuid

        while current_uuid:
            if current_uuid in seen:
                print(f"    [WARNING] 检测到循环引用 {current_uuid}，停止遍历")
                break
            seen.add(current_uuid)

            entry = uuid_map.get(current_uuid)
            if entry is None:
                break

            chain.append(entry)
            current_uuid = entry.parent_uuid if entry.parent_uuid else ""
        chain.reverse()
        return chain



if __name__ == "__main__":
    store = SessionStore(Path("test_storage"))
    session_id = "s2"

    msgs,last_uuid =  store.load_session(session_id)
    u4 = store.save_message(session_id,  Message.user_text("答案是1"), last_uuid)
    print(msgs)





