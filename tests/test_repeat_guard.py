"""重复只读调用护栏的规格钉子。

背景: 大文件整读被截断掐掉中段后, 模型会把同一个调用原样重发几十次
（实测一次会话同一 read_file 重复 24 次）。提示词拦不住, 执行层兜:
read_file/grep/glob 期间无写入时结果必然相同——第 2 次警告、第 3 次起
拒绝; 可能改状态的命令重置计数; 压缩激活清零。

bash/powershell 先做只读判定（shell_command_is_read_only）: ls/find/
grep/git log 这类确定性只读探查不清零计数——旧规则"任何 bash 都清零"
让 bash 密集的排查会话里护栏形同虚设（实测同一文件重读 4 次无告警）。
判定保守: 白名单 + 危险构造（命令替换/重定向写/未知名令）一票否决。

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


def test_只读bash不清零护栏_同参重读照旧拦截():
    """实测教训: bash 密集的排查会话里, 旧规则每条 bash 都清零计数,
    同一文件重读 4 次护栏一声不吭。cat/ls/git log 类只读命令不再清零。"""
    ex = RecordingExecutor()
    rt = make_runtime(ex)
    cat = ("bash", '{"command": "cat main.py"}')

    _run_tools(rt, [READ, READ, cat])
    outs = _run_tools(rt, [READ])

    # cat 没有改文件 → 计数保留, 第 3 次同参重读被拒
    assert outs[0].content[0].is_error is True
    assert "REFUSED" in outs[0].content[0].output


def test_变异bash清零护栏_轮询类合法重读不被误伤():
    ex = RecordingExecutor()
    rt = make_runtime(ex)
    mutate = ("bash", '{"command": "echo x > main.py"}')

    _run_tools(rt, [READ, READ, mutate])
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
# shell 只读判定 — 护栏变异序号的推进闸门（钉真实排查会话里的命令形状）
# ------------------------------------------------------------

import json as _json

import pytest

from runtime import shell_command_is_read_only


def _ro(name: str, cmd: str) -> bool:
    return shell_command_is_read_only(name, _json.dumps({"command": cmd}))


@pytest.mark.parametrize("cmd", [
    "pwd && ls -la",                                   # 真实 trace 首条
    "ls -la /d/workplace/KB 2>/dev/null",              # /dev/null 弃置无害
    'grep -rn "切部门" --include="*.cs" -l | grep -v "/obj/"',   # 引号内 | 不算分隔
    "git log --oneline -15 -- src/hooks/use.tsx",
    "git show b92943f --stat | head -20 && git log -1 --format=%h",
    "cd /d/workplace && git status",
    'find . -name "SysUser.cs" -not -path "*/obj/*"',
    "strings Zny.Web.Shared.dll | grep -i systemmanager",
    "cat main.py",
    'SQLCMD="C:/tools/sqlcmd.exe" ls',                 # 环境变量前缀 + 只读命令
    "ls > /dev/null",
])
def test_确定性只读命令不清零(cmd):
    assert _ro("bash", cmd) is True


@pytest.mark.parametrize("cmd", [
    "rm -rf build",
    "echo hi > out.txt",                               # 重定向写文件
    "git commit -m x",
    "git checkout main",                               # 动工作树
    "git stash pop",
    'python -c "print(1)"',                            # 解释器一票否决
    "for f in $(ls *.dll); do echo $f; done",          # 命令替换 + for
    "curl https://example.com",
    "touch newfile",
    'find . -name "*.tmp" -delete',                    # find 的变异动作
    "sort -o out.txt in.txt",
    "sed -n '1,10p' main.py",                          # 保守排除: sed 有 w/e 命令面
    "sudo cat /etc/shadow",
])
def test_可能变异的命令照旧清零(cmd):
    assert _ro("bash", cmd) is False


def test_解析失败与空命令保守视为变异():
    assert shell_command_is_read_only("bash", "{not json") is False
    assert shell_command_is_read_only("bash", '{"command": ""}') is False
    assert shell_command_is_read_only("bash", "{}") is False


def test_powershell_同规则():
    assert _ro("powershell", "Get-ChildItem -Recurse | Select-String foo") is True
    assert _ro("powershell", "Remove-Item x") is False
    assert _ro("powershell", "Get-Content a.txt > b.txt") is False


# ------------------------------------------------------------
# 结论检查点 — 本回合调用时钟跨过阈值档位时, 对靶提醒（引用原始问题）
# 附在最新工具结果上。按调用-检查点的节奏模拟 run_turn（检查点在每批
# 工具结果回填后调用）
# ------------------------------------------------------------

from hooks import HookResult
from models import ToolContentBlock

QUESTION = "马乐不是企业知识库管理员，有切部门的功能。排查一下原因"


def _step(rt, msgs: list, name: str, inp: str, i: int, q: str = QUESTION):
    """执行一次工具并回填检查点（= run_turn 每迭代的收尾动作）。"""
    block = ToolContentBlock(id=f"t{i}", name=name, input=inp)
    msg = rt._execute_tool(block, HookResult(messages=[], denied=False))
    msgs.append(msg)
    rt._maybe_conclusion_checkpoint(msgs, q)
    return msg


def test_调用时钟跨过阈值触发检查点_附原始问题引述():
    from runtime import CONCLUSION_CHECKPOINT_EVERY as EVERY

    ex = RecordingExecutor()
    rt = make_runtime(ex)
    msgs: list = []

    for i in range(EVERY):
        _step(rt, msgs, "read_file", f'{{"path": "f{i}.py"}}', i)

    # 检查点重建消息替换列表槽位, 断言对准 msgs（会话视角）
    assert "Pacing checkpoint" not in msgs[EVERY - 2].content[0].output
    assert "Pacing checkpoint" in msgs[-1].content[0].output
    assert msgs[-1].content[0].is_error is False      # 提醒不是错误
    # 提醒引用原始问题, 逼模型对靶自查
    assert QUESTION in msgs[-1].content[0].output


def test_检查点按倍数档位重复触发():
    from runtime import CONCLUSION_CHECKPOINT_EVERY as EVERY

    ex = RecordingExecutor()
    rt = make_runtime(ex)
    msgs: list = []

    for i in range(EVERY * 2 + 1):
        _step(rt, msgs, "read_file", f'{{"path": "f{i}.py"}}', i)

    fired = ["Pacing checkpoint" in m.content[0].output for m in msgs]
    assert fired[EVERY - 1] is True                   # 第 16 次: 跨过 16 档, 触发
    assert not any(fired[EVERY:EVERY * 2 - 1])        # 档位之间保持安静
    assert fired[EVERY * 2 - 1] is True               # 第 32 次: 跨过 32 档, 再触发
    assert fired[-1] is False                         # 触发点之后的调用是干净的


def test_并行批次跨过档位也触发():
    """一批 5 个调用把时钟从 14 推到 19: 19 % 16 != 0, 按"取模"判定会
    永远错过——按"跨档"判定必须命中。"""
    from runtime import CONCLUSION_CHECKPOINT_EVERY as EVERY

    ex = RecordingExecutor()
    rt = make_runtime(ex)
    msgs: list = []

    for i in range(EVERY - 2):
        _step(rt, msgs, "read_file", f'{{"path": "f{i}.py"}}', i)
    assert not any("Pacing checkpoint" in m.content[0].output for m in msgs)

    for i in range(5):                                # 时钟 14 → 19, 跨过 16
        _step(rt, msgs, "read_file", f'{{"path": "g{i}.py"}}', EVERY + i)

    # 提醒落在时钟到 16 的那条结果上（0 基槽位 15）, 其余干净
    assert "Pacing checkpoint" in msgs[EVERY - 1].content[0].output
    assert not any("Pacing checkpoint" in m.content[0].output
                   for i, m in enumerate(msgs) if i != EVERY - 1)


def test_sqlcmd_python螺旋不清零时钟_检查点照常触发():
    """v2 实测回归钉: 查库实锤后模型转入支线, 连跑 sqlcmd/python 反编译
    ——这些命令在变异判定里全是"可能改状态", 旧设计把检查点时钟挂在只读
    连击上被反复清零, 检查点在唯一需要它的螺旋里全程失明。时钟与变异判定
    解耦后, 混合螺旋照常计数、照常触发。"""
    from runtime import CONCLUSION_CHECKPOINT_EVERY as EVERY

    ex = RecordingExecutor()
    rt = make_runtime(ex)
    msgs: list = []

    spiral = [("read_file", '{"path": "a.cs"}')] * 6
    spiral += [("bash", '{"command": "sqlcmd -S db -Q \\"select 1\\""}'),
               ("bash", '{"command": "python - <<EOF\\nprint(1)\\nEOF"}')]
    spiral += [("grep", '{"pattern": "x"}')] * 3
    spiral += [("bash", '{"command": "strings a.dll | grep IsSystemManager"}')]
    spiral += [("read_file", '{"path": "b.cs"}')] * 4   # 共 16 个调用

    for i, (name, inp) in enumerate(spiral):
        _step(rt, msgs, name, inp, i)

    fired = ["Pacing checkpoint" in m.content[0].output for m in msgs]
    assert fired[-1] is True                          # 时钟 16: 螺旋里照常触发
    assert not any(fired[:-1])


def test_新回合时钟重置_检查点重新计数():
    from runtime import CONCLUSION_CHECKPOINT_EVERY as EVERY

    ex = RecordingExecutor()
    rt = make_runtime(ex)
    msgs: list = []

    for i in range(EVERY):
        _step(rt, msgs, "read_file", f'{{"path": "f{i}.py"}}', i)
    assert "Pacing checkpoint" in msgs[-1].content[0].output

    # 新用户回合: run_turn 顶部会清零时钟。这里手动模拟清零后的行为
    rt._turn_call_clock = 0
    rt._checkpoint_mark = 0
    msg = _step(rt, msgs, "read_file", '{"path": "next.py"}', 99)

    assert "Pacing checkpoint" not in msg.content[0].output   # 时钟 1: 不触发


def test_检查点不重复附加在同一条结果上():
    ex = RecordingExecutor()
    rt = make_runtime(ex)
    msgs: list = []

    for i in range(20):
        _step(rt, msgs, "read_file", f'{{"path": "f{i}.py"}}', i)
    rt._maybe_conclusion_checkpoint(msgs, QUESTION)   # 时钟未变: 重复调用不追加

    # 唯一提醒落在第 16 条（跨档点）; 之后到第 20 条都干净
    assert msgs[15].content[0].output.count("Pacing checkpoint") == 1
    assert not any("Pacing checkpoint" in m.content[0].output for m in msgs[16:])


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
