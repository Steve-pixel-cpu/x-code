"""复现: 计划模式下 present_plan 的审批链路是否发出 permission_request。

场景一（单会话）: A 会话计划提交 → permission_request → 批准 → 继续实施。
场景二（双会话, 前端"卡住"bug 的协议层语义）: A、B 同时在计划审批上挂起,
   各自的 permission_request 的 request_id 只在会话内唯一（perm-{seq}
   每会话独立计数, 两边可以同为 perm-1）; 批 B 不影响 A, 再批 A 独立收尾。
   前端计划面板必须按 (会话, request_id) 归属渲染——app.js 的
   syncPlanPanelForActiveSession / reqSession 标记依赖这里的语义。

协议中立打桩: 整体替换 server.api_client（返回中立事件）, 不依赖本机
active provider。用法: .venv/Scripts/python.exe scripts/repro_plan_card_flow.py
"""
import json
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

import server
server.API_TOKEN = ""

from api_client import ApiClient, MessageStopEvent, TextDeltaEvent, ToolUseEvent
from fastapi.testclient import TestClient
from models import Message
from permissions import PermissionMode
from storage import SessionStore
from tools import ToolRegistry
import pathlib
import tempfile
import threading

tmp = pathlib.Path(tempfile.mkdtemp())
server.store = SessionStore(storage_dir=tmp)
server._pending_sessions.clear()
server.app_state._mode = PermissionMode.PLAN   # 会话初值 = 计划模式


class _Scripted(ApiClient):
    """按用户文本区分 A/B: 首次调用回 present_plan 工具块, 批准后
    （历史里已有该工具块）再调回正文。"""
    protocol = "test"

    def __init__(self):
        self.thinking_level = "medium"
        self.calls = []

    def reset_to(self, *a, **kw):
        pass   # 测试桩无连接状态

    def stream(self, system_prompt, messages, thinking_level=None, *,
               model=None, include_tools=True, emit_output=None,
               on_event=None):
        self.calls.append(messages)
        user_text = ""
        asks_plan = False
        for m in messages:
            content = m.content if isinstance(m, Message) else m.get("content")
            role = m.role if isinstance(m, Message) else m.get("role")
            if role == "user":
                if isinstance(content, str):
                    user_text += content
                else:
                    for b in (content or []):
                        user_text += getattr(b, "text", "") or (
                            b.get("text", "") if isinstance(b, dict) else "")
            if role == "assistant" and isinstance(content, list):
                for b in content:
                    name = getattr(b, "name", None) or (
                        b.get("name") if isinstance(b, dict) else None)
                    asks_plan = asks_plan or name == "present_plan"
        who = "A" if "A 任务" in user_text else "B"
        submits = not asks_plan
        print(f"[stream] 会话={who} 提交计划={submits}")
        if submits:
            return [ToolUseEvent(id=f"plan-{who}", name="present_plan",
                                 input=json.dumps({"plan": f"# {who} 的计划\n1. 改 CSS"})),
                    MessageStopEvent()]
        return [TextDeltaEvent(text=f"{who} 开始实施"), MessageStopEvent()]


server.api_client = _Scripted()
inner = ToolRegistry()
inner.register("present_plan", lambda p, wd: "计划已批准")
server.registry = server.EmittingToolRegistry(inner)

client = TestClient(server.app)


def drain_until(ws, pred, limit=400):
    for _ in range(limit):
        m = json.loads(ws.receive_text())
        if pred(m):
            return m
    raise AssertionError("事件窗口内未等到目标事件")


print("\n=== 场景一: 单会话计划审批链路 ===")
with client.websocket_connect("/ws/plan-flow") as ws:
    ws.send_json({"type": "user", "text": "加长滚动条"})
    got_request = None
    while True:
        m = json.loads(ws.receive_text())
        if m["type"] == "permission_request":
            got_request = m
            print("[permission_request] ✓", m["tool_name"], m["request_id"])
            ws.send_json({"type": "permission_response",
                          "request_id": m["request_id"], "approved": True})
        if m["type"] == "tool_result":
            print("[tool_result]", m.get("id"), "denied=", m.get("denied"),
                  "plan_rejected=", m.get("plan_rejected"),
                  repr(m.get("output", ""))[:40])
        if m["type"] in ("turn_done", "error"):
            print("[", m["type"], "]", str(m)[:100])
            break

assert got_request, "计划模式没有发出 permission_request——审批卡无从渲染"
print("结论: 后端链路 OK——present_plan 会发审批请求; 批准后继续实施")

print("\n=== 场景二: 双会话同时挂计划审批（互不阻塞） ===")
with client.websocket_connect("/ws/flow-a") as ws_a, \
        client.websocket_connect("/ws/flow-b") as ws_b:
    ws_a.send_json({"type": "user", "text": "A 任务"})
    ws_b.send_json({"type": "user", "text": "B 任务"})
    req_a = drain_until(ws_a, lambda m: m["type"] == "permission_request")
    req_b = drain_until(ws_b, lambda m: m["type"] == "permission_request")
    print("[A permission_request]", req_a["request_id"])
    print("[B permission_request]", req_b["request_id"])
    assert req_a["request_id"] == req_b["request_id"] == "perm-1", (
        "request_id 每会话独立计数, 两边同为 perm-1 才是真实语义")
    print("    → id 跨会话重复: 前端必须按 (会话, request_id) 定位请求")

    ws_b.send_json({"type": "permission_response",
                    "request_id": req_b["request_id"], "approved": True})
    done_b = drain_until(ws_b, lambda m: m["type"] == "turn_done")
    print("[B turn_done] interrupted=", done_b.get("interrupted"))
    print("[A] 仍未决（无事件到达即为挂起）——现在批 A")
    ws_a.send_json({"type": "permission_response",
                    "request_id": req_a["request_id"], "approved": True})
    done_a = drain_until(ws_a, lambda m: m["type"] == "turn_done")
    print("[A turn_done] interrupted=", done_a.get("interrupted"))

for t in threading.enumerate():
    if t.name.startswith("turn-flow-") and t.is_alive():
        t.join(timeout=15)
print("结论: 双会话并发计划审批互不阻塞——前端按会话归属渲染即可修复卡住")
