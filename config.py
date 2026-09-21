import json
import os
from enum import Enum
from pathlib import Path
from typing import Literal, Optional, Any

from pydantic import BaseModel, Field

# 应用名与用户级目录的唯一来源: ~/.x-code（目录名跟 APP_NAME 走, 改名只动这一处）
APP_NAME = "x-code"
USER_DIR = Path.home() / ("." + APP_NAME)
SETTINGS_FILE = USER_DIR / "settings.json"


class ConfigSource(Enum):
    USER = "user"         # 用户全局 (~/.x-code/settings.json)
    PROJECT = "project"   # 项目级别 (.claude/settings.json)
    LOCAL = "local"       # 本地个人 (.claude/settings.local.json)


class ConfigEntry(BaseModel):
    """一个配置文件的位置和来源"""
    source: ConfigSource
    path: Path
    model_config = {"Frozen": True, "arbitrary_types_allowed": True}


class ConfigError(Exception):
    def __init__(self, message: str, kind: str = "parse"):
        self.kind = kind  # "io" or "parse"
        super().__init__(message)


# 模型上下文窗口默认值: GLM-5.3 官方端点为 1M tokens。第三方中转可能砍到
# 128k/200k——配置 contextWindow 或 env CLAUDE_CONTEXT_WINDOW 调小,
# auto-compact 阈值随之收缩（未显式配置时 = 窗口 × COMPACT_THRESHOLD_RATIO）。
DEFAULT_CONTEXT_WINDOW = 1_000_000
COMPACT_THRESHOLD_RATIO = 0.75


def deep_merge(target: dict, source: dict) -> dict:
    result = dict(target)

    for key, value in source.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            # 两边都是字典 → 递归合并
            result[key] = deep_merge(result[key], value)
        else:
            # 否则直接覆盖
            result[key] = value
    return result

class RuntimeFeatureConfig(BaseModel):
    hooks_pre_tool_use: list[str] = Field(default_factory=list)
    hooks_post_tool_use: list[str] = Field(default_factory=list)
    model: Optional[str] = None
    permission_mode: Optional[str] = None
    timeout: int = 30
    # 默认与 runtime.DEFAULT_MAX_ITERATIONS 对齐。10 是历史占位值，
    # 接线前从未生效——真放出来正常任务一轮就会被掐断
    max_iterations: int = 128
    # 模型上下文窗口（GLM-5.3 官方 1M; 第三方中转按实际窗口配）
    context_window: int = DEFAULT_CONTEXT_WINDOW
    # auto-compact 触发阈值。必须明显低于模型真实上下文窗口（还要给
    # max_tokens 留位）, 否则永远轮不到它触发——只会等 API 报 context
    # length。未显式配置时 = context_window × COMPACT_THRESHOLD_RATIO,
    # 在 parse_feature_config 里推导。
    token_budget: int = int(DEFAULT_CONTEXT_WINDOW * COMPACT_THRESHOLD_RATIO)
    # 默认 medium: 每轮思考预算 8192。high(16384) 下单轮思考的流式墙钟
    # 就有 50~60s, 且 GLM 系强制思考、思考内容不进历史——每轮循环都全额
    # 重付, 是长会话"思考很久不见动静"观感的大头。深任务按项目配
    # thinkingLevel 或 CLAUDE_THINKING_LEVEL 调高（会话内可随时切档）。
    thinking_level: str = "medium"
    # 单轮输出预算（含思考）。与 runtime.DEFAULT_TURN_OUTPUT_BUDGET 对齐:
    # 思考型模型一次大思考烧 8k~16k, 预算太紧会把轮次掐死在动手之前。
    turn_token_budget: int = 262_144

