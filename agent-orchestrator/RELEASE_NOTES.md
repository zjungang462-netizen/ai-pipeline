# 0.5.9：多模型圆桌升级、白名单测试编排、报告导出与双模型并行竞技场

- **多模型圆桌直接对话升级**：支持 1~3 轮 4 角色（`architect`/`developer`/`skeptic`/`reviewer`）交叉质询、`@相对路径` 工作区源码只读引用、`turn_consensus` 结构化共识总结与一键「采纳为审批附注 / 基于结论新建任务」；新增 `cli_agy` 严格只读席位（`mode="plan"` + `sandbox=True`）与兼容 API 混编，并在第 2~3 轮及共识阶段通过 `messages` 索引自动注入人工主持人最新插话引导。
- **项目管理与白名单确定性测试节点**：新增 `POST /api/projects/<id>/remove`（校验无活跃运行后安全注销 SQLite 登记及可选清理托管 `.toml`，绝不碰用户代码目录与历史运行）及「已登记项目列表」UI；动态节点编排器新增 `确定性测试 (test)` 节点与 `feedback_to` 自动打回，仅允许白名单预设 `pytest` / `python_unittest` / `npm_test` / `npm_run_build`。
- **交付报告导出、无损归档与一键路由推荐**：新增 `GET /api/runs/<key>/export-report` 一键导出 Markdown 交付报告（含阶段汇总、SHA 校验产物摘要、4 反引号累计 Diff、圆桌共识与用量）；新增独立 `archived_runs` 表与 `POST /api/runs/<key>/archive` 实现无损归档/隐藏与「已归档」筛选；新建任务弹窗新增 `✨ 按历史推荐` 按钮一键填充历史推荐模型。
- **双模型并行 Best-of-N 竞技场**：新增 `orchestrator/arena.py`、`POST /api/projects/<id>/arena`、`POST /api/projects/<id>/arena/apply` 与看板「双模型竞技场」页签：在两个隔离 `git worktree` 中并行运行 CLI 编码候选并可选执行白名单确定性测试，并排展示红绿高亮 Unified Diff；仅在工作区干净、HEAD 未漂移且经人工明确拍板后才以 `git merge --ff-only` 安全合入。

# 0.5.8：受控自优化、动态多节点编排与工作台全面升级

- **受控失败经验沉淀与多模型自适应路由推荐**：新增 `lessons.py`（只读扫描历史 `review_verdict.json` / `test_record.json` 高频缺陷，脱敏并绑定内容哈希，经审批写入 `approved_lessons.json` 后注入后续 `report`/`code` 提示词并同步无人值守指纹）与 `model_router.py`（聚合终态 `metrics.json` 按 `(stage_kind, tool, model)` 输出可解释排序，跳过未执行阶段、单独记账 `unknown_calls`）；接入 `lessons-propose` / `lessons-approve` / `route-recommend` CLI 及工作台「看板与优化」3 标签页。
- **动态多节点项目编排与本机实时模型目录**：新增 `model_catalog.py`（隔离环境只读探测本机已装 CLI 与 `agy models` / 兼容 API `/models` 真实可用模型）与 `project_manager.py`（支持 1..12 个自定义模型节点增删、排序、独立工具/模型、人工审批与打回目标，热加载生效无需重启）；「新建任务」弹窗支持直达「＋ 增删 / 编排节点」并展示阶段归属徽章。
- **控制页体验与审批闭环升级**：新增审批附注（局部采纳修订指令自动注入下游提示词）、深色红/绿高亮 Unified Diff、原生安全 DOM Markdown 富文本排版（标题、列表、缩进代码块一键复制、表格与安全链接）、角色配色卡片、横向阶段进度条与连续 `cli_progress` 心跳折叠。
- **反重力 CLI 适配与防幻觉模板**：`cli_agy` 适配 `1.5.14` 并新增可选同系列 `allow_patch_probe` 协议探针；CLI 编码提示词明确使用文件编辑工具不调用终端；沉淀仓库内置防幻觉审查模板 `prompts/evidence_review.md` 与交叉反查方案模板 `prompts/verified_repair_plan.md`；`dashboard` 新增 `--open` 自动打开浏览器。

# 0.5.7：容器隔离底座、调用次数预算、核心模块解耦与扩展主入口

