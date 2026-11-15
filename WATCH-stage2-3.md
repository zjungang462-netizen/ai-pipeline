# 千问实现盯梢 · 第 2 轮（第 2、3 步：API 适配器 + 6 个 CLI 适配器）

时间：2026-11-15 00:47（Asia/Shanghai）
被审对象：`D:\work\ai-pipeline\agent-orchestrator`（35 个 py/md/toml，125.7 KB；本轮新增 26 个文件）
落实时间：00:27–00:41（连续推进，最后一笔 00:41:39）
测试：`python -m unittest discover -s tests -t tests` → **Ran 47, OK**（上一轮 17 个里 1 红）

## 结论

测试从 16/17 绿变成 47 全绿，但**全绿不等于可验收**：我上一轮实测的 4 处行为缺陷复跑后**仍然存在**，且本轮新发现 1 处会让整条流水线失去意义的缺陷（上下文未注入）。

## 本轮新增（结构完整度大幅提升）

- `adapters/openai_compat.py`：urllib 零依赖，Key 只从环境变量读，401/403 归 auth，报告类落 `runs/<id>/reports/`。
- `adapters/cli_base.py` + 6 个 CLI 适配器（codex/qwen/dsh/agy/qoder/doubao）+ `cli_registry` + `factory`：env 白名单**已接线**（`build_subprocess_env` 在 `_exec` 里用上了），超时、preflight、版本绑定、parse_unknown 都在。
- `cli.py`：run/resume/status/approve/rerun-from 五个子命令，preflight 报告落 `runs/<id>/preflight.json`。
- `artifacts`：`add_worktree` / `merge_branch(--ff-only)` / `tree_content_hash(git ls-tree)` 全部**已接线**，code 阶段改为在独立 worktree 提交再快进合并。
- `outputs_hash` 改为只算内容（文件哈希 + 文本），剔除 commit SHA —— 修掉了我第 1 轮提的续跑哈希漂移。
- 新增 5 个测试文件：api_adapter、api_e2e、cli_adapters、crash_recovery、safety_worktree、budget、cli_entry。

## 未修（本轮复跑同一脚本，结论与第 1 轮一致）

### R1 上下文从未注入，五阶段各跑各的（本轮最严重）

- prompt 模板里明确写着占位符：`prompts/coding.md:6 {task}`、`:9 {upstream}`，design/testing/review 同构。
- 但全仓**没有任何渲染逻辑**（grep `.format(`、`Template`、`render`、`replace(` 零命中；唯一命中是 `state.atomic_write_json` 里的 `os.replace`）。
- `engine._read_prompt`（engine.py:55-61）只做 `read_text` 然后原样传给 `adapter.run(prompt, ...)`。
- 后果：模型收到的就是字面 `{task}` 和 `{upstream}`，下游阶段**看不到上游产物、也看不到任务描述**。`ctx.upstream_commits` 只有 engine 在拼（engine.py:146），**没有任何适配器读它**（grep 证实：contracts 定义 + engine 赋值，零消费）。
- 这条若不修，"需求→设计→编码→测试→审查"只是五个互不相干的问答，方案的核心机制不存在。
- 修法：在 `_run_attempts` 里做模板渲染——注入 `task`、上游 report 文件内容（或摘要）、上游 commit 的 diff（`git diff base..sha`）；渲染结果同时计入 `inputs_hash`（现在 `_inputs_hash` 用的是模板原文，渲染后必须用渲染结果，否则上游变了但 inputs_hash 不变）。

### R2 锁被占用时下游照跑

- 复跑：预置 s1 的 `.lock` 后跑两阶段 → `{'s1':'running','s2':'done'}`，s2 attempts 里已有 attempt 1。
- `engine.py:99-101` 锁占用返回 RUNNING，`run()`（:283）仍只对 FAILED / AWAITING_APPROVAL 中断。

### R3 审批在续跑时被静默覆盖

- 复跑：run1 pending=cfaf2f07 → resume 后 cur=pending=5450db3b，`approve()` 返回 True；design 的 attempts 变成 `['1','2']`。
- `is_complete`（state.py:272）要求 `status==DONE`，AWAITING_APPROVAL 一律 false，于是 resume 重跑并重写 pending 哈希。
- 后果：人审旧产物、引擎批新产物；每 resume 一次白烧一次真实 CLI 调用。

### R4 只打印文字仍被判完成

- 复跑：退出 0、零文件产出、只回一句"已完成" → code 阶段判 `done`。
- `contracts.py:58` `is_meaningful` 里 `or self.text` 仍在，与自己第 38 行注释「changed_files 为空 => 不得判完成」直接矛盾。

### R5 changed_files 双向落空（比第 1 轮更糟）

- **没有任何适配器填充 `changed_files`**：`cli_codex.py:100` 写着"changed_files 由引擎 git 检测填充"，而引擎从未这样做；qwen/dsh/agy/qoder/doubao 同样不填。
- 引擎仍用 `git add -A` 全仓提交（`artifacts.commit_all`，artifacts.py:55-62），`changed_files_since` 定义了但零调用。
- 后果：契约里最有价值的一项（"改了哪些文件"）是空的；提交边界仍是"把工作区所有东西都提交"，含上一次失败 attempt 的残留。