class RuntimeConfig(BaseModel):
    merged: dict = Field(default_factory=dict)
    loaded_entries: list[ConfigEntry] = Field(default_factory=list)
    feature_config: RuntimeFeatureConfig = Field(default_factory=RuntimeFeatureConfig)

    model_config = {"arbitrary_types_allowed": True}

    def get(self, key: str) -> Optional[Any]:
        return self.merged.get(key)



    def hooks_pre(self) -> list[str]:
        return self.feature_config.hooks_pre_tool_use

    def hooks_post(self) -> list[str]:
        return self.feature_config.hooks_post_tool_use

    def model(self) -> Optional[str]:
        return self.feature_config.model

    def thinking_level(self) -> str:
        return self.feature_config.thinking_level

    def permission_mode(self) -> Optional[str]:
        return self.feature_config.permission_mode

    def timeout(self) -> int:
        return self.feature_config.timeout

    def token_budget(self) -> int:
        return self.feature_config.token_budget

    def context_window(self) -> int:
        return self.feature_config.context_window

    def max_iterations(self) -> int:
        return self.feature_config.max_iterations

    def turn_token_budget(self) -> int:
        return self.feature_config.turn_token_budget

    @staticmethod
    def empty() -> "RuntimeConfig":
        """空配置 — 用于测试或默认场景。源码: config.rs:251-257"""
        return RuntimeConfig()

