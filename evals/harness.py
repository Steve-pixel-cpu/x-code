# --- evals 共享骨架 ---
#
# 职责: 工作区复制 / 任务发现 / checks 加载 / Agent 输出解析 / LLM 判分 /
# 报告与基线对比。run_evals.py 是薄 CLI, 逻辑都在这里——tests 直接测本模块。
#
# 任务夹具约定 (evals/tasks/<名字>/):
#   task.txt    必有。发给 Agent 的任务文本 (-p 的 prompt)。
#   project/    必有。夹具项目, 每次运行前整树复制到临时工作区, Agent 在
#               工作区里干活 (原夹具永远不被污染)。
#   checks.py   可选。确定性断言: 定义 evaluate(workspace: Path) -> list[Check]。
#   judge.txt   可选。LLM 判分标准 (给判分器的指令)。两者都没写的任务视为
#               坏夹具, 发现时直接报错。两者都有时 = checks 全过 且 judge 通过。
#
# 运行约定: Agent 经 subprocess 调 main.py -p --output-format json (见
# run_evals.build_agent_cmd), cwd = 临时工作区; 仓库 venv 的 python 直接作
# 为解释器 (uv run 会在无 pyproject 的工作区解析错环境)。

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
ROOT = EVALS_DIR.parent
TASKS_DIR = EVALS_DIR / "tasks"


# ============================================================================
# Check 与 checks.py 加载
# ============================================================================

@dataclass
class Check:
    """一条确定性断言的结果。"""
    name: str
    passed: bool
    detail: str = ""


