# 多智能体开发流水线编排器

按配置把设计、编码、真实命令测试和独立审查串成可恢复的串行流程。支持 OpenAI 兼容 API 与具名 CLI 适配器，提供本地单用户工作台、审批和运行证据。运行使用 Python 标准库；完整开发验证还需要 Git 和 Node.js。

当前版本、验收数字、升级步骤和未验证边界统一见 [DELIVERY.md](DELIVERY.md)。变更摘要见 [RELEASE_NOTES.md](RELEASE_NOTES.md)，各轮历史与旧数字归档到 [HISTORY.md](HISTORY.md)。

主线自动检查、双重完整验收发布、网页进程守护与固定自动修复任务集见 [OPERATIONS.md](OPERATIONS.md)。

## 开始使用

需要 Python 3.11+。在 `agent-orchestrator` 目录运行离线示例：

```powershell
python -m orchestrator.cli doctor workbench.example.toml
python -m orchestrator.cli dashboard --config workbench.example.toml --port 8766
```

打开终端给出的本次会话链接，新建任务并选择配置。该示例只运行真实 Python 断言，最终等待人工审批，不调用模型。端口占用时选择空闲端口；网页关闭不会取消后台任务。

也可直接使用 CLI：

```powershell
python -m orchestrator.cli run workbench.example.toml --task "离线交付验收" --run-id demo-1
python -m orchestrator.cli status workbench.example.toml --run-id demo-1
python -m orchestrator.cli approve workbench.example.toml --run-id demo-1 --stage acceptance
python -m orchestrator.cli resume workbench.example.toml --run-id demo-1
```

运行等待审批时退出 **11**，完成后退出 **0**，执行失败退出 **1**，配置或用法错误通常退出 **2**。不要把审批中当作成功；新任务使用新的 run-id。

## 先选好模型与思考强度

新建任务可按适配器填写账户可用的旗舰模型标识，并为支持的工具设置思考参数。Codex 可选 `xhigh`；兼容 API 可设置 `reasoning_effort` 或 `thinking`，具体值须由该端点和模型支持，拒绝时不会自动降级。Qwen/反重力可覆盖模型名，尚未验证的其它工具继续使用各自配置。工具默认/原配置同样可以保留；系统不猜测账户里哪个模型“最好”。

Qoder 1.1.89 已验证 `Ultimate + high + adaptive` 真实读取/编辑与一条维护任务闭环；控制台支持 Qoder 模型、思考强度和 `auto/adaptive/disabled`。`thinking=enabled` 需要该工具额外的固定思考预算，本接入不使用它。示例见 `qoder.example.toml`。

Claude Code 2.1.325 / 2.1.328 可作为文件工具代理接入你现有的 DeepSeek Anthropic 兼容连接：`cli_claude` 默认只继承 `~/.claude/settings.json` 的连接与模型字段，不改原配置。可指定 `deepseek-v4-pro[1M] + max`，控制台支持模型与思考强度；工具身份、连接配置和实报后端模型分开显示。未知版本在模型调用前停止。示例见 `claude-deepseek.example.toml`。

工作台新任务默认勾选“不限制 token”，取消本地运行/阶段/输出 token 限额，继续记录用量并保留时长、轮次、文件与审批约束。模型服务自身仍可能有输出/上下文上限。CLI 使用此模式需在 `[run_budget]` 显式设 `unlimited_tokens=true`，并删除运行/阶段/适配器的 `max_tokens`。

支持的 API 可同时启用“结构化审查 JSON”，约束审查输出为 JSON 对象，避免高思考模型额外说明导致协议拒收；不放宽审查判据。

本次选择写入独立配置副本，原配置不改写；任务房间显示实际绑定的模型与思考参数。高思考/不限 token 是执行选项，不代表所有工具/复杂任务已证明能成功，实际效果需结合真实验收。

## 接入模型与项目

复制一份示例，填写任务目录、命令、提示词、模型及预算，先运行 `doctor`。凭据只设置到配置指定的环境变量；真实 CLI 另需本机安装和已有登录。API 示例使用 `DEEPSEEK_API_KEY`。提示词可内联，也可引用相对配置文件目录的文件。

