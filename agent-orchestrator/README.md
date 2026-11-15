# 多智能体开发流水线编排器

把 **Codex、千问 Qwen Code、豆包、Antigravity、Qoder、dsh（DeepSeek Harness）** 等真实编码工具按角色分工，串成「需求 → 设计 → 编码 → 测试 → 审查」自动化流水线。

核心是**轻量编排引擎 + 可插拔适配器**，而非再包一层 LLM 框架。每个工具一个适配器，按配置把角色映射到工具，发挥各自所长。

## 2026-11-15 修复版（当前行为）

本节优先于后面的历史演进记录。保持零第三方依赖、阶段串行和现有适配器接口。

### 有上限的无人值守模式

原有配置继续按人工审批模式工作。新的 `unattended.example.toml` 显式预授权可修改文件、可自动批准阶段、调用预算和总返工轮数：

~~~powershell
python -m orchestrator.cli unattended unattended.example.toml --run-id auto-demo-1 --task "clean_name 去除首尾空格并转小写，parse_count 只转整数。" --max-restarts 2
~~~

此示例会实际调用配置的 API，修复 `examples/unattended_demo` 中故意保留的两个缺陷。测试不在可写清单中。用于其他项目时，先设置测试命令、`allowed_files`、`context_files` 与 `baseline_paths`；不要把整个用户目录列入基线或可写范围。

- 新任务先运行 `precheck_cmd`，之后每轮都提供最新文件原文、测试失败标识和有界输出。实际文件改动必须在白名单内，超过当轮文件批量会被拒绝并要求缩小补丁。
- 测试失败或带文件位置的审查失败会自动返回编码阶段；格式/网络异常在 `max_attempts` 内纠正重试。未解析结论不会自动批准，重试耗尽会停止。
- `max_repair_rounds` 限制整个反馈链，阶段 `max_rounds` 仍生效。阶段/run 预算累积不重置，输出 token 受实际 API 参数上限约束。发送前持久化预算预留，连接中断或进程退出后未知用量不会当成免费调用。
- `auto_approve` 只对显式列出的阶段生效，审批仍需通过产物校验。原来的未预授权人工审批不会被悄悄跳过。
- supervisor 固定配置和任务，worker 意外退出时最多重启指定次数；测试失败、认证失败和用完轮次不会被无限重启。重复调用同一已完成任务仍校验产物；新任务使用新的 run_id。
- supervisor 自身被操作系统结束后需再次启动命令；状态可以恢复，但总墙钟预算不会重置。本次没有安装系统服务或计划任务。worktree 和路径白名单不等于执行沙箱，测试命令应来自可信配置。

### 自动策略试用

`[autotune] enabled=true, auto_apply=true` 只在 `[autonomy] enabled=true` 时可用。至少收集 5 条同配置、用量已知的终态记录后，可自动产生并选择后续任务的配置副本：

- 允许减少反馈轮次上限，或在 `max_batch_size` 的预授权范围内将 `repair_batch_size` 加 1。
- 不自动增加轮数、预算、可写文件、命令或模型权限；不修改引擎源码及原始配置。
- 试用 token 中位数退化超过 20%，或连续两次调整未改善失败率时停止建议，并回退到来源策略。配置或副本哈希变化时拒绝自动使用。
- `python -m orchestrator.cli autotune 配置.toml` 可立即按当前预授权策略生成下一次配置；默认 `tune` / `approve-tune` 人工模式仍保留。

本轮已完成同一受控双缺陷样例的 5 次基线和 5 次自动策略试用：10 次均通过，真实 API 共 25 次，17,417 tokens。基线每任务 3 次调用、自动返工一次；试用每任务 2 次调用，无需返工。token 中位数 2027→1464。此结果验证机制及该样例收益，不代表不同业务任务的成功率保证。完整证据见上级 `autonomy-2026-11-15/`。

真实自验证补充：实际 API 审查、模型精确补丁、真实测试和模型复审已经在隔离副本中跑通。试跑也暴露了元数据根类型错误可导致恢复崩溃，以及整文件输出/连接截断的不稳定性；已补齐结构校验及有界精确补丁协议。详细失败记录与范围见上级目录 `live-validation-2026-11-15/`，不将此结果等同于无人值守交付。

