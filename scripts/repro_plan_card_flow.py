"""复现: 计划模式下 present_plan 的审批链路是否发出 permission_request。"""
import json
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

import server
server.API_TOKEN = ""

from fastapi.testclient import TestClient
from storage import SessionStore
from permissions import ALLOW_MODE, PermissionMode
import tempfile, pathlib, threading

tmp = pathlib.Path(tempfile.mkdtemp())
server.store = SessionStore(storage_dir=tmp)
server._pending_sessions.clear()
server.app_state._mode = PermissionMode.PLAN   # 会话初值 = 计划模式


class _NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeStream:
    def __init__(self, events):
        self._it = iter(events)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)


class Counter:
    n = 0


def scripted_stream(self, **kwargs):
    Counter.n += 1
    sys_blocks = " ".join(b.get("text", "") for b in (kwargs.get("system") or []))
    print(f"[stream call #{Counter.n}] 含计划段={'Plan Mode' in sys_blocks}")
    if Counter.n == 1:
        # 模型调 present_plan
        return _FakeStream([
            _NS(type="message_start", message=_NS(usage=_NS(
                input_tokens=5, output_tokens=None, cache_creation_input_tokens=None,
                cache_read_input_tokens=None))),
            _NS(type="content_block_start", index=0,
                content_block=_NS(type="tool_use", id="plan-1", name="present_plan")),
            _NS(type="content_block_delta", index=0,
                delta=_NS(type="input_json_delta",
                          partial_json=json.dumps({"plan": "# 计划\n1. 改 CSS"}))),
            _NS(type="content_block_stop", index=0),
            _NS(type="message_delta", delta=_NS(stop_reason="tool_use"),
                usage=_NS(input_tokens=None, output_tokens=9,
                          cache_creation_input_tokens=None, cache_read_input_tokens=None)),
            _NS(type="message_stop"),
        ])
    return _FakeStream([
        _NS(type="message_start", message=_NS(usage=_NS(
            input_tokens=5, output_tokens=None, cache_creation_input_tokens=None,
            cache_read_input_tokens=None))),
        _NS(type="content_block_start", index=0, content_block=_NS(type="text")),
        _NS(type="content_block_delta", index=0,
            delta=_NS(type="text_delta", text="开始实施")),
        _NS(type="content_block_stop", index=0),
        _NS(type="message_delta", delta=_NS(stop_reason="end_turn"),
            usage=_NS(input_tokens=None, output_tokens=3,
                      cache_creation_input_tokens=None, cache_read_input_tokens=None)),
        _NS(type="message_stop"),
    ])


from tools import ToolRegistry
inner = ToolRegistry()
inner.register("present_plan", lambda p, wd: "计划已批准")
server.registry = server.EmittingToolRegistry(inner)
server.api_client.client.messages._real = type("FM", (), {"stream": scripted_stream})()

client = TestClient(server.app)
events = []

with client.websocket_connect("/ws/plan-flow") as ws:
    ws.send_json({"type": "set_permission_mode", "mode": "plan"})
    # 吃掉 mode_changed 回执
    while True:
        m = json.loads(ws.receive_text())
        if m["type"] == "mode_changed":
            print("[mode_changed]", m["permission_mode"])
            break
    ws.send_json({"type": "user", "text": "加长滚动条"})
    got_request = None
    while True:
        m = json.loads(ws.receive_text())
        events.append(m)
        if m["type"] == "permission_request":
            got_request = m
            print("[permission_request] ✓", m["tool_name"], m["request_id"])
            # 批准
            ws.send_json({"type": "permission_response",
                          "request_id": m["request_id"], "approved": True})
        if m["type"] == "tool_result":
            print("[tool_result]", m.get("id"), "denied=", m.get("denied"),
                  "plan_rejected=", m.get("plan_rejected"), repr(m.get("output", ""))[:60])
        if m["type"] in ("turn_done", "error"):
            print("[", m["type"], "]", str(m)[:100])
            break

assert got_request, "计划模式没有发出 permission_request——审批卡无从渲染"
print("\n结论: 后端链路 OK——present_plan 会发审批请求; 批准后继续实施")
