### 目标行为

AI 生成中用户再发消息：**立即打断当前轮**（部分输出保留落盘、显示「已停止」），新消息气泡立即上屏并追加进对话，收束后与队列中其他未处理消息**合并为一次新请求**处理（一次响应，即"合并处理"）。

原"待发送卡片"（立即/编辑/删除）随静默排队一起移除——消息发送后直接进消息列。

### 后端 server.py

1. **ws "user" busy 分支**（server.py:1931-1962）：计划审批挂起与普通生成中两条路径统一为立即打断——`pending.append(...)`（qid/text/attachments）→ `stop_requested = True` → `prompter.cancel()` → 回执 `turn_interrupting`。删除 `turn_queued_user` 回执与计划分支的 `insert(0)` 优先逻辑（收束后合并所有 pending，优先级不再有意义）。保留 10 条上限守卫。
2. **`_start_pending_turn`**（server.py:1170-1190）：由"取一条接力"改为"**取出全部 pending 合并成一轮**"——texts 用 `\n\n` 连接、attachments 顺序拼接，broadcast 的 `turn_started` 携带 `items: [{qid,text,attachments},...]`（保留单 `qid`/`text` 字段取第一项，兼容）。`_drain_queued_turns` 的归队路径（server.py:1154-1161）不变，自然兼容。
3. **`request_stop`**（server.py:1212-1214）：保留"清空 pending"语义，`turn_queue_cleared` 事件增加 `qids` 字段（供前端撤回乐观气泡、文本放回输入框，不丢内容）。stop 按钮语义不变。
4. **删除** `promote_pending`（1219-1238）与 ws 的 `queue_promote`/`queue_remove` 分支（1980-1994），更新未知消息类型的提示文案。

### 前端

**app.js**：
- `sendCurrent` busy 分支（3354-3365）：改为乐观加气泡 `addUserBubble(..., qid)` + 清空输入框 + 直接发 `{type:"user"}`；与计划审批分支（3342-3353）合并为一个 busy 分支（计划挂起时多一步 `expirePlanCard`）。不再 push `run.queue`、不再渲染卡片。
- `onTurnStarted`（2720）与后台会话 `turn_started` 分支（~1958）：按 `msg.items` 逐项以 qid 查重补气泡（多窗口同步），删除 run.queue 撤卡逻辑；气泡已乐观加上时按现有 `_qid` 去重跳过。
- `onQueueCleared`（2748）：按事件的 `qids` 撤掉对应乐观气泡，文本/附件放回输入框（保留现有不丢逻辑）。
- 删除排队卡片整套：`renderQueueCards`/`editQueued`/`removeQueued`/`Q_PROMOTE_SVG`（3145-3230）、`run.queue` 字段（64）、`onTurnQueuedUser` 及其分发 case、`queue_promote`/`queue_remove` 发送点。
- `setBusyUi` 占位符（3265）：改为"继续输入将打断当前回复并追加"。

**index.html / app.css**：删除 `#queue-cards` 元素（index.html:187）与 `.q-card` 系列样式（app.css:675-714）。

### 测试 tests/test_server.py

- `test_ws_queues_second_turn_while_busy`（251）：改断言打断语义（`stop_requested=True`、消息进 pending、回执 `turn_interrupting`）。
- 删除 `test_ws_queue_promote_*`（267/288）、`test_ws_queue_remove_drops_pending_text`（306）、`test_promote_pending_*`（501/511）。
- 新增：`_start_pending_turn` 多条 pending 合并测试（文本 `\n\n` 连接、附件拼接、`turn_started` 带 items）。

### 验证

`python -m pytest tests/ -x -q` 全量通过；手动场景：生成中发一条（立即打断+气泡上屏+收束后自动接力）、收束窗口内连发两条（合并为一次响应）、打断后点停止（排队消息放回输入框）。