- 所有 review 都检查独立结论，是否设置 feedback_to 只影响自动返工。缺失/非法结论待人工确认；无返工目标的 fail 保持 failed。
- 每个 attempt 归档 review_verdict.json、manifest.json、decision.json。缓存命中、恢复和审批都重新校验文件内容与代码树；审批绑定具体 attempt 和输入/输出哈希，凭据写入 approval.json。
- 新状态使用 schema_version=2。旧记录缺少完整证据时不会直接采纳，会重新执行；升级前请先备份 state_dir。旧代码分支可审计保留。
- run/resume/approve/rerun 先取得 run 写锁，再读取状态；修改或测试同一工作区的 run 另有工作区锁。锁占用返回 running，不继续下游。
- 输入指纹覆盖实际提示词、适配器配置、命令 argv 和执行协议。新开启审批会对现有有效产物建立申请，不无谓重新生成。
- 元数据即使是语法合法的 JSON，也必须有正确的对象结构；非法 manifest/success/decision/approval 不得让恢复路径泄漏 AttributeError。
- API 保留 `# file:` 完整文件协议，增加 `# patch:` + JSON `edits` 精确替换：目标必须是工作区内不超过 2 MiB 的现有文件，最多 32 项替换，原文必须唯一匹配。支持有围栏代码块和无散文的显式补丁流。产物协议失败仍保留已返回的 API 用量。
- test 与模型阶段均检查预算和审批；时长预算累计到阶段与 run。测试优先运行在上游 commit 的独立 worktree，无上游代码时才使用任务目录并计算文件指纹。worktree 创建失败不回退到用户工作区。
- 父目录快照默认关闭。只有显式 watch_root 才扫描；默认仅告警，watch_enforce=true 才据此失败。API 读写均校验实际路径边界。
- Git 文件清单使用 NUL 分隔，支持中文；提交只包含声明文件，保留无关暂存内容。CLI 恢复执行前会补齐版本探测；真实 CLI stdin/格式仍需真机验证。
- 测试输出流式保留有界尾部，完整测试记录注入审查；上下文目录遍历提前剪掉依赖和缓存，每次 attempt 复用同一份已参与指纹的提示词。

### 离线策略优化

终态 run 写入不可变 metrics.json；等待审批和锁占用不会提前固化指标。对已结束 run 使用同一 run_id 重跑时保留首份指标，比较策略请使用新的 run_id。

~~~powershell
python -m orchestrator.cli tune --state-dir .orchestrator --config pipeline.example.toml --last 20
python -m orchestrator.cli approve-tune --state-dir .orchestrator --output pipeline.tuned.toml
~~~

配置路径唯一时 tune 可以省略 --config。至少需要同一配置的 5 条有效终态指标。

- 默认人工模式建议已有反馈阶段的 max_rounds 增减 1；启用 autonomy 后还可提议受限 repair_batch_size。提示词和测试命令提案暂不自动生成。
- tune 只生成 tuning/tune_proposal.json，不改变下一次 run 的配置；approve-tune 仅写入原配置同目录下尚不存在的副本，保留相对路径和原文件。
- 提案来源配置发生变化、包含不允许的字段或没有调整项时拒绝批准。新配置记录 tune_proposal_id。
- token 中位数相对基线上升超过 20%，或连续两次调整未降低失败率时，停止提出新的调整。
- 并行阶段、模型自动修改引擎源码、自动放宽预算/权限均未引入。


## 特点