- **容器适配器体系化与安全加固**：新增 `cli_grok` 与 `cli_doubao` 2.37.2 容器适配器，抽离 `docker_base.py`、`workspace_sync.py` 与多版本 `verified_runtimes.py` 三层公共底座；豆包编码阶段声明可编辑文件时使用隔离临时工作区副本并经宿主机基线核验后原子回写；`doctor` 补充容器二进制 SHA-256 与豆包低预算告警静态核验。
- **无用量 CLI 预算策略与基准扩展**：`run_budget` 新增 `max_model_calls` 与 `max_unknown_calls` 调用次数上限并同步至 `metrics.json`、`reporting` 与 `README.md` 配置表；`multi_project_benchmark.py` 新增分账退款样例 `ledger` 与 `--unlimited-tokens` / `--max-model-calls` / `--max-unknown-calls` 参数。
- **核心模块解耦与验收加速**：拆分全仓四个最大模块（`collaboration/service.py` → `run_listing.py`、`state.py` → `state_io.py`、`engine.py` → `deferred_recovery.py`、`review_policy.py` → `prompt_context.py`）；`artifacts.py` 减少高频 `git` 子进程调用；`verify_delivery.py` 增加可选 `--workers` 分片并发执行。
- **单人自用长期扩展接入主入口**：新增逐 token 实时会诊（`openai_compat` SSE 解析 + `discussion_stream.py` + `PIPELINE_STREAM_DISCUSSION=1`）、契约化状态迁移注册表（`state-migrate`，不臆造未验证 `v1→v2`）、脱敏状态通知 Webhook（飞书/钉钉/Slack/通用 JSON，`PIPELINE_WEBHOOK_URL`）、带审批哈希绑定的受控部署执行器（`deploy`）与双向 HMAC 防重放远程 Worker 协议（`worker-serve`）。

# 0.5.6：当前 CLI 真机文件工具与调用前版本门禁

- 增加 Codex 0.170.0、反重力 1.5.12 / 1.5.13、Qwen 0.26.0 真实读写夹具、来源指纹与严格终态回归；未知版本在模型调用前停止。
- Qwen 使用受限文件工具、隔离自定义配置和 stream-json；新增消息计量去重、工具错误与重复读取护栏。反重力显式开启自身 sandbox，不把它描述为 OS 文件隔离。
- 新增 qwen.example.toml 和 13 项 quick 回归；显式 Qwen 后端连接参与输入指纹，凭据值不入指纹。真实维护结果及发布证据以 DELIVERY 与根目录 WORK_STATUS 为准，历史失败保留。

# 0.5.5：完整状态备份、调优加固与自身维护基准

- 新增状态兼容检查、完整校验备份和新目录恢复，保留失败、用量、审批和原始路径，不自动续跑或改写未知 schema。
- 减少返工轮次的建议要求整条历史运行成功，避免局部测试通过掩盖后续失败；退化试用仍按原策略回退并冻结。
- Claude Code 增加 2.1.328 真实文件工具夹具与精确版本验证，未知版本在模型调用前停止，支持版本继续保留 2.1.325。
- 增加本机 CLI 盘点、工作台数据库与状态快照、自身真实维护基准入口；整合三项模型维护补丁，修复超大整数溢出、畸形现场用量和 job 摘要崩溃。实际验收、试跑失败记录和未覆盖范围见 DELIVERY.md。

# 0.5.4：主线自动检查、网页进程守护与固定修复场景

## 2026-11-19 稳定性收口

- 加固 CLI 从进程创建、Job 绑定到 I/O 线程接管之间的失败清理，关闭未接管管道并保留原始异常。
- 检测 stdin 未完整写入，避免子进程退出码为 0 时误报调用成功。
- 遥测收尾即使快照写入或日志关闭失败，也会释放剩余日志并保留启动失败、超时或执行错误。
- 在 CLI 输出流结束后复核最后一条无换行消息的 token 用量，避免漏判预算上限。
- 状态加载现在拒绝未知/缺失 schema、run_id 错配和无法安全保留的字段；校验失败后同一实例不能覆盖原文件。未捏造历史 schema 迁移，边界见 `DELIVERY.md`。
- 当前离线基线为 quick 129 项、full 637 项，零失败/错误/跳过；全量用时受环境负载影响，最新观测超过 480 秒目标。真实 CLI 账户、协议和账单边界仍以 `DELIVERY.md` 为准。

新增 Windows/Python 3.11、3.13 的 GitHub quick 检查和手动双重 full 验收打包。增量凭据扫描可指定 base-ref，避免干净 CI 检出遗漏已提交变更。远端是否通过以实际 Actions 结果为准。

网页守护独立于任务 supervisor，关闭自动任务恢复，提供 PID 绑定健康检查、租约锁、有限退出重启、停止标记和退出码日志。活动任务、排队指令或会诊阻止自动启动；存活但不健康时只留诊断，不自动强杀或创建第二份。

基准入口区分五项离线机制场景与真实模型小样本，记录返工、attempt、耗时和未知计量；真实配置先检查全部角色。当前本机的登录、版本、可执行文件阻塞与失败样本保留，未声称通用自主开发成功率已提高。详见 OPERATIONS.md 与 DELIVERY.md。

# 0.5.3：Claude Code接入用户的DeepSeek兼容连接

新增`cli_claude`，按真实Claude Code 2.1.325流式协议验证终态、工具事件和计量。只继承用户的连接/模型字段，凭据仅在子进程环境；原配置不改写，工具身份与DeepSeek实际后端分开记录。控制台支持模型、上下文后缀和max思考强度。

原生Claude Code经DeepSeek Pro连接完成真实文件读写和一条维护任务闭环，四阶段各一次通过，独立保护测试与审查通过，无主代理补写实现。计量按message.id去重、最终累计取最大值，缓存与thinking子计数不重复加，缺失/中断保留未知。CLI美元估值不冒充DeepSeek账单。具体范围、样本、操作和最终回归数字见[DELIVERY.md](DELIVERY.md)；旧包和标签保持冻结。

