"""循环层预算测试: usage 计量打通、单轮输出预算、迭代上限优雅收束、auto-compact 信号、配置接线。

运行方式（在 x-code 目录下）:
    uv run pytest tests/test_loop_budget.py -v
"""

import pytest

from api_client import MessageStopEvent, TextDeltaEvent, ToolUseEvent, UsageInfo
from config import ConfigLoader, RuntimeConfig, RuntimeFeatureConfig
from main import build_runtime
from models import Message, Session
from permissions import ALLOW_MODE, PermissionMode, PermissionPolicy
from runtime import ConversationRuntime, TokenUsage, build_assistant_message
from tools import ToolRegistry


@pytest.fixture(autouse=True)
def clean_budget_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_TURN_TOKEN_BUDGET", raising=False)


# ------------------------------------------------------------
# 测试替身
# ------------------------------------------------------------

def make_events(text: str, out_tokens: int, in_tokens: int = 1_000) -> list:
    """一轮纯文本回复的事件流，带指定用量。"""
    return [
        TextDeltaEvent(text=text),
        MessageStopEvent(usage=UsageInfo(input_tokens=in_tokens, output_tokens=out_tokens)),
    ]


def make_tool_events(out_tokens: int = 10) -> list:
    """带工具调用的事件流（工具会被 ALLOW 模式放行，NoopExecutor 执行）。"""
    return [
        ToolUseEvent(id="t1", name="bash", input='{"command": "ls"}'),
        MessageStopEvent(usage=UsageInfo(input_tokens=1_000, output_tokens=out_tokens)),
    ]


class ScriptedClient:
    """按剧本逐次返回事件流；剧本耗尽后重复最后一条。记录调用次数与每次看到的消息。"""

    def __init__(self, script: list):
        self.script = list(script)
        self.calls = 0
        self.seen: list[list] = []       # 每次调用收到的 messages 快照
        self.thinking_level = "medium"   # runtime 构建时读取（会话级等级初值）

    def stream(self, system_prompt, messages, thinking_level=None, *, model=None, include_tools=True, emit_output=None, on_event=None) -> list:
        if not include_tools or emit_output is not None:
            raise TypeError("side-calls unsupported")  # 锁定旧客户端语义: 无 side-call 能力
        self.seen.append(list(messages))
        events = self.script[self.calls] if self.calls < len(self.script) else self.script[-1]
        self.calls += 1
        return events


class NoopExecutor:
    def execute(self, tool_name, input, tool_use_id=None) -> str:
        return ""


def make_runtime(client, **budgets) -> ConversationRuntime:
    runtime = ConversationRuntime(
        session=Session(),
        api_client=client,
        tool_executor=NoopExecutor(),
        permission_policy=PermissionPolicy(active_mode=ALLOW_MODE),
        system_prompt=["你是助手"],
    )
    for name, value in budgets.items():
        getattr(runtime, f"with_{name}")(value)
    return runtime


# ------------------------------------------------------------
# 计量打通 — usage 从事件流走到 TokenUsage（此前永远返回 None）
# ------------------------------------------------------------

def test_build_assistant_message_carries_usage():
    events = make_events("你好", out_tokens=50, in_tokens=100)
    events[1].usage.cache_read_input_tokens = 10

    _, usage = build_assistant_message(events)

    assert usage is not None
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.cache_read_input_tokens == 10


def test_build_assistant_message_without_usage_stays_none():
    events = [TextDeltaEvent(text="hi"), MessageStopEvent()]
    _, usage = build_assistant_message(events)
    assert usage is None


# ------------------------------------------------------------
# 单轮输出预算 — 超限在循环顶部收束，历史保持一致
# ------------------------------------------------------------

