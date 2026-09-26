# --- evals 批量运行器 ---
#
# 用法 (在 x-code 目录下):
#   uv run python evals/run_evals.py                     # 全量, 有基线则对比
#   uv run python evals/run_evals.py --only fix-failing-test
#   uv run python evals/run_evals.py --save-baseline     # 本次结果存为基线
#   uv run python evals/run_evals.py --list              # 只列任务不跑
#   uv run python evals/run_evals.py --keep-workspaces   # 留工作区排查
#   uv run python evals/run_evals.py --agent-cmd "..."   # 替换被测 Agent (测试管线用)
#
# 每个任务的完整流程: 夹具项目复制到全新工作区 → subprocess 起真实 Agent
# (main.py -p --output-format json, cwd=工作区) → 跑确定性 checks → (可选)
# LLM 判分 → 记录结果 → 写 results JSON + report.md → 与基线对比。
# 退出码: 全过 0, 有失败 1 (CI 可直接用作门槛)。

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness
from harness import (diff_baseline, discover_tasks, judge_verdict,
                     load_checks, make_judge_client, parse_agent_output,
                     prepare_workspace, render_report, usage_total)

RESULTS_DIR = harness.EVALS_DIR / "results"
REPORT_PATH = harness.EVALS_DIR / "report.md"
BASELINE_PATH = harness.EVALS_DIR / "baseline.json"
DEFAULT_PERMISSION_MODE = "danger-full-access"   # 与日常使用同口径; 工作区是
                                                 # 一次性临时目录, 风险可控
DEFAULT_TIMEOUT = 900


def build_agent_cmd(args, task_prompt: str) -> list[str]:
    """被测 Agent 的命令行。默认走仓库真实入口 (当前解释器 = 仓库 venv);
    --agent-cmd 供管线自测: 用桩 Agent 验证 evals 自身, 不烧 token。"""
    if args.agent_cmd:
        import shlex
        return shlex.split(args.agent_cmd) + [task_prompt]
    cmd = [sys.executable, str(harness.ROOT / "main.py"), "-p", task_prompt,
           "--output-format", "json", "--permission-mode", args.permission_mode]
    if args.model:
        cmd += ["--model", args.model]
    return cmd


def run_task(task, args, run_dir: Path, judge_client) -> dict:
    """跑单个任务并打分, 返回结果记录 (JSON 可序列化)。"""
    workspace = prepare_workspace(task, run_dir)
    cmd = build_agent_cmd(args, task.prompt)

    record = {
        "task": task.name,
        "score_method": task.score_method,
        "exit_code": None,
        "subtype": None,
        "checks": None,
        "judge": None,
        "passed": False,
        "duration_s": 0.0,
        "iterations": 0,
        "total_tokens": 0,
        "result_snippet": "",
        "error": "",
    }
    t0 = time.monotonic()
    try:
        proc = subprocess_run(cmd, workspace, args.timeout)
    except subprocess.TimeoutExpired:
        # 注意不是内置 TimeoutError——TimeoutExpired 是 SubprocessError 家族,
        # 接不住它会让单个任务的超时炸掉整轮 (真实事故: 2026-09-26 首跑)
        record["duration_s"] = time.monotonic() - t0
        record["subtype"], record["error"] = "timeout", f"Agent 超时 (>{args.timeout}s)"
        return finalize(task, workspace, record, args, judge_client)
    record["duration_s"] = time.monotonic() - t0
    record["exit_code"] = proc.returncode

    payload = parse_agent_output(proc.stdout)
    if payload is None:
        record["subtype"] = "no-output"
        record["error"] = "stdout 无结果 JSON; stderr 尾部: " + \
            (proc.stderr or "").strip()[-300:]
        return finalize(task, workspace, record, args, judge_client)

    record["subtype"] = payload.get("subtype")
    record["iterations"] = int(payload.get("num_iterations") or 0)
    record["total_tokens"] = usage_total(payload.get("usage"))
    record["result_snippet"] = (payload.get("result") or "").strip()
    if payload.get("is_error"):
        record["error"] = f"Agent 自报失败 (subtype={record['subtype']})"
    return finalize(task, workspace, record, args, judge_client)


