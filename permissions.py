
from enum import IntEnum, Enum
from typing import Protocol, Dict, Optional

from pydantic import BaseModel
from typing import Self

"""权限模式层级 — 源码 permissions.rs:4-10

  从最严格到最宽松:
  - ReadOnly: 只能读，不能写任何东西
  - WorkspaceWrite: 可以写工作目录内的文件
  - DangerFullAccess: 可以做任何事（包括 rm -rf /）
  - Prompt: 总是询问用户
  - Allow: 跳过所有检查（最危险）

  为什么 Prompt 比 DangerFullAccess 更"高"？
  因为 Prompt 模式的意思不是"更有权限"，而是
  "这个模式下，需要升级的操作会触发用户提示"。
  在源码中 Prompt 模式会拦截所有需要确认的操作。
  """
class PermissionMode(IntEnum):
    READ_ONLY = 1
    WORKSPACE_WRITE = 2
    DANGER_FULL_ACCESS = 3
    PROMPT = 4
    ALLOW = 5

    def as_str(self) -> str:
        return {
            self.READ_ONLY: "read-only",
            self.WORKSPACE_WRITE: "workspace-write",
            self.DANGER_FULL_ACCESS: "danger-full-access",
            self.PROMPT: "prompt",
            self.ALLOW: "allow",
        }[self]

class PermissionDecision(Enum):
    ALLOW = "allow"
    DENY = "deny"

class PermissionResult(BaseModel):
    decision: PermissionDecision
    reason: str


class PermissionRequest(BaseModel):
    tool_name: str
    input: str
    current_mode: PermissionMode
    required_mode: PermissionMode

# Prompter 接口 — 用 Protocol 不用 ABC
# Protocol 不需要继承，只要有 decide() 方法就行（鸭子类型）
class PermissionPrompter(Protocol):

    def decide(self, request: PermissionRequest) -> PermissionResult:
        ...


class PermissionPolicy:
    def __init__(self, active_mode: PermissionMode):
        self._active_mode = active_mode
        self._tool_requirements: Dict[str, PermissionMode] = {}

    def with_tool_requirement(self,tool_name: str, required_mode: PermissionMode) -> Self:
        self._tool_requirements[tool_name] = required_mode
        return self

    def required_mode_for(self, tool_name: str) -> PermissionMode:
        return self._tool_requirements.get(
            tool_name, PermissionMode.DANGER_FULL_ACCESS
        )

    @property
    def active_mode(self) -> PermissionMode:
        return self._active_mode

    def authorize(self, tool_name: str, input: str, prompter: Optional[PermissionPrompter] = None,) -> PermissionResult:
        current = self.active_mode
        required = self.required_mode_for(tool_name)

        # 快速路径: Allow 模式或当前权限足够
        if current == PermissionMode.ALLOW or current >= required:
            return PermissionResult(decision= PermissionDecision.ALLOW, reason= "")

        request = PermissionRequest(tool_name = tool_name,
                                    input = input,
                                    current_mode= current,
                                    required_mode = required )


        if current == PermissionMode.PROMPT or (current == PermissionMode.WORKSPACE_WRITE and required == PermissionMode.DANGER_FULL_ACCESS):
            if prompter is not None:
                return prompter.decide(request)
            else:
                return PermissionResult(decision= PermissionDecision.DENY,
                                        reason= f"tool '{tool_name}' requires approval to escalate "
                                        f"from {current.as_str()} to {required.as_str()}")

        # 其他情况: 权限不足，直接拒绝
        return PermissionResult(
                    decision= PermissionDecision.DENY,
                    reason = f"tool '{tool_name}' requires {required.as_str()} " 
                    f"permission; current mode is {current.as_str()}"
                )

