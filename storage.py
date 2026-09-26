import json
import hashlib
import os
import threading
import uuid
from typing import Literal, List
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ValidationError

from fsatomic import read_text_with_retry
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
    """工作目录记录: 会话所属"项目"。与 title 同一套追加式设计, 取最新一条。
    workdir=None 表示"清除归属"(解除会话与项目的绑定), 旧目录记录保留在文件里。"""
    type: Literal["workdir"] = "workdir"
    workdir: Optional[str] = None
    timestamp: str


class PermissionModeRecord(BaseModel):
    """权限模式记录: 会话级隔离的持久化。追加式取最新一条;
    没有记录的会话回落全局默认（app_state）。"""
    type: Literal["permission_mode"] = "permission_mode"
    mode: str
    timestamp: str


class ModelRecord(BaseModel):
    """会话模型记录: 会话级模型选择的持久化。追加式取最新一条;
    没有记录的会话跟随全局 active 模型。model_id=None 表示"清除覆盖"
    （回到跟随全局）, 旧记录保留在文件里。"""
    type: Literal["model"] = "model"
    provider_id: Optional[str] = None
    model_id: Optional[str] = None
    timestamp: str


class SessionMemoryRecord(BaseModel):
    """Session Memory（三层压缩中间层的滚动摘要）记录: 后台消化每合并
    一步追加一条, 恢复时取最新。last_sha = 第 digested-1 条消息内容的
    稳定哈希——恢复时校验消息链对齐（repair/中断修补可能改变消息数,
    错位的摘要宁可弃用: 覆盖不全回落现场摘要, 不劣于没有持久化）。"""
    type: Literal["session_memory"] = "session_memory"
    summary: str
    digested: int
    last_sha: str = ""
    timestamp: str


class SessionStore:

    # 进程内互斥: 大记录(几十 KB 的工具结果)经缓冲写入可能拆成多次
    # write() 系统调用, 并发线程交错时另一条记录的半截插进中间——
    # JSONL 出现"一行撕成多行"的结构性损坏(20260925-022538 实测)。
    _append_lock = threading.Lock()

    def __init__(self, storage_dir: Path):
        self._storage_dir = storage_dir

    def  _append_entry(self, path: Path, entry):
        """追加一条 JSONL 记录。entry 是已 dump 的 dict 或 pydantic 模型。

        整条记录序列化成一段 bytes 后**单次** write 追加: json.dumps 的
        输出不可能含裸换行(字符串内控制字符一律转义), 单次写入再配合
        O_APPEND 语义, 记录要么整行落下要么不落, 不会被别的并发写半路
        撕开。代理字符(上游 errors="surrogateescape" 之类漏进来的)在
        编码时就地转 U+FFFD, 保证 utf-8 编码永不抛错、永不吐裸字节。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = entry.model_dump() if isinstance(entry, BaseModel) else entry
        text = json.dumps(data, ensure_ascii=False)
        payload = (text.encode("utf-8", errors="surrogatepass")
                   .decode("utf-8", errors="replace")
                   .encode("utf-8") + b"\n")
        with self._append_lock, open(path, "ab") as f:
            f.write(payload)


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

    # 注: 曾有 rewrite_session(压缩后重写整个会话文件)。压缩已改为
    # "给模型的请求期视图"(runtime._model_view), 历史不再被改写,
    # 存储永远只追加, 该函数随之退役。

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

    def load_session_detail(self, session_id: str) -> tuple[list[tuple[Message, str]], Optional[str]]:
        """与 load_session 相同的消息链, 但逐条携带落盘 timestamp。
        供历史回放场景（如 minimap 快速定位）展示相对时间, 不影响
        请求构造链路——后者继续用 load_session。"""
        file_path = self._session_path(session_id)
        entries = [e for e in self._read_entries(file_path)
                   if isinstance(e, StorageEntry)]
        if not entries:
            return [], None
        chain = self._rebuild_chain(entries)
        detail = [(Message.model_validate(entry.message), entry.timestamp)
                  for entry in chain]
        return detail, chain[-1].uuid

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

    @staticmethod
    def _message_sha(message: Message) -> str:
        """消息内容的稳定哈希（sort_keys 保证跨进程/跨次 dump 一致）。"""
        payload = json.dumps(message.model_dump(), ensure_ascii=False,
                             sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def save_session_memory(self, session_id: str, summary: str,
                            digested: int, messages: List[Message]) -> None:
        """追加一条 Session Memory 记录（后台消化每合并一步调用一次）。
        last_sha 取第 digested-1 条消息的哈希供恢复时校验对齐; digested
        越界时不记 sha（恢复时的在界校验必然弃用, 等价于没有持久化）。"""
        last_sha = (self._message_sha(messages[digested - 1])
                    if 0 < digested <= len(messages) else "")
        record = SessionMemoryRecord(
            summary=summary, digested=digested, last_sha=last_sha,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self._append_entry(self._session_path(session_id), record)

    def load_session_memory(self, session_id: str,
                            messages: List[Message]) -> Optional[tuple[str, int]]:
        """返回 (summary, digested) 或 None。恢复校验三连: 有记录且非空、
        digested 在界、尾部消息哈希一致——消息链被修补/改写过就视为摘要
        错位, 宁可弃用（回落现场摘要, 行为不劣于没有持久化）。"""
        latest: Optional[SessionMemoryRecord] = None
        for entry in self._read_entries(self._session_path(session_id)):
            if isinstance(entry, SessionMemoryRecord):
                latest = entry
        if latest is None or not latest.summary:
            return None
        if not 0 < latest.digested <= len(messages):
            return None
        if self._message_sha(messages[latest.digested - 1]) != latest.last_sha:
            return None
        return latest.summary, latest.digested

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

    def set_model(self, session_id: str, provider_id: Optional[str],
                  model_id: Optional[str]) -> None:
        """追加一条会话模型记录。model_id=None 表示清除覆盖（跟随全局）。"""
        record = ModelRecord(
            provider_id=provider_id,
            model_id=model_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self._append_entry(self._session_path(session_id), record)

    def get_model(self, session_id: str) -> tuple[Optional[str], Optional[str]]:
        """返回会话模型 (provider_id, model_id); 没有记录返回 (None, None)
        （调用方回落全局 active）。"""
        entries = self._read_entries(self._session_path(session_id))
        latest: Optional[ModelRecord] = None
        for entry in entries:
            if isinstance(entry, ModelRecord):
                latest = entry
        if latest is None:
            return (None, None)
        return (latest.provider_id, latest.model_id)

    def _session_path(self, session_id: str) -> Path:
        return self._storage_dir / f"{session_id}.jsonl"

    def _read_entries(self, file_path: Path) -> list[StorageEntry]:

        result : list[StorageEntry] = []
        if not file_path.exists(): return result
        # 读走重试: 原子替换的过渡窗口里, Windows 新开读句柄会瞬时被拒
        # （delete pending）; 整体读入再按行解析
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
                elif data.get("type") == "model":
                    result.append(ModelRecord.model_validate(data))
                elif data.get("type") == "session_memory":
                    result.append(SessionMemoryRecord.model_validate(data))
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





