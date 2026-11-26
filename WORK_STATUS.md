# 剩余工作推进记录

架构基线、2026-11-25 GitHub 调研和拟议验收指标见 [PROJECT_MEMORY.md](PROJECT_MEMORY.md)。

## 2026-11-25 · 反重力会话与编码预算复测

- 使用与先前哈希一致的反重力 CLI `1.5.17`。显式代理和 `gemini-3.8-flash-high` 的最小探针成功建立会话并收到预期响应。直连探针超时，代理下的默认模型探针复现后端空会话 ID。记录在 `D:/work/agy-session-check-009fq5s3/probe.json`。
- 原预算 `120,000` 下，标签规范化和站点读数汇总均建立会话，日志均出现文件读取和写入事件，后在编码阶段触发预算上限。累计计量为 `131,627` 和 `130,104`，包含未知用量的保守预留。记录在 `D:/work/agy-holdout-recheck-auqj618k/results.json`。
- 将标签规范化单次诊断的预算设为 `180,000` 后，设计、编码、保护测试和独立审查均通过。保护测试退出码为 `0`，指纹一致，最终报告 `113,356` tokens、三次调用，耗时 `154.985` 秒。该记录适用于 `180,000` 预算条件，原配对结论适用于 `120,000` 预算条件。记录在 `D:/work/agy-budget-check-a11zal75/results.json`。

## 2026-11-25 · 用独立任务核对路由排序

- 新增标签规范化和站点读数汇总两道任务，每个编码候选各跑一次。沿用每样本 120,000 本地计量 token、最多 8 次调用和 120 秒单次时限。Codex CLI `0.170.0`、反重力 CLI `1.5.17` 使用固定副本，运行前后 SHA-256 一致。四个样本均有编码调用记录，基线和保护测试哈希匹配。
- Codex 两次都通过保护测试和全流程验收。反重力两次都在编码阶段因后端返回空会话 ID 而失败，结果反映当前运行链路的可用性。二者各有两条记录的独立状态目录返回 `compared`、`agree: true`，与此前 18 个配对样本的排序一致。既有项目状态目录的对照仍为 `insufficient_history`。
- 证据在 `D:/work/pipeline-live-pair-20261125-d5d96bfe/history-holdout`，包括逐次结果、版本、哈希和两份路由对照。配对评测和路由对照专项测试共 10 项通过。

## 2026-11-25 · 编码路由只读对照

- `route-recommend --paired-results` 校验真实评测收据、任务基线和保护测试指纹，按逐次结果重算通过率并读取当前项目的历史。历史门槛按不同终态运行计数，同一候选在同一运行的多个编码阶段只计为一条运行。
- 用 18 条真实评测收据核对本机既有项目历史。Codex `gpt-6.2-sol` 有 0 条匹配历史，反重力 `gemini-3.8-flash-high` 有 1 条。按每个候选至少 2 条终态运行的门槛，分别还差 2 条与 1 条，当前状态为 `insufficient_history`。
- 17 项专项回归通过。Windows 快速回归两次均为 259 项零失败，耗时 203.84 和 202.20 秒，超出 120 秒时限。只读重算前后，评测文件和项目终态指标的 SHA-256 核验一致。

## 2026-11-25 · 真实候选配对评测

- 使用隔离的 Codex CLI `0.170.0` 副本，正式样本均探测到反重力 CLI `1.5.17`。反重力在初次预检 `1.5.15` 后更换了二进制，正式试验的报告保留更换前后指纹和逐样本版本。配置指定 `gpt-6.2-sol`（低推理）与 `gemini-3.8-flash-high`，公共设计和审查角色为 Codex。
- 先导试验复现 `50,000` 计量 token 上限在编码前拦截，以及带说明文字的审查响应未生成结构化结论。正式评测统一使用 `120,000` 计量 token、最多 8 次调用、单次 120 秒。CLI 审查复用仓库标准结论模板，编码提示明确保护测试由编排器在工作区外执行。
- 三项任务各三对，共 18 个样本。三个任务并行，每项内串行执行并交替候选顺序。Codex 候选完整通过 7/9，反重力候选 0/9，保护测试的通过数一致，脚本指纹全部匹配。Codex 两次停在公共设计超时。反重力一项停在公共设计超时，其余四项编码超时、四项编码后结算超出 token 预算。模型调用共 40 次，历史路由只有一条匹配且未达到最少样本数的记录，影子对照返回 `insufficient_history`。
- 结构化结果、失败阶段、逐次运行证据和报告在 `D:/work/pipeline-live-pair-20261125-d5d96bfe/live-120k`。配对评测契约回归 6 项通过。