def subprocess_run(cmd, cwd: Path, timeout: int):
    """起被测 Agent。stdin=DEVNULL: -p 会探测 stdin, 管道是空的,
    走"空管道不并入"分支 (tests/test_headless.py 已覆盖)。"""
    import os
    proc = subprocess.run(
        cmd, cwd=cwd, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    return proc


def finalize(task, workspace: Path, record: dict, args, judge_client=None) -> dict:
    """打分收束: 确定性 checks → LLM judge → 综合结论。"""
    if task.has_checks:
        evaluate = load_checks(task.dir)
        try:
            checks = evaluate(workspace)
        except Exception as e:   # 夹具 checks 自身抛错 = 任务判失败, 不炸整轮
            record["checks"] = [{"name": "checks.py 执行", "passed": False,
                                 "detail": f"{type(e).__name__}: {e}"}]
        else:
            record["checks"] = [{"name": c.name, "passed": c.passed,
                                 "detail": c.detail} for c in checks]
    if task.has_judge:
        if judge_client is None:
            record["judge"] = {"pass": False,
                               "reason": "judge 不可用 (API_KEY 缺失或 client 启动失败)"}
        else:
            instruction = (task.dir / "judge.txt").read_text(encoding="utf-8").strip()
            try:
                record["judge"] = judge_verdict(instruction, task.prompt,
                                                record["result_snippet"], judge_client)
            except Exception as e:
                record["judge"] = {"pass": False,
                                   "reason": f"judge 失败: {type(e).__name__}: {e}"}
    record["passed"] = combine_verdict(record)
    return record


def combine_verdict(record: dict) -> bool:
    """综合判分: checks 全过 且 (有 judge 时) judge 通过。
    Agent 层失败 (超时/无输出/自报错误) 不直接判死——checks/judge 说了算
    (Agent 偶发自报错误但任务其实完成的场景, 由 checks 把关)。"""
    if record["checks"] is not None:
        if not all(c["passed"] for c in record["checks"]):
            return False
    if record["judge"] is not None:
        return bool(record["judge"]["pass"])
    if record["checks"] is None:
        return False   # 无任何判分依据 (judge 失败且无 checks) → 不通过
    return True


def load_baseline(path: Path) -> dict | None:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="x-code agent evals 批量运行器")
    parser.add_argument("--only", default="",
                        help="只跑指定任务, 逗号分隔 (默认全量)")
    parser.add_argument("--model", default=None, help="覆盖被测 Agent 的模型")
    parser.add_argument("--permission-mode", default=DEFAULT_PERMISSION_MODE)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"单任务超时秒数 (默认 {DEFAULT_TIMEOUT})")
    parser.add_argument("--agent-cmd", default=None,
                        help="替换被测 Agent 命令 (任务文本追加在末尾); 管线自测用")
    parser.add_argument("--save-baseline", action="store_true",
                        help="把本次结果存为基线 (evals/baseline.json)")
    parser.add_argument("--no-baseline-diff", action="store_true",
                        help="本次不与基线对比")
    parser.add_argument("--keep-workspaces", action="store_true",
                        help="保留临时工作区 (排查 Agent 行为用)")
    parser.add_argument("--tasks-dir", default=None, help="自定义任务目录")
    parser.add_argument("--list", action="store_true", help="只列出任务, 不运行")
    args = parser.parse_args(argv)

    tasks_dir = Path(args.tasks_dir) if args.tasks_dir else harness.TASKS_DIR
    tasks = discover_tasks(tasks_dir)
    if args.only:
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        unknown = wanted - {t.name for t in tasks}
        if unknown:
            parser.error(f"未知任务: {', '.join(sorted(unknown))}")
        tasks = [t for t in tasks if t.name in wanted]

    if args.list:
        for t in tasks:
            print(f"{t.name:<24} [{t.score_method}] {t.prompt[:50]}…")
        return 0

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_dir = harness.EVALS_DIR / ".runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # 显式装仓库根 .env: 被测 Agent 是 subprocess, cwd 在临时工作区,
    # 不依赖 dotenv 的向上搜索; judge client 同样受益
    from dotenv import load_dotenv
    load_dotenv(harness.ROOT / ".env")

    judge_client = None
    if any(t.has_judge for t in tasks):
        try:
            judge_client = make_judge_client()
        except Exception as e:
            print(f"⚠ LLM 判分不可用, judge 类任务将判失败: {e}", file=sys.stderr)

    print(f"evals 开始: {len(tasks)} 个任务 (run {run_id})")
    records: list[dict] = []
    result = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": args.model or "default",
        "permission_mode": args.permission_mode,
        "tasks": records,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results_file = RESULTS_DIR / f"{run_id}.json"
    # 逐任务落盘: 单任务崩溃/进程被杀不丢已完成任务的数据
    # (真实教训: 首跑 crash 在第 5 个任务, 前 4 个白跑)
    results_file.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    for i, task in enumerate(tasks, 1):
        print(f"[{i}/{len(tasks)}] {task.name} …", flush=True)
        record = run_task(task, args, run_dir, judge_client)
        records.append(record)
        results_file.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        mark = "✅" if record["passed"] else "❌"
        print(f"    {mark} {record['subtype'] or ''} "
              f"{record['duration_s']:.0f}s, {record['total_tokens']:,} tokens",
              flush=True)

    baseline = None if args.no_baseline_diff else load_baseline(BASELINE_PATH)
    diff = diff_baseline(result, baseline)
    result["baseline_id"] = baseline.get("run_id") if baseline else None
    results_file.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    report = render_report(result, diff)
    REPORT_PATH.write_text(report, encoding="utf-8")
    if args.save_baseline:
        BASELINE_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                 encoding="utf-8")

    if not args.keep_workspaces:
        import shutil
        shutil.rmtree(run_dir, ignore_errors=True)
    else:
        print(f"工作区保留在 {run_dir}")

    print()
    print(report)
    print(f"结果: {results_file}")
    print(f"报告: {REPORT_PATH}")
    if args.save_baseline:
        print(f"基线已保存: {BASELINE_PATH}")

    n_pass = sum(1 for r in records if r["passed"])
    return 0 if n_pass == len(records) else 1


if __name__ == "__main__":
    sys.exit(main())
