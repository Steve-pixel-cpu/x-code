# 04 - 配置系统 (config.py)

## 问题背景

一个 CLI 工具的配置来自很多地方：用户全局设置、项目设置、本地个人设置、环境变量。当多个来源对同一个 key 有不同值时，谁赢？

## CC 的做法

CC 按固定顺序扫描 5 个配置文件，用**递归深度合并**逐个叠加，最后一次性解析成强类型。
源码: `rust/crates/runtime/src/config.rs`

## 你要练习的工程模式

| 模式 | 说明 |
|------|------|
| **5 源发现链** | 固定 5 个路径按顺序扫描，后面覆盖前面 (config.rs:185-212) |
| **递归深度合并** | 双方都是 dict 才递归；否则 last-write-wins (config.rs:777-791) |
| **Eager Feature Parsing** | load() 返回前就解析成强类型，不等运行时 (config.rs:230-239) |
| **叶模块** | config.py 不 import 项目内其他模块，防循环依赖 |
| **Legacy 容错** | 旧格式 .claude.json 解析失败时静默跳过 |

## 你需要写的东西

```
ConfigSource(Enum)     — USER / PROJECT / LOCAL
ConfigEntry(BaseModel) — source + path
ConfigError(Exception) — kind: "io" | "parse"

deep_merge(target, source) -> new_dict
  只有双方都是 dict 才递归，否则 source 覆盖

RuntimeFeatureConfig(BaseModel)
  hooks_pre_tool_use, hooks_post_tool_use
  model, permission_mode, timeout, max_iterations, token_budget
  thinking_level — 默认 "medium"; 合法值 low/medium/high/max

RuntimeConfig(BaseModel)
  merged: dict              — 原始合并结果（forward compatibility）
  loaded_entries: list       — 哪些文件被加载了（调试用）
  feature_config             — 解析后的强类型（快速访问）
  便捷方法: get(), model(), timeout(), hooks_pre() 等

ConfigLoader
  __init__(cwd, config_home) — 依赖注入，不硬编码路径
  discover() -> 5 个 ConfigEntry
  load() -> RuntimeConfig
    遍历 → 读 JSON → deep_merge → 环境变量覆盖 → eager parse
```

## deep_merge 示例

```python
a = {"model": "sonnet", "hooks": {"PreToolUse": ["echo hi"]}}
b = {"model": "opus",   "hooks": {"PostToolUse": ["prettier"]}, "timeout": 60}
result = deep_merge(a, b)
# {"model": "opus",                      ← source 覆盖 (都是 str)
#  "hooks": {"PreToolUse": ["echo hi"],   ← 递归合并 (都是 dict)
#            "PostToolUse": ["prettier"]},
#  "timeout": 60}                         ← 新 key
```

## 5 个配置路径（按优先级从低到高）

```
1. ~/.claude.json                        (User - 旧格式)
2. ~/.claude/settings.json               (User)
3. <project>/.claude.json                (Project)
4. <project>/.claude/settings.json       (Project)
5. <project>/.claude/settings.local.json (Local - 不提交 git)
```

## 易错点

- `deep_merge` 要返回新 dict，不要修改原 dict（immutability）
- legacy `.claude.json` 解析失败要静默跳过，不报错
- 空文件应返回空 dict `{}`，不是 None
- 环境变量类型转换要 try/except（比如 `CLAUDE_TIMEOUT=abc` 应该报错）
- permission mode 支持多个别名（如 `"auto"` = `"workspace-write"`）

## 配置键速查

| 配置键 | 环境变量 | 说明 |
|--------|----------|------|
| `model` | `CLAUDE_MODEL` | 模型名 |
| `timeout` | `CLAUDE_TIMEOUT` | 工具超时秒数 |
| `maxIterations` | `CLAUDE_MAX_ITERATIONS` | 单轮最大模型调用次数，超限优雅收束（不再抛异常） |
| `tokenBudget` | `CLAUDE_TOKEN_BUDGET` | auto-compact 阈值：最近一次调用 input_tokens 达到即压缩 |
| `thinkingLevel` | `CLAUDE_THINKING_LEVEL` | 思考档位 low/medium/high/max，默认 medium；budget 映射见 api_client.py |
| `turnTokenBudget` | `CLAUDE_TURN_TOKEN_BUDGET` | 单轮累计 output tokens（含思考）上限，默认 65536，超限本轮提前收束 |

| `utilityProvider` | — | side-call 专用供应商：`{"provider": <providers 里的 id>, "model": "<模型名>"}`。压缩摘要/会话记忆摘要/自动命名走它, 主循环不动; 未配置/无效回落主模型 |

### utilityProvider（side-call 小模型, 2026-09）

整理型调用（压缩摘要、Session Memory 消化、会话自动命名）不贵在频率而贵在
用主模型——配置一个便宜供应商后这些调用全部走小模型, 主循环 token 不受影响。

- 配置在 `settings.json` 顶层: `"utilityProvider": {"provider": "<id>", "model": "..."}`
- `config.load_utility_provider()` 校验（指向存在且 enabled 且有 key/base_url）,
  无效一律返回 None → 回落主模型, 行为与未配置一致
- CLI 在 `_assemble` 构建（`runtime.set_utility_client`）; Web 在
  `_apply_provider_config` 时随主配置重建（`server._utility_client`）,
  会话在 `load_runtime_for` 时挂载