- **零外部依赖**：仅 Python 3.11+ 标准库（`tomllib`/`urllib`/`unittest`/`dataclass`）。无需 pip 安装即可运行与测试。
- **上下文接力（L1）**：每阶段提示词渲染注入任务描述 + 上游报告全文 + 上游代码 diff 摘要；渲染结果计入 `inputs_hash`，上游变了下游自动 stale。五阶段真正接得上话，不是各自盲跑。
- **审查反馈环（L2）**：审查阶段输出结构化结论（`verdict` + `issues`）；不通过时引擎把问题清单反馈回编码阶段，自动重跑 编码→测试→审查，最多 `max_rounds` 轮、每轮计入预算，超限判失败。审查员与开发者通过引擎中介"辩论"，确定性、可离线测试、续跑一致。
- **续跑可靠**：完成判据 = `(inputs_hash, outputs_hash)` 双元组；attempt 目录隔离 + 原子切 current；崩溃中断可恢复；已成功 attempt 指纹匹配则**采纳不重跑**；未决审批续跑时**不重跑、不覆盖**。
- **产物即 commit**：代码阶段在独立 git worktree 执行（基于上游 commit 拉分支），**按变更清单提交**（非 `git add -A` 全仓），合并回主线；报告类产物落盘 + 哈希。可 diff、可复现。
- **完成判据按类型收紧**：code 阶段必须有文件变更/commit，report 阶段必须有报告文件；只打印文字不算完成。
- **安全约束**：可写根白名单（执行前校验 workdir + 执行后扫描根外改动）、子进程 env 白名单（API Key 不透传给 CLI）、`--yolo` 须显式开启、审批校验产物哈希、锁占用中止整轮。
- **版本绑定解析**：CLI 解析按工具名前缀白名单匹配，不识别的版本 => `parse_unknown` 禁止判完成。
- **预算上限**：阶段级与 run 级 token **和时长**预算，超限即 rejected；429/5xx 归为限流并指数退避。

## 快速开始

### 1. 离线演示（无需任何 Key/工具）

```powershell
cd D:\work\ai-pipeline\agent-orchestrator
python -m unittest discover -s tests        # 134 个离线测试（git 可用时需数分钟）
```

> **git 前置（重要）**：代码阶段（worktree/commit/merge/变更探测）依赖 git。
> - git 在 PATH，或在配置 `git_exe` 填绝对路径，或命中兜底（**Windows 注册表 InstallPath** + Program Files 推导）=> 全部 134 个测试真跑、零跳过。**注意**：git 可用时每个用例都真跑 git 子进程（Windows 上单次约 0.2–0.5 秒），全量需数分钟，属正常，不是卡死。
> - git 完全不可用 => 3 个 `TestWorktree` 用例被 **skip**，且**代码阶段会明确报错**（不再静默降级把"文件写成功"误判成"空跑"）。
> - 纯 API 流水线的 coding 阶段用 `openai_compat`（从代码块写文件），同样依赖 git 来隔离、归档代码。

### 2. 纯 API 流水线（只需一个 Key）

```powershell
$env:DASHSCOPE_API_KEY = "sk-你的百炼Key"
python demo/run_api_demo.py "做一个命令行待办事项工具"
# 或用火山方舟：
$env:ARK_API_KEY = "你的方舟Key"
python demo/run_api_demo.py "做一个命令行待办事项工具" ark
```

### 3. 完整流水线（配置文件驱动）

```powershell
Copy-Item config.example.toml config.toml    # 按需修改角色→工具映射
$env:DASHSCOPE_API_KEY = "sk-..."
python -m orchestrator.cli run config.toml --task "需求描述" --run-id myrun
python -m orchestrator.cli status config.toml --run-id myrun
python -m orchestrator.cli approve config.toml --run-id myrun --stage design
python -m orchestrator.cli resume config.toml --run-id myrun
python -m orchestrator.cli rerun-from config.toml --run-id myrun --stage coding
```

## 命令说明

| 命令 | 作用 |
|---|---|
| `run` | 启动新流水线（先 preflight，落盘环境证据） |
| `resume` | 断点续跑（只信完成标记，输入变化自动重跑下游） |
| `status` | 查看各阶段状态、attempt、产物哈希、用量 |
| `approve` | 批准某阶段（校验产物哈希未变才放行） |
| `rerun-from` | 从指定阶段重跑（**覆盖**，旧产物归档到 attempts/） |

> **重跑语义**：MVP 不提供"回退"。`rerun-from` 是重跑覆盖，旧产物保留在 `attempts/<n>/` 可审计。CLI→API 自动回退留作 v2（换通道模型不同、产物不等价）。

## 审查反馈环（L2）

让审查阶段不只是终点站，而是反馈源——审查不通过时自动返工，形成"审查员↔开发者"的闭环。

**配置**（审查阶段）：
```toml
[[stages]]
name = "review"
kind = "review"          # 必须是 review 类型
feedback_to = "coding"   # 不通过时反馈回哪个阶段（须在其之前）
max_rounds = 3           # 最多 3 轮（含首轮审查），超限判失败
```

