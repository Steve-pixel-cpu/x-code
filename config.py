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


# ============================================================================
# MCP 服务器配置: 三级配置里的 "mcpServers" key（与 Claude Code 格式兼容）
#
#   "mcpServers": {
#     "fetch": { "command": "uvx", "args": ["mcp-server-fetch"] },
#     "docs":  { "type": "http", "url": "http://localhost:3000/mcp",
#                "headers": { "Authorization": "Bearer ${DOCS_TOKEN}" } }
#   }
#
# 深度合并由 deep_merge 天然支持: 用户级声明、项目级覆盖同名项。
# ============================================================================
class McpServerConfig(BaseModel):
    """一台 MCP 服务器的连接描述。type 缺省 = stdio（本地子进程）。"""
    name: str                                   # 配置里的 key, 用作工具名前缀
    transport: Literal["stdio", "http", "sse"] = "stdio"
    command: Optional[str] = None               # stdio: 可执行文件
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)   # stdio: 额外环境变量
    cwd: Optional[str] = None                   # stdio: 子进程工作目录
    url: Optional[str] = None                   # http/sse: 端点地址
    headers: dict[str, str] = Field(default_factory=dict)  # http/sse: 请求头
    timeout: int = 60                           # 单次工具调用超时（秒）


def expand_env_vars(value: Any) -> Any:
    """递归展开字符串里的 ${VAR} / $VAR。未定义的变量替换为空串——
    和 shell 行为一致, 配置里引用可选变量时不用写条件判断。"""
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [expand_env_vars(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env_vars(v) for k, v in value.items()}
    return value


def _parse_mcp_servers(merged: dict) -> list[McpServerConfig]:
    """merged["mcpServers"] → McpServerConfig 列表。结构非法抛 ConfigError;
    单台服务器描述缺字段同样报错——MCP 配错应该响亮失败而不是静默丢弃。"""
    raw = merged.get("mcpServers")
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise ConfigError("mcpServers: expected JSON object keyed by server name",
                          kind="parse")

    servers: list[McpServerConfig] = []
    for name, spec in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("mcpServers: server name must be a non-empty string",
                              kind="parse")
        if not isinstance(spec, dict):
            raise ConfigError(f"mcpServers.{name}: expected a JSON object",
                              kind="parse")

        spec = expand_env_vars(spec)
        raw_type = spec.get("type", "stdio")
        if raw_type in ("stdio", "http", "sse"):
            transport = raw_type
        else:
            raise ConfigError(
                f"mcpServers.{name}.type: unsupported transport '{raw_type}' "
                "(stdio/http/sse)", kind="parse")

        timeout = spec.get("timeout", 60)
        if not isinstance(timeout, int) or timeout <= 0:
            raise ConfigError(
                f"mcpServers.{name}.timeout: expected positive integer, got {timeout!r}",
                kind="parse")

        if transport == "stdio":
            command = spec.get("command")
            if not isinstance(command, str) or not command.strip():
                raise ConfigError(
                    f"mcpServers.{name}: stdio server requires 'command'", kind="parse")
            args = spec.get("args", [])
            env = spec.get("env", {})
            cwd = spec.get("cwd")
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                raise ConfigError(f"mcpServers.{name}.args: must be an array of strings",
                                  kind="parse")
            if not isinstance(env, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
                raise ConfigError(
                    f"mcpServers.{name}.env: must be an object of string→string",
                    kind="parse")
            if cwd is not None and not isinstance(cwd, str):
                raise ConfigError(f"mcpServers.{name}.cwd: must be a string", kind="parse")
            servers.append(McpServerConfig(
                name=name.strip(), transport="stdio",
                command=command, args=args, env=env, cwd=cwd, timeout=timeout))
        else:
            url = spec.get("url")
            if not isinstance(url, str) or not url.strip():
                raise ConfigError(
                    f"mcpServers.{name}: {transport} server requires 'url'", kind="parse")
            headers = spec.get("headers", {})
            if not isinstance(headers, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
                raise ConfigError(
                    f"mcpServers.{name}.headers: must be an object of string→string",
                    kind="parse")
            servers.append(McpServerConfig(
                name=name.strip(), transport=transport, url=url,
                headers=headers, timeout=timeout))
    return servers


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
    # MCP 服务器列表（配置 mcpServers key）。空列表 = 未配置, 零开销。
    mcp_servers: list[McpServerConfig] = Field(default_factory=list)

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

    def mcp_servers(self) -> list["McpServerConfig"]:
        return self.feature_config.mcp_servers

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
            mcp_servers=_parse_mcp_servers(merged),
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