def test_turn_output_budget_stops_before_next_call():
    # 工具调用让循环想继续，累计输出超限后第三次调用在循环顶部被拦下
    client = ScriptedClient([make_tool_events(out_tokens=60)])
    runtime = make_runtime(client, turn_output_budget=100)

    summary = runtime.run_turn("干活")

    assert summary.budget_exhausted is True
    assert client.calls == 2
    assert summary.iterations == 2
    # 无悬空 tool_use; 收束说明作为 user 消息落进历史——对模型解释
    # "被掐了、证据都在、下一轮直接干活", 否则下一轮只会从头再查
    roles = [m.role for m in runtime.session().messages]
    assert roles == ["user", "assistant", "tool", "assistant", "tool", "user"]
    notice = runtime.session().messages[-1].content[0].text
    assert "output token budget" in notice
    assert "do NOT re-read" in notice


def test_turn_budget_not_triggered_when_under():
    client = ScriptedClient([make_events("ok", out_tokens=10)])
    runtime = make_runtime(client, turn_output_budget=65_536)

    summary = runtime.run_turn("hi")

    assert summary.budget_exhausted is False
    assert client.calls == 1


# ------------------------------------------------------------
# 迭代上限 — 优雅收束（旧实现在这里 raise 炸掉整轮）
# ------------------------------------------------------------

def test_max_iterations_stops_gracefully():
    client = ScriptedClient([make_tool_events()])
    runtime = make_runtime(client, max_iterations=3)

    summary = runtime.run_turn("hi")            # 旧实现: RuntimeError

    assert summary.iterations_exhausted is True
    assert summary.budget_exhausted is False
    assert client.calls == 3


# ------------------------------------------------------------
# auto-compact 信号 — 用最近一次调用的真实上下文占用（input + 缓存读写）,
# 而不是只增不减的累计 input; auto_compacted 语义 = "本请求使用了压缩
# 视图", 不是"闸门触发过"; 历史(内存/磁盘)不被改写——压缩只影响模型视图
# ------------------------------------------------------------

def test_auto_compact_triggers_on_latest_input():
    # 消息数充足: 过阈值且真压掉了 → True（preserve_recent=8, 12 旧消息
    # 才有可安全切割的位置）
    session = Session(messages=[
        Message.user_text(f"旧消息{i} " + "x" * 40) for i in range(12)
    ])
    client = ScriptedClient([make_events("ok", out_tokens=1, in_tokens=500_000)])
    runtime = ConversationRuntime(
        session=session,
        api_client=client,
        tool_executor=NoopExecutor(),
        permission_policy=PermissionPolicy(active_mode=ALLOW_MODE),
        system_prompt=["你是助手"],
    ).with_auto_compact_threshold(200_000)

    summary = runtime.run_turn("hi")

    assert summary.auto_compacted is True
    # 历史(内存)不被改写: 12 旧 + user"hi" + assistant 完整保留
    assert len(runtime.session().messages) == 14
    # 当轮请求时 usage 未知 → 全量视图; 轮末激活, 自下一次请求起压缩
    assert len(client.seen[0]) == 13
    assert runtime._compact_active is True


def test_auto_compact_not_fires_when_nothing_removable():
    # 过阈值但消息数 <= preserve_recent(4)（如单条超大粘贴）:
    # 没东西可压 → False, 不亮假阳性信号
    client = ScriptedClient([make_events("ok", out_tokens=1, in_tokens=500_000)])
    runtime = make_runtime(client, auto_compact_threshold=200_000)

    assert runtime.run_turn("hi").auto_compacted is False


def test_auto_compact_not_triggered_under_threshold():
    client = ScriptedClient([make_events("ok", out_tokens=1, in_tokens=100)])
    runtime = make_runtime(client, auto_compact_threshold=200_000)

    assert runtime.run_turn("hi").auto_compacted is False