| 示例 | 用途 |
|---|---|
| `workbench.example.toml` | 离线真实断言与人工验收 |
| `config.example.toml` / `pipeline.example.toml` | API 需求、设计、编码、测试、审查 |
| `unattended.example.toml` | 显式预授权的有界无人值守 |
| `cli-mixed.example.toml` | Codex 设计/审查与反重力编码 |
| `dsh-test.example.toml` | dsh 测试角色 |
| `qoder.example.toml` | API 设计/审查、Qoder 文件工具编码与独立测试 |
| `claude-deepseek.example.toml` | API 设计/审查、Claude Code 经现有 DeepSeek 连接编码与独立测试 |
| `qwen.example.toml` | API 设计/审查、Qwen Code 0.26.0 文件工具编码与独立测试 |

模型示例会真实调用工具或 API，并可能修改示例任务目录。用于业务项目时，先核对 `task_workdir`、测试命令、允许修改的文件及预算。工作台中的任务文字不会切换执行目录，也不会替配置增加设计或编码阶段。

## 命令

| 命令 | 作用 |
|---|---|
| `run 配置 --task 文本 [--run-id ID]` | 开始新运行 |
| `status 配置 --run-id ID` | 读取阶段与用量 |
| `resume 配置 --run-id ID` | 校验证据后继续 |
| `approve 配置 --run-id ID --stage 名称` | 校验并批准待审批产物 |
| `rerun-from 配置 --run-id ID --stage 名称` | 显式失效该阶段及下游并重跑 |
| `unattended 配置 --task 文本 --max-restarts 次数` | 按预授权策略有限恢复 |
| `doctor 配置 [--json]` | 本机静态诊断，不调用模型或登录探测 |
| `clean --state-dir 目录 [--dry-run]` | 预览过期状态清理；仅 `--apply` 执行 |
| `export-usage --state-dir 目录 --output 文件.csv` | 导出运行、阶段和工具用量；未报告的账单费用留空 |
| `state-check --state-dir 目录` | 只读检查现存状态与当前 schema 的兼容性 |
| `state-backup --state-dir 目录 --output 新目录` | 停写后备份完整状态并逐文件校验 SHA-256 |
| `state-restore --backup 备份目录 --output 新目录` | 校验并恢复原始字节，不覆盖现有目录或自动续跑 |
| `dashboard --config 配置 --port 端口` | 启动本机工作台，配置参数可重复 |
| `tune --state-dir 目录 --config 配置` | 生成待审批的历史策略提案 |
| `approve-tune --state-dir 目录 --output 新配置` | 批准并写入配置副本 |
| `autotune 配置` | 在显式预授权范围内选择后续策略 |

`doctor` 的退出码 0 表示无错误、1 表示发现问题。不存在但可创建的目录等情况只警告。`clean` 会报告候选路径、字节数及保留理由；先核对 dry-run，再决定是否 apply。详细参数、证据保护和保留期见 [DELIVERY.md](DELIVERY.md)。

## 配置字段

以下表格由运行配置 dataclass 的字段元数据生成，校验器与 doctor 使用同一份字段定义。适配器专属连接参数见下一节与同包示例。

