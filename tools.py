from typing import Callable, Self
from pydantic import BaseModel
import subprocess
import json


class ToolRegistry(BaseModel):
    _handlers: dict[str, Callable]

    def register(self, name: str, handler: Callable) -> Self:
        if name in self._handlers:
            raise ValueError(f"Tool already registered: {name}")
        self._handlers[name] = handler
        return self

    def execute(self, name: str, tool_input_json: str) -> str:
        if name not in self._handlers:
            return f"Unknown tool: {name}"

        try:
            params = json.loads(tool_input_json) if tool_input_json else {}
        except json.JSONDecodeError:
            return "Invalid JSON input: {tool_input_json}"

        try:
            result = self._handlers[name](params)
            return result
        except Exception as e:
            return f"Tool execution error: {e}"

def bash_tool(input_json:str):
    data = json.loads(input_json)
    cmd = data['command']
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout
        if result.stderr:
            output += f'\nSTDERR: {result.stderr}'
        return output
    except subprocess.TimeoutExpired:
        return 'ERROR: timeout for 30s'

def read_tool(input_json:str) -> str:
    data = json.loads(input_json) if input_json else {}
    path = data.get('path', '')
    try:
        with open(path, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        return f'ERROR: file not found {path}'
    return content

def write_tool(input_json:str) -> str:
    data = json.loads(input_json)
    path = data.get('path', '')
    content = data.get('content', '')
    try:
        with open(path, 'w') as f:
            f.write(content)
    except FileNotFoundError:
        return f'ERROR: directory not found {path}'
    return f'OK: wrote to {path}'