**运行机制**：
1. 审查阶段按 `prompts/review.md` 约定把结论写到**独立文件** `review_verdict.json`（Markdown 报告不参与判定）。
2. 引擎只对 `runs/<run_id>/stages/<stage>/attempts/<n>/review_verdict.json` 做 `json.loads` + 严格校验；缺失/非法 => **待人工确认**，不从正文推断。
3. `fail` 且**有文件级问题**且轮次未尽：只把「文件/问题/期望」条目 + 被点名文件内容注入 `coding` 的 `{feedback}`，跳回 coding 重跑（不重贴需求全文与累积 diff）。
4. `fail` 但**没有文件级问题** => 停下等人处理，不自动「全面自查」烧一轮。
5. `pass`（issues 须为空）：清除反馈，正常结束；轮次耗尽仍 `fail` => 判 `failed`。
6. 配了 `kind = "test"` 阶段时，**测试退出码非零直接触发返工**，不经审查模型复述。

**一致性保证**：反馈文本与失效计数（`invalidate_nonce`）都持久化并计入 `inputs_hash`——返工后旧产物不被误采纳，续跑时反馈状态完整恢复。每轮调用都计入 token/时长预算，超限即 rejected。

> L3 实时群聊（多 Agent 轮流发言）**不自研**：真实 CLI 都是一次性无头，群聊只能靠累积对话反复调用模拟，token 成本高、难收敛；真需要这层直接用 AutoGen/AG2。

## 各工具接入与登录

> **默认通道是 `openai_compat`**（唯一跑通全链的路径）。下表 CLI 适配器留在注册表，但**默认不参与流水线**：
> 各工具是否真从 stdin 读取、输出格式如何，尚无真机证据。启用前请先抓一次真实 `--version`
> 和一次真实成功输出，替换 `tests/fixtures/` 下的合成样本；版本对不上就停在待确认。

| 工具 | 适配器 type | 无头命令 | 认证 |
|---|---|---|---|
| Codex | `cli_codex` | `codex exec ... --json` | `CODEX_API_KEY`（CI）或交互登录 |
| 千问 Qwen Code | `cli_qwen` | `qwen -p ... --output-format json` | `OPENAI_API_KEY/BASE_URL/MODEL` |
| 豆包 | `cli_doubao` | `doubao -p ...` | `doubao login` 扫码（无 API-key） |
| Antigravity | `cli_agy` | `agy -p ... --output-format json` | 需先交互式登录一次 |
| Qoder | `cli_qoder` | `qoder -p ... --output-format json` | `QODER_PERSONAL_ACCESS_TOKEN` |
| dsh | `cli_dsh` | `dsh --profile headless ...` | 见 DeepSeek Harness 文档 |
| 百炼/方舟 API | `openai_compat` | HTTP | `DASHSCOPE_API_KEY` / `ARK_API_KEY` |

**绝对路径配置**：preflight 不假设工具在 PATH。Windows 上 npm 全局包常在 `%APPDATA%\npm`，若探测不到，在适配器配置 `exe = "C:\\完整路径\\工具.cmd"`。每次运行的探测结果落盘到 `runs/<run_id>/preflight.json`。

**密钥安全**：API Key 只注入 `openai_compat` 适配器；CLI 子进程用 env 白名单，默认不透传任何 Key。某 CLI 自身需 Key 时，用 `extra_env = ["变量名"]` 显式开启。

**WorkBuddyAI**：腾讯桌面办公 Agent，目前无官方 CLI/API，**不纳入编排**。未来若提供 CLI，Adapter 契约不变即可接入。

## 项目结构

```
orchestrator/
  contracts.py   # Result/StageSpec/RunContext/ErrorKind
  config.py      # tomllib + 手写校验
  engine.py      # 阶段状态机/重试/审批/预算
  state.py       # 双哈希/stale/attempts/租约锁/原子持久化
  artifacts.py   # commit/branch 产物 + worktree + 树哈希
  safety.py      # 可写根/env 白名单/preflight（快照默认只告警）
  review.py      # 结论文件严格校验（不从散文推断）
  testrun.py     # 测试阶段真实执行命令 + 记录退出码/耗时/输出
  cli.py         # run/resume/status/approve/rerun-from
  adapters/      # base/fake/openai_compat/cli_*/factory/registry
prompts/         # 阶段提示词模板（review 要求写 verdict.json）
demo/            # 纯 API 端到端演示
tests/           # 134 个离线测试 + fixtures（含合成样本说明）
config.example.toml
pipeline.example.toml   # 推荐流水线：含真测试命令 + 结论文件审查
```

