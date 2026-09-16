import platform
from typing import Callable, Self
from pydantic import BaseModel
import subprocess
import json

from runtime import ToolError


class ToolRegistry():
    def __init__(self):
        self._handlers = {}


    def register(self, name: str, handler: Callable) -> Self:
        if name in self._handlers:
            raise ValueError(f"Tool already registered: {name}")
        self._handlers[name] = handler
        return self

    def execute(self, name: str, tool_input_json: str) -> str:
        if name not in self._handlers:
            raise ToolError(f"Unknown tool: {name}")

        try:
            params = json.loads(tool_input_json) if tool_input_json else {}
        except json.JSONDecodeError:
            raise ToolError(f"Invalid JSON input: {tool_input_json}")

        try:
            result = self._handlers[name](params)
            return result
        except Exception as e:
            raise ToolError(f"Tool execution error: {e}")

def bash_tool(params:dict):
    cmd = params.get('command',"")
    # Windows 上没有 sh, 交给 PowerShell 执行,
    # 常用命令 (ls/cat/rm 等) 在 PowerShell 里有别名, 大多可用
    if platform.system() == "Windows":
        return powershell_tool(params)
    try:
        result = subprocess.run(
            ["sh", "-lc", cmd],  # 用 shell 执行命令
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace"
        )
        output = result.stdout
        if result.stderr:
            output += f'\nSTDERR: {result.stderr}'
        return output
    except subprocess.TimeoutExpired:
        return 'ERROR: timeout for 30s'

def powershell_tool(params: dict) -> str:
    cmd = params.get('command', "")
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                # 强制输出为 UTF-8，避免中文 Windows 下 GBK 乱码
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
                + cmd,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace"
        )
        output = result.stdout
        if result.stderr:
            output += f'\nSTDERR: {result.stderr}'
        return output
    except subprocess.TimeoutExpired:
        return 'ERROR: timeout for 30s'

def read_tool(params:dict) -> str:
    path = params.get('path', '')
    try:
        with open(path, 'r', encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return f'ERROR: file not found {path}'
    return content

def write_tool(params:dict) -> str:
    path = params.get('path', '')
    content = params.get('content', '')
    try:
        with open(path, 'w', encoding="utf-8") as f:
            f.write(content)
    except FileNotFoundError:
        return f'ERROR: directory not found {path}'
    return f'OK: wrote to {path}'