def test_auto_compact_fires_mid_turn_before_next_call():
    # 阈值在单轮中途被跨过: 第二次调用前就地压缩（此时工具结果已回填、
    # 历史一致），而不是等 turn 结束——更不会等上下文撑爆 API 报 400
    session = Session(messages=[
        Message.user_text(f"旧消息{i} " + "x" * 40) for i in range(12)
    ])
    first = make_tool_events(out_tokens=10)
    first[1].usage.input_tokens = 50_000          # 第一次调用后即跨过阈值
    client = ScriptedClient([
        first,
        make_events("ok", out_tokens=1, in_tokens=100),
    ])
    runtime = ConversationRuntime(
        session=session,
        api_client=client,
        tool_executor=NoopExecutor(),
        permission_policy=PermissionPolicy(active_mode=ALLOW_MODE),
        system_prompt=["你是助手"],
    ).with_auto_compact_threshold(10_000)

    summary = runtime.run_turn("新任务")

    assert summary.auto_compacted is True
    assert client.calls == 2
    # 第二次调用看到的是压缩视图: 摘要开头 + 保留的最近几条, 不再是全量
    # (12旧+user+assistant+tool=15 条, 切掉前 7 条 → 摘要 + 保留 8 条)
    second_call = client.seen[1]
    assert len(second_call) == 9
    assert "continued from a previous conversation" in second_call[0].content[0].text
    # 压缩不产生悬空 tool_use: tool_use 与 tool_result 必须成对保留在末尾
    assert second_call[-2].role == "assistant"
    assert second_call[-1].role == "tool"
    # 历史(内存)不被改写: 12旧 + user + assistant(tool_use) + tool + assistant(终答)
    assert len(runtime.session().messages) == 16


def test_context_tokens_counts_cache_usage():
    # 开 prompt caching 后 input_tokens 只计未命中部分;
    # 压缩闸门必须看 input + 缓存写入 + 缓存读的真实占用
    usage = TokenUsage(input_tokens=500,
                       cache_creation_input_tokens=30_000,
                       cache_read_input_tokens=70_000)
    assert usage.context_tokens() == 100_500


# ------------------------------------------------------------
# build_runtime 接线 — maxIterations / tokenBudget / turnTokenBudget 不再是死配置
# ------------------------------------------------------------

def test_build_runtime_wires_loop_budgets():
    config = RuntimeConfig(feature_config=RuntimeFeatureConfig(
        max_iterations=7,
        token_budget=123_456,
        turn_token_budget=777,
    ))

    runtime = build_runtime(
        session=Session(),
        api_client=ScriptedClient([]),
        registry=ToolRegistry(),
        system_prompt=[],
        hooks_config=config,
    )

    assert runtime._max_iterations == 7
    assert runtime._auto_compact_threshold == 123_456
    assert runtime._turn_output_budget == 777


# ------------------------------------------------------------
# turnTokenBudget 配置解析
# ------------------------------------------------------------

def test_config_turn_token_budget_default(tmp_path):
    assert ConfigLoader(cwd=tmp_path, config_home=tmp_path).load().turn_token_budget() == 262_144


def test_config_turn_token_budget_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_TURN_TOKEN_BUDGET", "999")
    assert ConfigLoader(cwd=tmp_path, config_home=tmp_path).load().turn_token_budget() == 999


# ------------------------------------------------------------
# 迭代收束说明 — 与预算收束同语义: 模型必须知道轮次为何结束
# ------------------------------------------------------------

def test_iterations_exhausted_appends_notice():
    client = ScriptedClient([make_tool_events(out_tokens=1)])
    runtime = make_runtime(client, max_iterations=2)

    summary = runtime.run_turn("hi")

    assert summary.iterations_exhausted is True
    assert summary.budget_exhausted is False
    roles = [m.role for m in runtime.session().messages]
    assert roles[-1] == "user"
    assert "iteration limit" in runtime.session().messages[-1].content[0].text


# ------------------------------------------------------------
# max_tokens 截断自愈 — 恢复提示继续循环（借鉴 Claude Code: "Resume
# directly — no apology, no recap"）, 最多 3 次
# ------------------------------------------------------------

def _truncated_events() -> list:
    return [
        TextDeltaEvent(text="partial"),
        MessageStopEvent(usage=UsageInfo(input_tokens=100, output_tokens=5_000),
                         stop_reason="max_tokens"),
    ]


