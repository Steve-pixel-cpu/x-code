"""判分: 测试全过 + 旧名零残留 + 新名确实存在。"""
from pathlib import Path

from harness import Check, pytest_check, text_absent


def evaluate(workspace: Path) -> list[Check]:
    new_name_present = any(
        "def fmt_price" in p.read_text(encoding="utf-8", errors="replace")
        for p in workspace.rglob("*.py"))
    return [
        pytest_check(workspace),
        text_absent(workspace, "format_price"),
        Check("新名 fmt_price 已定义", new_name_present),
    ]
