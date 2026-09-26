"""并发编辑同一文件的竞态回归测试（事故驱动）。

背景（事故驱动）: runtime 的 run_turn 把同一条 assistant 消息里的多个
tool_use 交给 ThreadPoolExecutor 并行执行（省冷启动等待）。两个
edit_file 指向同一文件时，旧实现是"检查→读盘→内存替换→整文件重写"
无锁执行: 两个线程都以同一磁盘版本为基线，后写者整文件覆盖先写者的
改动——编辑被静默吞掉，无任何报错（实测: resume 代码块加上了, 循环头的
start_step 改动被吞掉）。

修复: edit_file / write_file 的读-改-写临界区按路径加锁串行化（锁内
_record_read_state 刷新共享档案, 后到者以先到者已落盘的新内容为基线）。
不同文件的编辑仍然并行, 主对话 × subagent 并发（同为进程内线程）也一并
被覆盖。

运行: uv run pytest tests/test_edit_race.py -v
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import tools
from tools import edit_file_tool, read_tool, write_tool


@pytest.fixture(autouse=True)
def clean_file_state():
    """FileStateCache 是模块级跨测试共享的, 每个用例前清空。"""
    from tools import _FILE_STATE
    _FILE_STATE.clear()
    yield
    _FILE_STATE.clear()


@pytest.fixture
def slowed_disk_text(monkeypatch):
    """给 _disk_text 加延迟, 放大读-改-写窗口, 让竞态确定性复现。"""
    def _slow(path):
        text = path.read_text(encoding="utf-8", errors="replace")
        threading.Event().wait(0.05)   # 50ms >> 线程调度抖动
        return text
    monkeypatch.setattr(tools, "_disk_text", _slow)


def _read(path) -> str:
    return read_tool({"path": str(path)})


def _edit(path, old, new):
    return edit_file_tool({"path": str(path), "old_string": old, "new_string": new})


V0 = "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"


# ------------------------------------------------------------
# 竞态回归
# ------------------------------------------------------------

def test_并行双编辑同文件_两处改动都保留(tmp_path, slowed_disk_text):
    """同一条消息并行执行的两个 edit_file 编辑同一文件的不同锚点:
    修复前双方都以 V0 为基线整文件重写, 后写者吞掉先写者的改动。"""
    f = tmp_path / "a.py"
    f.write_text(V0, encoding="utf-8")
    _read(f)   # 建档案: 两线程共享同一份 read 记录

    barrier = threading.Barrier(2, timeout=10)

    def run(old, new):
        barrier.wait()   # 两线程同时进临界区, 窗口拉满
        return _edit(f, old, new)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(run, "return 1", "return 111")
        fb = pool.submit(run, "return 2", "return 222")

    out_a, out_b = fa.result(), fb.result()
    assert out_a.startswith("OK: edited"), out_a
    assert out_b.startswith("OK: edited"), out_b
    final = f.read_text(encoding="utf-8")
    assert "return 111" in final, f"先写者的编辑被吞掉: {final!r}"
    assert "return 222" in final, f"后写者的编辑被吞掉: {final!r}"
    assert "def alpha" in final and "def beta" in final


def test_串行双编辑同文件_两处改动都保留(tmp_path):
    """串行路径回归: 加锁重构后顺序编辑行为不变。"""
    f = tmp_path / "a.py"
    f.write_text(V0, encoding="utf-8")
    _read(f)

    assert _edit(f, "return 1", "return 111").startswith("OK: edited")
    assert _edit(f, "return 2", "return 222").startswith("OK: edited")
    final = f.read_text(encoding="utf-8")
    assert "return 111" in final and "return 222" in final


def test_编辑期间被外部修改_仍拒绝(tmp_path):
    """stale 语义不被锁破坏: 读过之后文件被外部改动, 编辑仍拒绝。"""
    f = tmp_path / "a.py"
    f.write_text(V0, encoding="utf-8")
    _read(f)
    f.write_text(V0.replace("alpha", "gamma"), encoding="utf-8")   # 外部改动

    out = _edit(f, "return 1", "return 111")

    assert out.startswith("REFUSED"), out
    assert "modified since read" in out


def test_并行write与edit互斥_不互相吞(tmp_path, slowed_disk_text):
    """write_file 与 edit_file 并发同一路径: 走同一把路径锁串行落地,
    不出现"双方以旧基线 V0 各写各的"的撕裂终态（这正是锁要防的）。

    两种串行序都合法:
    - W→E: write 先落, edit 在其结果上补丁 → 两个改动都在;
    - E→W: edit 先落, write 的内容快照在并发前已定 → 整写按语义覆盖
      edit（write_file 就是"以我给的内容为准"; 锁管不了快照新旧）。
    本地调度稳定走 W→E, CI 走出过 E→W——断言接受两种合法终态,
    撕裂终态（beta_v2 与 111 都不在、或出现第三种内容）仍然被拒。"""
    f = tmp_path / "a.py"
    f.write_text(V0, encoding="utf-8")
    _read(f)

    barrier = threading.Barrier(2, timeout=10)

    def do_write():
        barrier.wait()
        return write_tool({"path": str(f), "content": V0.replace("beta", "beta_v2")})

    def do_edit():
        barrier.wait()
        return _edit(f, "return 1", "return 111")

    with ThreadPoolExecutor(max_workers=2) as pool:
        fw = pool.submit(do_write)
        fe = pool.submit(do_edit)

    assert fw.result().startswith("OK: wrote")
    assert fe.result().startswith("OK: edited")
    final = f.read_text(encoding="utf-8")
    assert "beta_v2" in final, f"write 的改动丢失: {final!r}"
    if "return 111" not in final:
        # E→W 序: 终态必须精确等于 write 的快照（证明 edit 之后没有第三者
        # 再动过文件）; 出现其他内容 = 真撕裂
        assert final == V0.replace("beta", "beta_v2"), f"撕裂终态: {final!r}"
