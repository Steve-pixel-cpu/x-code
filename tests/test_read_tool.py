"""read_file 行窗口（offset/limit）规格钉子。

背景: app.js 这类几千行的文件整读会被 truncate_tool_output 掐掉中段,
模型只能绕道 bash sed——多花调用数还助长"反复排查"。行窗口让 read_file
自己能翻页, 返回头里带续读 offset。

运行方式（在 x-code 目录下）:
    uv run pytest tests/test_read_tool.py -v
"""

from pathlib import Path

from tools import read_tool


def _make_file(tmp_path: Path, n: int = 50) -> Path:
    p = tmp_path / "sample.py"
    p.write_text("\n".join(f"line {i}" for i in range(1, n + 1)) + "\n",
                 encoding="utf-8")
    return p


def test_无窗口参数时全文返回(tmp_path):
    p = _make_file(tmp_path, n=5)
    out = read_tool({"path": str(p)}, workdir=str(tmp_path))
    assert out.startswith("line 1")
    assert out.rstrip().endswith("line 5")
    assert "[" not in out.splitlines()[0]      # 全文路径不加窗口头


def test_offset_limit_按行取窗口(tmp_path):
    p = _make_file(tmp_path, n=50)
    out = read_tool({"path": str(p), "offset": 10, "limit": 3},
                    workdir=str(tmp_path))
    lines = out.splitlines()
    assert lines[0] == f"[{p} lines 10-12 of 50 total; continue with offset=13]"
    assert lines[1:] == ["line 10", "line 11", "line 12"]


def test_offset到文件末尾_不带续读提示(tmp_path):
    p = _make_file(tmp_path, n=10)
    out = read_tool({"path": str(p), "offset": 8, "limit": 100},
                    workdir=str(tmp_path))
    lines = out.splitlines()
    assert "continue with offset" not in lines[0]
    assert lines[-1] == "line 10"


def test_offset越界报错并带总行数(tmp_path):
    p = _make_file(tmp_path, n=10)
    out = read_tool({"path": str(p), "offset": 99}, workdir=str(tmp_path))
    assert out.startswith("ERROR: offset 99")
    assert "10 lines" in out


def test_limit缺省时读到文件尾(tmp_path):
    p = _make_file(tmp_path, n=12)
    out = read_tool({"path": str(p), "offset": 11}, workdir=str(tmp_path))
    lines = out.splitlines()
    assert lines[1:] == ["line 11", "line 12"]


def test_不可解码字节不再整读报错(tmp_path):
    p = tmp_path / "blob.py"
    p.write_bytes(b"ok \xff\xfe text\n")
    out = read_tool({"path": str(p)}, workdir=str(tmp_path))
    assert "ok" in out                          # 正文照常拿到
