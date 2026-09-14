from typing import Literal, Optional, Annotated, Union, List
from pydantic import BaseModel, Discriminator, Tag, Field

class ContentBlock(BaseModel):
    type: Literal['text', 'tool_use', 'tool_result']

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

# Discriminated Union: Pydantic 根据 type 字段自动选正确的子类反序列化
# 没有这个，model_validate() 只会创建基类 ContentBlock，丢失 text/name 等字段。
AnyContentBlock = Annotated[
    Union[
        Annotated[TextContentBlock, Tag('text')],
        Annotated[ToolContentBlock, Tag('tool_use')],
        Annotated[ToolResultContentBlock, Tag('tool_result')],
    ],
    Discriminator('type'),
]


class Message(BaseModel):
    role: Literal['user', 'assistant', 'tool']
    content: list[AnyContentBlock] = Field(default_factory=list)
    model_config = {"frozen": True}

    @classmethod
    def user_text(cls, text) -> Message:
        return cls(role='user', content=[TextContentBlock(type= 'text', text= text)])

    @classmethod
    def tool_use(cls, id, name, input) -> Message:
        return cls(role='assistant', content=[ToolContentBlock(id= id, name= name, input= input)])

    @classmethod
    def tool_result(cls, id, name, output, is_error) -> Message:
        return cls(role='tool', content=[ToolResultContentBlock(id= id, name= name, output= output, is_error= is_error)])


class Session(BaseModel):
    messages: list[Message] = Field(default_factory=list)


if __name__ == '__main__':
    text_Mess = Message.user_text('hi')