## 测试

```powershell
python -m unittest discover -s tests -v
```

覆盖：配置校验、双哈希/stale、attempts 隔离/中断扫描、租约锁、五阶段离线跑通、三种 kill 点续跑一致、空跑拒绝、重试、审批哈希校验、API mock（正常/401/超时/解析失败）、CLI 版本绑定解析、中文+ANSI+超长输出、env 白名单、可写根越界、worktree 传递与含空格中文路径、CLI 入口冒烟、预算超限，以及四个阻断项回归（锁占用中止、审批续跑不覆盖、纯文字拒判完成、已成功 attempt 采纳不重跑）。

**需真实环境联调**（离线测试不覆盖，不假装已验证）：
- 真实 Key 冒烟（百炼/方舟）。
- 各 CLI 安装与登录（豆包扫码/agy 登录）。
- **CLI fixture 目前是手写合成样本**（见 `tests/fixtures/README.md`），真实工具输出格式须真机抓包替换后核对；qwen 真实版本为 0.26.x，合成样本标 0.5 仅为占位。

## 设计依据

本实现采纳了 `D:\work\ai-pipeline\REVIEW.md` 的全部 4 个 blocker（输入指纹、回退语义、worktree 产物传递、重试竞态）与 6 个 major（Result 契约、版本绑定、preflight 实测、env 泄漏、可写根、审批对产物），以及 8 个补测用例。

第二轮审查（`D:\work\ai-pipeline\审查报告-千问实现.md`）指出的 4 个阻断项与 6 个重要项已全部修复：
- **B1** 提示词渲染注入（task + 上游报告 + 代码 diff），渲染结果计入 inputs_hash。
- **B2** 锁占用中止整轮，下游不在无上游产物时照跑。
- **B3** 未决审批续跑时不重跑、不覆盖 pending 哈希。
- **B4** 完成判据按 kind 收紧；适配器填 changed_files；引擎按清单提交（非 add -A）。
- **M1** 已成功 attempt 指纹匹配则采纳不重跑。
- **M2** 可写根守卫真正接入（执行前校验 + 执行后扫描根外改动）。
- **M3** 时长预算实现（阶段级 + run 级）。
- **M4** 版本白名单收紧为工具名前缀；fixture 如实标注为合成样本待真机抓包。
- **M5** qwen prompt 改走 stdin，避免撞 Windows 命令行长度上限。
- **M6** 429/5xx 归为限流并指数退避；API 编码阶段提取代码块写盘。

L2 审查反馈环（`审查报告-L2反馈环.md` 实测三条通过）后，第三轮实测硬缺陷已修：
- **G1** git 探测加兜底路径 + 配置 `git_exe`；代码阶段缺 git **明确报错**，不再静默降级误判空跑。
- **G2** 越界检测改为执行前后**可写根快照对比**，真正捕获 git diff 清单之外的根外写入。
- **G3** 审查结论不可解析 => **待人工确认**（AWAITING），不再默认放行。
- **G5** CLI 适配器按 `ctx.kind` 为 report/review 阶段写报告文件，同一适配器可兼任 coding 与 review。
- **G7**（qwen `-p -` 的 stdin 语义）仍标注为待真机验证，离线无法证明。

第四轮复验（G1/G2/G3/G5 全部确认真生效，零跳过）后修复：
- **N1** `rerun-from` 现在会清未决审批 **并递增失效计数**——只清 pending 不够，否则 `adopt_successful` 会重新采纳旧 success.json，把"没结论的审查"静默当通过。修复后 awaiting 的审查可经 `rerun-from` 真正再审一遍。
- git 兜底改读 **Windows 注册表 InstallPath**（机器无关），去掉硬编码盘符。

