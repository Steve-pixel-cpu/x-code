"""重复只读调用护栏的规格钉子。

背景: 大文件整读被截断掐掉中段后, 模型会把同一个调用原样重发几十次
（实测一次会话同一 read_file 重复 24 次）。提示词拦不住, 执行层兜:
read_file/grep/glob 期间无写入时结果必然相同——第 2 次警告、第 3 次起
拒绝; 任何写入/bash 重置计数; 压缩激活清零。

运行方式（在 x-code 目录下）:
    uv run pytest tests/test_repeat_guard.py -v
"""

from api_client import MessageStopEvent, TextDeltaEvent, ToolUseEvent
from models import Session
from permissions import PermissionMode, PermissionPolicy
from runtime import ConversationRuntime


class ScriptedClient:
    def __init__(self, script: list):
        self.script = list(script)
        self.calls = 0
        self.thinking_level = "medium"

    def stream(self, system_prompt, messages, thinking_level=None) -> list:
        events = self.script[self.calls] if self.calls < len(self.script) else self.script[-1]
        self.calls += 1
        return events


class RecordingExecutor:
    """记录每次执行; 可配置抛错。"""

    def __init__(self):
        self.executed: list[tuple[str, str]] = []

    def execute(self, tool_name, input, tool_use_id=None) -> str:
        self.executed.append((tool_name, input))
        return f"result-for-{tool_name}"


def make_runtime(executor) -> ConversationRuntime:
    return ConversationRuntime(
        session=Session(),
        api_client=ScriptedClient([]),      # 不实际 stream, 只测工具路径
        tool_executor=executor,
        permission_policy=PermissionPolicy(PermissionMode.ALLOW),
        system_prompt=["s"],
    )


def _run_tools(rt, calls: list[tuple[str, str]]):
    """直接走 _execute_tool 管线（绕过 stream）。返回每条的 output。"""
    from models import ToolContentBlock
    from hooks import HookResult

    outs = []
    for i, (name, inp) in enumerate(calls):
        block = ToolContentBlock(id=f"t{i}", name=name, input=inp)
        msg = rt._execute_tool(block, HookResult(messages=[], denied=False))
        outs.append(msg)
    return outs


READ = ("read_file", '{"path": "main.py"}')

# ------------------------------------------------------------
# 核心: 首次放行 / 第2次警告 / 第3次拒绝
# ------------------------------------------------------------

def test_首次放行_二次警告_三次拒绝():
    ex = RecordingExecutor()
    rt = make_runtime(ex)

    first, second, third, fourth = _run_tools(rt, [READ, READ, READ, READ])

    assert first.content[0].is_error is False
    assert first.content[0].output == "result-for-read_file"     # 首次原样
    assert second.content[0].is_error is False
    assert "2nd time" in second.content[0].output                # 警告附加
    assert third.content[0].is_error is True                                # 第3次拒绝
    assert "REFUSED" in third.content[0].output
    assert fourth.content[0].is_error is True
    # 拒绝不再触达执行器: 执行器只见到前两次
    assert len(ex.executed) == 2


def test_不同参数互不干扰():
    ex = RecordingExecutor()
    rt = make_runtime(ex)
    calls = [("read_file", '{"path": "main.py"}'),
             ("read_file", '{"path": "main.py", "offset": 300}'),
             ("read_file", '{"path": "main.py"}')]
    outs = _run_tools(rt, calls)
    assert all(o.content[0].is_error is False for o in outs)
    assert "2nd time" in outs[2].content[0].output               # 同参才计数


def test_grep_glob同样受护栏约束():
    ex = RecordingExecutor()
    rt = make_runtime(ex)
    grep = ("grep", '{"pattern": "foo"}')
    outs = _run_tools(rt, [grep, grep, grep])
    assert outs[1].content[0].is_error is False and "2nd time" in outs[1].content[0].output
    assert outs[2].content[0].is_error is True and "REFUSED" in outs[2].content[0].output


# ------------------------------------------------------------
# 写入重置: 文件真的变了, 重读合法
# ------------------------------------------------------------

def test_写入后同一读取重新放行():
    ex = RecordingExecutor()
    rt = make_runtime(ex)
    write = ("write_file", '{"path": "main.py", "content": "new"}')

    _run_tools(rt, [READ, READ])                       # 已到警告线
    _run_tools(rt, [write])                            # 写入推进变异序号
    outs = _run_tools(rt, [READ])

    assert outs[0].content[0].is_error is False                   # 视作首次, 无警告
    assert "2nd time" not in outs[0].content[0].output


def test_bash也推进变异序号_可轮询():
    """bash 不受护栏约束且推进变异序号: 轮询类合法重读不被误伤。"""
    ex = RecordingExecutor()
    rt = make_runtime(ex)
    bash = ("bash", '{"command": "cat main.py"}')

    _run_tools(rt, [READ, READ, bash])
    outs = _run_tools(rt, [READ])

    assert outs[0].content[0].is_error is False
    assert "2nd time" not in outs[0].content[0].output


def test_bash自身永不拒绝():
    ex = RecordingExecutor()
    rt = make_runtime(ex)
    bash = ("bash", '{"command": "sleep 1"}')
    outs = _run_tools(rt, [bash] * 5)
    assert all(o.content[0].is_error is False for o in outs)      # 轮询合法


# ------------------------------------------------------------
# 压缩激活清零: 旧只读结果可能已被归档, 重读重新合法
# ------------------------------------------------------------

def test_压缩激活清空护栏计数():
    ex = RecordingExecutor()
    rt = make_runtime(ex)
    _run_tools(rt, [READ, READ])                       # 已到警告线
    rt._compact_active = True
    with rt._guard_lock:
        rt._read_calls.clear()                         # 与 _maybe_auto_compact 同步的语义
    outs = _run_tools(rt, [READ])
    assert outs[0].content[0].is_error is False
    assert "2nd time" not in outs[0].content[0].output


# ------------------------------------------------------------
# 端到端: run_turn 内护栏对并行批次同样生效
# ------------------------------------------------------------

def test_并行批次内同参工具也计数():
    from api_client import MessageStopEvent

    ex = RecordingExecutor()
    rt = make_runtime(ex)
    rt._api_client = ScriptedClient([
        [ToolUseEvent(id="t1", name="read_file", input='{"path": "a.py"}'),
         ToolUseEvent(id="t2", name="read_file", input='{"path": "a.py"}'),
         MessageStopEvent()],
        [TextDeltaEvent(text="done"), MessageStopEvent()],
    ])

    rt.run_turn("hi")

    results = [m for m in rt.session().messages if m.role == "tool"]
    assert len(results) == 2
    assert results[0].content[0].output == "result-for-read_file"     # 首次
    assert "2nd time" in results[1].content[0].output                 # 同批次第二次: 警告
