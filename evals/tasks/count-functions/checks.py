"""判分: count.txt 内容正确 (答案是 7)。"""
from pathlib import Path

from harness import Check

ANSWER = "7"


def evaluate(workspace: Path) -> list[Check]:
    path = workspace / "count.txt"
    if not path.exists():
        return [Check("count.txt 存在", False, "文件未创建")]
    content = path.read_text(encoding="utf-8").strip()
    return [
        Check("count.txt 存在", True),
        Check("count.txt 内容 = 7", content == ANSWER, f"实际: {content!r}"),
    ]