## 2026-11-25 · Windows 耗时对照与配对评测

- Windows 同环境、独立源码副本、单工作进程顺序运行。基线 `697c3c1` 的 247 项为 154.14 秒，竞技场工作树的 254 项为 205.64 秒，均零测试失败。共同的 247 项合计多用 5.47 秒，新增 7 项合计用时 46.80 秒，其中后台竞技场模块用时 38.94 秒。基线本身也超过 120 秒门槛，现有耗时记录不足以将主机波动归因到安全扫描或磁盘。证据存于 `D:/work/pipeline-quick-compare-xljcte0k`。
- `precommit.py` 支持按历史用时将完整模块分给并行工作进程。四进程两次分片验收耗时为 124.56 秒和 98.97 秒，各运行 255 项用例且零失败。后一次通过 120 秒门槛，时长仍有波动。证据见对照目录下的 `balanced-workers4-evidence` 和 `balanced-recheck-evidence`。
- `paired_benchmark.py` 复用设计、编码、测试和审查流程，固定三项任务的基线提交、保护测试和预算。离线 9 对共 18 个样本，参考夹具完成 9/9，部分实现夹具完成 3/9，两个候选的保护测试通过数与完成数一致。报告包含每次结果，并支持在真实配置下对照历史路由。离线证据存于 `D:/work/pipeline-paired-20261125-90189844`。
- 真实候选预检使用 `cli-mixed.example.toml`。本机 Codex CLI 0.172.0 未列入已验证协议版本，反重力 CLI 版本命令超时。本轮保留离线评测结果，真实路由对照尚待候选通过预检。

## 2026-11-25 · 竞技场后台任务

- 后台进程独立于工作台 HTTP 请求。任务启动按幂等标识去重，保存候选进度和结果，工作台以 SSE 游标续接并支持从历史记录重新打开。
- 支持取消并回收工作树，异常退出后可查阅已完成候选的结果。
- Windows 上 254 项快速回归零失败。首次运行耗时 67.8 秒，最终复跑耗时 351.8 秒，超过 120 秒时限。Playwright 在 Edge 使用离线夹具核验了启动、取消和刷新后重新查看历史记录。

## 2026-11-25 · Windows Git 基线与竞技场验收加固

- Git 按字面文件清单提交，规避 Windows 命令行长度上限并保留无关暂存项。引擎自动生成的提交显式关闭签名。
- 竞技场在独立工作树中验收封存提交，人工采纳绑定文件范围、候选 SHA 和测试证据。采纳前写入意向，原分支成功合入但回执写入中断时可重试恢复。
- Windows 提交前验收通过，覆盖 18 项静态检查、9 份示例离线冒烟和 248 项快速回归，源码指纹一致。

## 0.5.9 多模型圆桌升级、白名单测试编排、报告导出与双模型竞技场

