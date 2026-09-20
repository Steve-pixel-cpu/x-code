## 目标

解耦"模型看到的"与"用户看到的":压缩只发生在给模型构建请求的视图层,原始对话全量保留在内存与磁盘;UI 永远显示完整对话,`"This session is being continued..."` 大墙从根上消失。已与用户确认按此方案实施。

## 改动

### 1. `runtime.py` — 压缩改为请求期视图
- `_maybe_auto_compact()` 不再改写历史:过阈值时只置 `self._compact_active = True`(粘性,防"压缩→恢复→再压缩"振荡),首次激活触发一次 `_on_compacted()`;返回"本轮是否激活"供 `TurnSummary.auto_compacted`
- 新增 `_model_view()`:`_compact_active` 时返回 `compact_session(messages, max_estimated_tokens=0).compacted_messages`,否则原样返回;`run_turn` 的 `stream(messages=self._model_view())`
- `compact()`(手动 /compact)不再删历史:置 `_compact_active` + dry-run 统计,返回"已切换压缩视图,归档 N 条早期消息"
- 更新 `_on_compacted` 注释(与存储重写解耦)

### 2. `server.py`
- `set_on_compacted` 回调改为只 `broadcast({"type": "context_compacted"})`,删除 `_rewrite_after_compact`
- `persist_turn` 不变——存储只追加,`persisted_count` 永不失准

### 3. `storage.py`
- 删除 `rewrite_session` 与只为它存在的 `save_message_to`;`_read_entries` 注释改为泛指"原子替换过渡窗口"

### 4. 存量会话兼容(app.js)
- 回放时按前缀(`This session is being continued from a previous conversation`)识别旧的续接消息,渲染为一行式提示卡("此处之前的上下文已压缩供模型使用"),不渲染大墙正文

## 测试改动
- `test_auto_compact_signal.py` / `test_loop_budget.py` / `test_runtime.py`:断言改为"stream 收到的视图被压缩、内存历史不变、摘要不落盘"
- `test_incremental_persist.py`:删除 rewrite 用例
- `test_server.py`:删 `_rewrite_after_compact` 用例,新增 `context_compacted` 广播用例

## 验证
- `pytest tests/` 全量
- 手动:旧会话大墙变一行提示卡;auto-compact 当场出提示、模型行为正常;重载后完整对话可见;CLI `/compact` 文案正确