<!-- CONFIG_SCHEMA:START -->
| 配置表 | 字段 | 类型 | 默认值 | 范围/说明 |
|---|---|---|---|---|
| root | `task_workdir` | `Path` | `必填` | 任务工作目录，相对配置文件目录解析 |
| root | `state_dir` | `Path` | `必填` | 状态目录，相对配置文件目录解析 |
| root | `stages` | `list[StageSpec]` | `必填` | 顺序执行的阶段数组，至少一个阶段 |
| root | `adapters` | `dict[str, AdapterConfig]` | `{}` | 具名适配器表；纯 test 流程可为空 |
| root | `serial` | `bool` | `true` | 当前只支持 true，阶段串行执行 |
| root | `allow_yolo` | `bool` | `false` | 兼容开关，不解除路径与审批约束 |
| root | `run_budget` | `RunBudget` | `{}` | 整个运行的 token 与时长上限 |
| root | `git_exe` | `str` | `""` | 可选 Git 可执行路径；为空时探测 PATH/安装位置 |
| root | `watch_root` | `str` | `""` | 有限监视域；相对配置目录解析；空或 off 关闭 |
| root | `watch_enforce` | `bool` | `false` | 根外快照变化是否判失败，默认仅告警 |
| root | `tune_proposal_id` | `str` | `""` | 已批准调优提案标识；空或 tune- 加 12 位十六进制 |
| root | `autonomy` | `Autonomy` | `{}` | 显式预授权的无人值守策略 |
| root | `autotune` | `AutoTune` | `{}` | 有界的后续运行策略选择 |
| root | `review_coverage` | `ReviewCoverage` | `{}` | 审查提示词覆盖上限与不完整处理策略 |
| stages | `name` | `str` | `必填` | 唯一阶段名；字母数字、下划线、点或横线，禁止父目录段 |
| stages | `adapter` | `str` | `必填` | 非 test 阶段必填，引用具名适配器 |
| stages | `prompt` | `str` | `必填` | 非 test 阶段必填；提示词文件或内联文本 |
| stages | `timeout_s` | `int` | `300` | >= 0.001；单次执行超时，秒 |
| stages | `approval` | `bool` | `false` | 成功产物是否等待人工审批 |
| stages | `max_attempts` | `int` | `2` | >= 1；本轮适配器调用尝试次数上限 |
| stages | `max_tokens` | `int | None` | `null` | >= 0；该阶段累计 token 上限；空表示无阶段级上限 |
| stages | `max_duration_s` | `int | None` | `null` | >= 0；该阶段累计计量秒数上限；空表示无阶段级上限 |
| stages | `kind` | `str` | `"code"` | code/report/review/test；verify 是 test 的兼容别名 |
| stages | `config_keys` | `dict[str, Any]` | `{}` | 参与输入指纹的可 JSON 序列化配置表 |
| stages | `feedback_to` | `str | None` | `null` | review/test 反馈目标，必须是之前的阶段 |
| stages | `max_rounds` | `int` | `1` | >= 1；反馈轮次上限，含首轮 |
| stages | `test_cmd` | `list[str]` | `[]` | test 必填的非空 argv 数组，其它类型禁止设置 |
| stages | `context_files` | `list[str]` | `[]` | 显式上下文相对文件路径列表 |
| stages | `precheck_cmd` | `list[str]` | `[]` | 仅 code 可设，运行前真实检查命令 argv |
| run_budget | `max_tokens` | `int | None` | `null` | >= 0；运行累计 token 上限；unlimited_tokens=true 时不设置 |
| run_budget | `max_duration_s` | `int | None` | `null` | >= 0；运行累计秒数上限；无人值守时必须为正数 |
| run_budget | `unlimited_tokens` | `bool` | `false` | 显式取消运行/阶段/输出 token 限额，保留墙钟与轮次约束；模型服务仍可能有自身上限 |
| autonomy | `enabled` | `bool` | `false` | 显式开启无人值守 |
| autonomy | `auto_approve` | `list[str]` | `[]` | 预授权自动批准的阶段名列表 |
| autonomy | `allowed_files` | `list[str]` | `[]` | 允许修改的相对文件路径白名单 |
| autonomy | `baseline_paths` | `list[str]` | `[]` | 首次初始化加入基线的相对路径 |
| autonomy | `max_repair_rounds` | `int` | `4` | >= 1，<= 20；整个反馈链的最大修复轮数 |
| autonomy | `max_stagnant_rounds` | `int` | `2` | >= 1；连续无进展的最大轮数 |
| autonomy | `repair_batch_size` | `int` | `1` | >= 1，<= 3；单轮最多修改文件数 |
| autonomy | `output_tokens` | `int` | `2048` | >= 1，<= 16384；有限 token 模式的单次输出额度；unlimited_tokens 时不施加此本地限制 |
| autonomy | `context_chars` | `int` | `32000` | >= 1，<= 128000；上下文字符数上限 |
| autonomy | `cli_input_overhead_tokens` | `int` | `32768` | >= 1；CLI 工具上下文的保守输入预留 |
| autonomy | `cli_idle_timeout_s` | `int` | `120` | >= 1，<= 3600；CLI 无活动超时，秒 |
| autonomy | `cli_max_tool_errors` | `int` | `3` | >= 1，<= 20；连续工具错误上限 |
| autonomy | `cli_max_repeated_reads` | `int` | `4` | >= 1，<= 20；重复读取上限 |
| autotune | `enabled` | `bool` | `false` | 开启策略提案与选择 |
| autotune | `auto_apply` | `bool` | `false` | 自动选择需同时启用 autonomy 和 autotune |
| autotune | `min_runs` | `int` | `5` | >= 5；最少同配置终态记录数 |
| autotune | `max_batch_size` | `int` | `2` | >= 1，<= 3；可自动选择的修复文件批量上限 |
| review_coverage | `diff_lines` | `int` | `600` | >= 1，<= 10000；累积 diff 注入行数上限 |
| review_coverage | `per_file_bytes` | `int` | `24000` | >= 1，<= 2097152；变更文件全文注入的单文件字节上限 |
| review_coverage | `total_bytes` | `int` | `180000` | >= 1，<= 8388608；变更文件全文注入总字节上限 |
| review_coverage | `max_files` | `int` | `60` | >= 1，<= 1000；全文注入文件数上限 |
| review_coverage | `existing_per_file_bytes` | `int` | `12000` | >= 1，<= 2097152；审查现有代码库的单文件注入字节上限 |
| review_coverage | `existing_total_bytes` | `int` | `120000` | >= 1，<= 8388608；审查现有代码库的总注入字节上限 |
| review_coverage | `incomplete` | `str` | `"manual"` | 覆盖不完整时 manual 待审批、fail 失败、allow 显式接受风险；无人值守 manual 仍失败 |
<!-- CONFIG_SCHEMA:END -->