def load_checks(task_dir: Path):
    """加载夹具的 checks.py, 返回其 evaluate 函数 (无 checks.py 返回 None)。

    checks.py 里 `from harness import Check` 能成立的前提: 本目录在
    sys.path——在这里保证, 夹具作者不用关心。"""
    path = task_dir / "checks.py"
    if not path.exists():
        return None
    evals_dir = str(task_dir.parent.parent)   # evals/
    if evals_dir not in sys.path:
        sys.path.insert(0, evals_dir)
    spec = importlib.util.spec_from_file_location(f"eval_checks_{task_dir.name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn = getattr(module, "evaluate", None)
    if fn is None:
        raise ValueError(f"{path} 缺少 evaluate(workspace) 函数")
    return fn


# --- checks.py 常用断言件 (夹具按需 import) ---

def pytest_check(workspace: Path, args: list[str] | None = None,
                 timeout: int = 180) -> Check:
    """在临时工作区跑夹具测试 (用当前解释器, pytest 已装在仓库 venv)。
    Agent 自己跑测试用 `uv run --with pytest pytest -q` (裸目录可用);
    判分侧用仓库 venv 更稳, 两者互不依赖。"""
    cmd = [sys.executable, "-m", "pytest", "-q", "--tb=short"] + (args or [])
    try:
        proc = subprocess.run(cmd, cwd=workspace, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return Check("pytest", False, f"测试超时 (>{timeout}s)")
    tail = (proc.stdout or "").strip().splitlines()
    summary = tail[-1] if tail else "(无输出)"
    return Check("pytest", proc.returncode == 0, summary)


def file_unchanged(workspace: Path, rel: str, original_dir: Path) -> Check:
    """关键文件必须原样: 防Agent 走捷径 (改测试凑绿 / 删文件消音)。"""
    name = f"未改动 {rel}"
    current, original = workspace / rel, original_dir / rel
    if not current.exists():
        return Check(name, False, "文件被删除")
    if current.read_bytes() == original.read_bytes():
        return Check(name, True)
    return Check(name, False, "文件内容与夹具原始版本不一致")


def text_absent(workspace: Path, pattern: str, globs: tuple[str, ...] = ("*.py",),
                excludes: tuple[str, ...] = ()) -> Check:
    """旧标识必须从指定文件族里消失 (重命名类任务的残留检查)。"""
    hits = []
    for g in globs:
        for p in workspace.rglob(g):
            if any(part in p.parts for part in excludes):
                continue
            if p.is_file() and pattern in p.read_text(encoding="utf-8", errors="replace"):
                hits.append(str(p.relative_to(workspace)))
    return Check(f"已无 {pattern}", not hits, "残留于: " + ", ".join(hits[:5]) if hits else "")


# ============================================================================
# 任务发现与工作区
# ============================================================================

@dataclass
class Task:
    """一个已发现的评测任务。score_method 由夹具文件决定 (见模块头)。"""
    name: str
    dir: Path
    prompt: str
    has_checks: bool
    has_judge: bool

    @property
    def score_method(self) -> str:
        if self.has_checks and self.has_judge:
            return "checks+judge"
        return "judge" if self.has_judge else "checks"


def discover_tasks(tasks_dir: Path = TASKS_DIR) -> list[Task]:
    """扫描任务目录。坏夹具 (缺 task.txt/project 或判分方式缺失) 直接抛错
    ——夹具错误应该在写的时候就炸, 不该混进评测结果里。"""
    if not tasks_dir.is_dir():
        raise FileNotFoundError(f"任务目录不存在: {tasks_dir}")
    tasks = []
    for d in sorted(p for p in tasks_dir.iterdir() if p.is_dir()):
        if (d / "task.txt").exists() and (d / "project").is_dir():
            prompt = (d / "task.txt").read_text(encoding="utf-8").strip()
            has_checks = (d / "checks.py").exists()
            has_judge = (d / "judge.txt").exists()
            if not has_checks and not has_judge:
                raise ValueError(f"任务 {d.name}: checks.py 与 judge.txt 至少要有一样")
            tasks.append(Task(d.name, d, prompt, has_checks, has_judge))
    if not tasks:
        raise FileNotFoundError(f"{tasks_dir} 下没有任务夹具")
    return tasks


def prepare_workspace(task: Task, run_dir: Path) -> Path:
    """夹具项目 → 全新临时工作区。evals 的可重复性靠它: 每次从原始状态起跑。"""
    workspace = run_dir / task.name / "workspace"
    shutil.copytree(task.dir / "project", workspace)
    return workspace


# ============================================================================
# Agent 输出解析与 LLM 判分
# ============================================================================

def parse_agent_output(stdout: str) -> dict | None:
    """从 -p --output-format json 的 stdout 解析结果对象。
    正常情况 stdout 就是纯 JSON; 异常路径 (崩溃/启动失败) 可能是空或报错
    文本——返回 None, 由调用方记为 agent 层失败并保留 stderr。"""
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 容错: 混入了非 JSON 行时抓第一个 {...} 块
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


class JudgeError(RuntimeError):
    """判分请求失败或回复不可解析。"""


def judge_verdict(instruction: str, task_prompt: str, agent_result: str,
                  api_client, timeout_s: int = 120) -> dict:
    """LLM 判分。返回 {"pass": bool, "reason": str}; 回复不可解析抛 JudgeError。

    用 generate_text side-call (非流式/无工具/不进会话), 与自动命名同一通道。
    """
    user = (
        f"## 任务描述 (发给被测 Agent 的原文)\n{task_prompt}\n\n"
        f"## 判分标准\n{instruction}\n\n"
        f"## Agent 的最终回复\n{agent_result or '(空)'}\n\n"
        "只输出一个 JSON 对象, 格式: {\"pass\": true/false, \"reason\": \"一句话理由\"}"
    )
    system = ["你是 AI Agent 评测的判分器。依据判分标准独立判断任务是否完成,"
              "不苛求完美, 但标准里明确要求的要点缺了就不通过。"]
    # 4096: GLM 系强制思考, 思考也吃 max_tokens——512 在长输入下会被
    # 思考耗尽, 正文为空 (真实事故: 判分器回复空串, 任务误判失败)
    response = api_client.generate_text(system, user, max_tokens=4096)
    match = re.search(r"\{.*\}", response or "", re.DOTALL)
    if not match:
        raise JudgeError(f"判分器回复里没有 JSON: {response[:200]!r}")
    try:
        verdict = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise JudgeError(f"判分器 JSON 解析失败: {e}") from e
    if not isinstance(verdict.get("pass"), bool):
        raise JudgeError(f"判分结果缺 pass 布尔字段: {verdict}")
    verdict["reason"] = str(verdict.get("reason", ""))
    return verdict


def make_judge_client(model: str | None = None):
    """判分用 API client (进程内 side-call, 与被测 subprocess 无关)。
    模型默认与 Agent 主模型同源 (main.DEFAULT_MODEL); 以后接
    utilityProvider 换便宜模型只改这里。"""
    import os
    import sys
    # 以脚本方式运行 run_evals.py 时 sys.path 只有 evals/, 仓库根模块
    # (api_client/main) 不可见——在这里补上, judge 才能构建 client
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("API_KEY")
    if not api_key:
        raise RuntimeError("API_KEY 未设置 (仓库根 .env), 无法 LLM 判分")
    from api_client import make_api_client, normalize_protocol
    from main import DEFAULT_MODEL
    try:
        protocol = normalize_protocol(os.getenv("XCODE_PROTOCOL"))
    except ValueError:
        protocol = "anthropic"
    return make_api_client(protocol, api_key=api_key,
                           model=model or DEFAULT_MODEL, emit_output=False)


# ============================================================================
# 报告与基线
# ============================================================================

def usage_total(usage: dict | None) -> int:
    if not isinstance(usage, dict):
        return 0
    return sum(int(v) for v in usage.values() if isinstance(v, (int, float)))


def diff_baseline(current: dict, baseline: dict | None) -> dict[str, str]:
    """逐任务对比上一次基线: improved / regressed / unchanged / new / gone。"""
    if not baseline:
        return {}
    base = {t["task"]: t.get("passed") for t in baseline.get("tasks", [])}
    now = {t["task"]: t.get("passed") for t in current["tasks"]}
    out = {}
    for name, passed in now.items():
        if name not in base:
            out[name] = "new"
        elif passed and not base[name]:
            out[name] = "improved"
        elif not passed and base[name]:
            out[name] = "regressed"
        else:
            out[name] = "unchanged"
    for name in base:
        if name not in now:
            out[name] = "gone"
    return out


def render_report(result: dict, diff: dict[str, str]) -> str:
    """结果 → markdown 报告 (控制台贴精简版, 文件存全量)。"""
    tasks = result["tasks"]
    n_pass = sum(1 for t in tasks if t["passed"])
    lines = [
        f"# x-code evals 报告 — {result['run_id']}",
        "",
        f"通过 {n_pass}/{len(tasks)}"
        + (f" (对比基线 {result['baseline_id']})" if result.get("baseline_id") else ""),
        "",
        "| 任务 | 结果 | 判分 | 用时 | tokens | 迭代 | vs 基线 |",
        "|---|---|---|---|---|---|---|",
    ]
    for t in tasks:
        status = "✅" if t["passed"] else "❌"
        judge = "—" if t.get("judge") is None \
            else ("✅" if t["judge"]["pass"] else f"❌ {t['judge']['reason'][:30]}")
        checks = "—"
        if t.get("checks") is not None:
            bad = [c for c in t["checks"] if not c["passed"]]
            checks = f"{len(t['checks']) - len(bad)}/{len(t['checks'])}"
            if bad:
                checks += " ✗" + ";".join(c["name"] for c in bad[:3])
        lines.append(
            f"| {t['task']} | {status} | checks {checks}; judge {judge} "
            f"| {t['duration_s']:.0f}s | {t['total_tokens']:,} "
            f"| {t['iterations']} | {diff.get(t['task'], '—')} |")
    lines.append("")
    # 失败详情: 失败原因/结果摘要, 排查用
    for t in tasks:
        if t["passed"]:
            continue
        lines += [f"## ✗ {t['task']}", ""]
        if t.get("error"):
            lines.append(f"- 错误: {t['error']}")
        if t.get("result_snippet"):
            lines.append(f"- Agent 回复摘要: {t['result_snippet'][:300]}")
        lines.append("")
    return "\n".join(lines)
