"""-p headless 模式的验收测试（REPL 路径由 test_main 覆盖）。

运行方式（在 x-code 目录下）:
    uv run pytest tests/test_headless.py -v

覆盖:
- run_headless 的输出契约（stdout 只出最终结果, 进度走 stderr）
- 退出码语义: 0=完成 1=错误 2=中断 3=预算收束 4=启动/用法错误
- 新消息落盘（含 user 消息, --resume 可接上）
- stdin 管道输入并入任务
- _parse_headless_args 的参数校验
- --permission-mode 覆盖的生效与 "allow" 红线（走真实 _assemble）
"""

import json
import sys
from types import SimpleNamespace

import pytest

import main as main_mod
from main import StartupError, _parse_headless_args, run_headless
from models import Message, Session, TextContentBlock
from permissions import PermissionMode
from runtime import TokenUsage, TurnSummary
from storage import SessionStore


# ------------------------------------------------------------
# 桩件: 替掉真实 runtime, 只保留 run_headless 消费的面
# ------------------------------------------------------------

class StubRuntime:
    """最小 runtime 面: session() / run_turn()。turn 行为由用例注入。"""

    def __init__(self, turn=None):
        self._session = Session()
        self._turn = turn if turn is not None else self._default_turn
        self.run_turn_calls = []   # [(user_input, prompter)]

    def session(self):
        return self._session

    def run_turn(self, user_input, prompter=None, attachments=None):
        self.run_turn_calls.append((user_input, prompter))
        return self._turn(self._session, user_input, prompter)

    @staticmethod
    def _default_turn(session, user_input, prompter):
        session.messages.append(Message.user_text(user_input))
        assistant = Message(role="assistant",
                            content=[TextContentBlock(text=f"收到: {user_input}")])
        session.messages.append(assistant)
        return TurnSummary(
            assistant_messages=[assistant],
            tool_results=[],
            iterations=1,
            usage=TokenUsage(input_tokens=100, output_tokens=20),
            auto_compacted=False,
        )


def _install_stub(monkeypatch, runtime: StubRuntime, calls: dict) -> None:
    """把 main._assemble 换成返回桩 runtime 的版本, 并记录装配旗标
    （run_headless 经模块全局查找 _assemble, monkeypatch 生效）。"""
    def fake_assemble(session_store, session_id, *, model_override=None,
                      permission_mode_override=None, progress_out=None,
                      emit_output=True):
        calls.update(model_override=model_override,
                     permission_mode_override=permission_mode_override,
                     progress_out=progress_out, emit_output=emit_output)
        return None, runtime, None

    monkeypatch.setattr(main_mod, "_assemble", fake_assemble)


# ------------------------------------------------------------
# 输出契约与退出码
# ------------------------------------------------------------

def test_text_mode_stdout_has_only_final_result(monkeypatch, tmp_path, capsys):
    calls = {}
    runtime = StubRuntime()
    _install_stub(monkeypatch, runtime, calls)

    code = run_headless(SessionStore(storage_dir=tmp_path),
                        "20260926-000001", "列出 TODO")

    assert code == 0
    assert capsys.readouterr().out == "收到: 列出 TODO\n"   # stdout 只有正文
    assert ("列出 TODO", None) in runtime.run_turn_calls    # prompter=None: 无人值守
    assert calls["emit_output"] is False                    # 流式回显整机关闭
    assert calls["progress_out"] is sys.stderr              # 进度走 stderr


def test_json_mode_payload_shape(monkeypatch, tmp_path, capsys):
    calls = {}
    _install_stub(monkeypatch, StubRuntime(), calls)
    store = SessionStore(storage_dir=tmp_path)

    code = run_headless(store, "20260926-000002", "任务", output_format="json")

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["type"] == "result"
    assert payload["subtype"] == "completed"
    assert payload["is_error"] is False
    assert payload["result"] == "收到: 任务"
    assert payload["session_id"] == "20260926-000002"
    assert payload["num_iterations"] == 1
    assert payload["auto_compacted"] is False
    assert payload["usage"]["input_tokens"] == 100
    assert payload["usage"]["output_tokens"] == 20


def test_budget_exhausted_maps_to_exit_3(monkeypatch, tmp_path, capsys):
    def turn(session, user_input, prompter):
        assistant = Message(role="assistant",
                            content=[TextContentBlock(text="写到一半")])
        return TurnSummary(
            assistant_messages=[assistant], tool_results=[],
            iterations=16, usage=TokenUsage(), auto_compacted=False,
            budget_exhausted=True)

    _install_stub(monkeypatch, StubRuntime(turn=turn), {})
    code = run_headless(SessionStore(storage_dir=tmp_path), "x", "任务",
                        output_format="json")

    payload = json.loads(capsys.readouterr().out)
    assert code == 3                                  # 结果可能不完整 → 非 0
    assert payload["subtype"] == "budget_exhausted"
    assert payload["is_error"] is True
    assert payload["result"] == "写到一半"            # 部分结果仍带出


def test_run_error_maps_to_exit_1(monkeypatch, tmp_path, capsys):
    def turn(session, user_input, prompter):
        raise RuntimeError("连接失败")

    _install_stub(monkeypatch, StubRuntime(turn=turn), {})
    code = run_headless(SessionStore(storage_dir=tmp_path), "x", "任务",
                        output_format="json")

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["subtype"] == "error"
    assert payload["is_error"] is True
    assert payload["result"] == ""