非 test 阶段须填写 `adapter` 与 `prompt`；test 阶段必须有真实 `test_cmd`，不调用模型。未知顶层、阶段、预算和策略字段会报错；`config_keys` 不接受 TOML 日期等无法 JSON 序列化的值。

## 适配器

| 类型 | 配置与验证范围 |
|---|---|
| `openai_compat` | `base_url`、`model`、`api_key_env` 必填；可设 `temperature`、`max_tokens`、`timeout_s` |
| `cli_codex` | 可设 `exe`、`model`、`isolate_config`、`reasoning_effort`、`extra_env`、`timeout_s`；0.170.0 真实读取/修改及维护闭环通过，旧协议样本保留 |
| `cli_agy` | 可设 `exe`、`model`、无凭据 `proxy_url`、`extra_env`、`timeout_s`；精确支持 1.5.9 / 1.5.12 / 1.5.13，沙箱与严格流式终态 |
| `cli_dsh` | 可设 `exe`、`permission_mode`、无凭据 `proxy_url`、`extra_env`、`timeout_s`；已有真实样本 |
| `cli_qwen` | 0.26.0 的 stdin、JSON 数组兼容与 stream-json Read/Edit 已真机验证；safe/bare 模式、文件工具、实时计量与停止护栏；其它版本未验证 |
| `cli_qoder` | 1.1.89 真机 stdin/流式成功终态及 Read/Edit 已验证；Ultimate/high/adaptive 真实维护任务通过；token计数不完整、credits分列 |
| `cli_claude` | Claude Code 2.1.325 / 2.1.328 的真实流式终态与文件工具；默认继承用户兼容连接，已用 DeepSeek Pro/max 跑通维护闭环，不冒充 Claude 原生模型 |
| `cli_doubao` | 未核实独立官方 CLI，属于未验证预留；调用豆包模型可使用方舟兼容API |
| `fake` | 仅用于离线演示和确定性回归 |

CLI 子进程使用环境白名单；额外凭据变量须在 `extra_env` 显式列名。Windows 的 Codex 建议配置原生 `codex.exe`，减少 npm `.cmd` 的参数转义影响。CLI 版本未知、缺少完成事件或输出无法解析时，不会按成功处理。样本来源与真实性见 [tests/fixtures/README.md](tests/fixtures/README.md)。

Qwen 0.26.0 新调用不加载用户/项目自定义配置、hooks、技能、MCP 或历史会话；兼容连接由显式 `extra_env` 提供，模型可由配置覆盖。编码使用 auto-edit，报告/审查使用 plan；显式禁用 shell、网络、目标管理和子代理工具，报告另禁 edit，真实编码初始化清单只有 read_file/edit。此版本 safe mode 忽略 core-tools，因此不把该参数当作限制依据。任务测试仍由引擎独立执行。反重力使用自身 sandbox 和 plan/accept-edits 模式；其 init 仍列出广泛内置工具，不能声称是 OS 文件沙箱或文件工具白名单。

## 执行、恢复与审查

