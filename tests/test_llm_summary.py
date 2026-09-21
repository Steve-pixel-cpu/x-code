"""LLM 摘要主路径测试: 压缩激活后首次构建视图时, 用 side-call 生成结构化
摘要; 规则摘要（compact.summarize_messages）只作端点故障回退。重算时只把
增量部分交给模型与上一摘要合并——重算发生在会话逼近阈值时, 全量重喂既慢
又贵。

side-call 卫生（钉死）: include_tools=False（摘要器不该有工具可调）、
emit_output=False（不在终端回放）、thinking=low（摘要是整理不是推理）。

运行: uv run pytest tests/test_llm_summary.py -v
"""

import pytest

from api_client import MessageStopEvent, TextDeltaEvent, UsageInfo
from models import Message, Session
from permissions import ALLOW_MODE, PermissionPolicy
from runtime import ConversationRuntime


class SummaryFakeClient:
    """正文调用（include_tools 缺省 True）返回固定文本事件;
    摘要 side-call（include_tools=False）按 fail/empty 剧本响应, 并记录入参。"""

    def __init__(self, summary_text: str = "LLM 摘要内容",
                 mode: str = "ok"):
        self.summary_text = summary_text
        self.mode = mode          # ok | raise | empty
        self.thinking_level = "medium"
        self.summary_calls: list[dict] = []
        self.body_calls = 0

    def stream(self, system_prompt, messages, thinking_level=None, *,
               include_tools=True, emit_output=None):
        if not include_tools:
            if self.mode == "raise":
                raise RuntimeError("endpoint down")
            self.summary_calls.append({
                "system_prompt": list(system_prompt),
                "messages": list(messages),
                "thinking_level": thinking_level,
                "emit_output": emit_output,
            })
            if self.mode == "empty":
                return [MessageStopEvent(usage=None)]
            return [
                TextDeltaEvent(text=self.summary_text),
                MessageStopEvent(usage=UsageInfo(input_tokens=5_000, output_tokens=100)),
            ]
        self.body_calls += 1
        return [
            TextDeltaEvent(text="ok"),
            MessageStopEvent(usage=UsageInfo(input_tokens=1_000, output_tokens=10)),
        ]


class NoopExecutor:
    def execute(self, tool_name, input, tool_use_id=None) -> str:
        return ""


def make_session(n: int = 12) -> Session:
    return Session(messages=[
        Message.user_text(f"旧消息{i} " + "x" * 40) for i in range(n)
    ])


def make_runtime(session, client) -> ConversationRuntime:
    return ConversationRuntime(
        session=session,
        api_client=client,
        tool_executor=NoopExecutor(),
        permission_policy=PermissionPolicy(active_mode=ALLOW_MODE),
        system_prompt=["助手"],
    )


# ------------------------------------------------------------
# 主路径: 摘要 = LLM 输出; side-call 卫生; 摘要按切割点缓存
# ------------------------------------------------------------

def test_compact_view_uses_llm_summary_and_caches():
    client = SummaryFakeClient(summary_text="1. Primary request: 修压缩 bug")
    runtime = make_runtime(make_session(), client)
    runtime._compact_active = True          # 直接激活, 专测视图构建

    view = runtime._model_view()

    assert len(client.summary_calls) == 1
    call = client.summary_calls[0]
    # side-call 卫生: 终端静默 + 低档思考
    assert call["emit_output"] is False
    assert call["thinking_level"] == "low"
    # 输入 = 被归档的前 4 条原样消息（12 - 保留 8）+ 末尾摘要指令
    assert len(call["messages"]) == 5
    assert call["messages"][0].content[0].text.startswith("旧消息0")
    assert "summary of this conversation" in call["messages"][-1].content[0].text
    # 视图首条 = 续接说明 + LLM 摘要正文（不是规则摘要的 "Summary:" 前缀）
    assert view[0].content[0].text.startswith("This session is being continued")
    assert "1. Primary request: 修压缩 bug" in view[0].content[0].text
    assert "Summary:" not in view[0].content[0].text

    # 缓存: 历史未推进超余量时, 再次构建视图不重发 side-call
    runtime._model_view()
    assert len(client.summary_calls) == 1


# ------------------------------------------------------------
# 回退: 端点异常 / 空返回 → 规则摘要接管, 视图照常可用
# ------------------------------------------------------------

@pytest.mark.parametrize("mode", ["raise", "empty"])
def test_llm_summary_failure_falls_back_to_rule_summary(mode):
    client = SummaryFakeClient(mode=mode)
    runtime = make_runtime(make_session(), client)
    runtime._compact_active = True

    view = runtime._model_view()

    # 回退 = 规则摘要: format_compact_summary 的 "Summary:" 前缀,
    # 且内容覆盖归档区（归档区末尾的用户请求 旧消息3 在列; 旧消息9 属于
    # 保留区, 不进摘要）
    assert "Summary:" in view[0].content[0].text
    assert "旧消息3" in view[0].content[0].text
    assert len(view) == 9                    # 摘要 + 保留 8 条
    # 回退摘要同样进缓存, 不至于每次构建视图都重试失败调用
    assert runtime._compact_cache is not None


# ------------------------------------------------------------
# 增量重算: 只把新归档部分交给模型, 与上一摘要合并
# ------------------------------------------------------------

def test_resumary_passes_previous_summary_and_increment_only():
    client = SummaryFakeClient()
    session = make_session()
    runtime = make_runtime(session, client)
    runtime._compact_active = True

    runtime._model_view()                    # 首次: 无上一摘要, 全量归档 12 条
    first = client.summary_calls[0]
    assert len(client.summary_calls) == 1
    assert first["messages"][0].content[0].text.startswith("旧消息0")
    assert "Previous summary" not in first["messages"][0].content[0].text
    first_cut = runtime._compact_cache[0]

    # 追加 22 条: 34-4=30 > 8+6（余量）→ 重算。切割点 4 → 26, 增量 = 22 条
    for i in range(22):
        session.messages.append(Message.user_text(f"新{i}"))
    view = runtime._model_view()

    assert len(client.summary_calls) == 2
    second = client.summary_calls[1]
    # 首条 = 上一摘要引导; 其后 = 增量消息（从旧切割点起）; 末条 = 指令
    assert second["messages"][0].content[0].text.startswith("Previous summary")
    assert "LLM 摘要内容" in second["messages"][0].content[0].text
    assert second["messages"][1].content[0].text.startswith("旧消息4")
    assert second["messages"][-1].content[0].text.count("summary of this conversation") == 1
    assert len(second["messages"]) == 1 + (26 - first_cut) + 1
    # 新视图 = [续接摘要] + 保留 8 条
    assert len(view) == 9
