# x-code

一个 Claude Code 风格的 AI 编程 Agent，从零实现的完整学习项目：终端 REPL + Web 桌面端双入口，内置工具循环、权限体系、多 Agent 编排、会话持久化与自动压缩。

> Python 3.14 + FastAPI + Tauri 2，兼容任意 Anthropic API 格式的模型服务（可自定义接口地址）。
>
> 本项目基于 [MiniCC](https://github.com/Louisym/MiniCC) 开发，在其 Agent 架构基础上扩展而来。

## 功能特性

- **Agent 工具循环**：模型自主决定调用工具 → 执行 → 回传结果，循环往复直到完成；支持 SSE 流式输出、thinking 块、单轮/循环层 token 与迭代预算
- **MCP 客户端**：配置文件里声明 MCP 服务器（stdio 子进程 / streamable HTTP / SSE），启动时自动连接，外部工具以 `mcp__<服务器>__<工具>` 注入工具循环，与内置工具同管线（权限审批、hooks、输出截断）；Web 端支持 `/api/mcp/reload` 热重载
- **Skills 技能包**：Claude Code 兼容的 `SKILL.md`（YAML frontmatter + 正文指令，可捆绑脚本/模板），任务匹配时模型先用 `skill_read` 读正文再遵循（渐进式披露，只注入 name+description 清单省 token）；项目级 `.claude/skills/` 与用户级 `~/.x-code/skills/` 两级发现、同名覆盖；Web 设置页从 GitHub 仓库一键安装社区 skills（如 `anthropics/skills`），装完即时生效无需重启；CLI `/skills` 查看
- **内置工具集**：`bash` / `powershell`（Windows 下走 Git Bash，UTF-8 无乱码）、`read_file` / `write_file`、`grep` / `glob`（纯 Python 实现，免 shell）、后台任务 `task_output` / `task_stop`、任务清单 `todo`、计划卡 `present_plan`、浏览器实测 `browser_navigate` / `browser_snapshot` / `browser_click` / `browser_type` / `browser_console` 等（Playwright 无头 Chromium，可实际操作 Web 系统做功能测试）
- **三级权限体系**：`plan`（只读）→ `workspace-write`（workspace 根内可写，写路径分级：根外审批/敏感路径任何模式都强制确认）→ `danger-full-access`（全放行）；每个工具登记权限档位，越权时 CLI 弹审批面板、Web 端弹审批卡；只读命令白名单 + 命令前缀白名单（全局/会话级）+ 附加目录记忆治审批疲劳，plan 模式下可一键升级
- **多 Agent 编排**：Leader 通过 `agent_tool` / `agent_status` / `agent_reap` / `agent_list` 派生 subagent 并行干活，白名单 + 规格过滤防递归失控，孤儿 agent 启动对账
- **会话持久化**：JSONL 增量落盘、断点恢复（`-c` / `--resume`）、自动命名、auto-compact（上下文超阈值自动压缩，保留近几条消息）
- **用户记忆**：跨会话画像事实记忆，让 Agent 更懂你。对话中模型用 `memory_write` / `memory_update` / `memory_delete` 三工具自动沉淀（免审批、写入反馈可见），每次会话注入系统提示词（prompt cache 友好：静态指引 + 动态边界之下独立 section）；存储为结构化 JSON（`~/.x-code/memory.json`），`MemoryStore` 抽象为 RAG 检索预留接口；零参数遗忘机制——200 条容量触发 LRU 淘汰（hits 为主权重、user 来源豁免、淘汰归档可找回）；CLI `/memory` 管理命令 + Web 设置页"记忆"区块
- **Hooks**：`PreToolUse` / `PostToolUse` 挂 shell 命令，工具执行前后触发
- **配置分层**：用户全局 → 项目 → 本地三级 JSON 配置深度合并，环境变量可覆盖；模型、思考档位（low/medium/high/max）、超时、预算均可配
- **限流重试**：连接抖动指数退避 + 429 专用长退避曲线（累计约 30s），重试进度实时上报界面
- **摸鱼电台**：Web 端内置网易云音乐公开接口的在线电台（搜索 + 流式播放 + 歌词 + 本地收藏/自定义歌单，收藏与歌单存 `~/.x-code/music-library.json`，播放列表跨重启恢复）

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
| `mcp_client.py` | MCP 客户端：外部服务器连接管理、工具注入（同步桥接异步 SDK） |
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
  },
  "mcpServers": {
    "fetch": { "command": "uvx", "args": ["mcp-server-fetch"] },
    "docs":  { "type": "http", "url": "http://localhost:3000/mcp",
               "headers": { "Authorization": "Bearer ${DOCS_TOKEN}" } }
  }
}
```

### MCP 服务器

在任意一级配置里声明 `mcpServers`（格式与 Claude Code 兼容，三级之间
字段级深度合并，项目级可覆盖用户级同名服务器的字段）：

| 字段 | 适用传输 | 说明 |
|---|---|---|
| `type` | 全部 | `stdio`（默认）/ `http`（streamable HTTP）/ `sse` |
| `command` / `args` / `env` / `cwd` | stdio | 子进程命令、参数、额外环境变量、工作目录 |
| `url` / `headers` | http / sse | 端点地址与请求头 |
| `timeout` | 全部 | 单次工具调用超时秒数（默认 60） |

字符串值里的 `${VAR}` 会展开为环境变量（适合注入 API key）。

- 连接在启动时并行进行，失败的服务器降级为警告，不阻塞启动
- **桌面端「设置 → MCP 服务器」可视化编辑**：添加/改名/删除、传输类型切换、
  连接状态徽标与失败原因就地显示，改动停顿后自动保存并热生效（无需重启）；
  手改配置文件后点「重连全部」同样即时生效
- 工具以 `mcp__<服务器名>__<工具名>` 注入，CLI 与 Web 端同时生效
- 权限上按"能力未知的外部工具"保守处理：plan 模式直接拒绝，
  其余模式每次调用走审批；Pre/PostToolUse hooks 与输出截断照常生效
- Web 端运维接口：`GET /api/mcp/status` 看连接状态，
  `POST /api/mcp/reload` 重读配置热重载（改完配置无需重启）
- 依赖：`mcp` Python SDK（`uv sync` 自动安装）；stdio 服务器需要
  对应运行时（如 `uvx` / `npx`）

环境变量覆盖：`CLAUDE_MODEL`、`CLAUDE_TIMEOUT`、`CLAUDE_MAX_ITERATIONS`、
`CLAUDE_TOKEN_BUDGET`、`CLAUDE_TURN_TOKEN_BUDGET`、`CLAUDE_THINKING_LEVEL`。

### Skills 技能包

一个技能 = 一个目录 + 一份 `SKILL.md`（YAML frontmatter + Markdown 正文
指令），可捆绑任意辅助文件（脚本/模板/数据），目录即技能工作区：

```markdown
---
name: pdf-tools
description: 处理 PDF 拆分/合并/提取文本时的标准流程与脚本
---
# 步骤
1. 先用 scripts/extract.py 提取文本……
```

- **两级发现**：用户级 `~/.x-code/skills/<名称>/SKILL.md`（跨项目可用）、
  项目级 `<项目>/.claude/skills/<名称>/SKILL.md`（随仓库走，同名覆盖用户级）；
  与 Claude Code 的技能目录兼容，已有的技能仓库可直接放进项目使用
- **渐进式披露**：系统提示词只注入 name + description 清单（几百 token），
  任务匹配时模型先用 `skill_read` 工具读 SKILL.md 全文再遵循——十个技能
  也不会把上下文吃满；`skill_read` 限定只能读技能目录内的文件，
  越界（含捆绑文件里的 `../` 穿越）一律拒绝
- **CLI**：启动摘要显示已装技能；`/skills` 列出全部，
  `/skills <名称>` 预览该技能的 SKILL.md
- **Web 端「设置 → Skills」**：输入 GitHub 仓库一键安装社区 skills
  （支持 `anthropics/skills` 短格式或完整 URL；仓库根目录、指定子目录、
  `skills/*/<名称>/` 一仓多技能三种布局都识别），装完所有活跃会话
  即时生效、无需重启；同名技能默认拒绝覆盖，勾选"覆盖同名"放行；
  用户级技能可就地卸载，项目级技能请回项目仓库管理
- 对应 REST 接口：`GET /api/skills`、`POST /api/skills/install`
  （body: `repo` / `subpath` / `overwrite`）、`DELETE /api/skills/{name}`

## 权限模式

| 模式 | 说明 |
|---|---|
| `plan` | 只读：仅放行读文件/搜索类工具，写操作被硬拒（并附提示引导切模式） |
| `workspace-write` | workspace 根内可写文件（工作目录 + 附加目录）、可派生 subagent；执行命令仍需审批 |
| `danger-full-access` | 全放行（含任意命令执行）；仅敏感路径仍强制确认 |

工具按"只读 / 本地写 / 任意命令"三档登记所需权限，越权即触发审批：
CLI 是黄色面板（y 本次 / a 总是 / s 本会话 / N 拒绝，Ctrl+C 一律朝安全侧
拒绝），Web 端是弹窗审批卡（同样带"总是允许 / 本会话允许"记忆按钮）。

**写路径分级（应用层策略沙箱）**：`write_file`/`edit_file` 的目标路径
resolve 后与 workspace 根（会话工作目录 + `additionalDirectories` 附加目录）
比对——根内静默放行（零新增弹窗）；写出根外升档弹审批，审批卡上可
"允许并记住该目录"（目录进附加目录，之后免问）；命中敏感路径
（`.git/`、`~/.ssh`、shell 配置文件、`~/.x-code` 本应用配置）则
bypass-immune：任何模式（含 danger-full-access/allow）都强制人工裁决，
不可被白名单或记忆机制豁免。shell 侧同口径：`rm`/`mv`/`cp`/`tee` 等
破坏族点名敏感路径、或显式重定向写入敏感文件（`echo x > ~/.bashrc`）
同样强制弹审批；`git commit`/`git add` 等正常工作流不受影响。

**审批疲劳治理**：shell 只读白名单（ls/cat/git log 等探查命令免问，
纯 `--version`/`--help` 查询同样免问）+ 用户命令前缀白名单（全局持久，
设置页与审批卡可维护）+ 会话级白名单（只对本会话生效）三层免问；
组合命令逐段取并集——每段"命中规则或属于只读探查"即放行，
`git status && uv run pytest` 不再因 status 段无规则而白弹；重复命令
靠 a/s 记忆消除逐条审批。

**拒绝清单与敏感路径**：`commandDenylist` 的前缀规则任一段命中即整体
拒绝，优先于一切放行路径——白名单放行 `git push` 的同时可加
`git push --force` 拦住强推；`sensitivePaths` 可声明项目级敏感路径
（如 `secrets/`、`.env.production`，相对路径按会话根解析），命中与
内置敏感清单同一语义。两者都在设置页"行为"分区维护，保存即对所有
会话生效。

**诚实边界**：这是应用层策略沙箱 + 人工审核（policy gate），没有内核
强制力——用户批准的 shell 命令仍以完整用户权限执行，审批本身就是闸门
而非技术隔离；shell 命令的路径静态分析是已知盲区（命令替换等构造无法
可靠解析），靠"变异命令需审批"兜底。

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

## 项目来源与致谢

本项目基于 [Louisym/MiniCC](https://github.com/Louisym/MiniCC) 开发，在其 Agent
架构的基础上进行了多方向扩展与定制，例如桌面端 Tauri 2 壳、三级权限体系、
多 Agent 编排、Hooks、auto-compact、限流重试与摸鱼电台等。

感谢 MiniCC 原作者的工作；本项目同样以学习 Agent 架构设计为目的，
欢迎参考与交流。

## 免责声明

本项目用于学习 Agent 架构设计，`danger-full-access` 模式下模型可执行任意命令，
请注意在可信环境下使用。