def test_max_tokens截断注入恢复提示继续循环():
    client = ScriptedClient([_truncated_events(), make_events("done", out_tokens=10)])
    runtime = make_runtime(client)

    runtime.run_turn("hi")

    roles = [m.role for m in runtime.session().messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    notice = runtime.session().messages[2].content[0].text
    assert "Output token limit hit" in notice
    assert "no apology, no recap" in notice
    assert client.calls == 2                      # 恢复后继续, 第二次正常收尾


def test_截断恢复最多3次后收束():
    client = ScriptedClient([_truncated_events()] * 5)
    runtime = make_runtime(client)

    summary = runtime.run_turn("hi")

    notices = [m for m in runtime.session().messages
               if m.role == "user" and "Output token limit hit" in m.content[0].text]
    assert len(notices) == 3                      # 恢复上限
    assert client.calls == 4                      # 首发 + 3 次恢复, 之后收束
    assert summary.budget_exhausted is False


def test_正常结束不带截断标记不受影响():
    client = ScriptedClient([make_events("done", out_tokens=10)])
    runtime = make_runtime(client)

    runtime.run_turn("hi")

    roles = [m.role for m in runtime.session().messages]
    assert roles == ["user", "assistant"]         # 无恢复提示插入


# ------------------------------------------------------------
# 压缩摘要缓存 — 切割点未推进超余量时复用旧摘要, 保留区温和变长;
# 推进超余量才重算。钉住: 摘要生成是"每若干条消息一次", 不是每请求一次
# ------------------------------------------------------------

def test_compacted_view_reuses_cached_summary_within_margin():
    session = Session(messages=[
        Message.user_text(f"旧消息{i} " + "x" * 40) for i in range(12)
    ])
    client = ScriptedClient([make_events("ok", out_tokens=1, in_tokens=100)])
    runtime = make_runtime(client)
    runtime._session = session               # make_runtime 不收 session, 挂上专用的
    runtime._compact_active = True           # 直接激活, 专测视图构建

    view1 = runtime._model_view()
    cut1 = runtime._compact_cache[0]
    assert cut1 == 4                          # 12 - preserve_recent(8)
    assert len(view1) == 9                    # 摘要 + 保留 8 条

    # 追加 2 条: 切割点若重算会前移, 但 14-4=6 <= 8+6(余量) → 复用旧摘要
    session.messages.append(Message.user_text("新消息A"))
    session.messages.append(Message.user_text("新消息B"))
    view2 = runtime._model_view()
    assert runtime._compact_cache[0] == cut1  # 缓存未失效
    assert view2[1] is session.messages[cut1] # 保留区从同一位置起, 变长了
    assert len(view2) == 11

    # 再追加 6 条: 20-4=16 > 8+6 → 重算, 切割点前移
    for i in range(6):
        session.messages.append(Message.user_text(f"更多{i}"))
    view3 = runtime._model_view()
    cut3 = runtime._compact_cache[0]
    assert cut3 == 12                         # 20 - 8
    assert view3[1] is session.messages[cut3]
    assert len(view3) == 9                    # 摘要 + 保留 8 条


# ------------------------------------------------------------
# 摘要内容 — 助手结论逐字保留(反循环核心: 压缩后模型仍"记得"查到了什么)
# ------------------------------------------------------------

def test_summary_preserves_assistant_findings_verbatim():
    from compact import CompactionConfig, compact_session
    from models import TextContentBlock, ToolContentBlock

    conclusion = ("实锤了——bg-user.png 被写进 PyInstaller 的临时解包目录 "
                  "_MEIPASS, 重启即失。")
    msgs = [
        Message.user_text("排查外观设置持久化"),
        Message(role="assistant", content=[
            TextContentBlock(text=conclusion),
            ToolContentBlock(id="t1", name="bash", input="ls"),
        ]),
        Message.tool_result(id="t1", name="bash",
                            output="x" * 500, is_error=False),
    ] + [Message.user_text(f"填充{i}") for i in range(10)]

    result = compact_session(msgs, CompactionConfig(max_estimated_tokens=0))

    assert result.removed_count > 0
    assert conclusion in result.formatted_summary   # 结论逐字活着
    # 提示语不再与证据缺失自相矛盾: 不许重读 → 改为"需要精确内容再重读"
    assert "settled results" in result.formatted_summary
    assert "do NOT re-verify" not in result.formatted_summary