- 阶段按配置串行执行。完成依赖输入指纹、产物内容、真实测试退出码和独立审查结论；恢复会核对证据，显式重跑会失效下游旧结果。
- 编码使用 Git worktree 和文件清单提交；零净变更不能作为完成。既有非 Git 工作区首次编码会建立已有文件基线。
- 测试执行配置中的 argv。失败记录可直接反馈到先前编码阶段，不依赖模型转述；测试隔离创建失败会停止，不回退到用户工作区。
- 审查只认独立的 `review_verdict.json`；`pass` 要求问题列表为空。`fail` 有文件级问题且轮次允许时返工；缺少可执行问题或结论无法解析时交人工处理。
- 审查 diff 过长时尝试补充变更文件全文，仍覆盖不完整时不自动采纳 pass：人工模式待确认，无人值守失败。审查现有代码库时工作区变化会使旧结论失效。
- `[review_coverage]` 可配置覆盖上限；默认 `incomplete=manual`，也可选择 `fail` 或显式承担不完整风险的 `allow`。未覆盖清单与策略保存在 attempt 和结果记录；CLI 额外读取不会自动被算作完整覆盖。
- code/test 审批校验申请时冻结值、attempt 归档与 evidence；report/review 仍检查实时输入变化。讨论建议不会自动成为实现或通过结论。
- CLI 中断与恢复保留已报告用量及未知预留，续跑不重置原预算、期限和调用次数。CLI token 是观察与预扣口径，不等于实际账单，也不是服务端单次输出硬上限。

无人值守须显式声明允许文件、自动审批阶段、轮数及正数时长，并选择正数 token 预算或显式不限 token。自动调优只为后续任务产生有界配置副本，不增加预算或写权限，不修改引擎源码。

## 本地工作台与边界

工作台支持全历史关键词/项目/状态筛选、稳定分页、SSE 活动、审批、暂停/继续、取消和有限 API 双角色会诊。创建任务绑定所选配置和配置哈希；执行范围以配置为准。固定测试流程的完成只证明配置的命令通过。

可保存常用配置与任务文字为模板，再核对并启动；配置变化时旧模板禁用。运行对比展示已报告/预留用量与时长差值，不能代替实现质量审查。失败看板逐页扫描历史，只按明确错误类别和停止事件聚合；多类别可以重叠，读取失败会单独提示。预算到 80% 时提示，不自动修改限额。

只监听本机 loopback，写操作需要会话、同源和 CSRF 校验。控制指令入队后仍须等待执行收据；暂停在检查点生效，关闭页面或工作台不等于取消独立任务。数据库投影可重建，原状态与 attempt 证据为权威来源。

只登记可信配置；worktree、文件白名单和 watch 快照不是操作系统沙箱。watch 默认关闭，显式配置后默认只告警；强制监视的升级与隔离处理见 DELIVERY。测试命令和 CLI 工具仍可能执行任意其账户可做的操作。

## 开发与证据

快速/完整验收命令、最新测试数量、日志脱敏和证据保留规则统一维护在 [DELIVERY.md](DELIVERY.md)。生成配置表及示例注释使用 `scripts/generate_config_docs.py`，加 `--check` 可检查漂移；提交前脚本会调用同一快速验收入口。

| 模块 | 职责 |
|---|---|
| `engine.py` | 阶段状态机、运行所有权、审批和重置 |
| `review_policy.py` | 审查上下文、指纹、覆盖与反馈 |
| `execution.py` | test/模型 attempt 执行 |
| `archive.py` / `guards.py` / `budget.py` | 归档、路径/watch 守卫、预算与结算 |
| `config.py` / `schema.py` / `contracts.py` | 字段定义、配置加载与公共契约 |
| `state.py` / `evidence.py` | 持久状态、锁及产物完整性 |
| `adapters/` / `dashboard/` / `collaboration/` | 工具适配、本地工作台及控制 |

开发机 `live-validation-*`、`review-*` 原始证据不入包；交付说明和包内结构化验收可独立复现。受控演示与离线回归不能代替真实业务成功率；未验证的版本和平台如实标注。

本地一键打包使用 `python scripts/release.py --output <仓库外全新目录>`，必须先提交到干净 HEAD；它执行两次完整离线验收，生成原始 Git 字节源码包、快照清单、自动提交 CHANGELOG 和 SHA-256。不会推送、发布远端或覆盖已有交付。`python -m orchestrator.cli --version` 可查看源码版本。
