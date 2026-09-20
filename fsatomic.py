"""跨线程安全的原子文件写。

问题背景（Windows 特有）:
- os.replace 的目标文件被并发读者 open() 着时（CPython 打开文件不带
  FILE_SHARE_DELETE）, 替换会抛 PermissionError; 杀软扫描新建的临时
  文件也会造成同样的瞬时锁。
- 固定名的临时文件在多线程同时写同一目标时会互踩: A 正在写 tmp,
  B 把同一个 tmp 截断重写, A 的 replace 就把半截内容发布了——读者
  撞见非法 JSON, 数据看起来"凭空消失"。

约定:
- 临时文件唯一命名（同目录、同卷, replace 的原子性不变）
- replace 撞上瞬时锁时指数退避重试, 超时才放弃
"""

import os
import random
import time
import uuid
from pathlib import Path
from typing import Union

PathLike = Union[str, os.PathLike]

_RETRY_CAP = 10          # 首次之外的最多重试次数
_BASE_DELAY_S = 0.02     # 首次退避
_MAX_DELAY_S = 0.25      # 单次退避上限（累计约 1.5s, 覆盖读者句柄/杀软扫描窗口）


def unique_tmp_path(target: Path) -> Path:
    """目标文件旁边的唯一临时名: 同目录保证同卷（replace 原子）, 唯一
    后缀保证并发写者互不踩踏。"""
    return target.with_name(
        f"{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")


def _replace_with_retry(tmp: Path, target: Path) -> None:
    """os.replace 带退避重试: 目标被读者占住 / 杀软瞬时锁时, 对方松手后
    下一次尝试即可成功。非"目标被占"类错误（tmp 丢失等）不重试。"""
    last_exc: PermissionError | None = None
    for attempt in range(_RETRY_CAP + 1):
        try:
            os.replace(tmp, target)
            return
        except PermissionError as e:
            last_exc = e
            if attempt < _RETRY_CAP:
                delay = min(_BASE_DELAY_S * (2 ** attempt), _MAX_DELAY_S)
                delay *= random.uniform(0.8, 1.2)
                time.sleep(delay)
    assert last_exc is not None   # 循环耗尽必然带出最后一次错误
    raise last_exc


def atomic_write_text(target: PathLike, text: str, encoding: str = "utf-8") -> None:
    """原子覆盖写: 唯一临时文件 + 带重试的原子替换。

    并发读者要么看到完整的旧内容、要么看到完整的新内容; 多个写者
    并发时各自完整落盘, 后替换者胜, 绝不出现半截文件。
    """
    target = Path(target)
    tmp = unique_tmp_path(target)
    try:
        with open(tmp, "w", encoding=encoding) as f:
            f.write(text)
        _replace_with_retry(tmp, target)
    finally:
        # replace 成功后 tmp 已不存在; 失败路径把残片清掉, 不留垃圾
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_replace(tmp: Path, target: PathLike) -> None:
    """调用方自建临时文件的场景（如先流式追加、再整体替换）: 带重试替换。"""
    _replace_with_retry(Path(tmp), Path(target))


def read_text_with_retry(target: PathLike, encoding: str = "utf-8") -> str:
    """读文本, open() 撞 PermissionError 时退避重试。

    replace 执行的瞬间目标处于 Windows 的"delete pending"过渡态, 此时
    新开的读句柄会拿到 ERROR_ACCESS_DENIED——窗口极窄但高频读写下
    会撞上。一旦打开成功, 句柄就安全了（写者侧此时反而会被读者挡住,
    由写者重试）。与 atomic_write_text 配对使用。"""
    target = Path(target)
    last_exc: PermissionError | None = None
    for attempt in range(_RETRY_CAP + 1):
        try:
            return target.read_text(encoding=encoding)
        except PermissionError as e:
            last_exc = e
            if attempt < _RETRY_CAP:
                delay = min(_BASE_DELAY_S * (2 ** attempt), _MAX_DELAY_S)
                delay *= random.uniform(0.8, 1.2)
                time.sleep(delay)
        except FileNotFoundError:
            raise
    assert last_exc is not None
    raise last_exc
