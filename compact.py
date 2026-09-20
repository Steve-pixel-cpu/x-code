import re
from typing import List

from pydantic import BaseModel

from models import (Message, TextContentBlock, ToolContentBlock, ToolResultContentBlock,
                    ImageContentBlock, FileContentBlock)
from prompt import _collapse_blank_lines


class CompactionConfig(BaseModel):
    # 保留区默认 8 条 = 通常覆盖最近两个完整的工具交换块。压缩摘要再好
    # 也是有损的, 最近的原始终据（刚读的代码段、命令输出）必须逐字活着,
    # 模型才不必为了"看清手上这点事"重新去读文件。
    preserve_recent_messages: int = 8
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
        elif isinstance(block, ImageContentBlock):
            # 图片按固定值估算: base64 体积与像素数都不反映真实 token 开销,
            # 视觉端点按分辨率分档计, 1500 是常见的单图近似值
            res = res + 1500
        elif isinstance(block, FileContentBlock):
            res = res + (len(block.name) + len(block.text)) // 4 + 1
    return res


def estimate_session_tokens(msgs: List[Message]) -> int:
    return sum(estimate_message_tokens(msg) for msg in msgs)

def should_compact(msgs: List[Message], config: CompactionConfig) -> bool:
    return len(msgs) > config.preserve_recent_messages and estimate_session_tokens(msgs) > config.max_estimated_tokens


# 摘要内容配额。设计立场: 压缩摘要的职责是"保住调查结论", 不是"复述流水账"。
# 旧实现给每条消息留一行时间线、工具结果只留 40 字符——证据被磨成渣, 模型
# 压缩后只能把同样的文件重读一遍（实测同一命令被重跑 10 次）。现在助手正文
# 逐字进摘要（结论都在里面）, 时间线只留一小段衔接保留区。
_MAX_FINDINGS = 30           # 进入摘要的助手结论文本条数上限（取最新的）
_MAX_FINDING_CHARS = 1_500   # 单条结论截断上限（正常助手消息远小于此）
_RECENT_TIMELINE = 10        # 时间线覆盖的条数: 只衔接保留区, 不复述全史