- **多模型圆桌升级**：支持 1~3 轮交叉辩论、4 角色独立选模与 `openai_compat` + `cli_agy`（强制 `mode="plan"` + `sandbox=True` 只读）混合席位、`@相对路径` 只读源码引用、结构化共识总结、一键采纳为审批附注或新建任务，以及基于 `messages_run` 索引的轮间人工主持人插话自动注入。
- **项目管理与确定性测试节点**：新增 `POST /api/projects/<id>/remove`（活跃进程拦截 + 安全注销，不删工作区与历史运行）与弹窗已登记项目列表；动态节点编排器支持白名单确定性 `test` 节点（`pytest` / `python_unittest` / `npm_test` / `npm_run_build`）及 `feedback_to` 打回，拒绝原始 shell 命令。
- **交付报告导出、无损归档与历史推荐一键套用**：新增 `GET /api/runs/<key>/export-report` 导出 `.md` 交付报告；新增独立 `archived_runs` 表与 `POST /api/runs/<key>/archive`（严格布尔校验、默认列表隐藏、支持「已归档」筛选恢复，磁盘 `state.json`/`metrics.json` 零修改）；新建任务弹窗新增 `✨ 按历史推荐` 一键自动填充。
- **双模型并行 Best-of-N 竞技场**：新增 `orchestrator/arena.py` 与 `/api/projects/<id>/arena`、`/api/projects/<id>/arena/apply`，隔离 `git worktree` 并行运行 CLI 编码候选 + 白名单测试，并排对比红绿 Unified Diff，经人工确认后仅允许 `git merge --ff-only` 合入；严格正则校验模型名防 CLI 参数注入。
- **验收与实测**：
  - 全量 `scripts/precommit.py` 通过（18 项配置/密钥扫描 `ok: true`，离线冒烟通过，**242 / 242** 快速测试全绿，`source_unchanged: true`）；
  - 真浏览器端到端实测 `http://127.0.0.1:8766` 全部 5 项新功能（0 控制台报错、0 失败请求，圆桌 `cli_agy` 下拉补全实测载入 23 个本机真实模型），并同步修复新建任务弹窗按钮组折行样式。

## 0.5.8 受控自优化、动态多节点与工作台全面升级

- 落地两项受控自优化模块并完成两轮代码审查加固：`orchestrator/lessons.py`（高频失败经验提取、环境密钥与通用凭据正则脱敏、哈希绑定审批、按阶段注入提示词并同步无人值守 `logical_hash`）与 `orchestrator/model_router.py`（跳过未执行下游阶段、严格有限浮点校验、独立计量 `unknown_calls`），接入 CLI（`lessons-propose` / `lessons-approve` / `route-recommend`）及工作台「看板与优化」3 标签页。
- 落地本机可用模型与工具动态探测（`model_catalog.py`，使用 `build_subprocess_env()` 隔离宿主机无关密钥，`256 KiB` 响应上限，实机探测到本机 `agy 1.5.14` 全部 23 个真实模型及全部已装 CLI）与动态多节点项目编排（`project_manager.py`，支持 1..12 个节点增删、排序、独立工具/模型/审批/打回目标，非托管外部项目编辑默认 `-custom` 避免同名重复，`cli_codex` 默认 `isolate_config = true`）。
- 工作台控制页全面升级：横向阶段进度条 + 快捷审批附注（修订指令注入下游提示词）、红绿高亮 Unified Diff、原生安全 Markdown 富文本排版（含缩进代码块、表格、安全链接）、角色配色与模型内联徽章、连续 CLI 心跳折叠、`dashboard --open` 自动拉起浏览器，以及 `cli_agy` `1.5.14` + 可选同系列 `allow_patch_probe` 协议探针与内置防幻觉审查模板（`prompts/evidence_review.md`、`prompts/verified_repair_plan.md`）。
- 真机验证：
  - `cli_agy 1.5.13`（`gemini-3.8-flash-high` + 本地代理 `127.0.0.1:7897`）完成 `invoice` 四阶段全链路闭环（`design/coding/testing/review` 全 `done`，`acceptance_exit: 0`，`protected_unchanged: true`，3 次调用、93,065 tokens、耗时 `64.438s`，证据：`D:/work/ai-pipeline-live-agy-20261121-r6/results.json`）；
  - `cli_agy 1.5.14` 经本机工作台（`http://127.0.0.1:8766`）在真实业务仓库 `D:\work\jsy_work_new\jsy-app-test-plus` 完成 `code_review` → `repair_plan`（停在人工审批卡点，0 控制台报错、0 失败请求），并据此沉淀防幻觉证据核验与交叉反查提示词模板。
- 全套回归：`scripts/precommit.py --base-ref 2b0d71d` 通过（18 项配置检查、0 凭据/漂移问题），离线冒烟与快速回归 **239 / 239** 全绿通过，源码指纹一致。

## 0.5.7 本地收口与新能力接入

