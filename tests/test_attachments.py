# -*- coding: utf-8 -*-
"""新增测试: models 新块往返 / Message.user_input 构造;
api_client._convert_message image 透传与 file→text; compact 对新块的估值与时间线。"""

import json

from api_client import _convert_message
from compact import estimate_message_tokens, summarize_messages
from models import (
    AnyContentBlock,
    FileContentBlock,
    ImageContentBlock,
    Message,
    TextContentBlock,
)


# ------------------------------------------------------------
# models — 新块序列化/反序列化往返
# ------------------------------------------------------------

def test_image_block_roundtrip():
    b = ImageContentBlock(
        type="image",
        source={"type": "base64", "media_type": "image/webp", "data": "AAAA"},
    )
    loaded = Message.model_validate(
        {"role": "user", "content": [b.model_dump()]})
    block = loaded.content[0]
    assert isinstance(block, ImageContentBlock)
    assert block.source == {"type": "base64", "media_type": "image/webp",
                            "data": "AAAA"}
    assert block.model_dump() == b.model_dump()


def test_file_block_roundtrip():
    b = FileContentBlock(type="file", name="n.md", text="# hi")
    loaded = Message.model_validate(
        {"role": "user", "content": [b.model_dump()]})
    block = loaded.content[0]
    assert isinstance(block, FileContentBlock)
    assert block.name == "n.md" and block.text == "# hi"


def test_frozen_blocks():
    """新块保持 frozen 约定。"""
    b = ImageContentBlock(type="image", source={"media_type": "image/png", "data": "x"})
    try:
        b.source = {}
        raise AssertionError("应抛 ValidationError")
    except Exception:
        pass


def test_user_input_mixed_order():
    """user_input: [text(可省)] + image 块们 + file 块们。"""
    m = Message.user_input("看图", [
        {"kind": "image", "name": "a.png", "media_type": "image/png", "data": "IMG1"},
        {"kind": "file", "name": "n.md", "text": "notes"},
        {"kind": "image", "media_type": "image/jpeg", "data": "IMG2"},
    ])
    assert m.role == "user"
    assert [b.type for b in m.content] == ["text", "image", "file", "image"]
    assert m.content[0].text == "看图"
    assert m.content[1].source["data"] == "IMG1"
    assert m.content[3].source["media_type"] == "image/jpeg"


def test_user_input_image_only():
    """只发图不打字: 无 text 块。"""
    m = Message.user_input("", [
        {"kind": "image", "media_type": "image/gif", "data": "G"},
    ])
    assert len(m.content) == 1
    assert m.content[0].type == "image"


def test_user_input_file_only():
    m = Message.user_input(None, [{"kind": "file", "name": "a.py", "text": "x=1"}])
    assert len(m.content) == 1
    assert isinstance(m.content[0], FileContentBlock)


def test_user_input_no_attachments_degenerates_to_text():
    """全空: 退化为单空 text 块, 消息不悬空。"""
    m = Message.user_input("", None)
    assert len(m.content) == 1
    assert m.content[0] == TextContentBlock(type="text", text="")


def test_user_text_still_works():
    """旧构造器不动。"""
    m = Message.user_text("hi")
    assert m.content == [TextContentBlock(type="text", text="hi")]


# ------------------------------------------------------------
# api_client — image 透传 / file→text 转换
# ------------------------------------------------------------

def test_convert_message_image_passthrough():
    m = Message.user_input("看", [
        {"kind": "image", "name": "a.webp", "media_type": "image/webp", "data": "AAAA"},
    ])
    out = _convert_message([m])
    assert len(out) == 1 and out[0]["role"] == "user"
    assert out[0]["content"][0] == {"type": "text", "text": "看"}
    assert out[0]["content"][1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/webp", "data": "AAAA"},
    }


def test_convert_message_file_becomes_text_with_header():
    m = Message.user_input("", [
        {"kind": "file", "name": "notes.md", "text": "# 标题"},
    ])
    out = _convert_message([m])
    assert out[0]["content"] == [
        {"type": "text", "text": "--- 附件: notes.md ---\n# 标题"},
    ]


def test_convert_message_merge_keeps_adjacent_user():
    """相邻同 role 合并逻辑保持: 两条 user 消息并成一条。"""
    out = _convert_message([
        Message.user_text("第一条"),
        Message.user_input("带附件", [
            {"kind": "image", "media_type": "image/png", "data": "D"},
        ]),
    ])
    assert len(out) == 1
    assert len(out[0]["content"]) == 3


def test_convert_message_assistant_tool_unchanged():
    """assistant / tool 分支不受影响。"""
    msgs = [
        Message.tool_use("t1", "bash", '{"command":"ls"}'),
        Message.tool_result("t1", "bash", "ok", False),
    ]
    out = _convert_message(msgs)
    assert out[0]["role"] == "assistant"
    assert out[0]["content"][0]["type"] == "tool_use"
    assert out[1]["role"] == "user"
    assert out[1]["content"][0]["type"] == "tool_result"


# ------------------------------------------------------------
# compact — 估值与时间线
# ------------------------------------------------------------

def test_estimate_tokens_image_and_file():
    m = Message.user_input("看图", [
        {"kind": "image", "media_type": "image/png", "data": "A"},
        {"kind": "file", "name": "n.md", "text": "x" * 40},
    ])
    # text(2//4+1=1) + image(1500) + file((4+40)//4+1=12)
    assert estimate_message_tokens(m) == 1 + 1500 + 12


def test_estimate_tokens_image_only():
    m = Message.user_input("", [
        {"kind": "image", "media_type": "image/png", "data": "A"},
    ])
    assert estimate_message_tokens(m) == 1500


def test_summarize_timeline_marks_attachments():
    m = Message.user_input("看图", [
        {"kind": "image", "media_type": "image/png", "data": "A"},
        {"kind": "file", "name": "报告.md", "text": "内容"},
    ])
    s = summarize_messages([m])
    assert "[图片]" in s
    assert "[附件 报告.md]" in s