def _clean_snippet(text: str, limit: int) -> str:
    """摘要片段净化: 工具输出里常有无法解码的字节（解码器以 U+FFFD 替代,
    压缩摘要直接截取就会带出一串乱码）和多行内容——剔除替换符、压平空白
    后再截断, 超长以省略号结尾。"""
    text = text.replace("\ufffd", "")
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def summarize_messages(msgs: List[Message]) -> str:
    """
    将一组消息压缩成摘要。

    摘要包含：
    - 消息统计（几条 user/assistant/tool 消息）
    - 使用了哪些工具
    - 最近的用户请求
    - 待完成的工作
    - 涉及的关键文件
    - 助手结论（逐字保留——这是压缩后模型还能"记得自己查到了什么"的关键）
    - 附件标记（图片/文件块的位置）
    - 少量近期活动时间线（衔接保留区的桥）

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
                    recent_requests.append(_clean_snippet(block.text, 160))
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
                    pending_work.append(_clean_snippet(block.text, 160))
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

    # 6. 助手结论逐字保留: 助手正文是调查结论的唯一载体（"实锤了——X 写进了
    # Y"这类句子）。工具结果可以截断, 结论截断 = 模型失忆 = 重新排查。
    findings: list[str] = []
    attachment_lines: list[str] = []
    for msg in msgs:
        if msg.role == "assistant":
            for block in msg.content:
                if isinstance(block, TextContentBlock) and block.text.strip():
                    findings.append(block.text.strip()[:_MAX_FINDING_CHARS])
        marks = []
        for block in msg.content:
            if isinstance(block, ImageContentBlock):
                marks.append("[图片]")
            elif isinstance(block, FileContentBlock):
                marks.append(f"[附件 {block.name}]")
        if marks:
            attachment_lines.append(f"  - {msg.role}: {' / '.join(marks)}")
    findings = findings[-_MAX_FINDINGS:]

    # 7. 时间线只覆盖末尾一小段: 它的职责是衔接下面的保留区（"最近在干
    # 什么"）, 不是复述全部历史——全史复述正是旧实现越压越大的原因。
    timeline_msgs = msgs[-_RECENT_TIMELINE:]
    timeline: list[str] = []
    for msg in timeline_msgs:
        role = msg.role
        parts = []
        for block in msg.content:
            if isinstance(block, TextContentBlock):
                parts.append(_clean_snippet(block.text, 80))
            elif isinstance(block, ToolContentBlock):
                parts.append(f"tool_use {block.name}({_clean_snippet(block.input, 40)})")
            elif isinstance(block, ToolResultContentBlock):
                status = "error " if block.is_error else ""
                parts.append(f"tool_result {block.name}: {status}{_clean_snippet(block.output, 40)}")
            elif isinstance(block, ImageContentBlock):
                parts.append("[图片]")
            elif isinstance(block, FileContentBlock):
                parts.append(f"[附件 {block.name}]")
        timeline.append(f"  - {role}: {' | '.join(parts)}")

    # 8. 组装摘要
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

    if key_files:
        lines.append(f"- Key files referenced: {', '.join(sorted(key_files)[:8])}.")

    if findings:
        lines.append("- Findings and conclusions stated by the assistant "
                     "(verbatim, oldest first — these are settled results, "
                     "do NOT re-verify or re-read their sources):")
        for i, finding in enumerate(findings, 1):
            lines.append(f"  {i}. {finding}")

    if attachment_lines:
        lines.append("- Non-text attachments in earlier messages:")
        lines.extend(attachment_lines)

    if pending_work:
        lines.append("- Pending work:")
        for item in pending_work:
            lines.append(f"  - {item}")

    if timeline:
        lines.append("- Recent activity (the newest messages follow verbatim "
                     "after this summary):")
        lines.extend(timeline)

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


def cut_point(messages: List[Message], config: CompactionConfig) -> int:
    """保留区起点: 末尾 preserve_recent 条整体保留, 切割点回退到安全边界
    （不以 tool 消息开头）。返回 0 = 没有可安全压缩的内容。

    独立成纯函数是为了让调用方（runtime 的请求期视图）能在不生成摘要的
    前提下先问"压了能压掉几条", 并把切割点当缓存键复用已生成的摘要。
    """
    if len(messages) <= config.preserve_recent_messages:
        return 0
    return _adjust_cut_point(messages, len(messages) - config.preserve_recent_messages)


def continuation_message(formatted_summary: str, preserved: bool) -> Message:
    """压缩视图的首条消息: 续接说明 + 摘要正文。role 用 user（models.py
    没有 system role, 与旧实现一致）。"""
    text = (
        "This session is being continued from a previous conversation "
        "that ran out of context. The summary below covers the earlier portion.\n\n"
        f"{formatted_summary}"
    )
    if preserved:
        text += "\n\nRecent messages are preserved verbatim."
    text += (
        "\nContinue the conversation from where it left off without "
        "asking the user any further questions."
    )
    return Message(role="user", content=[TextContentBlock(text=text)])


def compact_session(messages: List[Message], config: CompactionConfig) -> CompactionResult:
    """执行会话压缩 — 源码: compact.rs:75-111

        核心逻辑:
        1. 判断是否需要压缩
        2. 分割: 旧消息（要压缩的）+ 新消息（要保留的）, 切割点落在安全边界
        3. 只对旧消息生成摘要（保留区自己的内容反正是逐字带走的, 没必要
           再在摘要里复述一遍）
        4. 创建延续消息（user 角色）
        5. 返回: [延续消息] + 保留的消息
    """
    keep_from = cut_point(messages, config)
    if keep_from == 0:
        return CompactionResult(
            summary="",
            formatted_summary="",
            compacted_messages=messages,
            removed_count=0
        )
    removed = messages[:keep_from]
    preserved = messages[keep_from:]

    #生成摘要（只压被移除的部分）
    summary = summarize_messages(removed)
    formatted_summary = format_compact_summary(summary)

    compacted = [continuation_message(formatted_summary, preserved=bool(preserved))] + list(preserved)

    return CompactionResult(
        summary=summary,
        formatted_summary=formatted_summary,
        compacted_messages=compacted,
        removed_count=len(removed),
    )

