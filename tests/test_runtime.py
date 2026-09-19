"""runtime.py 的规格钉子。

原则：测试钉的不是"代码现在长什么样"，而是"我们决定它应该长什么样"。
每个断言都是一个已经做出的设计决定；谁改了行为，测试当场红。

两个替身（fake）是这套测试的核心道具：
- ScriptedApiClient 按剧本逐次返回事件流 —— 我们测的是 runtime 的循环逻辑，不是网络。
  它能存在，靠的是 ApiClient 是可替换的注入接口（ABC/Protocol 的回报）。
- EchoExecutor 模拟工具执行：总是成功 / 可切换为抛 ToolError。
  它能存在，靠的是 ToolExecutor 是 Protocol（结构化鸭子类型，无需继承）。
"""

from api_client import ApiClient, MessageStopEvent, TextDeltaEvent, ToolUseEvent
from models import Session, TextContentBlock, ToolContentBlock, ToolResultContentBlock
from permissions import PermissionMode, PermissionPolicy
from runtime import (
    ConversationRuntime,
    TokenUsage,
    ToolError,
    UsageTracker,
    build_assistant_message,
    merge_hook_feedback,
)


# ============================================================
# 替身
# ============================================================

class ScriptedApiClient(ApiClient):
    """按剧本顺序返回事件流；记录每次调用时看到的消息列表。"""

    def __init__(self, script: list[list]):
        self.script = [list(events) for events in script]
        self.calls: list[list] = []
        self.thinking_level = "medium"   # runtime 构建时读取（会话级等级初值）

    def stream(self, system_prompt, messages, thinking_level=None):
        self.calls.append(list(messages))
        return self.script.pop(0)


class EchoExecutor:
    """总是成功返回 reply；fail=True 时抛 ToolError，并暴露自己是否被调用过。"""

    def __init__(self, reply: str = "done", fail: bool = False):
        self.reply = reply
        self.fail = fail
        self.called = False

    def execute(self, tool_name: str, input: str, tool_use_id=None) -> str:
        self.called = True
        if self.fail:
            raise ToolError("boom")
        return self.reply


class RecordingExecutor:
    """并发安全地记录每次执行入参的执行器（并行调度路径用）。"""

    def __init__(self, reply: str = "done"):
        import threading
        self.reply = reply
        self.inputs: list[str] = []
        self.ids: list[str] = []
        self._lock = threading.Lock()

    def execute(self, tool_name: str, input: str, tool_use_id=None) -> str:
        with self._lock:
            self.inputs.append(input)
            self.ids.append(tool_use_id)
        return self.reply


def make_runtime(api_client, executor=None, policy=None) -> ConversationRuntime:
    return ConversationRuntime(
        session=Session(),
        api_client=api_client,
        tool_executor=executor or EchoExecutor(),
        permission_policy=policy or PermissionPolicy(PermissionMode.ALLOW),
        system_prompt=["system prompt"],
    )


# ============================================================
# merge_hook_feedback — 三种情况，钉死输出格式
# 格式是"给模型看的 UX"：output 原文转发、空行分界、标签标明来源。
# 注意：标签与消息同行是当初拍板的选择，测试钉的就是这个选择。
# ============================================================

def test_merge_hook_feedback_无消息时原样返回():
    assert merge_hook_feedback([], "raw output", denied=True) == "raw output"


def test_merge_hook_feedback_有输出有反馈():
    result = merge_hook_feedback(["note a", "note b"], "file content", denied=False)
    assert result == "file content\n\nHook feedback: note a\nnote b"


def test_merge_hook_feedback_输出为空白时不拼空段():
    result = merge_hook_feedback(["note a"], "   ", denied=False)
    assert result == "Hook feedback: note a"


def test_merge_hook_feedback_denied_标签变化():
    result = merge_hook_feedback(["nope"], "out", denied=True)
    assert result == "out\n\nHook feedback (denied): nope"


# ============================================================
# build_assistant_message — flush 缓冲 + 两个必须报错的残局
# ============================================================

def test_build_纯文本流合并为一个块():
    events = [TextDeltaEvent(text="你"), TextDeltaEvent(text="好"), MessageStopEvent()]
    message, usage = build_assistant_message(events)
    assert usage is None  # 已知断线：api_client 尚不发 usage 事件
    assert len(message.content) == 1
    assert message.content[0] == TextContentBlock(text="你好")


def test_build_文本块在tool_use处flush且结尾再flush():
    events = [
        TextDeltaEvent(text="def main:"),
        ToolUseEvent(id="t1", name="bash", input="ls"),
        TextDeltaEvent(text="done"),
        MessageStopEvent(),
    ]
    message, _ = build_assistant_message(events)
    assert [b.type for b in message.content] == ["text", "tool_use", "text"]
    assert message.content[0] == TextContentBlock(text="def main:")
    assert message.content[1] == ToolContentBlock(id="t1", name="bash", input="ls")
    assert message.content[2] == TextContentBlock(text="done")


def test_build_没有MessageStop必须报错():
    import pytest
    with pytest.raises(RuntimeError):
        build_assistant_message([TextDeltaEvent(text="无终点的流")])


def test_build_只有MessageStop没有内容必须报错():
    import pytest
    with pytest.raises(RuntimeError):
        build_assistant_message([MessageStopEvent()])


# ============================================================
# UsageTracker — record 是累加不是替换
# ============================================================

def test_tracker_跨次累加():
    tracker = UsageTracker()
    tracker.record(TokenUsage(input_tokens=100, output_tokens=10))
    tracker.record(TokenUsage(input_tokens=50, output_tokens=5))
    cumulative = tracker.cumulative_usage()
    assert cumulative.input_tokens == 150
    assert cumulative.output_tokens == 15
    assert tracker.turns() == 2
    assert tracker.current_turn_usage().input_tokens == 50  # latest 只留最近一次


