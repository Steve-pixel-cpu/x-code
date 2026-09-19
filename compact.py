import re
from typing import List

from pydantic import BaseModel

from models import Message, TextContentBlock, ToolContentBlock, ToolResultContentBlock
from prompt import _collapse_blank_lines


class CompactionConfig(BaseModel):
    preserve_recent_messages: int = 4
    max_estimated_tokens: int = 200_000

class CompactionResult(BaseModel):
    summary: str
    formatted_summary: str
    compacted_messages: list[Message]
    removed_count: int


def estimate_message_tokens(msg: Message) -> int:
    res = 0
    for block in msg.content:
        if isinstance(block, TextContentBlock):
            res = res + len(block.text) // 4 + 1
        elif isinstance(block, ToolContentBlock):
            res = res + (len(block.name) + len(block.input)) // 4 + 1
        elif isinstance(block, ToolResultContentBlock):
            res = res + (len(block.name) + len(block.output)) // 4 + 1
    return res


def estimate_session_tokens(msgs: List[Message]) -> int:
    return sum(estimate_message_tokens(msg) for msg in msgs)

def should_compact(msgs: List[Message], config: CompactionConfig) -> bool:
    return len(msgs) > config.preserve_recent_messages and estimate_session_tokens(msgs) > config.max_estimated_tokens


def summarize_messages(msgs: List[Message]) -> str:
    """
    将一组消息压缩成摘要。

    摘要包含：
    - 消息统计（几条 user/assistant/tool 消息）
    - 使用了哪些工具
    - 最近的用户请求
    - 待完成的工作
    - 涉及的关键文件
    - 时间线概要

    对应源码: compact.rs:113-198
    """
    # 1. 统计各角色的消息数
    user_count = sum(1 for m in msgs if m.role == "user")
    assistant_count = sum(1 for m in msgs if m.role == "assistant")
    tool_count = sum(1 for m in msgs if m.role == "tool")

    # 2. 收集使用过的工具名
    tool_names = set()
    for msg in msgs:
        for content in msg.content:
            if isinstance(content, ToolContentBlock):
                tool_names.add(content.name)
            if isinstance(content, ToolResultContentBlock):
                tool_names.add(content.name)

    # 3. 收集最近的用户请求
    recent_requests = []
    for msg in reversed(msgs):
        if msg.role == "user":
            for block in msg.content:
                if isinstance(block, TextContentBlock) and block.text.strip():
                    text = block.text[:160] + "..." if len(block.text) > 160 else block.text
                    recent_requests.append(text)
                    if len(recent_requests) >= 3:
                        break
        if len(recent_requests) >= 3:
            break
    recent_requests.reverse()

    # 4. 检测待完成的工作（含"todo"/"next"等关键词的消息）
    pending_work = []
    for msg in reversed(msgs):
        for block in msg.content:
            if isinstance(block, TextContentBlock):
                lower = block.text.lower()
                if any(kw in lower for kw in ["todo", "next", "pending", "remaining"]):
                    text = block.text[:160] + "..." if len(block.text) > 160 else block.text
                    pending_work.append(text)
        if len(pending_work) >= 3:
            break
    pending_work.reverse()

    # 5. 提取关键文件路径
    key_files = set()
    for msg in msgs:
        for block in msg.content:
            content = ""
            if isinstance(block, TextContentBlock):
                content = block.text
            elif isinstance(block, ToolContentBlock):
                content = block.input
            elif isinstance(block, ToolResultContentBlock):
                content = block.output
            # 简单的文件路径提取：包含 / 且有常见扩展名
            for token in content.split():
                token = token.strip(",:;()\"'`")
                if "/" in token and any(token.endswith(ext) for ext in [".py", ".rs", ".ts", ".js", ".json", ".md"]):
                    key_files.add(token)


    # 6. 组装摘要
    lines = [
        "<summary>",
        "Conversation summary:",
        f"- Scope: {len(msgs)} earlier messages compacted (user={user_count}, assistant={assistant_count}, tool={tool_count}).",
    ]

    if tool_names:
        lines.append(f"- Tools mentioned: {', '.join(sorted(tool_names))}.")

    if recent_requests:
        lines.append("- Recent user requests:")
        for req in recent_requests:
            lines.append(f"  - {req}")

    if pending_work:
        lines.append("- Pending work:")
        for item in pending_work:
            lines.append(f"  - {item}")

    if key_files:
        lines.append(f"- Key files referenced: {', '.join(sorted(key_files)[:8])}.")

    # 7. 时间线（每条消息的简短描述）
    lines.append("- Key timeline:")
    for msg in msgs:
        role = msg.role
        parts = []
        for block in msg.content:
            if isinstance(block, TextContentBlock):
                text = block.text[:80].replace("\n", " ")
                parts.append(text)
            elif isinstance(block, ToolContentBlock):
                parts.append(f"tool_use {block.name}({block.input[:40]})")
            elif isinstance(block, ToolResultContentBlock):
                status = "error " if block.is_error else ""
                parts.append(f"tool_result {block.name}: {status}{block.output[:40]}")
        lines.append(f"  - {role}: {' | '.join(parts)}")

    lines.append("</summary>")
    return "\n".join(lines)


