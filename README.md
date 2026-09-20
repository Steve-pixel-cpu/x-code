# x-code

一个 Claude Code 风格的 AI 编程 Agent，从零实现的完整学习项目：终端 REPL + Web 桌面端双入口，内置工具循环、权限体系、多 Agent 编排、会话持久化与自动压缩。

> Python 3.14 + FastAPI + Tauri 2，兼容任意 Anthropic API 格式的模型服务（可自定义接口地址）。

## 功能特性

- **Agent 工具循环**：模型自主决定调用工具 → 执行 → 回传结果，循环往复直到完成；支持 SSE 流式输出、thinking 块、单轮/循环层 token 与迭代预算
- **内置工具集**：`bash` / `powershell`（Windows 下走 Git Bash，UTF-8 无乱码）、`read_file` / `write_file`、`grep` / `glob`（纯 Python 实现，免 shell）、后台任务 `task_output` / `task_stop`、任务清单 `todo`、计划卡 `present_plan`
- **三级权限体系**：`plan`（只读）→ `workspace-write`（工作目录内可写）→ `danger-full-access`（全放行）；每个工具登记权限档位，越权时 CLI 弹 y/N 面板、Web 端弹审批卡，plan 模式下可一键升级
- **多 Agent 编排**：Leader 通过 `agent_tool` / `agent_status` / `agent_reap` / `agent_list` 派生 subagent 并行干活，白名单 + 规格过滤防递归失控，孤儿 agent 启动对账
- **会话持久化**：JSONL 增量落盘、断点恢复（`-c` / `--resume`）、自动命名、auto-compact（上下文超阈值自动压缩，保留近几条消息）
- **Hooks**：`PreToolUse` / `PostToolUse` 挂 shell 命令，工具执行前后触发
- **配置分层**：用户全局 → 项目 → 本地三级 JSON 配置深度合并，环境变量可覆盖；模型、思考档位（low/medium/high/max）、超时、预算均可配
- **限流重试**：连接抖动指数退避 + 429 专用长退避曲线（累计约 30s），重试进度实时上报界面
- **摸鱼电台**：Web 端内置网易云音乐公开接口的在线电台（榜单 + 流式播放 + 歌词）

## 架构总览

```
┌─────────────┐   ┌──────────────────────────────┐
│  CLI (main) │   │  桌面端: Tauri 2 壳 + WebView │
└──────┬──────┘   │  static/ 前端 (原生 JS/CSS)   │
       │          └──────────────┬───────────────┘
       │            WebSocket/REST (token 门禁)
       │          ┌──────────────┴───────────────┐
       └──────────►   server.py (FastAPI 后端)    │
                  └──────────────┬───────────────┘
                                 │
        ┌────────────────────────┴───────────────────────┐
        │ runtime.py  Agent 工具循环（线程池并行工具执行） │
        ├────────────────────────────────────────────────┤
        │ api_client  流式客户端 + 重试    tools  工具集  │
        │ permissions 权限模式/审批        hooks  生命周期│
        │ multi_agent 多Agent编排          compact 压缩   │
        │ config      分层配置             storage 会话库 │
        └────────────────────────────────────────────────┘
                    数据目录: ~/.x-code/
```

| 模块 | 职责 |
|---|---|
| `main.py` | CLI 入口：REPL、斜杠命令、工具 spec、装配（CLI 与 Web 共用） |
| `server.py` | FastAPI 后端：WebSocket 推流、权限审批桥、会话/设置 REST API |
| `runtime.py` | Agent 主循环：事件流、并行工具执行、打断、循环预算 |
| `api_client.py` | Anthropic API 流式客户端、思考档位、退避重试 |
| `tools.py` | 工具实现与注册表（后台任务、取消检查点） |
| `permissions.py` | 权限模式层级、策略、CLI/Web 审批器 |
| `multi_agent.py` / `agent_tools.py` | 多 Agent 编排内核 / 工具接线层 |
| `config.py` | 三级配置发现合并、供应商配置读写 |
| `storage.py` / `fsatomic.py` | 会话存储（JSONL）/ Windows 原子文件写 |
| `compact.py` | auto-compact 会话压缩 |
| `prompt.py` | 系统提示词构建（OS 信息、CLAUDE.md 指令、plan 模式段） |
| `hooks.py` | Pre/PostToolUse shell 钩子 |
| `retry.py` | 指数退避 + 429 长曲线 |
| `static/` | Web 前端（无框架，原生 JS） |
| `src-tauri/` | 桌面壳：拉起后端、令牌门禁、单实例、看门狗 |