class ConfigLoader:
    """
        参数:
            cwd: 当前工作目录（项目根目录）
            config_home: 用户配置目录（x-code 使用 ~/.x-code）
    """
    def __init__(self, cwd: Path, config_home: Path):
        self.cwd = cwd
        self.config_home = config_home



    def discover(self)  -> list[ConfigEntry]:

        return [
            # 用户全局配置（两个位置）
            ConfigEntry(source = ConfigSource.USER,path= self.config_home /".claude.json"),
            ConfigEntry(source =ConfigSource.USER, path=self.config_home / "settings.json"),
            # 项目配置（两个位置）
            ConfigEntry(source =ConfigSource.PROJECT, path=self.cwd / ".claude.json"),
            ConfigEntry(source =ConfigSource.PROJECT, path=self.cwd / ".claude"/ "settings.json"),
            # 本地配置（一个位置）
            ConfigEntry(source =ConfigSource.LOCAL, path=self.cwd / ".claude"/ "settings.local.json"),
        ]

    def parse_feature_config(self, merged: dict) -> RuntimeFeatureConfig:
        hooks = merged.get("hooks", {})
        if not isinstance(hooks, dict):
            raise ConfigError("hooks: expected JSON object", kind="parse")

        pre = hooks.get("PreToolUse", [])
        post = hooks.get("PostToolUse", [])
        if not isinstance(pre, list) or not isinstance(post, list):
            raise ConfigError("hooks.PreToolUse/PostToolUse: must be arrays", kind="parse")

        # permission_mode: CC 支持多种别名 (config.rs:511-518)
        raw_mode = merged.get("permissionMode")
        permission_mode = None
        if isinstance(raw_mode, str):
            mode_map = {
                # plan 是正名; read-only/default 是旧写法, 归一为 plan
                "default": "plan", "plan": "plan", "read-only": "plan",
                "acceptEdits": "workspace-write", "auto": "workspace-write",
                "workspace-write": "workspace-write",
                "dontAsk": "danger-full-access", "danger-full-access": "danger-full-access",
            }
            if raw_mode not in mode_map:
                raise ConfigError(f"permissionMode: unsupported mode '{raw_mode}'", kind="parse")
            permission_mode = mode_map[raw_mode]

        # thinking_level: 思考深浅档位，budget 映射见 api_client.THINKING_LEVEL_TO_BUDGET
        raw_level = merged.get("thinkingLevel", "medium")
        if not isinstance(raw_level, str) or raw_level.strip().lower() not in (
            "low", "medium", "high", "max",
        ):
            raise ConfigError(f"thinkingLevel: unsupported level '{raw_level}'", kind="parse")

        # context_window: 模型真实上下文窗口, 决定 auto-compact 阈值的
        # 推导基数。tokenBudget 未显式配置时 = 窗口 × 0.75; 显式配置仍覆盖。
        context_window = merged.get("contextWindow", DEFAULT_CONTEXT_WINDOW)
        if not isinstance(context_window, int) or context_window <= 0:
            raise ConfigError(
                f"contextWindow: expected positive integer, got {context_window!r}",
                kind="parse",
            )
        raw_budget = merged.get("tokenBudget")
        if raw_budget is not None and (
                not isinstance(raw_budget, int) or raw_budget <= 0):
            raise ConfigError(
                f"tokenBudget: expected positive integer, got {raw_budget!r}",
                kind="parse",
            )
        token_budget = (raw_budget if raw_budget is not None
                        else int(context_window * COMPACT_THRESHOLD_RATIO))

        return RuntimeFeatureConfig(
            hooks_pre_tool_use=pre,
            hooks_post_tool_use=post,
            model=merged.get("model"),
            permission_mode=permission_mode,
            timeout=merged.get("timeout", 30),
            max_iterations=merged.get("maxIterations", 128),
            context_window=context_window,
            token_budget=token_budget,
            thinking_level=raw_level.strip().lower(),
            turn_token_budget=merged.get("turnTokenBudget", 262_144),
        )

    @staticmethod
    def _read_json(path: Path) -> Optional[dict]:
        """读取 JSON 文件；空文件返回 {}，不存在或非法返回 None"""

        if not path.exists():
            return None

        is_legacy = path.name == ".claude.json"

        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise ConfigError(f"{path}: {e}", kind="io") from e

        # 空文件或纯空白 → 视为空配置
        if not text.strip():
            return {}

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            if is_legacy:
                return None

            raise ConfigError(f"{path}: {e}", kind="parse") from e

        if not isinstance(data, dict):
            if is_legacy:
                return None
            raise ConfigError(f"{path}: top-level value must be a JSON object", kind="parse")

        return data

    @staticmethod
    def _apply_env_overrides(merged: dict) -> None:
        env_map = {
            "ANTHROPIC_API_KEY": "api_key",
            "CLAUDE_MODEL": "model",
            "CLAUDE_TIMEOUT": ("timeout", int),
            "CLAUDE_MAX_ITERATIONS": ("maxIterations", int),
            "CLAUDE_CONTEXT_WINDOW": ("contextWindow", int),
            "CLAUDE_TOKEN_BUDGET": ("tokenBudget", int),
            "CLAUDE_TURN_TOKEN_BUDGET": ("turnTokenBudget", int),
            "CLAUDE_THINKING_LEVEL": "thinkingLevel",
        }

        for key, target in env_map.items():
            value = os.getenv(key)
            if value is None:
                continue
            if isinstance(target, tuple):
                config_key, converter = target
                try:
                    merged[config_key] = converter(value)
                except ValueError as e:
                    raise ConfigError(
                        f"env {key}={value!r}: cannot convert to {converter.__name__}",
                        kind="parse",
                    )
            else:
                merged[target] = value

    def load(self) -> RuntimeConfig:
        """
        加载并合并所有配置。

            算法：
            1. 遍历所有可能的配置文件路径
            2. 跳过不存在的文件
            3. 读取存在的文件（JSON）
            4. 按优先级深度合并

        """
        merged: dict[str, Any] = {}
        loaded_entries: list[ConfigEntry] = []

        for entry in self.discover():
            content = self._read_json(entry.path)
            if content is None:
                continue
            merged = deep_merge(merged, content)
            loaded_entries.append(entry)

        self._apply_env_overrides(merged)

        # ★ Eager Feature Parsing — 加载完立刻解析
        feature_config = self.parse_feature_config(merged)

        return RuntimeConfig(
            merged = merged,
            loaded_entries = loaded_entries,
            feature_config= feature_config
        )







# ============================================================================
# x-code 用户设置: SETTINGS_FILE（~/.x-code/settings.json）
# 供应商配置的读写归口在此（providers / activeProvider 是其中的普通 key,
# 其余 key 原样保留——以后的用户级设置也放这个文件, 不再另起文件名）
# ============================================================================
def load_providers() -> dict:
    """读供应商配置; 无文件/损坏/缺 key = 未配置空态（不写盘,
    由前端初始化页引导填写）。没有 .env 之类的兜底来源。"""
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"active": {}, "providers": []}
    if not isinstance(data, dict) or not isinstance(data.get("providers"), list):
        return {"active": {}, "providers": []}
    active = data.get("activeProvider")
    return {"active": active if isinstance(active, dict) else {},
            "providers": data["providers"]}


def save_providers(cfg: dict) -> None:
    """写供应商配置: 读-改-写, 文件里其他 key 原样保留。"""
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data["providers"] = cfg.get("providers", [])
    data["activeProvider"] = cfg.get("active") or {}
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
