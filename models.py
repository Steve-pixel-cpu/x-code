from typing import Literal, Optional, Annotated, Union, List
from pydantic import BaseModel, Discriminator, Tag, Field

class ContentBlock(BaseModel):
    type: Literal['text', 'tool_use', 'tool_result', 'image', 'file']

class TextContentBlock(ContentBlock):
    text: str = ''
    type: Literal['text'] = 'text'

    # 关键：设置为 frozen（不可变）, 防止消息被篡改
    model_config = {"frozen": True}


class ToolContentBlock(ContentBlock):
    id: str
    name: str = ''
    input: str = ''
    type: Literal['tool_use'] = 'tool_use'

    # 关键：设置为 frozen（不可变）, 防止消息被篡改
    model_config = {"frozen": True}

class ToolResultContentBlock(ContentBlock):
    id: str
    name: str = ''
    output: str = ''
    is_error: Optional[bool] = None
    type: Literal['tool_result'] = 'tool_result'

    # 关键：设置为 frozen（不可变）, 防止消息被篡改
    model_config = {"frozen": True}

class ImageContentBlock(ContentBlock):
    """用户上传的图片块。source 即 Anthropic 线格式的 base64 来源,
    API 层原样透传, 不做本地识别。"""
    source: dict
    type: Literal['image'] = 'image'

    # 关键：设置为 frozen（不可变）, 防止消息被篡改
    model_config = {"frozen": True}

class FileContentBlock(ContentBlock):
    """用户上传的文本类附件（内部表示）。API 层转成带分隔头的 text 块发给模型。"""
    name: str = ''
    text: str = ''
    type: Literal['file'] = 'file'

    # 关键：设置为 frozen（不可变）, 防止消息被篡改
    model_config = {"frozen": True}

# Discriminated Union: Pydantic 根据 type 字段自动选正确的子类反序列化
# 没有这个，model_validate() 只会创建基类 ContentBlock，丢失 text/name 等字段。
AnyContentBlock = Annotated[
    Union[
        Annotated[TextContentBlock, Tag('text')],
        Annotated[ToolContentBlock, Tag('tool_use')],
        Annotated[ToolResultContentBlock, Tag('tool_result')],
        Annotated[ImageContentBlock, Tag('image')],
        Annotated[FileContentBlock, Tag('file')],
    ],
    Discriminator('type'),
]


class Message(BaseModel):
    role: Literal['user', 'assistant', 'tool']
    content: list[AnyContentBlock] = Field(default_factory=list)
    # 工具结果富展示元数据（write_file 的 diff 等）, 仅 tool 角色可能携带。
    # 不进 API 请求体（_convert_message 忽略）, 只落盘 + 回放给前端渲染。
    result_meta: Optional[dict] = None
    model_config = {"frozen": True}

    @classmethod
    def user_text(cls, text) -> Message:
        return cls(role='user', content=[TextContentBlock(type= 'text', text= text)])

    @classmethod
    def user_input(cls, text: Optional[str],
                   attachments: Optional[list[dict]] = None) -> Message:
        """带附件的用户消息: [文字块(可省)] + image 块们 + file 块们。
        text 与 attachments 全空时退化为单空 text 块（保证消息不悬空）。"""
        blocks: list[AnyContentBlock] = []
        if text:
            blocks.append(TextContentBlock(type='text', text=text))
        for att in (attachments or []):
            if att.get("kind") == "image":
                blocks.append(ImageContentBlock(
                    type='image',
                    source={
                        "type": "base64",
                        "media_type": att.get("media_type") or "image/png",
                        "data": att.get("data") or "",
                    },
                ))
            elif att.get("kind") == "file":
                blocks.append(FileContentBlock(
                    type='file',
                    name=att.get("name") or "",
                    text=att.get("text") or "",
                ))
        if not blocks:
            blocks.append(TextContentBlock(type='text', text=text or ""))
        return cls(role='user', content=blocks)

    @classmethod
    def tool_use(cls, id, name, input) -> Message:
        return cls(role='assistant', content=[ToolContentBlock(id= id, name= name, input= input)])

    @classmethod
    def tool_result(cls, id, name, output, is_error, result_meta=None) -> Message:
        return cls(role='tool', content=[ToolResultContentBlock(id= id, name= name, output= output, is_error= is_error)],
                   result_meta=result_meta)


class Session(BaseModel):
    messages: list[Message] = Field(default_factory=list)


if __name__ == '__main__':
    text_Mess = Message.user_text('hi')
