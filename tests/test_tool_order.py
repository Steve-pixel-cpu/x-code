"""工具列表排序不变式: 内置工具稳定前缀 + mcp__* 纯后缀。

工具清单是请求前缀的一部分——它的顺序必须与安装/连接时序无关, prompt
cache 才保得住（中段变动打穿该点之后全部缓存, 最贵 12 倍）。这里钉住:

1. skill_read 插在内置与 MCP 的边界上, 不随安装时序漂移（sync_skill_tools）
2. MCP 工具永远是纯后缀, 换绑/热重载不打乱非 mcp 项的相对顺序
   （sync_tools_list）
3. 两个同步操作任意次组合, 不变式不破

运行方式（在 x-code 目录下）:
    uv run pytest tests/test_tool_order.py -v
"""

import pytest

from mcp_client import McpManager
from skills import SKILL_TOOL_NAME, sync_skill_tools


def _builtin(name):
    return {"name": name}


def _is_mcp(spec):
    return spec.get("name", "").startswith("mcp__")


def _assert_invariant(tools):
    """不变式: 非 mcp 项构成连续前缀, mcp__* 只出现在尾部。"""
    seen_mcp = False
    for t in tools:
        if _is_mcp(t):
            seen_mcp = True
        else:
            assert not seen_mcp, f"mcp 之后混入内置工具: {t.get('name')}"


# ------------------------------------------------------------
# sync_skill_tools — skill_read 的位置与 MCP 有无/时序无关
# ------------------------------------------------------------

def test_skill_read_appends_without_mcp():
    tools = [_builtin("bash"), _builtin("grep")]
    sync_skill_tools(tools, [object()])
    assert [t["name"] for t in tools] == ["bash", "grep", SKILL_TOOL_NAME]


def test_skill_read_lands_before_mcp_block():
    tools = [_builtin("bash"), _builtin("mcp__srv__tool1"), _builtin("mcp__srv__tool2")]
    sync_skill_tools(tools, [object()])
    assert [t["name"] for t in tools] == [
        "bash", SKILL_TOOL_NAME, "mcp__srv__tool1", "mcp__srv__tool2"]


def test_skill_read_insert_idempotent():
    tools = [_builtin("bash"), _builtin("mcp__srv__t")]
    sync_skill_tools(tools, [object()])
    sync_skill_tools(tools, [object()])
    assert sum(1 for t in tools if t["name"] == SKILL_TOOL_NAME) == 1


def test_skill_read_removal_keeps_rest():
    tools = [_builtin("bash"), _builtin(SKILL_TOOL_NAME), _builtin("mcp__srv__t")]
    sync_skill_tools(tools, [])
    assert [t["name"] for t in tools] == ["bash", "mcp__srv__t"]


# ------------------------------------------------------------
# sync_tools_list — MCP 换绑不打乱非 mcp 相对顺序, 永远纯后缀
# ------------------------------------------------------------

def _manager_with_specs(specs, monkeypatch):
    manager = McpManager()
    monkeypatch.setattr(manager, "tool_specs", lambda: list(specs))
    return manager


def test_mcp_sync_appends_suffix_and_keeps_order(monkeypatch):
    tools = [_builtin("bash"), _builtin("read_file"), _builtin("mcp__old__t")]
    manager = _manager_with_specs([_builtin("mcp__new__a"),
                                   _builtin("mcp__new__b")], monkeypatch)
    manager.sync_tools_list(tools)
    assert [t["name"] for t in tools] == [
        "bash", "read_file", "mcp__new__a", "mcp__new__b"]
    _assert_invariant(tools)


def test_mcp_reload_restores_invariant_from_legacy_order(monkeypatch):
    """历史遗留序（skill_read 曾被 append 到 mcp 之后）: 一次重载即归位。"""
    tools = [_builtin("bash"), _builtin("mcp__srv__t"), _builtin(SKILL_TOOL_NAME)]
    manager = _manager_with_specs([_builtin("mcp__srv__t")], monkeypatch)
    manager.sync_tools_list(tools)
    _assert_invariant(tools)
    assert tools[-2]["name"] == SKILL_TOOL_NAME   # 又回到 mcp 边界之前


def test_skill_and_mcp_sync_composition_stable(monkeypatch):
    """热装技能 → MCP 热重载 → 再热装技能: 顺序只动后缀, 前缀不动。"""
    tools = [_builtin("bash"), _builtin("mcp__srv__t")]
    sync_skill_tools(tools, [object()])            # 热装技能
    before_prefix = [t["name"] for t in tools if not _is_mcp(t)]
    manager = _manager_with_specs([_builtin("mcp__srv__t")], monkeypatch)
    manager.sync_tools_list(tools)                 # MCP 热重载
    sync_skill_tools(tools, [object()])            # 重复热装(幂等)
    after_prefix = [t["name"] for t in tools if not _is_mcp(t)]
    assert before_prefix == after_prefix
    _assert_invariant(tools)