def test_tracker_初始全零():
    tracker = UsageTracker()
    assert tracker.cumulative_usage() == TokenUsage()
    assert tracker.turns() == 0


# ============================================================
# run_turn — 循环、协议顺序、三层防线
# ============================================================

def test_run_turn_纯文本一轮即停():
    fake = ScriptedApiClient([[TextDeltaEvent(text="你好"), MessageStopEvent()]])
    rt = make_runtime(fake)

    summary = rt.run_turn("hi")

    roles = [m.role for m in rt.session().messages]
    assert roles == ["user", "assistant"]
    assert summary.iterations == 1
    assert summary.tool_results == []
    assert summary.usage == TokenUsage()  # 断线显形：无 usage 数据，收据如实为零
    assert summary.auto_compacted is False
    # 第一圈请求里必须已经带着用户的话
    assert fake.calls[0][0].role == "user"


def test_run_turn_工具调用后世界回答再入历史():
    fake = ScriptedApiClient([
        [ToolUseEvent(id="t1", name="bash", input="ls"), MessageStopEvent()],
        [TextDeltaEvent(text="done"), MessageStopEvent()],
    ])
    rt = make_runtime(fake, executor=EchoExecutor(reply="file1\nfile2"))

    summary = rt.run_turn("list files")

    # 协议顺序：user → assistant(tool_use) → tool(result) → assistant(回答)
    roles = [m.role for m in rt.session().messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert summary.iterations == 2
    result_block = summary.tool_results[0].content[0]
    assert isinstance(result_block, ToolResultContentBlock)
    assert result_block.id == "t1"
    assert result_block.output == "file1\nfile2"
    assert not result_block.is_error
    # 第二圈请求里，世界的回答必须已经在历史中（3 条：user、assistant、tool）
    assert len(fake.calls[1]) == 3


def test_run_turn_权限拒绝则工具不执行():
    fake = ScriptedApiClient([
        [ToolUseEvent(id="t1", name="bash", input="rm"), MessageStopEvent()],
        [TextDeltaEvent(text="ok"), MessageStopEvent()],
    ])
    executor = EchoExecutor()
    policy = (
        PermissionPolicy(PermissionMode.PLAN)
        .with_tool_requirement("bash", PermissionMode.DANGER_FULL_ACCESS)
    )
    rt = make_runtime(fake, executor=executor, policy=policy)

    summary = rt.run_turn("rm -rf")  # 无 prompter：无人可问 → fail-closed

    result_block = summary.tool_results[0].content[0]
    assert result_block.is_error
    assert "permission" in result_block.output  # 拒绝理由回流给大脑
    assert not executor.called  # 第一层防线：工具根本没碰


def test_run_turn_工具抛错转为is_error消息():
    fake = ScriptedApiClient([
        [ToolUseEvent(id="t1", name="bash", input="boom"), MessageStopEvent()],
        [TextDeltaEvent(text="sorry"), MessageStopEvent()],
    ])
    rt = make_runtime(fake, executor=EchoExecutor(fail=True))

    summary = rt.run_turn("do it")

    result_block = summary.tool_results[0].content[0]
    assert result_block.is_error
    assert result_block.output == "boom"  # 异常翻译成文字，而不是炸穿循环


def test_run_turn_多工具并行执行且按原序回填():
    # 同一条 assistant 消息里的多个 tool_use 相互独立: 执行可并行,
    # 但回填顺序必须与消息中 tool_use 的顺序一致（不产生乱序历史）
    fake = ScriptedApiClient([
        [
            ToolUseEvent(id="t1", name="bash", input="cmd1"),
            ToolUseEvent(id="t2", name="bash", input="cmd2"),
            MessageStopEvent(),
        ],
        [TextDeltaEvent(text="done"), MessageStopEvent()],
    ])
    executor = RecordingExecutor()
    rt = make_runtime(fake, executor=executor)

    summary = rt.run_turn("multi")

    assert sorted(executor.inputs) == ["cmd1", "cmd2"]  # 两个都被执行
    assert sorted(executor.ids) == ["t1", "t2"]  # tool_use_id 直传执行器（事件配对靠它）
    assert [m.content[0].id for m in summary.tool_results] == ["t1", "t2"]
    roles = [m.role for m in rt.session().messages]
    assert roles == ["user", "assistant", "tool", "tool", "assistant"]


def test_run_turn_并行工具可同时进入执行():
    # 回归: 并行任务必须各自 copy_context——共享同一份 contextvars 快照时,
    # 并发 ctx.run 会炸 "cannot enter context: already entered"（Web 端整个
    # turn 报错）。两个工具用栅栏对齐, 只有真能同时进入执行才算通过。
    import threading

    class BarrierExecutor:
        def __init__(self):
            self.barrier = threading.Barrier(2, timeout=5)

        def execute(self, tool_name: str, input: str, tool_use_id=None) -> str:
            self.barrier.wait()   # 第二个工具进不来时这里超时炸穿
            return "ok"

    fake = ScriptedApiClient([
        [
            ToolUseEvent(id="t1", name="bash", input="cmd1"),
            ToolUseEvent(id="t2", name="bash", input="cmd2"),
            MessageStopEvent(),
        ],
        [TextDeltaEvent(text="done"), MessageStopEvent()],
    ])
    rt = make_runtime(fake, executor=BarrierExecutor())

    summary = rt.run_turn("multi")

    assert [m.content[0].output for m in summary.tool_results] == ["ok", "ok"]
