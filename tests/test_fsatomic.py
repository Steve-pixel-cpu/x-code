"""fsatomic 原子写的规格钉子: 唯一临时名 + PermissionError 重试。

背景: 固定名 tmp 在多写者并发时会互踩（A 正在写 tmp, B 截断重写同一个
tmp, A 的 replace 发布出半截内容 → 读者撞见非法 JSON, 数据看似凭空
消失）; Windows 上 replace 的目标被并发读者 open() 着时抛
PermissionError（CPython 不带 FILE_SHARE_DELETE）, 杀软扫描 tmp 也是
同样的瞬时锁。test_agent_tools 的偶发红就是这两件事叠加。

运行方式（在 x-code 目录下）:
    uv run pytest tests/test_fsatomic.py -v
"""

import json
import threading
import time
from pathlib import Path

import fsatomic


# ------------------------------------------------------------
# 并发写互不踩踏
# ------------------------------------------------------------

def test_并发原子写同一目标_互不截断(tmp_path):
    """两个线程高频原子写同一个文件: 全部成功、内容始终是完整 JSON。
    旧实现（固定名 tmp）在这里会互相截断对方的临时文件, 发布出半截
    内容并抛 FileNotFoundError/PermissionError。"""
    dst = tmp_path / "agent.json"
    errors: list[Exception] = []
    ROUNDS = 40

    def writer(tag: str) -> None:
        for i in range(ROUNDS):
            try:
                fsatomic.atomic_write_text(
                    dst, json.dumps({"tag": tag, "i": i}))
            except Exception as e:   # noqa: BLE001 - 故意宽收, 断言时展示
                errors.append(e)

    threads = [threading.Thread(target=writer, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    data = json.loads(dst.read_text(encoding="utf-8"))
    assert data["tag"] in ("a", "b")               # 某一次完整写胜出
    assert not list(tmp_path.glob("*.tmp"))        # 无临时文件残片


def test_原子写期间读者要么旧要么新_绝无半截(tmp_path):
    """读者持续读的同时写者持续写: 每次读到的都是合法完整内容。
    读者走 read_text_with_retry（生产读路径）——replace 的过渡窗口里
    Windows 新开读句柄会被瞬时拒绝（delete pending）, 裸 read_text
    在高频读写时会撞上, 这是读侧必须走重试助手的原因。
    （生产对应: Leader 轮询 get_status 时 worker 正写终态。）"""
    dst = tmp_path / "m.json"
    fsatomic.atomic_write_text(dst, json.dumps({"v": 0}))
    stop = threading.Event()
    bad: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                json.loads(fsatomic.read_text_with_retry(dst))
            except Exception as e:   # noqa: BLE001
                bad.append(repr(e))
                return

    reader_t = threading.Thread(target=reader, daemon=True)
    reader_t.start()
    for i in range(1, 51):
        fsatomic.atomic_write_text(dst, json.dumps({"v": i}))
    stop.set()
    reader_t.join(timeout=5)

    assert bad == []
    assert json.loads(fsatomic.read_text_with_retry(dst))["v"] == 50


# ------------------------------------------------------------
# Windows 目标被占时的重试
# ------------------------------------------------------------

def test_目标被读者占住时重试_松手后成功(tmp_path):
    """回归钉: 读者 open() 着目标文件时 replace 抛 PermissionError
    （Windows）。重试必须在读者松手后成功落盘。POSIX 上 replace 本就
    不受打开句柄影响, 此用例退化为普通写, 同样通过。"""
    dst = tmp_path / "held.json"
    dst.write_text("old", encoding="utf-8")

    hold = open(dst, "r", encoding="utf-8")        # noqa: SIM115 - 故意占住
    done = threading.Event()
    write_error: list[Exception] = []

    def writer() -> None:
        try:
            fsatomic.atomic_write_text(dst, "new")
        except Exception as e:   # noqa: BLE001
            write_error.append(e)
        finally:
            done.set()

    t = threading.Thread(target=writer)
    t.start()
    time.sleep(0.15)            # 让写者先撞上被占住的目标（首次 replace 失败）
    hold.close()                # 读者松手
    t.join(timeout=10)

    assert done.is_set()
    assert write_error == []
    assert dst.read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob("*.tmp"))


# ------------------------------------------------------------
# 临时文件命名与清理
# ------------------------------------------------------------

def test_唯一临时名_同目标多次调用互不相同(tmp_path):
    dst = tmp_path / "x.json"
    names = {fsatomic.unique_tmp_path(dst).name for _ in range(50)}
    assert len(names) == 50                        # 并发写者互不踩踏的前提
    for n in names:
        assert n.startswith("x.json")              # 同目录（replace 原子）


def test_写失败时清理临时残片(tmp_path, monkeypatch):
    """replace 一直失败（超过重试上限）: 异常上抛, 但 tmp 残片被清掉,
    不留垃圾; 原文件保持不动。"""
    dst = tmp_path / "x.json"
    dst.write_text("old", encoding="utf-8")

    def always_locked(tmp, target):
        raise PermissionError(13, "locked")

    monkeypatch.setattr(fsatomic, "_replace_with_retry", always_locked)
    try:
        fsatomic.atomic_write_text(dst, "new")
        raise AssertionError("应当上抛 PermissionError")
    except PermissionError:
        pass

    assert dst.read_text(encoding="utf-8") == "old"   # 原文件完好
    assert not list(tmp_path.glob("*.tmp"))           # 残片已清
