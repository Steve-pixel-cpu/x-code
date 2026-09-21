"""搜索工具空结果反馈测试: 空结果必须带"换思路"引导, 不能只回一句干巴巴
的 No matches——flash 档模型收到空结果后倾向于换关键词连发搜索（实测同一
主题 5 连发变体搜索）, 明确告诉它"零命中=换个方法"能压住这种空转。

运行: uv run pytest tests/test_tool_feedback.py -v
"""

from tools import glob_tool, grep_tool


def test_grep空结果附思路引导(tmp_path):
    out = grep_tool({"pattern": "zzz_no_such_token", "path": str(tmp_path)})

    assert "No matches" in out
    assert "System note" in out
    assert "Do NOT re-issue" in out          # 明确禁止换关键词连发


def test_grep正常命中不带提醒(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("def hello():\n    pass\n", encoding="utf-8")

    out = grep_tool({"pattern": "hello", "path": str(tmp_path)})

    assert "a.py" in out
    assert "System note" not in out          # 有结果时不掺提醒噪音


def test_glob空结果附思路引导(tmp_path):
    out = glob_tool({"pattern": "**/*.nonexistent", "path": str(tmp_path)})

    assert "No files found" in out
    assert "System note" in out


def test_glob正常命中不带提醒(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.py").write_text("x = 1\n", encoding="utf-8")

    out = glob_tool({"pattern": "**/*.py", "path": str(tmp_path)})

    assert "a.py" in out
    assert "System note" not in out
