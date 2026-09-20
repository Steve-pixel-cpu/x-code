"""后台任务工具的规格钉子: bash/powershell 的 background=true、
task_output、task_stop。

设计决定（谁改行为谁让测试红）:
- background 启动立即返回, 不等命令结束——服务器类常驻命令从此
  脱离轮次生命周期, 不再出现"跑个 Minecraft 服务器对话就卡死"。
- task_output 只回日志尾部 + 存活状态——服务日志再大也不撑上下文。
- task_stop 杀整棵进程树——服务常带子进程, 只杀主进程会漏。

运行方式（在 x-code 目录下）:
    uv run pytest tests/test_bg_tasks.py -v
"""

import time

import tools


def _task_id_of(started: str) -> str:
    """从启动返回文案里解析 task_id（格式见 _start_background）。"""
    assert started.startswith("Background task started")
    return started.split("id=")[1].split()[0]


def _wait_log_contains(task_id: str, text: str, timeout: float = 10) -> str:
    """轮询直到日志尾部出现目标文本（后台进程写日志有竞态窗口）。"""
    deadline = time.monotonic() + timeout
    body = ""
    while time.monotonic() < deadline:
        body = tools.task_output_tool({"task_id": task_id})
        if text in body:
            return body
        time.sleep(0.2)
    return body


# ------------------------------------------------------------
# background 启动 + task_output
# ------------------------------------------------------------

def test_后台启动立即返回_日志可读():
    out = tools.bash_tool({"command": "echo bg-hello", "background": True})
    task_id = _task_id_of(out)

    assert "log:" in out                        # 返回里带日志路径, 模型可自查
    body = _wait_log_contains(task_id, "bg-hello")
    assert "bg-hello" in body
    assert body.startswith(f"[{task_id}]")      # 状态头: running / exited


def test_后台任务脱离轮次_进程继续存活可查询():
    """启动后进程不被等待: task_output 随时探活。"""
    out = tools.bash_tool({"command": "sleep 5", "background": True})
    task_id = _task_id_of(out)

    body = tools.task_output_tool({"task_id": task_id})
    assert body.startswith(f"[{task_id}] running")


def test_task_output_支持截取尾部行数():
    out = tools.bash_tool({
        "command": "for i in 1 2 3 4 5; do echo line-$i; done",
        "background": True,
    })
    task_id = _task_id_of(out)

    body = _wait_log_contains(task_id, "line-5")
    tail = tools.task_output_tool({"task_id": task_id, "tail_lines": 2})
    assert "line-5" in tail
    assert "line-1" not in tail                 # 只取尾部, 不撑上下文


# ------------------------------------------------------------
# task_stop
# ------------------------------------------------------------

def test_task_stop杀整树_常驻进程被终止():
    out = tools.bash_tool({"command": "sleep 30", "background": True})
    task_id = _task_id_of(out)

    assert tools.task_output_tool({"task_id": task_id}).startswith(
        f"[{task_id}] running")
    assert "stopped" in tools.task_stop_tool({"task_id": task_id})

    body = _wait_log_contains(task_id, "exited")
    assert "exited" in body


def test_task_stop对已退出任务幂等():
    out = tools.bash_tool({"command": "echo quick", "background": True})
    task_id = _task_id_of(out)
    _wait_log_contains(task_id, "quick")

    stop = tools.task_stop_tool({"task_id": task_id})

    assert "already exited" in stop             # 幂等: 不报错, 说明状态


# ------------------------------------------------------------
# 边界: 未知 task_id / 坏参数
# ------------------------------------------------------------

def test_未知task_id朝安全侧报错():
    assert "unknown task_id" in tools.task_output_tool({"task_id": "bg-99999"})
    assert "unknown task_id" in tools.task_stop_tool({"task_id": "bg-99999"})


def test_tail_lines坏值兜底不炸():
    out = tools.bash_tool({"command": "echo ok", "background": True})
    task_id = _task_id_of(out)
    _wait_log_contains(task_id, "ok")

    body = tools.task_output_tool({"task_id": task_id, "tail_lines": "x"})
    assert "ok" in body                         # 坏值回落默认行数, 不抛异常
