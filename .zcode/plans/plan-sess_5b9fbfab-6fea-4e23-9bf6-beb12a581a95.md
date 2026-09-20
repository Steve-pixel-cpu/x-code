## 目标

根治 `test_agent_tools` 的偶发失败(Windows 写竞态),并消灭生产代码里同类的数据丢失风险。

## 根因(两个叠加)

1. **固定名临时文件互踩**:`multi_agent.py` 的 `_persist_terminal_state`/`mark_delivered` 都用 `{agent_id}.json.tmp` 固定名。worker 线程写终态与 Leader `mark_delivered` 并发时:B 把 A 正在写的 tmp 截断重写,A 的 `os.replace` 把**半截 JSON** 发布出去 → 读者 `list_agents` 解析失败被吞 → 该 agent 从收割结果里"消失"(对应 `结果一 missing` 断言失败、`json.decode` 错误)。
2. **`os.replace` 无重试**:Windows 上目标文件被并发读者 `open()` 着(CPython 不带 FILE_SHARE_DELETE)或杀软扫描新建 tmp 的瞬时锁,replace 抛 `PermissionError`(对应 `multi_agent.py:309` 的报错)。Linux 上永远不会发生,所以只在 Windows 偶发。
3. **测试夹具引真实线程**(测试侧放大器):`fake_orchestrator` 夹具注释说"不真正起线程",但 `AgentOrchestrator(Path(tmp))` 没传 `spawn_fn`,走默认 spawn——每个用例起**真实 worker 线程**(还会打真实 LLM),线程晚到的 FAILED 终态覆盖测试写入的 COMPLETED → 状态翻转、断言随机失败。

同类隐患:`storage.py` 的 `rewrite_session` 也是固定 `.tmp` 名 + 无重试,生产里 auto-compact 重写会话文件时与 HTTP 读者(`load_session`)并发,同一失败类,一起治。

## 改动

### 1. 新模块 `fsatomic.py`(共享原子写工具)
- `unique_tmp_path(target)`:同目录唯一临时名(同卷保证 replace 原子;唯一后缀保证并发写者互不踩踏)
- `_replace_with_retry(tmp, target)`:`os.replace` 撞 `PermissionError` 时指数退避重试(10 次,累计约 1.5s,覆盖读者句柄/杀软窗口);其他 OSError 不重试
- `atomic_write_text(target, text)`:唯一 tmp + 带重试替换 + 失败清理残片
- `atomic_replace(tmp, target)`:给"自建临时文件再替换"的场景用

### 2. `multi_agent.py` 三处接线
- `_persist_terminal_state`:固定 tmp → `atomic_write_text`
- `mark_delivered`:同上
- `spawn_agent`:初始 manifest 目前是**直写目标文件**(并发 `list_agents` glob 到半截文件),也换 `atomic_write_text`

### 3. `storage.py` `rewrite_session`
- 固定 `.tmp` → `unique_tmp_path` 建临时文件(保留增量追加写法),收尾 `os.replace` → `atomic_replace`

### 4. `tests/test_agent_tools.py` 夹具修复
- `AgentOrchestrator(Path(tmp))` → `AgentOrchestrator(Path(tmp), spawn_fn=lambda job: None)`,兑现"不真正起线程"的注释(消灭真实线程打真网、晚到覆盖终态);顺带补 `mkdtemp` 的清理
- 核对过全部用例:没有任何用例依赖真实线程,`test_status_running_state_hints_polling`/`test_reap_skips_running_agents` 反而因此变确定(running 不再被真线程悄悄改成 completed)

### 5. 新增 `tests/test_fsatomic.py`(回归钉)
- 并发双线程原子写同一目标 × 多轮:无异常、目标始终是合法 JSON、无 `.tmp` 残留(旧实现在此必然互踩)
- 目标被读者句柄占住时:写线程阻塞在重试,读者松手后成功落盘(钉 Windows PermissionError 重试)
- POSIX 上这些测试同样通过(replace 语义无差异),CI 友好

## 验证
- `uv run pytest tests/test_agent_tools.py -q` **连跑 5 遍**全绿(此前每 2~3 遍就随机红)
- `uv run pytest tests/ -q` 全量无回归(含 `test_multi_agent.py`、`test_incremental_persist.py` 等涉及 storage 的用例)