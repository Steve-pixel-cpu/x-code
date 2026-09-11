import json
import os
from enum import Enum
from pathlib import Path
from typing import Literal, Optional, Any

from pydantic import BaseModel, Field


class ConfigSource(Enum):
    USER = "user"         # 用户全局 (~/.claude/settings.json)
    PROJECT = "project"   # 项目级别 (.claude/settings.json)
    LOCAL = "local"       # 本地个人 (.claude/settings.local.json)


class ConfigEntry(BaseModel):
    """一个配置文件的位置和来源"""
    source: ConfigSource
    path: Path
    model_config = {"Frozen": True}

class ConfigError(Exception):
    def __init__(self, message: str, kind: str = "parse"):
        self.kind = kind  # "io" or "parse"
        super().__init__(message)


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
    max_iterations: int = 10
    token_budget: int = 200_000

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

    def permission_mode(self) -> Optional[str]:
        return self.feature_config.permission_mode

    def timeout(self) -> int:
        return self.feature_config.timeout

    def token_budget(self) -> int:
        return self.feature_config.token_budget

    @staticmethod
    def empty() -> "RuntimeConfig":
        """空配置 — 用于测试或默认场景。源码: config.rs:251-257"""
        return RuntimeConfig()

class ConfigLoader:
    """
        参数:
            cwd: 当前工作目录（项目根目录）
            config_home: 用户配置目录（通常是 ~/.claude）
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
                "default": "read-only", "plan": "read-only", "read-only": "read-only",
                "acceptEdits": "workspace-write", "auto": "workspace-write",
                "workspace-write": "workspace-write",
                "dontAsk": "danger-full-access", "danger-full-access": "danger-full-access",
            }
            if raw_mode not in mode_map:
                raise ConfigError(f"permissionMode: unsupported mode '{raw_mode}'", kind="parse")
            permission_mode = mode_map[raw_mode]

        return RuntimeFeatureConfig(
            hooks_pre_tool_use=pre,
            hooks_post_tool_use=post,
            model=merged.get("model"),
            permission_mode=permission_mode,
            timeout=merged.get("timeout", 30),
            max_iterations=merged.get("maxIterations", 10),
            token_budget=merged.get("tokenBudget", 200_000),
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
            "CLAUDE_TOKEN_BUDGET": ("tokenBudget", int),
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