## 0.5.2：Qoder真实文件工具与维护闭环

精确适配Qoder 1.1.89/协议1.5.0的stdin、stream-json和严格成功终态。控制台可选Qoder账户模型/思考强度及adaptive模式；代码阶段只开放文件工具，独立测试不交给模型执行，未知版本或工具拒绝不算完成。token全零按未知计量处理，credits在state/metrics/CSV单列留存。

Ultimate/high/adaptive已真实读取和修改文件，并完成一条只修改一个源码文件的真实维护闭环，外部保护测试和独立审查均通过，无主代理补写实现；整合该任务产出的畸形事件摘要修复并新增回归。真实样本provenance、未知用量口径、Grok订阅CLI外部试用边界和最终验证数字见[DELIVERY.md](DELIVERY.md)。旧版本包和标签保持冻结。

## 0.5.1：高思考模型的结构化审查选项

真实重放发现模型在结论 JSON 后附带说明，严格解析器因此拒收。兼容 API 现在可显式启用 `review_json_mode`，只在 review 请求发送端点的 JSON 对象输出约束；报告与编码请求保持原协议，解析器和通过判据不放宽。控制台可配置此选项，原配置默认关闭。已对 DeepSeek v4-pro/high/thinking 做真实探针，独立 verdict 成功落盘；详细回归和任务重放结果以交付报告为准。

## 0.5.0：优先选择模型与思考强度，补齐运维和发布入口

新建任务可按适配器设置账户可用模型及受支持思考参数，默认勾选“不限制 token”，用独立配置副本固化选择。该模式不施加本地 token 总量/输出限额，用量和未知部分继续记录；时长、轮次、权限与审批保持。工具拒绝不支持的型号或参数时不自动降级。

增加运行/阶段/工具用量 CSV、80%预算提示、配置绑定任务模板、两运行摘要对比和有界历史失败看板。旧 metrics 不改写，未记录的字段保持未知，未报告实际账单费用时不估算金额。修正未记录期限的误报，并隔离旧 SSE 回调。

提供单一版本号与本地一键发布脚本：固定干净 HEAD 的 Git 原始字节，源码完整验收、最终包独立解压完整复验均通过后生成正式 ZIP/SNAPSHOT/SHA；自动提交 CHANGELOG 仅作追溯，功能和验证边界仍以交付说明为准。当前验收数字及操作见 [DELIVERY.md](DELIVERY.md)。旧发布包/标签保持冻结，豆包与 Qoder 成功 CLI 样本缺口未由本阶段消除。

## 阶段四：输入完整性与根外文件保护

测试输出新增内容哈希绑定，改变或删除输出会使旧缓存失效；测试期间改写或删除既有输入会拒收。根外变化只保存有界元数据，原文件保留原位；证据写入失败仍判阶段失败。旧的仅路径输出记录需重新验证。

失败 attempt 在调用前持久化，成功更新同一记录；metrics 的重试按真实编号计数，旧终态账本不改写。审查覆盖上限及不完整策略可配置，未覆盖清单随 attempt、success/decision 及缓存恢复留存。

加入 Qwen Code 0.26.0 真实 stdin/JSON 数组终态样本与精确版本解析；Qoder 仅认证失败有真机证据，豆包成功 CLI 协议仍未验证。完成受保护测试的真实仓库维护试点并保留失败样本，后续整合 doctor 内联提示词、UTF-16 凭据扫描与诊断保留摘要修复。Python 版本与 Linux 的有限验证范围、实际完成率和账单费用缺失统一见 DELIVERY，不将小样本视为通用成功率。

当前开发增量的最终验收与升级说明见 [DELIVERY.md](DELIVERY.md)；下节为阶段三已完成基线。

## 2026-11-17 阶段三：职责拆分与配置、文档门禁

引擎按预算、归档、审查策略、路径守卫和阶段执行拆分；Engine 保留状态机、所有权与审批接口，异常类型继续从原入口导出。三批方法搬移均记录 AST 等价证据并通过完整回归。

配置 dataclass 统一声明字段、默认值、数值约束和说明，加载器、doctor、README 配置表与六份示例注释复用这些定义；生成文件漂移纳入提交前门禁。配置特征对照保持合法配置、坏类型和边界的接受结果及报错一致。

README 保留当前用法，历史过程与旧发布说明移至 HISTORY。测试提供 fast/slow/full 分层；quick 与 fast 使用同一集合，slow 自动取完整集合的补集。每次保存测试标识、逐项耗时与慢用例列表；长日志仍可脱敏。流式日志测试使用握手结束，保留执行中观察和部分用量断言，减少固定等待。

本阶段不改变产品执行、预算、审批或审查策略。最新验收数量、耗时、发布包区别与未验证范围统一见 [DELIVERY.md](DELIVERY.md)。历史记录见 [HISTORY.md](HISTORY.md)，冻结的旧包与标签保持原样。
