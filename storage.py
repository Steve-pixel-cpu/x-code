import json
import os
import uuid
from typing import Literal, List
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ValidationError

from fsatomic import atomic_replace, read_text_with_retry, unique_tmp_path
from models import Message


class StorageEntry(BaseModel):
    uuid: str
    parent_uuid: Optional[str] = None
    message: dict  # Message.model_dump() 的结果
    timestamp: str


class TitleRecord(BaseModel):
    """命名记录: 与消息条目并存于同一 JSONL, 不进消息链。追加式改名:
    同一会话可有多条 title 记录, 展示永远取最新一条。"""
    type: Literal["title"] = "title"
    title: str
    timestamp: str


class WorkdirRecord(BaseModel):
    """工作目录记录: 会话所属"项目"。与 title 同一套追加式设计, 取最新一条。"""
    type: Literal["workdir"] = "workdir"
    workdir: str
    timestamp: str


class PermissionModeRecord(BaseModel):
    """权限模式记录: 会话级隔离的持久化。追加式取最新一条;
    没有记录的会话回落全局默认（app_state）。"""
    type: Literal["permission_mode"] = "permission_mode"
    mode: str
    timestamp: str


class SessionStore:

    def __init__(self, storage_dir: Path):
        self._storage_dir = storage_dir

    def  _append_entry(self, path: Path, entry):
        """追加一条 JSONL 记录。entry 是已 dump 的 dict 或 pydantic 模型。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = entry.model_dump() if isinstance(entry, BaseModel) else entry
        with open(path, "a", encoding='utf-8') as f:
            f.write(json.dumps(data, ensure_ascii=False))
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

    def rewrite_session(self, session_id: str, messages: List[Message]) -> tuple[int, Optional[str]]:
        """原子重写整个会话文件为给定消息序列（重建 uuid 链）。

        用于上下文压缩后: 内存历史被替换为"摘要+保留区", 追加式落盘的
        persisted_count 从此失准——重写让磁盘与内存重新一致。
        标题/工作目录等非消息记录原样保留。返回 (消息数, 新链尾 uuid)。

        临时文件唯一命名 + 替换带重试（fsatomic）: 重写发生在 turn 工作
        线程（auto-compact 后）, 而 HTTP 读者（历史接口的 load_session）
        可能正开着同一个会话文件——固定名 tmp 会被并发重写互踩, Windows
        上目标被读者占住时 replace 抛 PermissionError, 由重试兜住。
        """
        file_path = self._session_path(session_id)
        others = [e for e in self._read_entries(file_path)
                  if not isinstance(e, StorageEntry)]
        tmp = unique_tmp_path(file_path)
        try:
            tmp.write_text("", encoding="utf-8")
            for e in others:
                self._append_entry(tmp, e)
            last: Optional[str] = None
            for m in messages:
                last = self.save_message_to(tmp, m, last)
            atomic_replace(tmp, file_path)
        finally:
            # 替换成功后 tmp 已不存在; 失败路径清掉残片
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return len(messages), last

    def save_message_to(self, path: Path, message: Message,
                         parent_uuid: Optional[str]) -> str:
        """save_message 的指定文件变体（rewrite_session 用）。"""
        entry = StorageEntry(
            uuid=str(uuid.uuid4()),
            parent_uuid=parent_uuid,
            message=message.model_dump(),
            timestamp=datetime.now(timezone.utc).isoformat()
        ).model_dump()
        self._append_entry(path, entry)
        return entry["uuid"]

    def load_session(self, session_id:str) -> tuple[list[Message], Optional[str]]:
        file_path = self._session_path(session_id)
        result : list[Message] = []
        entries = [e for e in self._read_entries(file_path)
                   if isinstance(e, StorageEntry)]
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

    def delete_session(self, session_id: str) -> None:
        """删除会话（消息 + 命名记录同在一个 JSONL，删文件即可）。文件不存在抛 KeyError。"""
        file_path = self._session_path(session_id)
        if not file_path.exists():
            raise KeyError(session_id)
        file_path.unlink()

    # --- 会话命名 (prompt_dev/session_title.md) ---
    # 存储方案: 标题作为独立记录类型与消息条目共存于同一 JSONL 文件。
    # 改名 = 追加一条新 title 记录, 旧记录保留——追加式哲学不被破坏,
    # 消息链(parent_uuid)完全不受影响, _read_entries 按字段校验会
    # 自动跳过 title 行, load_session 行为不变。

    def set_title(self, session_id: str, title: str) -> None:
        """追加一条命名记录。改名不删旧记录, 展示层取最新。"""
        record = TitleRecord(
            title=title,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self._append_entry(self._session_path(session_id), record)

    def count_messages(self, session_id: str) -> int:
        """会话消息数（活跃链长度）。无记录/空会话返回 0。"""
        entries = [e for e in self._read_entries(self._session_path(session_id))
                   if isinstance(e, StorageEntry)]
        if not entries:
            return 0
        return len(self._rebuild_chain(entries))

    def get_title(self, session_id: str) -> Optional[str]:
        """返回会话名; 没有命名记录返回 None（旧会话 → 展示"(未命名)"）。"""
        entries = self._read_entries(self._session_path(session_id))
        latest: Optional[str] = None
        for entry in entries:
            if isinstance(entry, TitleRecord):
                latest = entry.title
        return latest

    def set_workdir(self, session_id: str, workdir: str) -> None:
        """追加一条工作目录记录（会话的项目目录）。"""
        record = WorkdirRecord(
            workdir=workdir,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self._append_entry(self._session_path(session_id), record)

    def get_workdir(self, session_id: str) -> Optional[str]:
        """返回会话工作目录; 没有记录返回 None（工具回退到服务进程 cwd）。"""
        entries = self._read_entries(self._session_path(session_id))
        latest: Optional[str] = None
        for entry in entries:
            if isinstance(entry, WorkdirRecord):
                latest = entry.workdir
        return latest

    def set_permission_mode(self, session_id: str, mode: str) -> None:
        """追加一条权限模式记录（会话级隔离的持久化）。mode 为模式名
        （MODE_TO_NAME 的值, 如 "plan"）; 非法值由调用方校验。"""
        record = PermissionModeRecord(
            mode=mode,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self._append_entry(self._session_path(session_id), record)

    def get_permission_mode(self, session_id: str) -> Optional[str]:
        """返回会话权限模式名; 没有记录返回 None（调用方回落全局默认）。"""
        entries = self._read_entries(self._session_path(session_id))
        latest: Optional[str] = None
        for entry in entries:
            if isinstance(entry, PermissionModeRecord):
                latest = entry.mode
        return latest

    def _session_path(self, session_id: str) -> Path:
        return self._storage_dir / f"{session_id}.jsonl"

    def _read_entries(self, file_path: Path) -> list[StorageEntry]:

        result : list[StorageEntry] = []
        if not file_path.exists(): return result
        # 读走重试: rewrite_session 的原子替换过渡窗口里, Windows 新开
        # 读句柄会瞬时被拒（delete pending）; 整体读入再按行解析
        text = read_text_with_retry(file_path)
        for line_no, line in enumerate(text.splitlines(), 1):
            if line.strip() == "":
                continue
            try:
                data = json.loads(line)
                # title/workdir/permission_mode 记录与消息条目共存一个文件, 按类型分流
                if data.get("type") == "title":
                    result.append(TitleRecord.model_validate(data))
                elif data.get("type") == "workdir":
                    result.append(WorkdirRecord.model_validate(data))
                elif data.get("type") == "permission_mode":
                    result.append(PermissionModeRecord.model_validate(data))
                else:
                    result.append(StorageEntry.model_validate(data))
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
        entries = [e for e in self._read_entries(path)
                   if isinstance(e, StorageEntry)]
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





