"""evals 管线自测桩 Agent: 模拟一个真实 Agent 的可观测行为。

用法 (配合 run_evals 的 --agent-cmd):
    python tests/evals_stub_agent.py "<任务文本>"

行为约定:
- 任务文本含 "FAIL"  → 不写文件, 输出 is_error=true 的结果 JSON
- 否则              → 在 cwd 写 ok.txt (内容 "done"), 输出成功 JSON
桩只认最后一个参数是任务文本 (run_evals.build_agent_cmd 的拼接约定)。
"""
import json
import sys
from pathlib import Path


def main() -> int:
    task = sys.argv[-1] if len(sys.argv) > 1 else ""
    fail = "FAIL" in task
    if not fail:
        (Path.cwd() / "ok.txt").write_text("done", encoding="utf-8")
    print(json.dumps({
        "type": "result",
        "subtype": "completed",
        "is_error": fail,
        "result": "失败了" if fail else "干完了",
        "session_id": "stub",
        "num_iterations": 2,
        "auto_compacted": False,
        "usage": {"input_tokens": 100, "output_tokens": 20,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
