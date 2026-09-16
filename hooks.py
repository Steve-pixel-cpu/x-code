import json
import os
import subprocess
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from config import RuntimeConfig


class HookEvent(Enum):
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"

class HookOutcome(Enum):
    ALLOW = "allow"
    DENY = "deny"
    WARN = "warn"

class HookResult(BaseModel):
    denied: bool
    messages: list[str] = Field(default_factory=list)

    @classmethod
    def allow(cls,messages: list[str] | None = None) -> "HookResult":
        return cls(denied= False, messages= messages or [])

    @classmethod
    def deny(cls, messages: list[str] | None = None) -> "HookResult":
        return cls(denied=True, messages=messages or [])


class HookRunner:

    def __init__(self, pre_tool_use: Optional[list[str]] = None, post_tool_use: Optional[list[str]] = None):
        self.pre_tool_use = pre_tool_use or []
        self.post_tool_use = post_tool_use or []

    @classmethod
    def from_config(cls, config: RuntimeConfig) -> "HookRunner":
        """从 RuntimeConfig 加载。源码: hooks.rs:61-63"""
        return cls(
            pre_tool_use=config.hooks_pre(),
            post_tool_use=config.hooks_post(),
        )

    def run_pre_tool_use(self, tool_name: str, tool_input: str) -> HookResult:
        return self._run_commands(
            event=HookEvent.PRE_TOOL_USE,
            commands = self.pre_tool_use,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=None,
            is_error=False,
        )

    def run_post_tool_use(self, tool_name: str, tool_input: str, tool_output: str, is_error: bool) -> HookResult:
        return self._run_commands(
            event=HookEvent.POST_TOOL_USE,
            commands=self.post_tool_use,
            tool_name=tool_name,
            tool_input = tool_input,
            tool_output = tool_output,
            is_error= is_error
        )

    def _run_command(
        self,
        command: str,
        event: HookEvent,
        tool_name: str,
        tool_input: str,
        tool_output: Optional[str],
        is_error: Optional[bool],
    ) -> tuple[HookOutcome, str]:
        # 构造传给 hook 的 JSON 数据（通过 stdin 传入）
        payload = json.dumps({
            "hook_event_name": event.value,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_output": tool_output,
            "tool_result_is_error": is_error,
        })
        # 设置环境变量（hook 脚本可以直接读取）
        env = os.environ.copy()
        env["HOOK_EVENT"] = event.value
        env["HOOK_TOOL_NAME"] = tool_name
        env["HOOK_TOOL_INPUT"] = tool_input
        env["HOOK_TOOL_IS_ERROR"] = "1" if is_error else "0"
        if tool_output is not None:
            env["HOOK_TOOL_OUTPUT"] = tool_output

        try:
            result = subprocess.run(
                ["sh", "-lc", command],  # 用 shell 执行命令
                input=payload,  # 通过 stdin 传入 JSON
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            stdout = result.stdout.strip()
            stderr = result.stderr.strip()
            exit_code = result.returncode
            if exit_code == 0:
                # 退出码 0 → 允许
                return HookOutcome.ALLOW, stdout

            elif exit_code == 2:
                # 退出码 2 → 拒绝
                return HookOutcome.DENY, stdout

            else:
                # 其他退出码 → 警告（允许执行，但记录警告）
                warning = f"Hook `{command}` exited with status {exit_code}; allowing tool execution to continue"
                if stdout:
                    warning += f": {stdout}"
                elif stderr:
                    warning += f": {stderr}"

                return HookOutcome.WARN, warning

        except subprocess.TimeoutExpired:
            return HookOutcome.WARN, f"Hook `{command}` timed out"
        except Exception as e:
            return HookOutcome.WARN, f"Hook `{command}` failed: {e}"

    def _run_commands(
        self,
        event: HookEvent,
        commands: list[str],
        tool_name: str,
        tool_input: str,
        tool_output: Optional[str],
        is_error: Optional[bool],
    ) -> HookResult:
        if not commands:
            return HookResult.allow()

        messages = []
        for command in commands:
            outcome, message = self._run_command(
                command=command,
                event=event,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_output=tool_output,
                is_error=is_error,
            )
            if outcome == HookOutcome.ALLOW:
                if message:
                    messages.append(message)
            elif outcome == HookOutcome.DENY:
                deny_message = message or f"{event.value} hook denied tool `{tool_name}`"
                messages.append(deny_message)
                return HookResult.deny(messages=messages)
            elif outcome == HookOutcome.WARN:
                messages.append(message)

        return HookResult.allow(messages=messages)


