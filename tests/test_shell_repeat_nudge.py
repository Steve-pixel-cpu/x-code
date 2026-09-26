"""变异感知的 shell 重复提醒（无改动连击）的验收测试。

只读护栏的逻辑是"重跑结果必然一致"→ 可拒绝; 变异命令（跑测试等）重跑
本身合法。这里钉住更窄的反模式: 同一条命令原样重发且中间没有任何写动作
（输出大概率一致, 纯烧轮次）→ 第 2 次温和提醒、第 3 次强提醒、永不拒绝。

运行: uv run pytest tests/test_shell_repeat_nudge.py -v
"""

from test_repeat_guard import (RecordingExecutor,  # noqa: E402
                               _run_tools, make_runtime)

PYTEST_CMD = ("bash", '{"command": "uv run --with pytest pytest -q"}')
EDIT_CMD = ("edit_file",
            '{"path": "durations.py", "old_string": "a", "new_string": "b"}')


def test_首次执行不提醒():
    rt = make_runtime(RecordingExecutor())
    (msg,) = _run_tools(rt, [PYTEST_CMD])
    assert "System note" not in msg.content[0].output


def test_无改动重跑_第二次温和提醒():
    rt = make_runtime(RecordingExecutor())
    outs = _run_tools(rt, [PYTEST_CMD, PYTEST_CMD])
    assert "almost certainly identical" in outs[1].content[0].output
    assert all(not o.content[0].is_error for o in outs)   # 只提醒, 照常执行


def test_连续三次_强提醒_永不拒绝():
    rt = make_runtime(RecordingExecutor())
    outs = _run_tools(rt, [PYTEST_CMD] * 3)
    assert "3+ times" in outs[2].content[0].output
    assert all(not o.content[0].is_error for o in outs)   # 拒绝权留给只读护栏


def test_有写入介入_连击清零():
    """跑测试 → 改代码 → 再跑测试: 重跑合法, 不提醒。"""
    rt = make_runtime(RecordingExecutor())
    outs = _run_tools(rt, [PYTEST_CMD, EDIT_CMD, PYTEST_CMD])
    assert "System note" not in outs[2].content[0].output


def test_不同命令互不触发():
    rt = make_runtime(RecordingExecutor())
    other = ("bash", '{"command": "uv run --with pytest pytest test_x.py -q"}')
    outs = _run_tools(rt, [PYTEST_CMD, other])
    assert all("System note" not in o.content[0].output for o in outs)


def test_提醒后再写入_再重跑不提醒():
    """连击被写入打断后重新计数, 不是永久烙印。"""
    rt = make_runtime(RecordingExecutor())
    outs = _run_tools(rt, [PYTEST_CMD, PYTEST_CMD, EDIT_CMD, PYTEST_CMD])
    assert "almost certainly identical" in outs[1].content[0].output
    assert "System note" not in outs[3].content[0].output


def test_压缩激活清零连击():
    """旧输出可能已被归档出视图: 无改动重跑重见输出合法。"""
    rt = make_runtime(RecordingExecutor())
    _run_tools(rt, [PYTEST_CMD])
    with rt._guard_lock:
        rt._shell_streaks.clear()          # _maybe_auto_compact 同款清理
    (msg,) = _run_tools(rt, [PYTEST_CMD])
    assert "System note" not in msg.content[0].output
