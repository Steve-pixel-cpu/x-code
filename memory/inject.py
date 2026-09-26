"""记忆注入渲染: 把记忆渲染成系统提示词里的 <user-memory> 段。

无记忆时返回 None —— prompt.py 据此整段省略, 不输出空壳标签。
预算截断照 guide 08 双层预算模式: 单条上限在写入侧截断,
段级总预算在此按「hits 升序 → 时间升序」丢最不常用的, 末尾标注未展示条数。
"""
from typing import Optional

# 记忆段总字符预算（对齐 CC 指令文件单文件预算量级）
MEMORY_BUDGET_CHARS = 4000

# 分类缩写: 渲染示例 [pref] / [fact] / [ctx]
_CATEGORY_ABBR = {"preference": "pref", "fact": "fact", "context": "ctx"}


def render_memories(store, budget: int = MEMORY_BUDGET_CHARS) -> Optional[str]:
    """渲染记忆 section。无记忆返回 None; 渲染成功的条目 touch_hits。"""
    mems = store.list_memories()
    if not mems:
        return None

    lines: list[str] = []
    shown: list[dict] = []
    used = 0
    truncated = 0
    for m in mems:
        # list_memories 按 updated_at 降序（最重要/最新在前）; 预算不够时丢尾部
        line = f"- [{_CATEGORY_ABBR.get(m['category'], m['category'])}] {m['content']}"
        if used + len(line) > budget and shown:
            truncated = len(mems) - len(shown)
            break
        if len(line) > budget:      # 单行超预算且是第一条: 丢弃并全部标注未展示
            truncated = len(mems)
            break
        lines.append(line)
        shown.append(m)
        used += len(line) + 1       # +1 换行

    if not lines:
        return None

    head = f"以下是跨会话积累的用户记忆（共 {len(mems)} 条，来自过往对话）："
    if truncated:
        head = (f"以下是跨会话积累的用户记忆"
                f"（共 {len(mems)} 条，来自过往对话，另有 {truncated} 条未展示）：")
    store.touch_hits([m["id"] for m in shown])
    return "<user-memory>\n" + head + "\n" + "\n".join(lines) + "\n</user-memory>"
