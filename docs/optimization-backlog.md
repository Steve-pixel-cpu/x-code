# x-code 优化待办（源自 Claude Code 参考设计）

> 来源：`D:\workplace\minicc\reference\docs\chapters\`（Claude Code 参考实现的 19 章设计文档，
> 其中 10-context-assembly / 11-compact-system / 01-query-engine / 02-tool-system / 06-bash-engine /
> 07-permission-pipeline / 08-agent-swarms / 09-session-persistence 已通读提取）。
> 本文档记录"已评估、有价值、暂未实现"的优化项，按价值排序。下次开发时按项取用。

## 已完成的借鉴（基线，勿重做）

- **压缩触发口径修复**：`_context_over_compact_threshold` 读最近一次 API 调用的 usage（非本轮累加）
- **阈值 750k**（GLM-5.3-Flash 1M 窗口的 75%），`contextWindow` 配置 + env `CLAUDE_CONTEXT_WINDOW`
- **LLM 摘要主路径**：压缩时 side-call 生成结构化摘要（6 段式），规则摘要（`summarize_messages`）仅作回退；
  重算时增量合并（上一摘要 + 新归档切片）
- **MicroCompact**：估算 token > 60k 时，保留窗口（最近 8 条）外的高产出可复现工具结果替换为占位符，
  白名单 = read_file/grep/glob/bash/powershell/web_search/web_fetch（`runtime.py` `_microcompact_view`）
- **压缩后文件重注入**：归档区最近 5 个读过的文件（每个 5k 字符）随续接摘要带回
- **重复只读护栏**：同参 read_file/grep/glob 第 2 次警告、第 3 次拒绝；bash/powershell 先做只读判定
  （白名单 + 危险构造一票否决），只读命令不清零计数
- **结论检查点**：本回合调用时钟每跨过 16 的倍数档位，在最新工具结果上附对靶提醒（引用原始问题）
- **max_tokens 截断自愈**：注入 "Resume directly — no apology, no recap" 恢复提示继续循环，最多 3 次
- **搜索反馈**：grep/glob 空结果附"换思路"引导；提示词引导优先用内置搜索工具
- **单轮输出预算**：262_144（约 16 次大思考调用）

---

## 待办 1：Session Memory（三层压缩体系的中间层）

**价值**：全量压缩（LLM 摘要）要付一次 750k 输入的 side-call；session memory 用"后台代理持续维护的
滚动摘要"替代，压缩时零 LLM 调用、零延迟。Claude 的三层体系：

| 层 | 机制 | 压缩率 | 成本 |
|---|---|---|---|
| MicroCompact | 清旧工具结果 | 每轮回收 10-50K | 零调用（已实现 ✅） |
| **Session Memory** | **预构建滚动摘要直接当压缩结果** | **~60-80%** | **后台增量维护，压缩时零调用** |
| Full Compact | LLM 现场摘要 | ~80-95% | 一次大调用（已实现 ✅） |

**参考参数**（Claude `DEFAULT_SM_COMPACT_CONFIG`）：
- 保留区 `minTokens: 10_000` / `minTextBlockMessages: 5` / `maxTokens: 40_000`（从最后摘要点向前扩展，双下限满足即止，硬上限封顶）
- 后台代理：会话空闲时增量消化新消息、更新滚动摘要（x-code 可在 `_notify_iterate` 一致点后异步做）

**实现要点**：
- 后台摘要任务的触发时机（Claude 用后台 agent；x-code 可用 `threading.Thread` + 空闲检测，或每 N 次迭代后同步增量）
- 压缩激活时优先用 session memory；memory 不可用/过旧时回退现有 LLM 现场摘要
- API 不变量保护：截断保留区时 tool_use/tool_result 配对完整（x-code 的 `_adjust_cut_point` 已有，需适配新切割逻辑）
- 递归保护：摘要任务自身不得触发压缩（`querySource` 门控思路）

**来源章节**：11-compact-system.md §Session Memory Compact

---

## 待办 2：auto-compact 阈值体系改造

**价值**：Claude 不用百分比，用绝对差值三档状态机，语义清晰且天然适配不同窗口：

```
effectiveWindow = contextWindow - min(maxOutputTokens, 20_000)
autoCompactThreshold = effectiveWindow - 13_000
warningThreshold     = effectiveWindow - 20_000   # 警告（UI 提示）
blockingLimit        = effectiveWindow -  3_000   # 强制手动压缩
```

200K 模型示例：effective ≈ 180K，auto-compact ≈ 167K（窗口的 ~84%）。

**GLM 适配注意**：Claude 敢压到窗口的 96.7% 是因为模型长上下文能力强；GLM-5.3-Flash 未必。
建议方案：公式照搬（做成 config 推导），但额外引入一个"注意力保护系数"配置（如 `compact_headroom`，
默认可在绝对差值上再加一档），或保持现有 75% 比例作为默认、公式作为可选模式。**先观察 microcompact
上线后长会话的表现再定**——microcompact 把上下文压在 60k 附近后，全量压缩的触发频率本身会大降。

**配套（Claude 同款，可选）**：
- 五级警告状态机（percentLeft / warning / error / autoCompact / blocking）
- 熔断器：连续 3 次 auto-compact 失败（API 报错等）→ 本会话停用自动压缩（Claude 数据：此前每天浪费 25 万次调用）
- PTL 自愈：压缩请求自身超长时，按 API 轮次分组从最老组丢弃重试（最多 3 次，兜底每次丢 20%）

**来源章节**：11-compact-system.md §Threshold Calculation / §Warning State Machine / §Circuit Breaker / §PTL

---

## 待办 3：工具结果超限落盘（替代中段丢弃）

**价值**：现在 `truncate_tool_output` 超过 20k 字符把中段掐掉，信息永久丢失。Claude 的方案是二段式：
超限全文写盘、只返回首尾预览 + 文件路径，模型需要全量时自己再读。

**参考参数**（Claude 分工具上限）：
| 工具 | maxResultSizeChars |
|---|---|
| Bash | 30,000 |
| Edit / Glob / Grep | 100,000 |
| Read | ∞（有自己的分页限制；落盘会让"读结果"形成循环依赖，故排除） |

**实现要点**：
- 落盘目录 `~/.x-code/tool-results/{hash}.txt`（对齐现有 `USER_CONFIG_HOME` 约定）
- 截断消息里带路径 + 指引："Full output saved to {path}; use read_file to inspect specific ranges"
- 与 MicroCompact 的关系：落盘后，被 MicroCompact 清掉的旧结果理论上可从盘上找回（Claude 没做这层，
  占位符不可恢复；x-code 可以做——占位符里带落盘路径即可，成本几乎为零，**建议顺手做**）
- 定期清理策略（避免 tool-results 目录无限增长；Claude 未提及，自行设计如按 mtime 清 7 天前）

**来源章节**：02-tool-system.md §Tool Result Truncation（配套 11 章 content-clear）

---

## 待办 4：权限管线的 bypass-immune 安全检查 ✅（已落地）

**状态**：已在 permissions.py 落地（2026-09）——写路径分级
`classify_write_path`（inside/outside/sensitive）+ shell 破坏族敏感路径
扫描 `shell_command_touches_sensitive_path`，敏感路径（`.git/`、
`~/.x-code/`、`~/.ssh/`、shell 配置文件）在任何模式（含
danger-full-access/allow）下都强制人工裁决，不可被命令白名单短路；
无 prompter（subagent）直接拒绝。测试见 tests/test_path_policy.py。
设计说明见 guides/05_permissions.md 末节。

**价值**：Claude 有四个"即使 bypassPermissions 模式也照样拦截"的检查（bypass-immune）：
1. 工具实现级 denied（如 BashTool 对单个危险子命令的判定）
2. `requiresUserInteraction()` 为 true 的场景
3. 内容级 ask 规则（如 `Bash(npm publish:*)`）
4. **敏感路径安全检查**：`.git/`、`.claude/`（x-code 对应 `.x-code/`）、`.vscode/`、shell 配置文件

对 x-code 最直接的是第 4 条：**写/删 `.git/`、`.x-code/`、shell 配置文件（.bashrc/.zshrc）等路径时，
无论当前权限模式（包括 danger-full-access）都强制询问或拒绝**。防的两类事故：
- 模型抽风改掉自己的权限配置/护栏配置（自逃脱）
- " rm -rf .git" 类不可逆破坏被 ALLOW 模式静默放行

**实现要点**：
- ~~挂点在 `runtime._authorize_tool_use`（所有写类工具：write_file/bash/powershell）之前做路径扫描~~
  实际落点在 `permissions.PermissionPolicy.authorize` 开头（策略层统一闸门，
  CLI/Web/subagent 全走这里）
- 路径匹配需解析 bash 命令里的重定向目标与命令参数（复用 `shell_command_is_read_only` 的 shlex 解析）
  ——已实现：破坏族（rm/mv/cp/tee 等）参数 + `>`/`>>` 重定向目标；
  命令替换等解析不了的构造是已知盲区（不误报，靠审批兜底）
- Claude 还有"进 auto 模式剥离危险权限、退出恢复"的模式切换副作用集中化（`transitionPermissionMode`），
  x-code 权限模式切换（permissions.py）可参考
- 更深的（OS 沙箱、extglob 禁用、bare git repo 攻击防护）见 06-bash-engine.md §安全加固，优先级低

**来源章节**：07-permission-pipeline.md §七步管线（1d-1g bypass-immune）/ §模式转换副作用

---

## 附：参考文档里其余可借鉴但优先级更低的设计

- **重试按错误码分诊**：429 指数退避（base 500ms 上限 32s）、连续 3 次 529 切 fallback 模型、
  无人值守无限重试 + 30s 心跳、context overflow 自动算安全 max_tokens 重试（01 章）
- **流式 idle watchdog 90s**：90 秒无 SSE 数据 → abort 重试，防死连接挂起（01 章）
- **Read 去重 file_unchanged**：同文件 mtime 未变返回 stub（Claude 遥测 ~18% Read 是同文件碰撞）；
  注意与 MicroCompact 占位符的交互——占位符替换后模型重读需要真实内容，需保留最近 N 条的原文（02 章）
- **Stale Write Guard**：Edit 前比对 FileStateCache 的 mtime（Windows 加内容比较 fallback），
  "File has been modified since read. Read it again."（02 章）
- **工具排序保缓存**：内置工具连续前缀 + MCP 工具后缀，外部工具插中间会让全部下游缓存失效（12 倍成本）（02 章）
- **后台任务 size watchdog**：每 5s stat 输出文件，超限 SIGKILL（Claude 曾被填满 768GB 磁盘）（06 章）
- **记忆头部预计算**：相对时间戳（"saved 3 days ago"）会打爆 prompt cache，附件头部要预计算成稳定文本（10 章）
- **环境信息会话级快照**：git status 等 memoize 为会话快照，不随对话更新（10 章）
