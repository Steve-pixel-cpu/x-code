"""evals 框架自身的验收测试 (不烧 token: judge 用桩, 端到端用桩 Agent)。

运行方式（在 x-code 目录下）:
    uv run pytest tests/test_evals_harness.py -v
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

import harness  # noqa: E402
import run_evals  # noqa: E402
from harness import (Check, diff_baseline, discover_tasks, judge_verdict,  # noqa: E402
                     load_checks, parse_agent_output, prepare_workspace,
                     render_report, usage_total)

STUB_AGENT = ROOT / "tests" / "evals_stub_agent.py"


# ------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------

def _make_task(tmp_path: Path, name: str = "demo", *,
               checks: str = "", judge: bool = False) -> Path:
    """搭一个最小合法夹具。checks 传 checks.py 的 evaluate 源码。"""
    d = tmp_path / "tasks" / name
    (d / "project").mkdir(parents=True)
    (d / "project" / "hello.txt").write_text("hi", encoding="utf-8")
    (d / "task.txt").write_text("测试任务", encoding="utf-8")
    if checks:
        (d / "checks.py").write_text(checks, encoding="utf-8")
    if judge:
        (d / "judge.txt").write_text("标准", encoding="utf-8")
    return d


def test_discover_tasks_and_score_method(tmp_path):
    _make_task(tmp_path, "with-checks",
               checks="def evaluate(ws):\n    return []\n")
    _make_task(tmp_path, "judge-only", judge=True)
    tasks = discover_tasks(tmp_path / "tasks")
    by_name = {t.name: t for t in tasks}
    assert by_name["with-checks"].score_method == "checks"
    assert by_name["judge-only"].score_method == "judge"
    assert [t.name for t in tasks] == ["judge-only", "with-checks"]  # 排序稳定


def test_discover_rejects_unscorable_fixture(tmp_path):
    _make_task(tmp_path, "broken")   # 无 checks 也无 judge
    with pytest.raises(ValueError, match="broken"):
        discover_tasks(tmp_path / "tasks")


def test_prepare_workspace_copies_isolated(tmp_path):
    task_dir = _make_task(tmp_path, "demo",
                          checks="def evaluate(ws):\n    return []\n")
    task = discover_tasks(task_dir.parent)[0]
    run_dir = tmp_path / "run"
    ws = prepare_workspace(task, run_dir)
    assert (ws / "hello.txt").read_text(encoding="utf-8") == "hi"
    # 工作区改动不回写夹具
    (ws / "hello.txt").write_text("changed", encoding="utf-8")
    assert "hi" == (task.dir / "project" / "hello.txt").read_text(encoding="utf-8")


def test_load_checks_runs_evaluate(tmp_path):
    _make_task(tmp_path, "demo", checks=(
        "from pathlib import Path\n"
        "from harness import Check\n"
        "def evaluate(ws):\n"
        "    return [Check('样例', (ws / 'hello.txt').exists())]\n"))
    task = discover_tasks(tmp_path / "tasks")[0]
    ws = prepare_workspace(task, tmp_path / "run")
    checks = load_checks(task.dir)(ws)
    assert len(checks) == 1 and checks[0].passed is True


def test_load_checks_rejects_missing_evaluate(tmp_path):
    _make_task(tmp_path, "demo", checks="x = 1\n")
    task = discover_tasks(tmp_path / "tasks")[0]
    with pytest.raises(ValueError, match="evaluate"):
        load_checks(task.dir)


# ------------------------------------------------------------
# Agent 输出解析
# ------------------------------------------------------------

def test_parse_agent_output_variants():
    good = json.dumps({"subtype": "completed", "result": "ok"})
    assert parse_agent_output(good)["subtype"] == "completed"
    assert parse_agent_output(good + "\n")["result"] == "ok"          # 尾换行容忍
    assert parse_agent_output("") is None                             # 崩溃无输出
    noisy = "警告行\n" + good                                          # 混入杂行容错
    assert parse_agent_output(noisy)["subtype"] == "completed"
    assert parse_agent_output("不是 JSON") is None


# ------------------------------------------------------------
# LLM 判分 (桩 client, 不打真网)
# ------------------------------------------------------------

class _StubLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def generate_text(self, system, user, max_tokens=512):
        self.calls.append((system, user))
        return self.reply


def test_judge_verdict_parses_fenced_json():
    llm = _StubLLM('判断如下:\n```json\n{"pass": true, "reason": "两点都指出"}\n```')
    verdict = judge_verdict("标准", "任务", "Agent 回复", llm)
    assert verdict == {"pass": True, "reason": "两点都指出"}
    # 判分材料齐: 任务原文 + 标准 + Agent 回复都在 user prompt 里
    system, user = llm.calls[0]
    assert "任务" in user and "标准" in user and "Agent 回复" in user


def test_judge_verdict_rejects_non_json():
    with pytest.raises(harness.JudgeError, match="没有 JSON"):
        judge_verdict("标准", "任务", "回复", _StubLLM("我觉得挺好的"))


def test_judge_verdict_rejects_missing_pass_field():
    with pytest.raises(harness.JudgeError, match="pass"):
        judge_verdict("标准", "任务", "回复", _StubLLM('{"score": 9}'))


# ------------------------------------------------------------
# 结果聚合
# ------------------------------------------------------------

def test_usage_total_ignores_missing_fields():
    assert usage_total({"input_tokens": 10, "output_tokens": 20}) == 30
    assert usage_total(None) == 0


def _record(name, passed):
    return {"task": name, "passed": passed, "checks": [], "judge": None,
            "duration_s": 1.0, "total_tokens": 0, "iterations": 1,
            "subtype": "completed", "result_snippet": "", "error": ""}


def test_diff_baseline_states():
    current = {"tasks": [_record("a", True), _record("b", False), _record("c", True)]}
    baseline = {"tasks": [_record("a", True), _record("b", True), _record("d", False)]}
    diff = diff_baseline(current, baseline)
    assert diff == {"a": "unchanged", "b": "regressed", "c": "new", "d": "gone"}
    assert diff_baseline(current, None) == {}


def test_render_report_marks_failures():
    result = {"run_id": "R", "baseline_id": None, "tasks": [
        {**_record("good", True), "checks": [{"name": "pytest", "passed": True, "detail": "1 passed"}]},
        {**_record("bad", False), "checks": [
            {"name": "pytest", "passed": False, "detail": "1 failed"}],
         "error": "checks 未全过", "result_snippet": "我改了测试"},
    ]}
    report = render_report(result, {"bad": "regressed"})
    assert "通过 1/2" in report
    assert "| bad | ❌" in report and "regressed" in report
    assert "## ✗ bad" in report and "我改了测试" in report


# ------------------------------------------------------------
# 端到端 (桩 Agent, 不打真网): 覆盖 发现→复制→subprocess→解析→checks→报告
# ------------------------------------------------------------

CHECKS_OK_TXT = (
    "from pathlib import Path\n"
    "from harness import Check\n"
    "def evaluate(ws):\n"
    "    p = ws / 'ok.txt'\n"
    "    ok = p.exists() and p.read_text(encoding='utf-8') == 'done'\n"
    "    return [Check('ok.txt = done', ok)]\n")


def _run_e2e(tmp_path, monkeypatch, task_prompt: str, *, save_baseline=False):
    """搭单任务夹具 + 桩 Agent, 跑完整 run_evals.main。返回 (rc, 结果文件)。"""
    _make_task(tmp_path, "stub-task", checks=CHECKS_OK_TXT)
    (tmp_path / "tasks" / "stub-task" / "task.txt").write_text(task_prompt,
                                                               encoding="utf-8")
    # 隔离运行产物目录: results / report / baseline / .runs 全进 tmp
    monkeypatch.setattr(run_evals, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(run_evals, "REPORT_PATH", tmp_path / "report.md")
    monkeypatch.setattr(run_evals, "BASELINE_PATH", tmp_path / "baseline.json")
    monkeypatch.setattr(harness, "EVALS_DIR", tmp_path)
    argv = ["--tasks-dir", str(tmp_path / "tasks"),
            "--agent-cmd", f"{Path(sys.executable).as_posix()} {STUB_AGENT.as_posix()}",
            "--no-baseline-diff"]
    if save_baseline:
        argv.append("--save-baseline")
    rc = run_evals.main(argv)
    results = sorted((tmp_path / "results").glob("*.json"))
    return rc, json.loads(results[-1].read_text(encoding="utf-8"))


def test_e2e_stub_agent_pass(tmp_path, monkeypatch):
    rc, result = _run_e2e(tmp_path, monkeypatch, "写一个 ok.txt")
    assert rc == 0
    rec = result["tasks"][0]
    assert rec["passed"] is True
    assert rec["exit_code"] == 0
    assert rec["subtype"] == "completed"
    assert rec["total_tokens"] == 120
    assert rec["checks"][0]["passed"] is True   # 桩真的在工作区写了 ok.txt


def test_e2e_stub_agent_fail(tmp_path, monkeypatch):
    rc, result = _run_e2e(tmp_path, monkeypatch, "FAIL 这次任务")
    assert rc == 1
    rec = result["tasks"][0]
    assert rec["passed"] is False
    assert rec["checks"][0]["passed"] is False  # ok.txt 没写出来


def test_e2e_save_baseline_and_diff(tmp_path, monkeypatch):
    _run_e2e(tmp_path, monkeypatch, "写一个 ok.txt", save_baseline=True)
    baseline = json.loads((tmp_path / "baseline.json").read_text(encoding="utf-8"))
    assert baseline["tasks"][0]["passed"] is True


def test_e2e_judge_task_scores_and_persists(tmp_path, monkeypatch):
    """judge 任务走 finalize 的判分分支（此前无覆盖——真实首跑在此 NameError）。"""
    _make_task(tmp_path, "judge-task", checks=CHECKS_OK_TXT, judge=True)
    monkeypatch.setattr(run_evals, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(run_evals, "REPORT_PATH", tmp_path / "report.md")
    monkeypatch.setattr(run_evals, "BASELINE_PATH", tmp_path / "baseline.json")
    monkeypatch.setattr(harness, "EVALS_DIR", tmp_path)

    class StubLLM:
        def generate_text(self, system, user, max_tokens=512):
            return '{"pass": true, "reason": "完成"}'

    monkeypatch.setattr(run_evals, "make_judge_client", lambda: StubLLM())
    rc = run_evals.main([
        "--tasks-dir", str(tmp_path / "tasks"),
        "--agent-cmd", f"{Path(sys.executable).as_posix()} {STUB_AGENT.as_posix()}",
        "--no-baseline-diff",
    ])

    assert rc == 0
    result = json.loads(next((tmp_path / "results").glob("*.json")
                             ).read_text(encoding="utf-8"))
    rec = result["tasks"][0]
    assert rec["score_method"] == "checks+judge"
    assert rec["judge"]["pass"] is True
    assert rec["passed"] is True
