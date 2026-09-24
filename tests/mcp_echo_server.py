"""MCP 测试用 stdio 服务器: 由 test_mcp_client.py 以子进程拉起。

两个工具:
- echo(text)      → 原样回显, 验证参数传递与返回
- fail(reason)    → isError=true 返回, 验证错误路径
"""

import sys

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

server = MCPServer("x-code-mcp-test")


@server.tool(description="Echo the given text back.")
def echo(text: str) -> str:
    return f"echo: {text}"


@server.tool(description="Always fail with the given reason.")
def fail(reason: str) -> str:
    # SDK 约定: 预期失败抛 ToolError, 消息原样进 isError 结果的 content
    raise ToolError(reason)


if __name__ == "__main__":
    server.run(transport="stdio")
