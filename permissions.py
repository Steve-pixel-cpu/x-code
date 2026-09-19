
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
    PLAN = 1
    WORKSPACE_WRITE = 2
    DANGER_FULL_ACCESS = 3
    PROMPT = 4
    ALLOW = 5

    def as_str(self) -> str:
        return {
            self.PLAN: "plan",
            self.WORKSPACE_WRITE: "workspace-write",
            self.DANGER_FULL_ACCESS: "danger-full-access",
            self.PROMPT: "prompt",
            self.ALLOW: "allow",
        }[self]

# --- 模式名 <-> 枚举: /mode 命令的参数解析与显示用 ---
PLAN_MODE = PermissionMode.PLAN
# 兼容别名: 旧代码/旧配置里的只读模式 = 计划模式
READ_ONLY_MODE = PLAN_MODE
WORKSPACE_WRITE_MODE = PermissionMode.WORKSPACE_WRITE
DANGER_FULL_ACCESS_MODE = PermissionMode.DANGER_FULL_ACCESS
PROMPT_MODE = PermissionMode.PROMPT
ALLOW_MODE = PermissionMode.ALLOW

MODE_TO_NAME = {
    PLAN_MODE: "plan",
    WORKSPACE_WRITE_MODE: "workspace-write",
    DANGER_FULL_ACCESS_MODE: "danger-full-access",
    PROMPT_MODE: "prompt",
    ALLOW_MODE: "allow",
}

NAME_TO_MODE = {name: mode for mode, name in MODE_TO_NAME.items()}

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
    # 镜像方（如 Web 端）配对工具卡用: 授权询问/拒绝时知道结果该落到哪张卡
    tool_use_id: Optional[str] = None

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

    def set_mode(self, mode: PermissionMode) -> Self:
        """切换当前权限模式（运行时可随时调用，如 /mode 命令）。"""
        self._active_mode = mode
        return self

    def authorize(self, tool_name: str, input: str, prompter: Optional[PermissionPrompter] = None,
                  tool_use_id: Optional[str] = None) -> PermissionResult:
        current = self.active_mode
        required = self.required_mode_for(tool_name)

        # 快速路径: Allow 模式跳过一切; 其余模式仅在"当前权限足够"时放行。
        # PROMPT(4) 数值上 >= 大多数 required, 但它的语义是"每次都问",
        # 不是"权限更高"——必须赶在 >= 比较之前拦截, 否则 prompt 模式
        # 形同虚设(所有工具默认 required=DANGER_FULL_ACCESS < 4, 全被放行)。
        if current == PermissionMode.ALLOW:
            return PermissionResult(decision= PermissionDecision.ALLOW, reason= "")
        if current == PermissionMode.PROMPT:
            request = PermissionRequest(tool_name = tool_name,
                                        input = input,
                                        current_mode= current,
                                        required_mode = required,
                                        tool_use_id = tool_use_id )
            if prompter is not None:
                return prompter.decide(request)
            return PermissionResult(decision= PermissionDecision.DENY,
                                    reason= f"tool '{tool_name}' requires approval "
                                    f"(prompt mode) but no prompter is available")
        if current >= required:
            return PermissionResult(decision= PermissionDecision.ALLOW, reason= "")

        request = PermissionRequest(tool_name = tool_name,
                                    input = input,
                                    current_mode= current,
                                    required_mode = required,
                                    tool_use_id = tool_use_id )


        # "可升级弹问"分支（相邻档位）: 当前档差一档且目标可议时交给
        # prompter 裁决——workspace-write→DANGER(危险命令单次放行) 与
        # plan→WORKSPACE_WRITE(present_plan 计划审批/write_file 单次放行)。
        # 批准 present_plan 的同时把模式升级为 workspace-write 由调用方
        # （server 的 on_plan_approved 回调 / CLI 的 /mode）负责, 授权层
        # 只管这一次的决定。
        prompter_decides = (
            prompter is not None
            and (current == PermissionMode.PLAN
                 and required == PermissionMode.WORKSPACE_WRITE)
        ) or (
            prompter is not None
            and current == PermissionMode.WORKSPACE_WRITE
            and required == PermissionMode.DANGER_FULL_ACCESS
        )
        if prompter_decides:
            return prompter.decide(request)
        if (current == PermissionMode.PLAN
                and required == PermissionMode.WORKSPACE_WRITE):
            return PermissionResult(decision= PermissionDecision.DENY,
                                    reason= f"tool '{tool_name}' requires approval to escalate "
                                    f"from {current.as_str()} to {required.as_str()}")
        if current == PermissionMode.WORKSPACE_WRITE and required == PermissionMode.DANGER_FULL_ACCESS:
            return PermissionResult(decision= PermissionDecision.DENY,
                                    reason= f"tool '{tool_name}' requires approval to escalate "
                                    f"from {current.as_str()} to {required.as_str()}")

        # 其他情况: 权限不足，直接拒绝
        # 计划模式下(典型: bash 默认 DANGER 档, PLAN→DANGER 跨两档)附带
        # 教学指引——拒绝本身是对模型的一次纠正, 否则它不知道自己在计划
        # 模式, 只会反复换命令撞墙
        plan_hint = (" Plan mode is active: do not execute or modify anything. "
                     "Research with read_file, then call present_plan with "
                     "your implementation plan.")
        return PermissionResult(
                    decision= PermissionDecision.DENY,
                    reason = f"tool '{tool_name}' requires {required.as_str()} " 
                    f"permission; current mode is {current.as_str()}" + (plan_hint if current == PermissionMode.PLAN else "")
                )

