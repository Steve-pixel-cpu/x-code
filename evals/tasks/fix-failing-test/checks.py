"""判分: 测试全过 + 测试文件未被篡改。"""
from pathlib import Path

from harness import Check, file_unchanged, pytest_check

ORIGINAL_DIR = Path(__file__).parent / "project"


def evaluate(workspace: Path) -> list[Check]:
    return [
        pytest_check(workspace),
        file_unchanged(workspace, "test_shapes.py", ORIGINAL_DIR),
    ]
