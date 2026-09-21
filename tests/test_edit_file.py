"""edit_file 工具 + FileStateCache 测试。

背景（事故驱动）: 模型"改一处"却只有 write_file（整文件覆盖）可用, 写了一
行把整个 index.html 毁掉。对标 Claude FileEditTool 补齐:
- edit_file: old_string → new_string 字面替换（唯一性/replace_all/逐字匹配）
- FileStateCache: read 建档, 写前校验（read-before-write + stale 检查）
- write_file 收紧: 覆盖已存在文件必须先读过

运行: uv run pytest tests/test_edit_file.py -v
"""

import json

import pytest

from tools import edit_file_tool, read_tool, write_tool


@pytest.fixture(autouse=True)
def clean_file_state():
    """FileStateCache 是模块级跨测试共享的, 每个用例前清空。"""
    from tools import _FILE_STATE
    _FILE_STATE.clear()
    yield
    _FILE_STATE.clear()


def _read(path) -> str:
    return read_tool({"path": str(path)})


def _edit(path, old, new, replace_all=False):
    return edit_file_tool({
        "path": str(path), "old_string": old, "new_string": new,
        "replace_all": replace_all,
    })


# ------------------------------------------------------------
# edit_file 基本语义
# ------------------------------------------------------------

def test_基本替换成功_带diff(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("def hello():\n    return 1\n", encoding="utf-8")
    _read(f)

    out = _edit(f, "return 1", "return 42")

    assert out.startswith("OK: edited")
    assert "1 replacement" in out
    assert f.read_text(encoding="utf-8") == "def hello():\n    return 42\n"
    meta = getattr(out, "_meta", None) or {}
    assert "diff" in meta and "-    return 1" in meta["diff"]


def test_old_string不存在报错(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("hello\n", encoding="utf-8")
    _read(f)

    out = _edit(f, "nonexistent", "x")

    assert out.startswith("ERROR: old_string not found")
    assert "verbatim" in out                 # 引导逐字复制
    assert f.read_text(encoding="utf-8") == "hello\n"   # 未被改动


def test_多处命中未replace_all报错(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\nx = 1\n", encoding="utf-8")
    _read(f)

    out = _edit(f, "x = 1", "x = 2")

    assert "matches 2 places" in out
    assert f.read_text(encoding="utf-8") == "x = 1\nx = 1\n"


def test_replace_all替换全部(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\nx = 1\n", encoding="utf-8")
    _read(f)

    out = _edit(f, "x = 1", "x = 2", replace_all=True)

    assert "2 replacements" in out
    assert f.read_text(encoding="utf-8") == "x = 2\nx = 2\n"


def test_old等于new报no_op(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("hello\n", encoding="utf-8")
    _read(f)

    out = _edit(f, "hello", "hello")

    assert "identical" in out


def test_新文件引导用write_file(tmp_path):
    out = _edit(tmp_path / "new.py", "a", "b")

    assert out.startswith("ERROR: file not found")
    assert "write_file" in out


# ------------------------------------------------------------
# FileStateCache: read-before-write + stale 检查
# ------------------------------------------------------------

def test_未读先改拒绝(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("hello\n", encoding="utf-8")

    out = _edit(f, "hello", "hi")

    assert out.startswith("REFUSED")
    assert "has not been read" in out


def test_未读先写覆盖拒绝_新文件放行(tmp_path):
    f = tmp_path / "existing.py"
    f.write_text("old content\n", encoding="utf-8")
    new = tmp_path / "new.py"

    out = write_tool({"path": str(f), "content": "one line\n"})

    assert out.startswith("REFUSED")
    assert "has not been read" in out
    assert f.read_text(encoding="utf-8") == "old content\n"   # 文件没被毁

    out2 = write_tool({"path": str(new), "content": "fresh\n"})
    assert out2.startswith("OK")                              # 新文件不受限


def test_读过后正常整写与再编辑(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("v1\n", encoding="utf-8")
    _read(f)

    out = write_tool({"path": str(f), "content": "v2\n"})
    assert out.startswith("OK")

    out2 = _edit(f, "v2", "v3")               # write 后档案已刷新, 编辑可继续
    assert out2.startswith("OK")
    assert f.read_text(encoding="utf-8") == "v3\n"


def test_外部修改后stale拒绝(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("v1\n", encoding="utf-8")
    _read(f)
    f.write_text("externally changed\n", encoding="utf-8")   # 模拟外部改动

    out = _edit(f, "v1", "v2")
    assert out.startswith("REFUSED")
    assert "modified since read" in out

    out2 = write_tool({"path": str(f), "content": "v2\n"})
    assert out2.startswith("REFUSED")         # write 同样受 stale 保护

    _read(f)                                  # 重读刷新档案后放行
    assert _edit(f, "externally changed", "v3").startswith("OK")


def test_分页read同样建档(tmp_path):
    f = tmp_path / "big.py"
    f.write_text("\n".join(f"line{i}" for i in range(50)) + "\n", encoding="utf-8")
    read_tool({"path": str(f), "offset": 1, "limit": 5})   # 只读了前 5 行

    out = _edit(f, "line0", "LINE0")          # 但整个文件视为已读
    assert out.startswith("OK")


def test_编辑后档案刷新_连续编辑不误拦(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("a\nb\nc\n", encoding="utf-8")
    _read(f)

    assert _edit(f, "a", "A").startswith("OK")
    assert _edit(f, "b", "B").startswith("OK")   # 第二次编辑: 档案已是 A b c
    assert f.read_text(encoding="utf-8") == "A\nB\nc\n"


def test_拒绝后重读即恢复(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("keep\n", encoding="utf-8")

    assert write_tool({"path": str(f), "content": "destroy\n"}).startswith("REFUSED")
    assert f.read_text(encoding="utf-8") == "keep\n"      # 文件完好

    _read(f)
    assert write_tool({"path": str(f), "content": "deliberate full rewrite\n"}).startswith("OK")