def test_interrupt_maps_to_exit_2_and_repairs(monkeypatch, tmp_path, capsys):
    def turn(session, user_input, prompter):
        raise KeyboardInterrupt()

    repaired = []
    monkeypatch.setattr(main_mod, "repair_interrupted_turn",
                        lambda session: repaired.append(session))
    _install_stub(monkeypatch, StubRuntime(turn=turn), {})

    code = run_headless(SessionStore(storage_dir=tmp_path), "x", "任务")

    assert code == 2
    assert len(repaired) == 1   # 悬空 tool_use 修补照常执行
    assert "已中断" in capsys.readouterr().err


def test_startup_error_maps_to_exit_4(monkeypatch, tmp_path, capsys):
    def boom(*args, **kwargs):
        raise StartupError("API_KEY not set!")

    monkeypatch.setattr(main_mod, "_assemble", boom)
    code = run_headless(SessionStore(storage_dir=tmp_path), "x", "任务")

    assert code == 4
    assert "API_KEY" in capsys.readouterr().err


# ------------------------------------------------------------
# 落盘与管道输入
# ------------------------------------------------------------

def test_messages_persisted_for_resume(monkeypatch, tmp_path):
    _install_stub(monkeypatch, StubRuntime(), {})
    store = SessionStore(storage_dir=tmp_path)

    code = run_headless(store, "20260926-000003", "任务")

    assert code == 0
    messages, last_uuid = store.load_session("20260926-000003")
    assert [m.role for m in messages] == ["user", "assistant"]   # user 消息也进链
    assert last_uuid is not None


class _FakeStdin:
    def __init__(self, data: str, tty: bool):
        self._data = data
        self._tty = tty

    def isatty(self):
        return self._tty

    def read(self):
        return self._data


def test_stdin_pipe_merged_into_task(monkeypatch, tmp_path):
    calls = {}
    runtime = StubRuntime()
    _install_stub(monkeypatch, runtime, calls)
    monkeypatch.setattr(sys, "stdin", _FakeStdin("- fix(a): 改坏了 x\n", tty=False))

    code = run_headless(SessionStore(storage_dir=tmp_path), "x", "审查这次改动")

    assert code == 0
    sent = runtime.run_turn_calls[0][0]
    assert sent.startswith("审查这次改动")           # 任务在前
    assert "- fix(a): 改坏了 x" in sent              # 管道内容随后并入


def test_stdin_tty_not_merged(monkeypatch, tmp_path):
    calls = {}
    runtime = StubRuntime()
    _install_stub(monkeypatch, runtime, calls)
    monkeypatch.setattr(sys, "stdin", _FakeStdin("", tty=True))

    run_headless(SessionStore(storage_dir=tmp_path), "x", "纯任务")

    assert runtime.run_turn_calls[0][0] == "纯任务"   # 交互终端: 不动任务文本


def test_stdin_blank_pipe_not_merged(monkeypatch, tmp_path):
    calls = {}
    runtime = StubRuntime()
    _install_stub(monkeypatch, runtime, calls)
    monkeypatch.setattr(sys, "stdin", _FakeStdin("\n \n", tty=False))

    run_headless(SessionStore(storage_dir=tmp_path), "x", "纯任务")

    assert runtime.run_turn_calls[0][0] == "纯任务"   # 空管道内容跳过


# ------------------------------------------------------------
# _parse_headless_args — 参数校验
# ------------------------------------------------------------

def test_parse_flags_anywhere_and_defaults():
    task, opts, err = _parse_headless_args(
        ["--output-format", "json", "审查", "--model", "m1"])
    assert err is None
    assert task == "审查"
    assert opts == {"output_format": "json", "model_override": "m1",
                    "permission_mode_override": None}


@pytest.mark.parametrize("rest, frag", [
    ([], "缺少任务文本"),
    (["--output-format"], "需要值"),
    (["--output-format", "xml", "任务"], "无效"),
    (["--model"], "需要模型名"),
    (["--permission-mode"], "需要值"),
    (["--unknown", "任务"], "未知选项"),
    (["任务", "多余"], "多余"),
])
def test_parse_errors(rest, frag):
    task, opts, err = _parse_headless_args(rest)
    assert task is None
    assert err is not None and frag in err


# ------------------------------------------------------------
# _assemble 的 --permission-mode 覆盖: 生效 + "allow" 红线
# （真实装配路径, 与 test_start_reconciles_orphans 同款隔离）
# ------------------------------------------------------------

def _isolate_assembly(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setattr(main_mod, "get_orchestrator", lambda: SimpleNamespace(
        reconcile_orphans=lambda: 0))


def test_permission_mode_override_lands_in_policy(monkeypatch, tmp_path):
    _isolate_assembly(monkeypatch)
    _, runtime, _ = main_mod._assemble(
        SessionStore(storage_dir=tmp_path), "20260926-000004",
        permission_mode_override="plan")
    assert runtime.permission_mode() == PermissionMode.PLAN


def test_permission_mode_override_rejects_allow(monkeypatch, tmp_path):
    """allow 只许在 REPL 里 /mode 临时开, 不许从 CLI 覆盖进入
    （与配置路径 resolve_permission_mode 同一条红线）。"""
    _isolate_assembly(monkeypatch)
    with pytest.raises(StartupError) as ei:
        main_mod._assemble(
            SessionStore(storage_dir=tmp_path), "20260926-000005",
            permission_mode_override="allow")
    assert "allow" in str(ei.value)


def test_permission_mode_override_rejects_unknown(monkeypatch, tmp_path):
    _isolate_assembly(monkeypatch)
    with pytest.raises(StartupError) as ei:
        main_mod._assemble(
            SessionStore(storage_dir=tmp_path), "20260926-000006",
            permission_mode_override="sudo")
    assert "sudo" in str(ei.value)
