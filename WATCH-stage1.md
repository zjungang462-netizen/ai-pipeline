# 千问实现盯梢 · 第 1 轮（骨架+离线引擎）

时间：2026-11-15 00:35（Asia/Shanghai）
被审对象：`D:\work\ai-pipeline\agent-orchestrator`（9 个源文件，约 49 KB，落盘时间 00:21:53–00:25:05）
审查方式：逐文件读代码 + 独立复现（复现脚本在会话工作区，未改动千问任何文件）
结论：骨架与我方评审的 4 条 blocker 基本对上，但**测试 17 个里 16 绿 1 红**，另有 5 处已实测确认的行为缺陷。

## 已落地（与 REVIEW.md 对应）

- inputs_hash + outputs_hash 双元组判据（B1）：`state.compute_inputs_hash` 拼 prompt 模板 + 上游哈希 + config_keys，`is_complete` 双哈希比对。
- stale 传播（B1）：`mark_stale_downstream` 把该阶段及下游置 STALE。
- attempt 目录隔离 + `success.json` + 原子切 current（B4）：`new_attempt_dir` / `commit_success` / `atomic_write_json`。
- 启动扫描 interrupted（B4）：`scan_interrupted` 把无 success.json 的 current attempt 标 INTERRUPTED。
- 无回退、改 rerun-from 重跑（B2）：engine 文档与 `rerun_from` 一致。
- 适配器契约扩展（M1）：Result 含 changed_files / commit_sha / branch / report_files / usage / error_kind / tool_version / parse_unknown。
- 零外部依赖（我方建议）：TOML + 手写校验替代 pydantic/YAML。
- 实测有效：离线五阶段跑通、二次续跑全 `skip_done` 且 outputs_hash 不变（我单独复现过）。

## 缺陷（已实测，按严重度排序）

### D1 事件日志丢尾（证据链缺最后一条）

- 现象：`_run_attempts` 成功路径里 `commit_success()` 内部 save 之后才 `log_event("attempt_done")`，之后再无 save。进程结束时，最后一条 attempt_done 只在内存里。
- 证据：五阶段跑完，内存 events 10 条、state.json 落盘 9 条，缺 review 的 attempt_done。
- 影响：事件流不能作为完成证据；崩溃恢复时无法判断最后一阶段是否真跑完。
- 修法：`log_event` 后就 save，或在 `run()` 收尾统一 save 一次；建议后者（少写盘）。

### D2 锁被占用时下游照跑

- 现象：`_run_stage` 拿不到锁返回 `RUNNING`，`run()` 只在 FAILED / AWAITING_APPROVAL 处中断，于是继续下一阶段。
- 证据：预置 s1 的 `.lock`（mtime 新鲜）后跑两阶段，结果 `{'s1':'running','s2':'done'}`，s2 attempts 目录已生成 `1`。
- 影响：双进程或上次进程未退净时，下游在**没有上游产物**的情况下执行，产出与上游无因果关系。
- 修法：锁占用必须中止整个 run（返回 RUNNING 并在 `run()` 里 break），或抛 `LockBusy` 由调用方决策。

### D3 审批在续跑时被静默覆盖

- 现象：AWAITING_APPROVAL 不满足 `is_complete`（要求 status==DONE），resume 会把这阶段**重跑一遍**；重跑后 `pending_approvals[stage].outputs_hash` 被新产物哈希覆盖。
- 证据：run1 pending=cfaf2f07；resume 后 cur=5450db3b、pending=5450db3b，`approve()` 返回 True。
- 影响：人审的是旧产物、引擎批的是新产物，哈希校验退化为例行通过；每次 resume 还白烧一次真实 CLI 调用（真实工具是按 token 计费的）。
- 修法：`run()` 开头先处理 `pending_approvals`——存在未决审批就返回 AWAITING_APPROVAL，不重跑；pending 里的哈希只写一次，绝不在重跑时覆盖。

### D4 只打印文字也被判完成

- 现象：`Result.is_meaningful()` 为 `changed_files or commit_sha or report_files or text`，`text` 一项就够。
- 证据：构造"退出 0、只打印『已完成，无需改动』、零文件产出"的适配器，code 阶段判定 `done`。
- 影响：与 `contracts.py` 自己的注释「changed_files 为空 => 不得判完成」直接矛盾；真实 CLI 空跑（例如只回一句"我看过了"）会被当成功。
- 修法：按阶段 kind 收紧——code 类要求 `changed_files or commit_sha`，report 类要求 `report_files`；`text` 只能作为留证，不参与判完成。

### D5 changed_files 不参与提交边界

- 现象：`_archive` 对 code 阶段直接 `git add -A` + 全仓提交（`artifacts.commit_all`），`result.changed_files` 只用过一次（判有意义）。
- 影响：失败 attempt 留下的半成品改动，会被下一次成功 attempt 一起提交，产物与"这次适配器实际改了什么"脱钩；也无法拦越界写入（本机无 git，此项未实测，属代码可见缺陷）。
- 修法：以 `changed_files` 为提交清单（`git add -- <files>`），变更清单外的工作区改动一律记告警并拒绝提交。

### D6 三件安全设施是死代码

- `artifacts.add_worktree` / `remove_worktree`、`safety.WriteRootGuard`、`safety.build_subprocess_env` 全仓仅定义、零调用（grep 证实）。
- 后果：engine 直接把 `config.task_workdir` 交给适配器，code 阶段在主干工作区提交；**没有 per-stage worktree 隔离**；CLI 子进程 env 白名单与可写根校验都还没接线。
- 定位：stage1 只跑 FakeAdapter，属正常欠账；但必须在接真实 CLI 之前补上，否则 B3/M4/M5 三条评审结论等于没落地。

### D7 时长预算静默无效

- `RunBudget.max_duration_s` 与 `StageSpec.max_duration_s` 定义后没有任何使用点，`_check_budget` 只校验 tokens。
- 影响：配置里写 `max_duration_s` 不生效，用户以为有闸门。

### 次要

- `StageLock.stale_s` 默认 3600：真实 CLI 阶段超过 1 小时仍持有锁时，另一进程会把活跃锁当陈旧锁删掉，独占性失效。
- `Engine.__init__` 的 `task` 会被 `state.load()` 覆盖，续跑时 task 描述可能与命令行传参不一致。
- `config.py` 对 `max_tokens` / `max_attempts` / `max_duration_s` 未做类型校验，TOML 里写成字符串会穿透到运行时比较处。

## 测试现状

`python -m unittest discover -s tests -t tests`：Ran 17，16 绿，1 红。

- 失败：`test_resume_idempotent`——断言 `events2 == []`，实际拿到 4 条 `attempt_done`。根因是 D1（丢尾）+ 断言语义错（`state.load()` 会恢复历史 events，断言不能假设事件流为空）。续跑本身是幂等的（我单独复现全 skip_done），所以这条测试失败不代表续跑坏了，但它暴露了 D1。
- 建议补测：锁占用时下游不得执行（D2）、审批续跑不得重跑（D3）、text-only 不得判完成（D4）。这三条现在都能稳定复现。

## 我方建议的下一步顺序

1. 先修 D1、D2、D3、D4（都在 engine/state 两个文件内，改动小、回归可测）。
2. 把 D4 的判据写进 stage1 测试，作为接真实 CLI 前的闸门。
3. 接 CLI 之前完成 D6 接线（worktree + env 白名单 + 可写根），并与 D5 的 `changed_files` 提交边界一起做——这三件是同一个"产物可信"问题。
4. D7 与次要项可并入同一轮，不单独排期。