- 已完成 `cli_grok` 与 `cli_doubao` 容器适配器接入及代码审查修复，抽离 `docker_base.py`、`workspace_sync.py`、`verified_runtimes.py`（支持每通道多已验证版本与 `runtime_version` 绑定），豆包编码阶段补齐隔离临时工作区与宿主机基线核验原子回写，`doctor` 补齐只读 SHA-256 与预算告警检查。
- `run_budget` 新增 `max_model_calls` 与 `max_unknown_calls` 调用次数上限（同步 `metrics.json`、`reporting` 与配置表），解决无 token 上报 CLI 多轮返工被未知用量预扣拦住的问题；`multi_project_benchmark.py` 新增 `ledger` 分账退款样例与调用次数参数。
- 完成四大核心大模块低风险拆分（`collaboration/service.py`、`state.py`、`engine.py`、`review_policy.py`）、`artifacts.py` 减少冗余 `git` 子进程、以及 `verify_delivery.py --workers` 并发执行选项与 `.gitignore` 规则补齐。
- 落地并接入主入口五项单人自用长期扩展：逐 token 实时会诊（`PIPELINE_STREAM_DISCUSSION=1`）、契约化状态迁移注册表（`state-migrate`，默认干跑且不臆造未验证 v1→v2）、飞书/钉钉/Slack 脱敏 Webhook 通知（`PIPELINE_WEBHOOK_URL`）、受控部署执行器（`deploy`，默认干跑 + 审批哈希绑定）与双向 HMAC 防重放远程 Worker 协议（`worker-serve`）。按当前自用定位跳过多用户 RBAC。
- 本地离线快速交付验收（`verify_delivery.py --quick`）229/229 全部通过，零失败、错误和跳过，9 份示例与 CLI/HTTP 冒烟全通，233 份源码指纹一致；`scripts/precommit.py --base-ref 7eb2dd8` 通过（18 项配置检查、0 凭据/漂移问题）。
- 真机实测（`deepseek-chat`）：
  - 流式 SSE 探针（`OpenAICompatAdapter(stream_tokens=True)`）真机通过，精确获取流尾用量（27 tokens，0.579s，无 `usage_unknown`）；
  - `multi_project_benchmark.py --case invoice --unlimited-tokens --max-model-calls 6` 四阶段（design/coding/testing/review）全部 `done`，保护测试由失败变为通过且哈希未变，3 次调用、2468 tokens、耗时 6.562s（证据：`D:/work/ai-pipeline-live-057-invoice-20261121/results.json`）；并基于该真实运行状态通过 `orchestrator deploy` 完成干跑 + 审批哈希绑定受控部署审计留证（`deploy-c2e7cce39f08`）；
  - 新样例 `ledger` 同参数真机试跑中，模型将 `settle()` 返回值误写为 `refunded`，被保护测试准确拦截且测试哈希未变（3 次调用、3453 tokens、9.672s），失败证据保留于 `D:/work/ai-pipeline-live-057-ledger-20261121/results.json`。
- 远端 GitHub Actions Windows/Linux 全量矩阵验收待远端触发后核对。

## 0.5.6 交付和跨项目验收