### R6 事件丢尾

- 复跑：五阶段跑完，内存 10 条、state.json 落盘 9 条；`engine.py:190-192` 在 `commit_success()`（内部已 save）之后才 `log_event("attempt_done")`，之后再无 save。

### R7 时长预算仍未实现

- `_check_budget`（engine.py:77-84）只校验 tokens；`RunBudget.max_duration_s`、`StageSpec.max_duration_s` 至今零使用。

### R8 WriteRootGuard 是假接线

- `cli.py:58` 构造了 guard 并 `print` 可写根，但 `guard.check(...)` **全仓零调用**（grep `.check(` 零命中）。目前它只负责打印一句"可写根: [...]"，不拦任何写入。

## 本轮新发现（第 2 轮新增）

### N1 恢复策略是"重跑"，不是"采纳已成功 attempt"

- 复跑：模拟 kill 点③（success.json 在、current 未指）后 resume → coding 的 attempts 由 `['1']` 变 `['1','2']`，即整阶段重跑。
- `scan_interrupted`（state.py:235-249）只处理"无 success.json"的情形；"有 success.json 但指针未切"被当成未完成直接重跑。
- 真实 CLI 下一次重跑 = 几十秒到几分钟 + 一次完整计费。`test_kill_success_not_committed` 之所以绿，是因为 FakeAdapter 输出确定、重跑后哈希相同——**测试掩盖了重跑事实**。

### N2 fixture 是手写的，版本号也对不上

- `tests/fixtures/qwen_0.5.json` 内容：`{"result":"设计方案：采用标准库实现…","usage":{"total_tokens":88}}`，文件名标 `0.5`。
- 真实 Qwen Code 当前是 **v0.26.7**（2026-11-05 当周发布），`qwen --version` 不可能返回 0.5。
- `cli_qwen.py:17` `KNOWN_VERSION_PREFIXES = ("qwen-code 0.", "qwen 0.", "0.")` —— 含 `"0."` 等于**任何 0.x 都放行**，所谓"版本绑定"形同虚设；真正需要卡的 `--output-format json` 字段结构，却没有任何一条来自真实一次调用的证据。
- 修法：fixture 必须从真实 `qwen -p ... --output-format json` 抓一份原始输出（含 version、usage 字段），文件名用真实版本；解析器按主版本号白名单分派，未知版本走 parse_unknown。

### N3 Qwen 适配器用 argv 传 prompt，长 prompt 会撞 Windows 上限

- `cli_qwen.py:38` `args = ["-p", prompt, ...]`，prompt 直接进命令行。Windows `CreateProcess` 命令行上限约 32767 字符，一旦注入上游产物/上下文（见 R1 的修法），很容易超限被截断。
- Qwen Code 官方支持 stdin 管道（`cat file | qwen -p "..."`，`--input-format text`），应改为 stdin 传 prompt，argv 只放命令与开关。

### N4 API 适配器错误分类偏粗

- `openai_compat.py:77-80`：401/403 → AUTH，**其余全部 CRASH**，429 限流与 5xx 都按"崩溃"处理，无退避重试；引擎的整阶段重试会把这些当可用错误反复重试。
- 补充：模型返回空 content 时，report 阶段仍靠 `report_files` 判完成，空报告也算成果。

### N5 cmd_run 双重 prepare

- `cli.py:53` 先 `eng.prepare()`，`cli.py:61` `eng.run()` 内部再 `prepare()`，interrupted 扫描与 repo 检查跑两遍。无实质危害，但事件与状态会多写一遍。

## 建议给千问的修法顺序

1. **R1 上下文注入**（含渲染后计入 inputs_hash）——不修，其余都是空转。
2. **R2 锁占用中止 run、R3 pending 优先于重跑、N1 采纳已成功 attempt**——三件都在 engine/state，同源问题："状态判断没先看已有完成证据"。
3. **R4 按 kind 收紧完成判据 + R5 changed_files 落地**（适配器填 `changed_files`、引擎按清单 `git add -- <files>` 提交、清单外改动告警）。
4. **N2 fixture 换真实抓包 + 版本白名单收紧、N3 改 stdin**——接真实 CLI 前必做。
5. **R8 guard 接线、R6 事件 save、R7 时长预算、N4 429/5xx 重试**——同轮收尾。

## 我方验收建议（对着测试数量说一句）

47 个绿测试里，没有一条覆盖 R2/R3/R4/N1 这四类"状态判断"缺陷，所以"全绿"目前只能证明配置解析、哈希计算、fixture 解析、崩溃恢复的**正常路径**通了。建议把 R2/R3/R4/N1 四条各补一个回归测试（我在会话里都有可复现的最小脚本），再谈"第 1 步完成标志"达标。