def format_compact_summary(summary: str) -> str:
    """格式化压缩摘要 — 源码 compact.rs:38-50

    处理 XML 标签:
    1. 删除 <analysis>...</analysis> 块（这是给内部用的分析）
    2. 把 <summary>...</summary> 替换成 "Summary:\n" 前缀
    """
    # 删除 <analysis> 块
    result = re.sub(r'<analysis>.*?</analysis>', '', summary, flags=re.DOTALL)
    # 替换 <summary> 标签
    match = re.search(r'<summary>(.*?)</summary>', result, flags=re.DOTALL)
    if match:
        content = match.group(1).strip()
        result = result.replace(match.group(0), f"Summary:\n{content}")
    return _collapse_blank_lines(result).strip()

def _adjust_cut_point(messages: List[Message], keep_from: int) -> int:
    """把切割点回退到安全边界: 保留区不能以 tool 消息开头。

    消息序列是 user → assistant(tool_use) → tool(result) → ... 条数切割
    可能正好切在工具交换块中间——tool_use 被压进摘要、tool_result 留在
    保留区, 下一轮 _convert_message 会发出引用了不存在 tool_use 的
    tool_result, API 直接 400 掀翻整轮。回退到这组交换块的起点
    （assistant(tool_use) 之前）, 让 tool_use 和它的 result 同生共死。
    """
    while keep_from > 0 and messages[keep_from].role == "tool":
        keep_from -= 1
    return keep_from


def compact_session(messages: List[Message], config: CompactionConfig) -> CompactionResult:
    """执行会话压缩 — 源码 compact.rs:75-111

        核心逻辑:
        1. 判断是否需要压缩
        2. 分割: 旧消息（要压缩的）+ 新消息（要保留的）, 切割点落在安全边界
        3. 对旧消息生成摘要
        4. 创建延续消息（System 角色）
        5. 返回: [延续消息] + 保留的消息
    """
    if not should_compact(messages, config):
        return CompactionResult(
            summary="",
            formatted_summary= "",
            compacted_messages = messages,
            removed_count= 0
        )

    # 分割点: 保留最后 N 条; 再回退出工具交换块（悬空 tool_result 防护）
    keep_from = max(0, len(messages) - config.preserve_recent_messages)
    keep_from = _adjust_cut_point(messages, keep_from)
    if keep_from == 0:
        # 回退到头: 没有可安全切割的位置, 宁可不压——原样返回,
        # 调用方按 removed_count == 0 的"没压掉东西"语义处理
        return CompactionResult(
            summary="",
            formatted_summary="",
            compacted_messages=messages,
            removed_count=0
        )
    removed = messages[:keep_from]
    preserved = messages[keep_from:]

    #生成摘要
    summary = summarize_messages(messages)
    formatted_summary = format_compact_summary(summary)

    continuation_text = (
        "This session is being continued from a previous conversation "
        "that ran out of context. The summary below covers the earlier portion.\n\n"
        f"{formatted_summary}"
    )
    if preserved:
        continuation_text += "\n\nRecent messages are preserved verbatim."
    continuation_text += (
        "\nContinue the conversation from where it left off without "
        "asking the user any further questions."
    )

    # 摘要作为 system 角色的消息 — 源码: compact.rs:95-99
    system_msg = Message(
        role="user",  # 我们的 models.py 没有 system role，用 user 代替
        content=[TextContentBlock(text=continuation_text)],
    )

    compacted = [system_msg] + list(preserved)

    return CompactionResult(
        summary=summary,
        formatted_summary=formatted_summary,
        compacted_messages=compacted,
        removed_count=len(removed),
    )

