"""压缩摘要熔断器的验收测试。

场景: 压缩激活 + LLM 端点持续故障——没有熔断器时, 每轮请求的视图构建
都会重试一次注定失败的摘要 side-call（白烧调用 + 拖慢响应）。熔断器在
连续失败 3 次后停用本会话的 LLM 摘要, 直接走规则摘要; 手动 /compact 或
热换 api_client 复位。

运行: uv run pytest tests/test_compact_breaker.py -v
"""

from permissions import ALLOW_MODE, PermissionPolicy
from runtime import ConversationRuntime

from test_llm_summary import NoopExecutor, SummaryFakeClient, make_session


class CountingFailClient(SummaryFakeClient):
    """摘要 side-call 一律抛端点错误, 但记录尝试次数（raise 模式不进
    summary_calls, 这里单独数）。"""

    def __init__(self):
        super().__init__(mode="raise")
        self.attempts = 0

    def stream(self, system_prompt, messages, thinking_level=None, *,
               include_tools=True, emit_output=None):
        if not include_tools:
            if self.mode == "raise":
                self.attempts += 1
                raise RuntimeError("endpoint down")
            return super().stream(system_prompt, messages,
                                  thinking_level=thinking_level,
                                  include_tools=False, emit_output=emit_output)
        return super().stream(system_prompt, messages,
                              thinking_level=thinking_level,
                              include_tools=include_tools,
                              emit_output=emit_output)


def make_runtime(client) -> ConversationRuntime:
    return ConversationRuntime(
        session=make_session(12), api_client=client,
        tool_executor=NoopExecutor(),
        permission_policy=PermissionPolicy(active_mode=ALLOW_MODE),
        system_prompt=["助手"],
    )


def test_breaker_opens_after_three_consecutive_failures(capsys):
    client = CountingFailClient()
    rt = make_runtime(client)
    msgs = make_session(12).messages

    summaries = [rt._build_compact_summary(msgs, keep_from=8) for _ in range(5)]

    assert client.attempts == 3          # 第 4、5 次不再发起 side-call
    assert rt._summary_disabled is True
    assert all(isinstance(s, str) and s for s in summaries)   # 规则摘要兜底可用
    assert "停用 LLM 压缩摘要" in capsys.readouterr().out


def test_success_resets_streak():
    client = CountingFailClient()
    rt = make_runtime(client)
    msgs = make_session(12).messages
    rt._build_compact_summary(msgs, keep_from=8)   # 失败 1 次
    assert rt._summary_fail_streak == 1
    client.mode = "ok"                              # 端点恢复
    rt._build_compact_summary(msgs, keep_from=8)
    assert rt._summary_fail_streak == 0             # 连击清零, 不会累积误熔断
    assert rt._summary_disabled is False


def test_manual_compact_resets_breaker():
    client = CountingFailClient()
    rt = make_runtime(client)
    msgs = make_session(12).messages
    for _ in range(3):
        rt._build_compact_summary(msgs, keep_from=8)
    assert rt._summary_disabled is True
    rt.compact()
    assert rt._summary_disabled is False            # 用户明确要求重试
    assert rt._summary_fail_streak == 0


def test_client_swap_resets_breaker():
    client = CountingFailClient()
    rt = make_runtime(client)
    msgs = make_session(12).messages
    for _ in range(3):
        rt._build_compact_summary(msgs, keep_from=8)
    assert rt._summary_disabled is True
    rt.set_api_client(SummaryFakeClient("新端点摘要"))   # 换端点
    assert rt._summary_disabled is False
    assert rt._build_compact_summary(msgs, keep_from=8) == "新端点摘要"
