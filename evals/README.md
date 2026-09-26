# x-code evals

Agent 级评测框架:用真实的 `main.py -p` 跑任务夹具,用确定性断言 + LLM 判分
打分,产出可对比的回归基线。定位:改 prompt、调压缩参数、上新功能(记忆、
Session Memory)之后,跑一遍就知道整体是变好还是变坏。

## 用法

```bash
uv run python evals/run_evals.py                        # 全量, 有基线则自动对比
uv run python evals/run_evals.py --only fix-failing-test  # 只跑指定任务
uv run python evals/run_evals.py --save-baseline        # 本次结果存为基线
uv run python evals/run_evals.py --list                 # 只列任务不跑
uv run python evals/run_evals.py --keep-workspaces      # 保留工作区排查 Agent 行为
uv run python evals/run_evals.py --timeout 900          # 调单任务超时
```

- 退出码:全过 0,有失败 1(CI 可直接当门槛用)
- 结果存 `evals/results/<时间戳>.json`,报告写 `evals/report.md`,
  基线在 `evals/baseline.json`(可提交,让回归对比随仓库走)
- **跑真任务要花钱**:每个任务是一次完整的 Agent 会话(几千到十几万 token)。
  `--only` 挑任务、先小后大。

## 判分模型

每个任务得分 = 确定性 checks 全过 **且**(有 judge 时)LLM 判分通过。

| 判分方式 | 何时用 |
|---|---|
| checks | 结果可用代码断言:测试过没过、文件内容对不对、旧标识是否清干净 |
| judge | 结果开放:code review 质量、文案、没有唯一正确答案的任务 |
| checks+judge | 两者都要:既要客观正确,又要主观合格 |

Agent 层失败(超时 / 崩溃 / `is_error`)不直接判死,checks/judge 说了算;
但 `--output-format json` 都拿不到(崩溃)自然全断言失败。

## 加一个任务

```
evals/tasks/<任务名>/
  task.txt    必有 — 发给 Agent 的任务文本
  project/    必有 — 夹具项目,每次运行复制到全新临时工作区(原夹具不被污染)
  checks.py   可选 — 定义 evaluate(workspace) -> list[Check]
  judge.txt   可选 — 给 LLM 判分器的判分标准
```

`checks.py` 里可用的现成断言件(`from harness import ...`):

- `pytest_check(workspace)` — 在工作区跑夹具测试(用仓库 venv 的 pytest)
- `file_unchanged(workspace, rel, ORIGINAL_DIR)` — 关键文件必须原样
  (防 Agent 改测试凑绿、删文件消音)
- `text_absent(workspace, "旧名")` — 重命名类任务的残留检查

写 `task.txt` 的经验:给 Agent 一条可靠的跑测试命令(`uv run --with pytest pytest -q`,
裸目录可用);任务约束写明确("不要修改 test_xxx.py");让 Agent 把关键产物
贴进最终回复(judge 只看最终回复)。

夹具错误(缺判分方式、缺 task.txt)在发现时就报错,不会混进评测结果。

## 设计要点

- **被测 Agent 是 subprocess**:走仓库真实入口 `main.py -p --output-format json`,
  退出码 / `subtype` / usage 全部进入结果记录;evals 不 import 被测代码,
  测的是用户实际拿到的东西
- **工作区一次性**:每个任务每次运行都是全新复制,可重复
- **--agent-cmd 可替换被测 Agent**:管线自测不烧 token
  (tests/test_evals_harness.py 用桩 Agent 端到端测本框架)
- **判分 side-call 与被测会话分离**:judge 用 `generate_text`(与自动命名
  同通道),以后接 utilityProvider 换便宜模型只动 `harness.make_judge_client`