- 发布包固定在 `4f51a9b`。远端源码和解压包各完整运行 701 项测试，零失败、错误和跳过。216 个交付文件与 Git 提交一致，ZIP SHA-256 为 `b386c6fff84ea8c05cddb3fa0bd36802b39abf902d7db8eeb61e8a9d60cd1115`。[远端发布验收](https://github.com/zjungang462-netizen/ai-pipeline/actions/runs/44695327363)
- 本机端口 `8766` 已从该包启动工作台。升级后和守护重启后均复验了 10 个项目、25 条历史运行、141 份账本文件哈希和静态文件。自动任务恢复关闭，服务启动未调用模型。证据保存在 `D:/work/ai-pipeline-release-056-cb48f023/upgrade-verified.json`。
- `87524a7` 上的 Linux Python 3.11 和 3.13 全量矩阵均通过。每个环境收集 701 项，692 项通过，9 项 Windows 专属用例按登记跳过。修正前的 Linux 失败证据也已保留。[Linux 全量矩阵](https://github.com/zjungang462-netizen/ai-pipeline/actions/runs/44700044402)
- 归档及业务基准变更提交 `2acb4df` 在远端 Windows Python 3.11、3.13 各通过 179 项快速检查，在 Linux Python 3.11、3.13 各通过 694 项并跳过 9 项 Windows 专属用例。四份验收产物的哈希已核对。[Windows 检查](https://github.com/zjungang462-netizen/ai-pipeline/actions/runs/44755401049) · [Linux 矩阵](https://github.com/zjungang462-netizen/ai-pipeline/actions/runs/44755451080)
- 同一提交在本地 Windows 完整通过 703 项，零失败、错误和跳过，源码指纹保持一致。耗时 677.984 秒，超过 480 秒观察目标。证据保存在 `D:/work/ai-pipeline-release-056-cb48f023/continuation-windows-2acb4df/verification.json`。
- 归档、工作树清理和测试产物记录已改为 `Engine` 显式持有的组件。测试输出作为结构化诊断数据传入返工提示，HTTP 回归覆盖客户端断开后重放同一暂停请求。
- `agent-orchestrator/scripts/multi_project_benchmark.py` 使用独立工作区复现两个业务缺陷，随后由仓库外的保护测试和审查判定结果。DeepSeek API 最终轮两个样例都完成，订单金额 3 次模型调用、报告 2402 tokens，预约容量包含一次返工，共 4 次调用、报告 3948 tokens。两个样例各改动两个模块，保护测试文件哈希保持一致。证据保存在 `D:/work/ai-pipeline-release-056-cb48f023/multi-project-api-final-129e691e/results.json`。
- 早期 API 试跑出现输出截断、补丁格式或签名错误。固定 Claude Code 2.1.328 试跑中，订单金额触及预算，预约容量编码超时。失败记录均保留于同一交付目录，实际账单金额及其它项目的成功率仍无数据。
- Grok 1.0.89 已通过设备授权登录。`streaming-json` 真机样本采集到正文、`tool_call`、`tool_call_update`、用量及 `end` 终态。显式关闭 `search_tool,use_tool` 后，工具清单仅有 `read_file,search_replace`，两者均完成真实文件操作。该轮终态报告 18741 tokens，包含 14592 缓存读取 tokens。Windows `workspace-write` 复验中，内置 `search_replace` 同时改写了工作区内文件和工作区外的临时样本。
- 已在仓库外核验 `@bytedance/doubao-cli` 2.37.2 的 npm 包完整性、Windows 原生包 SHA-256 与 Windows 数字签名。扫码后，标准输入和 `stream-json` 已完成真实文本与 `Glob`、`Read`、`Edit` 工具调用。`ask_always` 被当前账号拒绝，`ask_risky` 可执行，后者也允许 `Edit` 改写工作区外的临时样本。成功终态未报告 token 用量。未登录探针仍保留退出码 4 和 `login_required` 的记录。[豆包 CLI](https://www.npmjs.com/package/@bytedance/doubao-cli)
- 两次边界复验仅使用仓库外的临时文件。无人值守接入仍需能约束内置文件工具的执行环境。原始流式记录保存在 `D:/work/ai-pipeline-release-056-cb48f023/` 的 `grok-1.0.89-*` 和 `doubao-cli-2.37.2/authenticated-*` 目录。
- Grok 1.0.89 的 Linux 原生程序已在 Docker 中通过真实写入边界验证。容器以非 root 用户运行，根文件系统只读，唯一可写的宿主机挂载是测试工作区，临时状态写入内存盘。`search_replace` 成功修改工作区内的样本，尝试修改只读挂载中的工作区外样本时收到只读文件系统错误，宿主机样本 SHA-256 未变。随后用同样的隔离方式完成订单金额样例修复，调用前钩子拒绝了工作区外的读取，保护测试在修复前失败、修复后通过，测试文件哈希一致，DeepSeek 独立审查通过。两项证据位于 `D:/work/ai-pipeline-release-056-cb48f023/grok-container-boundary-0a017ccd/` 下的 `grok-live-explicit-fixtures/verification.json` 和 `isolated-maintenance/verification.json`。本次使用仓库外的验证脚本，正式适配器接入和豆包容器验证待完成。

- 正式 `cli_grok` 适配器已接入配置、CLI 实时计量与回写核验。订单金额用例在 DeepSeek 设计、Grok 容器编码、保护测试、DeepSeek 独立审查四阶段全部通过。测试前失败、修复后通过，保护文件哈希一致。复跑预算 90000 tokens，完成用量 48951 tokens，共 3 次模型调用。证据保存在 `D:/work/ai-pipeline-release-056-cb48f023/grok-formal-adapter-retry-20261120/verification.json`，首轮因工具错误护栏停止的记录一并保留。
- Grok 隔离回归已加入 Quick。最终 183 项通过，耗时 43.438 秒。带逗号和空格的宿主机路径已用真实 Docker 复验，容器读取到与宿主机一致的样本。
- 豆包 2.37.2 Linux 原生包的 SHA-256 已核对，补齐系统依赖后容器内可以读取版本并启动登录服务。精简镜像缺少 `/etc/machine-id` 时，设备注册返回 `device_error=9`。给测试容器配置独立且持久的 Linux machine ID 后，设备注册成功。直连登录服务返回 HTTP 403；经本机 `7897` 代理扫码确认后，`auth status` 退出码为 0。容器边界中工作区 `inside.txt` 改为 `TEMP_NEW`，只读挂载的 `outside.txt` 仍为 `TEMP_OLD`，Edit 返回 Permission denied。证据在 `D:/work/ai-pipeline-release-056-cb48f023/doubao-cli-2.37.2/container-boundary-20261121/verification.json`。正式适配器只运行这套容器和 `doubao 2.37.2` 的 stream-json 终态，成功终态不报告 token。没有越权尝试时终态省略 `permission_denials`，不能因此判解析失败。

- 2026-11-21 快速回归 194 项通过，耗时 62.250 秒。随后 Grok 适配器真实调用把 `inside.txt` 写成 `TEMP_NEW`，终态 19099 tokens。豆包适配器同一次调用也写成 `TEMP_NEW`，但因终态缺少 `permission_denials` 被误判解析失败；修正后用已保存流复验为成功。解析修正之后的快速回归再次 194 项通过，耗时 41.547 秒，源码指纹未变。证据分别在 `D:/work/ai-pipeline-release-056-cb48f023/quick-grok-doubao-20261121/`、`adapter-live-20261121/` 和 `quick-after-doubao-parse-20261121/verification.json`。
- 同一未提交源码的本地全量回归收集 718 项，全部通过，零失败、错误和跳过，源码指纹未变。耗时 885.485 秒，超过 480 秒观察目标。没有模型调用。证据在 `D:/work/ai-pipeline-release-056-cb48f023/full-grok-doubao-20261121/verification.json`。
- 豆包编码接入两个维护样例。订单金额首轮因容器内生成 `__pycache__` 被白名单拒绝；容器已关闭 Python 字节码写入。重跑后设计、编码、测试、审查均为 done，保护测试由失败变为通过，测试文件哈希未变，3 次模型调用。预约容量的编码在基准 120 秒调用时限内未结束。豆包调用仍无 token 报告，订单金额保留 38046 预扣，API 已报告 1610。证据在 `D:/work/ai-pipeline-release-056-cb48f023/doubao-formal-adapter-20261121/` 与 `doubao-formal-adapter-retry-20261121/results.json`。
- 预约容量在 240 秒编码时限下一次通过。设计、编码、测试、审查均为 done，保护测试由失败变为通过，测试文件哈希未变，3 次模型调用，耗时 161.047 秒。豆包仍无 token 报告，保留 37883 预扣，API 已报告 1779。此前 120 秒时限会中断编码；另一次编码已完成但实现未通过测试，返工又被 50000 token 上限和未知用量预扣拦住。证据在 `D:/work/ai-pipeline-release-056-cb48f023/doubao-reservations-20261121/` 与 `doubao-reservations-repair-20261121/results.json`。
- 字节码环境变量纳入豆包容器参数回归后，当前源码再次全量回归。收集 718 项，全部通过，零失败、错误和跳过，源码指纹未变。耗时 458.344 秒，低于 480 秒观察目标。没有模型调用。证据在 `D:/work/ai-pipeline-release-056-cb48f023/full-after-doubao-maintenance-20261121/verification.json`。

## 0.5.6 当前 CLI 验证

2026-11-20 已补齐 Codex 0.170.0、反重力 1.5.12 和 Qwen 0.26.0 的真实文件读写协议、脱敏夹具与来源指纹。版本探测失败、未知版本或超时在模型调用前停止；Qwen 当前执行仅开放 read_file/edit，并使用流式计量和工具停止护栏。新增 qwen.example.toml，显式环境端点和模型参与输入指纹，凭据值不参与。

三条固定旧源码维护任务均完成设计、实际编码、保护测试、独立审查，四阶段各 attempt 1，保护测试哈希未变，主代理未补写实现。Codex 总已报告 43963 tokens、49.813 秒；Qwen 36747 tokens、58.985 秒；反重力保守计量 197793 tokens、203.719 秒，其 cache_read 单列口径继续保留，实际账单未知。反重力首次任务在设计 API 响应中断时失败、未调用编码 CLI，保留该轮未知计量和失败证据；本轮维护任务总计 4 次尝试、3 次完成，不当作跨项目成功率。

增加 13 项协议回归并加入 quick。首轮本地 full 668 项中 1 错误，原因是旧 stdin 测试使用虚构 codex 1.0，被新增版本门禁拦截；已改为绑定真实支持版本，stdin 行为断言保留。后续增加两项 Qwen 输入指纹回归，当前应收集 quick 173 / full 670 项、9 份示例，最终以新候选的完整验收与发布证据为准。原始证据位于 D:/work/ai-pipeline-cli-next-20261120，机器可读摘要见 CLI_VALIDATION.json。

固定提交、工作台升级和 Linux 验收见上文。反重力 1.5.13 也已通过固定副本的真实读写协议验证。

## 0.5.5 历史交付

日期：2026-11-20。产品版本 0.5.5。本地首版冻结提交为 `c05431f748375b9c5d1945dee161236a15afbb22`，本轮远端验收包冻结提交为 `6944f045a2b5402cac58ac4532542ef8371e0e1a`。后者只调整诊断测试及快速验收集合，68 个运行文件与现有工作台完全一致；远端包的全部 205 个交付文件已按 Git blob 和 SHA-256 核对。后续根目录交付记录不改变这份产品源码，不改写冻结包的提交元数据。机器可读摘要见 `REMOTE_VALIDATION.json`。

## 已完成

| 项目 | 结果与证据 |
| --- | --- |
| 最终快速检查 | 修正后本地 160 项通过，62.906 秒；远端 Python 3.11、3.13 各 160 项通过，分别 16.406、15.103 秒，五项离线基准均符合预期 |
| 双重完整发布验收 | 远端源码与解压 ZIP 各 657 项通过，零失败/错误/跳过，分别 218.219、207.859 秒；本地首版两轮为 355.984、346.719 秒，均低于 480 秒观察目标 |
| 工作台升级 | 8766 已运行固定发布快照；10 个项目、25 条历史运行保留，141 份受保护账本文件哈希未变，静态文件与快照一致 |
| 守护恢复 | 网页子进程异常退出后成功重启一次，重启后再次核对历史与账本；自动任务恢复关闭，本次运维没有模型调用 |
| 状态备份恢复 | 新增 state-check/state-backup/state-restore；已备份工作台数据库与 10 个状态根，所有历史 schema 2 可加载；391 文件的独立恢复演练通过 |
| 自动调优复盘 | 原基线 4/5 完成、试用 3/5 完成，token 中位数 15593→24846；已回退原配置并冻结退化策略。降低轮数现在要求整条运行成功 |
| 自身维护基准 | 三类真实维护问题均完成闭环，保护测试不变且独立审查通过；全部 11 次试跑为 4 次完成、7 次失败，原失败记录保留。三个模型补丁已整合并加入回归 |
| Claude 新版本 | 2.1.328 真实 Read/Edit 夹具与严格终态通过，继续支持 2.1.325；未知版本在模型调用前拦截 |
| 工作流配置 | 两份工作流的四处 job env/runner 上下文错误已修复，actionlint 通过；修复后的远端 Offline Checks 与 Verified Package 均已成功，产物已下载并核对 Actions 摘要、包内清单和 ZIP SHA-256 |
| 远端诊断回归 | 首轮全量暴露路径标签与环境凭据重合时的测试假设错误；已有用例现在分别验证普通及已脱敏标签，并加入 quick。保持运行时脱敏规则，原失败运行 44576477097 和产物保留 |

## 平台矩阵

| 平台 | 状态 |
| --- | --- |
| Windows / Python 3.11.9 | 本地首版及远端修正版本，固定源码与解压 ZIP 各 657 项完整通过 |
| Windows / Python 3.12.10 | 标准导入路径环境完整复验 657 项通过，零失败/错误/跳过，源码指纹不变，763.516 秒；初轮隔离路径导致的 5 失败/1 错误证据保留 |
| Windows / Python 3.13.17 | 标准导入路径环境完整复验 657 项通过，零失败/错误/跳过，源码指纹不变，761.320 秒；初轮隔离路径导致的 5 失败/1 错误证据保留 |
| Linux | 未完成。当前仅有 Docker Desktop WSL 发行版，Docker 引擎未运行 |

3.12/3.13 使用本机已有的官方嵌入式 CPython 发布资产，已核对原始 ZIP SHA-256、解释器与标准库字节；在新解释器副本中停用隔离 `_pth`，恢复标准工作目录导入查找。没有改动产品源码来迎合解释器配置。并发平台矩阵超过 480 秒性能观察目标，保留 `full_within_target=false`，耗时不作串行发布性能比较。两份本地跨版本全量证据绑定首版 `c05431f`；后续测试修正 `6944f04` 的远端证据为 Python 3.11 双重 full 和 Python 3.13 quick，不冒充重新运行了 3.12/3.13 全量，也不外推 Linux 或其它解释器发行方式。

## 0.5.5 当时的后续工作

1. Codex 0.170.0、反重力 1.5.12 需要新版本的脱敏真实协议、文件工具和维护闭环；Qwen 0.26.0 仍缺本轮文件工具真机复验。未知版本不放宽解析。
2. Grok 1.0.89 的 models 命令明确报告未认证。认证及隔离用户/项目配置验证后，再采集协议并实现适配器；当前没有注册 cli_grok。
3. 豆包独立 CLI 尚未核实且未安装，保留未验证状态；不能根据猜测命令接入。
4. 配置可用 Linux 运行环境后执行完整矩阵。当前只有 Windows 交付证据。
5. 扩大自身维护任务的模块和复杂度，再考虑其它真实项目；本轮同模块小样本不外推跨项目成功率。实际账单金额未取得，继续明确留空。

跨 schema 状态迁移需真实旧版本样本及迁移契约；现存历史样本均为 schema 2，本轮提供兼容检查、备份与原字节恢复，不臆造 v1→v2 迁移。多用户、逐 token 会诊、远程 worker、外部聊天平台和自动部署属于另行规划的产品扩展。

## 本机交付位置

- 最新远端验收包：`D:/work/ai-pipeline-delivery-20261120/remote-release/6944f045a2b5/artifact-14558767628/agent-orchestrator-0.5.5-6944f045.zip`
- 最新包 SHA-256：`aa7246abe00cd6da3230da1a17c04e4dae88045149305ddf1fc15b09b6e4203f`
- 远端验收摘要：`D:/work/ai-pipeline-delivery-20261120/remote-release/6944f045a2b5/verified.json`
- 远端快速检查证据：`D:/work/ai-pipeline-delivery-20261120/remote-ci-6944f045a2b5/verified.json`
- 本地首版包与发布结果：`D:/work/ai-pipeline-release-055-20261120/`；保留原始提交、哈希和证据
- 工作台私密入口：`D:/work/ai-pipeline-delivery-20261120/service/工作台入口.md`
- 部署、守护恢复、业务基准与工具盘点：`D:/work/ai-pipeline-delivery-20261120/`
- 升级前备份：`D:/work/ai-pipeline-workbench-backup-20261120/`
- 平台矩阵：`D:/work/ai-pipeline-matrix-20261120/`

远端本轮快速检查运行为 44579229092，正式打包运行为 44579632014，验收包保存在 Actions artifact 14558767628。该流程没有创建公开 GitHub Release 或标签。普通 Git 推送连接异常时使用 Git Database API 传送原 Git 对象，提交、源码树和文件 SHA 全部匹配后才以 `force=false` 快进 main，没有重写提交。

运行状态、配置、原始模型输出、数据库和本机凭据不提交 GitHub。原始失败记录和旧发布包保持冻结。