## 快速开始

### 环境要求

- **Python ≥ 3.14**，推荐 [uv](https://docs.astral.sh/uv/)
- **Git for Windows**（Windows 必装）——命令执行器依赖 Git Bash（MSYS2 coreutils
  对 UTF-8 字节直通，PowerShell 会按 GBK 转码导致乱码），未检测到时拒绝启动
- 打包桌面端另需 Rust 工具链（cargo）与 Node.js

### 安装

```bash
uv sync
```

### 配置模型服务

在项目根目录新建 `.env` 填写（CLI 读取）：

```ini
API_KEY=sk-xxx        # CLI 用的 key
```

Web 端不读 `.env`：首次启动进入初始化页，在界面里填写 API Key、接口地址与模型，
配置保存到 `~/.x-code/settings.json`，支持添加多个供应商随时切换。

### 启动 CLI

```bash
uv run python main.py            # 新会话
uv run python main.py -c         # 继续最近一次会话
uv run python main.py --resume <id>   # 恢复指定会话
uv run python main.py --list     # 列出全部会话
```

CLI 斜杠命令：`/help` `/status` `/compact` `/mode` `/thinking` `/rename` `/exit`

### 启动 Web 端

```bash
uv run python server.py          # 默认 127.0.0.1:8000
uv run python server.py --port 8020
```

浏览器打开后即与 CLI 共享同一份会话记录与工具集；带图形界面（自定义标题栏、
计划卡、设置页、摸鱼电台）建议用桌面端。

## 配置说明

配置按优先级从低到高深度合并：

| 位置 | 作用域 |
|---|---|
| `~/.x-code/settings.json`（及 `.claude.json`） | 用户全局 |
| `<项目>/.claude/settings.json`（及 `.claude.json`） | 项目级 |
| `<项目>/.claude/settings.local.json` | 本地个人 |

可配置项（key 与 Claude Code 兼容）：

```json
{
  "model": "glm-5.3-flash",
  "thinkingLevel": "high",
  "permissionMode": "workspace-write",
  "timeout": 30,
  "maxIterations": 128,
  "tokenBudget": 100000,
  "turnTokenBudget": 65536,
  "hooks": {
    "PreToolUse": ["python check.py"],
    "PostToolUse": []
  }
}
```

环境变量覆盖：`CLAUDE_MODEL`、`CLAUDE_TIMEOUT`、`CLAUDE_MAX_ITERATIONS`、
`CLAUDE_TOKEN_BUDGET`、`CLAUDE_TURN_TOKEN_BUDGET`、`CLAUDE_THINKING_LEVEL`。

## 权限模式

| 模式 | 说明 |
|---|---|
| `plan` | 只读：仅放行读文件/搜索类工具，写操作被硬拒（并附提示引导切模式） |
| `workspace-write` | 工作目录内可写文件、可派生 subagent；执行命令仍需审批 |
| `danger-full-access` | 全放行（含任意命令执行） |

工具按"只读 / 本地写 / 任意命令"三档登记所需权限，越权即触发审批：
CLI 是黄色 y/N 面板（Ctrl+C 一律朝安全侧拒绝），Web 端是弹窗审批卡。

## 桌面端打包

一键打包（PyInstaller 冻结后端 → cargo tauri build）：

```cmd
build-exe.cmd
```

产物在 `dist\`：`x-code_0.1.0_x64-setup.exe`（NSIS 安装包，Tauri 2 + WebView2，
约 28MB）。细节见 [PACKAGING.md](PACKAGING.md)：端口自动避让（8000 被占时退让
8010–8019）、父子进程看门狗、令牌门禁等。

`build-mac.sh` 提供 macOS 打包入口。

## 测试

```bash
uv run pytest
```

测试覆盖：runtime 工具循环、权限模式、多 Agent、auto-compact、限流重试、
原子落盘、并发会话、后台任务、附件、Windows shell 选壳等。

## 深入阅读

[guides/](guides/) 下有 13 篇按模块拆解的实现笔记（models / tools / api_client /
config / permissions / hooks / retry / prompt / compact / storage / multi_agent /
runtime / main），适合按顺序阅读源码。

## 免责声明

本项目用于学习 Agent 架构设计，`danger-full-access` 模式下模型可执行任意命令，
请注意在可信环境下使用。