第五轮（DeepSeek 真实模型全链实测）修复：
- **API 适配器 review 类型**：此前只判 `kind=="report"`，`review` 掉进代码分支被误当代码提取 → 判空跑。已改为 `kind in ("report","review")`。
- **M0 审查上下文**：审查阶段此前只拿 `commit~1..commit` 末次提交 diff，返工轮里看不到完整实现，误判"实现代码缺失"。改为**相对项目基线的累积全量 diff + 变更文件清单**（`cumulative_diff`）；其他阶段仍用末次摘要。

**真实模型验证结论**（DeepSeek `deepseek-chat`，全链五阶段）：
- L1 上下文接力生效：设计报告引用需求阶段的 FR/NFR/歧义点编号。
- L2 反馈环完整闭环：review fail → 反馈回 coding → coding/testing 重跑 → review 复审 → 轮次耗尽判 failed。
- M0 修复后审查结论从"实现代码缺失"变为"实现代码质量高，需求已满足"，剩余 fail 为真实测试覆盖缺口。
- 预算保护生效：run 级 token 超限即 rejected。

## 审查现有代码库（自审查 / dogfooding）

除"审查本次流水线生成的代码"外，也支持**审查一个已存在的代码库**：

```toml
task_workdir = "."                      # 指向被审项目根
[[stages]]
name = "review"
kind = "review"
prompt = "prompts/review_existing.md"   # 无 feedback_to：只出报告不自动返工
```

- 无上游 coding 阶段时，审查阶段直接读工作区源码（`existing_code_context`，跳过缓存/状态/依赖目录，单文件与总量都有上限）。
- **纯 review 流程不会给被审项目建 git 仓库**，不改动被审代码。
- 实跑：`python -m orchestrator.cli run selfreview.toml --task "审查要点..." --run-id self-1`

第六轮（自审查 + 收敛验证）修复：
- **B1 审查误判**：`"需修复后方可通过"` 曾被判 pass。退化判定改为保守策略，条件/否定语气一律返回 None 交人工确认。
- **M1 路径遍历**：`C:/evil.py`、`a:b`（NTFS ADS）曾可越界写出工作区。改为拒绝盘符/冒号 + `resolve` 后 `relative_to` 边界校验。
- **M2/M3 git 子进程**：改用 env 白名单（不透传 API Key）+ 120s 超时（防挂死）。
- **B4 契约**：`is_meaningful` 现在尊重 `parse_unknown`，一律不判完成。
- **M4 静默降级**：worktree 创建失败现在记 `worktree_fallback` 告警事件。
- **轮次重置**：`rerun-from` 此前漏清 `review_rounds`，跨次重跑累积导致刚返工一轮就误判"轮次耗尽"。
- **护栏算法**：`feedback_loop_guard` 上限改为 `阶段数 × (最大轮次+1)`，此前简单相加会在正常返工中途误中止。
- **结论一致性**：模型有时写 `verdict:"pass"` 却列出 `severity:"must"` 问题。现在有必须修复项一律改判 fail，绝不放过缺陷。

第七轮（真 git 端到端复验）修复：
- **G2 越界检测重做（此前是空转）**：根因是 `snapshot()` 只扫可写根，`after-before` 必然全在根内，`not is_inside()` 恒假 => 函数恒返回 `[]`。现把**监视域与可写根分离**：配了 `watch_root` 时快照扫监视域，差集里不在可写根内的才判越界。实测根外 `escaped.txt` 能被捕获并判阶段失败。
  - **默认关闭，需显式开启**：`watch_root` 未配置时**不做根外扫描**（零开销）。原因：默认取父目录会扫到系统临时目录等海量目录，实测把测试从 57 秒拖到超时。需要该保护时显式配 `watch_root = "."`（工作区父目录）或具体路径；`watch_root = "off"` 显式关闭。
  - **边界声明**：开启后也**只覆盖受深度和数量限制的监视域，不做全盘审计**——写到其他盘、远处目录或兄弟目录深处，快照方案抓不到。需要强隔离请用容器/沙箱。
  - **测试方法纠正**：旧回归用例手工往 `after` 集合塞根外路径，绕开了真实 `snapshot()` 盲区，属"测试绿、功能假"。现用例**必须走真实快照产出**，并断言根外文件确实进了快照。
