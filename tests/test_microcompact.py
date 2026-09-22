"""MicroCompact 测试: 估算 token 超过软阈值时, 保留窗口之外的可复现工具
结果替换为占位符（借鉴 Claude Code microCompact 设计）。

关键不变量:
- 块结构与 role 原样保留（tool_use/tool_result 配对不破坏, API 不 400）
- 最近 MICROCOMPACT_KEEP_RECENT 条原文保留
- 白名单外（todo/plan/agent）结果不动
- 纯视图操作: session.messages 原样, 展示层看不到占位符
- 清完即稳定: 视图骤降, 重复构建视图不二次改动

运行: uv run pytest tests/test_microcompact.py -v
"""

from models import Message, Session
from permissions import ALLOW_MODE, PermissionPolicy
from runtime import ConversationRuntime, MICROCOMPACT_PLACEHOLDER


class ScriptedClient:
    def __init__(self):
        self.thinking_level = "medium"

    def stream(self, system_prompt, messages, thinking_level=None, *, model=None, include_tools=True, emit_output=None, on_event=None):
        return []


class NoopExecutor:
    def execute(self, tool_name, input, tool_use_id=None) -> str:
        return ""


def make_runtime(session) -> ConversationRuntime:
    return ConversationRuntime(
        session=session,
        api_client=ScriptedClient(),
        tool_executor=NoopExecutor(),
        permission_policy=PermissionPolicy(active_mode=ALLOW_MODE),
        system_prompt=["s"],
    )


def big_result(i: int, name: str = "read_file", chars: int = 30_000) -> Message:
    return Message.tool_result(id=f"t{i}", name=name, output="x" * chars,
                               is_error=False)


def big_session(n: int) -> Session:
    return Session(messages=[big_result(i) for i in range(n)])


def test_超过阈值清除旧工具结果保留最近8条():
    rt = make_runtime(big_session(12))        # 12 × 30k 字符 ≈ 90k 估算 token

    view = rt._model_view()

    assert view is not rt.session().messages          # 纯视图: 会话原样
    assert len(view) == 12
    cleared = [m for m in view if m.content[0].output == MICROCOMPACT_PLACEHOLDER]
    assert len(cleared) == 12 - 8                     # 最近 8 条原文保留
    for m in view[-8:]:
        assert m.content[0].output != MICROCOMPACT_PLACEHOLDER
    # 块结构与 role 原样: 配对不破坏, API 不会 400
    assert all(m.role == "tool" and m.content[0].type == "tool_result" for m in view)
    # 展示层看不到占位符
    assert all(m.content[0].output == "x" * 30_000
               for m in rt.session().messages)


def test_低于阈值不触发():
    rt = make_runtime(big_session(4))         # 4 × 7.5k ≈ 30k < 60k 阈值

    view = rt._model_view()

    assert all(m.content[0].output == "x" * 30_000 for m in view)


def test_白名单外的工具结果不动():
    msgs = [big_result(0, name="todo")] + [big_result(i) for i in range(1, 13)]
    rt = make_runtime(Session(messages=msgs))

    view = rt._model_view()

    # todo 最老且超长, 但不在可清除白名单
    assert view[0].content[0].output == "x" * 30_000
    assert view[0].content[0].name == "todo"


def test_短结果不清除():
    msgs = [big_result(0, chars=100)] + [big_result(i) for i in range(1, 12)] \
        + [big_result(99, chars=100)]
    rt = make_runtime(Session(messages=msgs))

    view = rt._model_view()

    # 两个短结果（首尾）即使落在清除窗口外也原文保留
    assert view[0].content[0].output == "x" * 100
    assert view[-1].content[0].output == "x" * 100


def test_清完即稳定_重复构建视图无二次改动():
    rt = make_runtime(big_session(12))

    view1 = rt._model_view()
    view2 = rt._model_view()

    assert view1 == view2
    assert sum(1 for m in view2
               if m.content[0].output == MICROCOMPACT_PLACEHOLDER) == 4


def test_压缩视图同样过microcompact():
    """压缩视图 = [续接摘要] + 保留区; 保留区膨胀后旧工具结果同样被清。"""
    msgs = [big_result(i) for i in range(14)]
    session = Session(messages=msgs)
    rt = make_runtime(session)
    rt._compact_active = True
    rt._compact_cache = (2, "压缩摘要")       # 保留区 = msgs[2:], 12 条结果

    view = rt._model_view()

    body = view[1:]
    assert len(body) == 12
    cleared = [m for m in body if m.content[0].output == MICROCOMPACT_PLACEHOLDER]
    assert len(cleared) == 12 - 8
