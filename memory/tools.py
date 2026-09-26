"""记忆工具: memory_write / memory_update / memory_delete 三件套。

spec 与实现同源（对齐 music.py 的 MUSIC_PLAY_SPEC 模式）, main.py 导入注册。
三个工具都是免审批档位（READ_ONLY）——记忆是 ~/.x-code/ 本机数据读写,
写入反馈经工具结果回传自然可见（CLI 打印 / Web 工具卡）, 不新增 UI 组件。

store 用模块级单例: CLI 命令、Web API、Agent 工具三方共用同一实例,
实例内缓存与落盘始终一致。
"""
from typing import Optional

from memory.store import JsonFileStore

# 单条 content 上限: 超长记忆挤占注入预算, 写入侧先截断（工具描述同步注明）
MEMORY_CONTENT_MAX_CHARS = 500

_VALID_CATEGORIES = ("preference", "fact", "context")

_store: Optional[JsonFileStore] = None


def get_memory_store() -> JsonFileStore:
    """进程级单例。路径来自 config.MEMORY_FILE（惰性导入, 免测试期副作用）。"""
    global _store
    if _store is None:
        import config
        _store = JsonFileStore(config.MEMORY_FILE)
    return _store


def set_memory_store(store: Optional[JsonFileStore]) -> None:
    """测试注入口。传 None 复位为默认单例。"""
    global _store
    _store = store


def _clip(content: str) -> str:
    """content 规范化: 去首尾空白 + 500 字符截断。"""
    c = " ".join(str(content or "").split())
    return c if len(c) <= MEMORY_CONTENT_MAX_CHARS else c[:MEMORY_CONTENT_MAX_CHARS]


def memory_write_tool(params: dict, workdir: Optional[str] = None) -> str:
    """新增一条记忆。精确去重: 完全相同（空白归一化后）返回已有条目不重复存。"""
    content = _clip(params.get("content"))
    if not content:
        return "ERROR: content is required"
    category = str(params.get("category") or "fact")
    if category not in _VALID_CATEGORIES:
        category = "fact"
    m = get_memory_store().add(content=content, category=category, source="agent")
    return f"已记住（id={m['id']}）：{m['content']}"


def memory_update_tool(params: dict, workdir: Optional[str] = None) -> str:
    """按 id 修改一条记忆内容。id 不存在时返回明确错误文本, 模型可自行纠正。"""
    memory_id = str(params.get("memory_id") or "")
    content = _clip(params.get("content"))
    if not memory_id or not content:
        return "ERROR: memory_id and content are required"
    m = get_memory_store().update(memory_id, content)
    if m is None:
        return f"ERROR: memory not found: {memory_id}（可先用 /memory 或列举工具查看已有记忆 id）"
    return f"已更新（id={m['id']}）：{m['content']}"


def memory_delete_tool(params: dict, workdir: Optional[str] = None) -> str:
    """按 id 删除一条记忆。仅用于用户明确表示某条记忆不对/不要时。"""
    memory_id = str(params.get("memory_id") or "")
    if not memory_id:
        return "ERROR: memory_id is required"
    if get_memory_store().remove(memory_id):
        return f"已忘记（id={memory_id}）"
    return f"ERROR: memory not found: {memory_id}"


# --- 工具 spec（进系统提示词工具清单; 描述里写清使用边界, 提示词不再重复） ---

MEMORY_WRITE_SPEC = {
    "name": "memory_write",
    "description": (
        "Persist a lasting fact about the user to cross-session memory "
        f"(max {MEMORY_CONTENT_MAX_CHARS} chars). Use when the user expresses a "
        "preference, corrects your behavior, or reveals long-lived facts. "
        "Do NOT record: one-off task details, secrets (passwords/keys), or "
        "code content itself. If a similar memory already exists, call "
        "memory_update instead of creating a duplicate. "
        "category: 'preference' (likes/dislikes/style), 'fact' (long-lived "
        "facts), 'context' (project/environment background)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The memory to store, one self-contained fact.",
            },
            "category": {
                "type": "string",
                "enum": list(_VALID_CATEGORIES),
                "description": "Memory category, default 'fact'.",
            },
        },
        "required": ["content"],
    },
}

MEMORY_UPDATE_SPEC = {
    "name": "memory_update",
    "description": (
        "Update an existing memory's content by id (use instead of adding a "
        "near-duplicate or when the user corrects a stored memory)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string",
                          "description": "Memory id, e.g. mem_a3f8c2…"},
            "content": {"type": "string",
                        "description": f"New content (max {MEMORY_CONTENT_MAX_CHARS} chars)."},
        },
        "required": ["memory_id", "content"],
    },
}

MEMORY_DELETE_SPEC = {
    "name": "memory_delete",
    "description": (
        "Delete a memory by id. Only when the user explicitly says a memory "
        "is wrong or asks you to forget something — never delete on your own."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string",
                          "description": "Memory id, e.g. mem_a3f8c2…"},
        },
        "required": ["memory_id"],
    },
}

MEMORY_TOOL_SPECS = [MEMORY_WRITE_SPEC, MEMORY_UPDATE_SPEC, MEMORY_DELETE_SPEC]