- **G6 预算前置**：此前只在调用**后**校验 token，超限时钱已花掉。现调用前按 prompt 规模预估输入 token 并拦截（测试断言适配器 `calls==0`）；单个上游报告注入上限 20000 字符，超出截断并标注全文路径。
- **裸 JSON 丢问题清单**：`_BARE_OBJ` 正则不允许嵌套括号，对象数组形式的 `issues` 匹配失败 => 反馈退化成"请全面自查"。改用 `json.JSONDecoder().raw_decode` 从每个 `{` 扫描，嵌套问题自然解决。
- **G4 护栏静默中止**：`feedback_loop_guard` 触发此前只记事件后 `break`，留下部分 `done` 像跑完了。现置 **FAILED** 并写明原因。
- 附带防御：`REVIEW_FEEDBACK` 但未配 `feedback_to` 时判失败，不再抛 `ConfigError` 崩溃。

**仍开放**：G7（`qwen -p -` 的 stdin 语义）待真机验证。

## 确定性改造（当前版本核心）

骨架不变：引擎与适配器分离、完成判据 `(inputs_hash, outputs_hash)`、代码产物用 commit、阶段串行。
变的是**不再用启发式充当沙箱、审查员和测试运行器**。

### 1. 审查结论 = 独立文件
审查阶段把结论写到 `runs/<run_id>/stages/<stage>/attempts/<n>/review_verdict.json`，引擎只对它做 `json.loads` + 严格校验：
```json
{"verdict": "fail", "issues": [{"file": "todo/store.py", "problem": "非原子写", "expected": "用 os.replace"}]}
```
- `verdict` 只接受 `pass`/`fail`；`pass` 时 `issues` 必须为空。
- 文件缺失、JSON 非法、字段不合法、`pass` 却带问题 => **待人工确认**，绝不从正文推断。
- Markdown 报告只给人看，**不参与判定**。
- `success.json` 只表示「这次 attempt 产物已归档」，不表达审查是否通过。

### 2. 测试阶段 = 真实执行命令
```toml
[[stages]]
name = "testing"
kind = "test"                                    # 不调用模型
test_cmd = ["python", "-m", "unittest", "discover", "-s", "tests"]
feedback_to = "coding"
max_rounds = 2
```
在该阶段工作目录执行（不经 shell，避免注入），记录退出码/耗时/截断输出到 `test_record.json` + `test_output.txt`。
**非零退出码即失败**，执行记录直接作为反馈回编码，不再让审查模型复述一遍。

### 3. 隔离：产物正确性 vs 不受信写入
- **产物正确性**：git worktree + 按清单提交 + 关闭 hook。为了可 diff、可续跑。
- **不受信写入**：只约束 API 适配器——只能按解析出的相对路径写，拒绝 `..`/绝对路径/盘符/ADS/`.git`。
- **目录快照降级为告警**：默认 `watch_enforce = false`，根外变更只记 `watch_warning` 事件，不判阶段失败（父目录一次无关保存不该让阶段挂）。设 `true` 才当失败。
- CLI 适配器本身能跑命令，目录快照挡不住，**不假装挡住**。需要硬隔离请用容器或系统级作业对象。

### 4. 反馈环收窄
- 返工轮的编码提示词**只含问题条目 + 被点名文件的内容**，不再重贴需求全文和累积 diff。
- 审查**没有文件级问题**时停下等人处理，不自动「全面自查」再烧一轮。
- `max_rounds` 可以保持很小。

### 5. 默认通道
第一版以 `openai_compat` 为默认（唯一跑通全链的路径）。CLI 适配器留在注册表但**不参与默认流水线**——
各工具 stdin 语义与输出格式尚无真机样本，先抓真实 `--version` 与一次成功输出替换合成 fixture 再启用。

### 推荐流水线
见 `pipeline.example.toml`：
```
需求(模型) → 设计(模型) → 编码(模型，worktree 提交)
    → 测试(执行 test_cmd) → 审查(模型写 verdict.json)
         ↑                        │
         └── 仅当测试失败或 issues 含文件位置 ┘
    否则停在待审批
```

## MVP 边界与 v2

**第一版**：引擎 + API 适配器 + 六个 CLI 适配器 + 串行 + 断点续跑 + 审批 + 纯 API demo + 零依赖测试。

**留作 v2**：回退、CLI→API 自动回退、并行调度（并行会引出 worktree 合并冲突）、dsh sdk/acp 模式、Web 看板。
