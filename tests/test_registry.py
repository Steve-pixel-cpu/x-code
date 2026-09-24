"""ToolRegistry.unregister: MCP 热重载换绑依赖的注销能力。"""

import pytest

from runtime import ToolError
from tools import ToolRegistry


def test_unregister_removes_handler():
    r = ToolRegistry().register(name="t", handler=lambda p, w: "ok")
    r.unregister("t")
    with pytest.raises(ToolError, match="Unknown tool"):
        r.execute("t", "{}")


def test_unregister_missing_is_silent():
    """不存在的工具静默——换绑路径先清后挂, 幂等更省心。"""
    ToolRegistry().unregister("never-registered")


def test_unregister_then_reregister():
    r = ToolRegistry().register(name="t", handler=lambda p, w: "old")
    r.unregister("t").register(name="t", handler=lambda p, w: "new")
    assert r.execute("t", "{}") == "new"


def test_unregister_chainable():
    r = ToolRegistry()
    assert r.unregister("x") is r
