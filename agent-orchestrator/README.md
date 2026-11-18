# 多智能体开发流水线编排器

把 **Codex、豆包、Antigravity、dsh（DeepSeek Harness）** 等真实编码工具按角色分工，串成「需求 → 设计 → 编码 → 测试 → 审查」自动化流水线。

核心是**轻量编排引擎 + 可插拔适配器**，而非再包一层 LLM 框架。每个工具一个适配器，按配置把角色映射到工具，发挥各自所长。

工作台创建任务时需明确选择项目配置，并显示实际目录与执行阶段。任务描述中写入另一个路径不会自动切换工作目录，也不会为固定测试配置增加设计或开发阶段。只有固定测试的配置显示为“固定测试流程”，完成后标注“固定测试已通过”；它只证明已配置的命令通过，不能当作业务功能已实现。历史配置未绑定或已变化时，界面不会用当前配置推断原运行的交付范围。

## 工作台布局与时间导航

左侧“任务工作树”按日期分组，每项显示项目、状态和创建时间。可选择“最新在前”或“最早在前”；排序覆盖完整历史，支持与关键词、项目和状态筛选组合，URL 保存选择。切换方向重新获取分页，保留当前任务房间与实时连接。任务数表示当前已加载数量，带 `+` 表示还有后续页。

任务房间的流程说明、完整任务文本和长执行日志按需展开；配置变化或待审批提示自动展开。窄窗口点击“执行证据”查看侧面板，手机宽度通过左上角菜单打开任务列表。

## 当前交付基线：2026-11-16

2026-11-17 阶段二开发已增加 `doctor`、默认 dry-run 的状态清理、`verify_delivery --quick`、提交前检查和可选日志脱敏；阶段二基线全量验收为 **514 项通过**。当前增量再补充 8 项测试输入完整性与根外文件保护回归，完整结果以 `verify_delivery.py` 的新输出为准。下述已发布标签与 479 项源码包保持原样。操作与证据保留规则见 [DELIVERY.md](DELIVERY.md)。

当前增量修正两项续跑边界：测试产物必须绑定内容哈希，产物变更/丢失都会重新执行测试；测试期间既有输入被改写或删除时拒收，不能把源码降格为输出。`watch_enforce=true` 仍持久化失败，但只保存有界变化元数据，保留根外文件原位置，不再移动其它进程的文件。仅有路径、没有哈希绑定的旧测试记录需重新验证；本 run 内失败尝试自己新建的产物会登记为输出，修复后重试不会因该文件被判输入变化。同一工作区上一次运行登记且内容未变的测试产物会被继承为输出，后续任务不再把它当作既有输入；被用户或其它进程改写过的生成文件仍按输入变化保守拒收，建议先清理已确认的生成物或让测试写入独立输出目录。

本次交付标签为 `delivery-2026-11-16-round9`，纳入后台同步、CLI 可靠性、执行范围展示及第九轮审查修复。完整离线回归为 **479 项通过，0 失败、0 错误、0 跳过**；发布验收以源码包附带的 `delivery-metadata/verification.json` 为准，提交与逐文件 SHA-256 见包内 `SNAPSHOT.json`，压缩包 SHA-256 见包旁的 `.sha256` 文件。Python 运行依赖为标准库；完整开发测试还需要 Git 与 Node.js。版本内容与适用边界见 [RELEASE_NOTES.md](RELEASE_NOTES.md)，干净目录启动、升级注意事项与回归步骤见 [DELIVERY.md](DELIVERY.md)。以下分节保留各轮历史验证数量，不能将历史样例结果视为所有业务任务的成功率保证。

### 本次发布内容（第九轮审查修复后）

旧交付基线 `e2a4535` 的 388 项结果属于历史记录。本次发布包含其后的 `1e26ee9`、`9ce666e`、`e886066`、`8b02785` 及发布文档收口提交；完整回归入口为 `scripts/verify_delivery.py`。包内 [RELEASE_NOTES.md](RELEASE_NOTES.md) 独立说明修复内容与适用边界。开发机上的 `../review-2026-11-16/修复复验报告.md` 为补充证据，不是交付依赖。

第九轮修复后的关键行为变化：

- **路径安全**：`.git` 及其 Windows 等价变体（`.git.`、`.git `、`.git...`）在读取（审查点名文件）与写入（API 适配器/清单提交）两侧统一拒绝；解析后再次检查相对路径分段。
- **审查覆盖**：累积 diff 超过 600 行时，按 commit 注入变更文件全文；仍无法完整注入时 pass 不自动采纳（人工模式转待审批，无人值守判失败）。审查现有代码库模式的输入指纹纳入工作区内容哈希，源码变化后不再沿用旧 verdict（含 autonomy 逻辑哈希）。
- **续跑一致性**：测试命令写出的工作区产物记入 `state.test_artifacts` 并从测试阶段指纹中排除，避免每次 resume 重跑测试；失败尝试新建的产物同样登记，同一工作区上一次运行中内容未变的产物会被下一次运行继承；已消费的审查反馈保留参与返工 attempt 的指纹，review 通过后 resume 不再重跑 coding/review。
- **审批**：code/test 阶段的审批不再实时重算可能被自身执行改写的 inputs_hash，只比对申请时冻结值、attempt 归档与 evidence；report/review 阶段仍拒绝输入变化后的审批。
- **可写根外变更（watch_enforce=true）**：根外文件保留原位置；`<run>/violations/<stage>/` 只保存最多 100 条变化元数据，不读取或复制文件正文。新增/改写/删除都会按 inputs_hash 持久记录，续跑保持失败且不再调用模型，需 `rerun-from` 显式重试。证据写入失败也会保持阶段失败并记录错误。`watch_root` 落在可写根内会记 `watch_disabled` 事件并禁用该监视域。
- **完成判据**：code 阶段零净变更判失败（与无人值守一致）；既有非 git 工作区首次初始化时把已有文件提交为基础基线，coding 合并不再因 untracked 冲突永久失败。
- **配置**：未知顶层/阶段/run_budget 字段直接报错，避免 `approval`/`watch_enforce`/`max_tokens` 拼写错误静默失效；`config_keys` 必须可 JSON 序列化。
- **锁**：过期锁回收先抢占 `.reclaim` sidecar 再核对接管，避免两个恢复者互删新锁；内容损坏的锁给 5 秒宽限期后回收。
- **CLI 启动**：Windows 挂起进程恢复优先使用 `NtResumeProcess`（失败回退到线程枚举），启动开销不再计入 CLI 执行超时；挂起创建窗口与清理路径异常也会终止本次进程。
- **工作台**：单个登记配置损坏/删除只把该条目标记为不可用，不再连坐 `/api/projects` 或阻止启动；后台投影线程对畸形状态/瞬时异常不退出；新建任务携带 `expected_config_hash`，会话中改配置后创建会被 409 拒绝并提示刷新。
- **交付验收**：`verify_delivery` 源码指纹覆盖 `demo/`；`verification.json` 的失败/错误正文按已知凭据与可识别令牌脱敏（主回归日志保持原始内容，见 DELIVERY）。`demo/run_api_demo.py` 退出码与 CLI 对齐：完成 0、等待审批 11、失败/未完成 1。

## 全历史搜索与 dsh 测试角色

工作台现支持服务端全历史关键词、项目与状态组合筛选，覆盖未加载到页面的旧任务。筛选结果继续稳定分页，切换条件时忽略旧请求；刷新保留任务房间与实时连接，筛选和选中任务写入 URL。清除项目可回到全部项目。

`GET /api/runs` 新增 `q`（最多 200 字符）、`project_id`、`status`；关键词按 Unicode casefold 作字面子串匹配。`running` 包含 pending/running/recovering，`failed` 包含 failed/timeout/cancelled。游标绑定筛选条件，显式更换条件须重新从首页查询；只传游标可继承原范围，原 `before`、无筛选游标与响应结构保持兼容。

历史列表仅从 `state.json` / `job.json` 读取标题与状态，不执行事件投影、不写投影游标，也不扫描尝试目录或加载报告正文。最多缓存 2048 条摘要，每次请求检查两份源文件签名，变化后重新读取并校验一致性；淘汰不影响查询结果。摘要缓存与房间投影缓存独立，打开任务房间和后台同步仍正常投影消息、事件及产物。首次查询不再为每条历史运行生成数据库写入；关键词、状态分组和稳定分页规则保持原有语义。

后台同步采用独立轮巡游标，每轮最多检查 50 个目录和 50 条已登记运行，项目间轮换发现目录；已登记运行按唯一 ID 翻页，覆盖全部历史，不再限于最近 100 条。启动时和显式 `Service.sync()` 仍做完整同步；工作台定时调用 `sync(incremental=True)`。未变化的记录通过文件签名跳过重复登记、job 读取和投影，仍检查排队控制指令与会诊恢复。导入/投影轻量签名各最多 4096 条，完整状态缓存最多 512 条；淘汰后可重建。扫描在下一轮持续推进，目录变化、旧任务更新或游标之前的新记录最迟在后续完整轮巡中发现，不保证所有历史每 0.75 秒立即同步；打开房间仍读取当前状态。增量轮次中单条记录或单个项目配置的任意失败只计入 `errors` 并继续后续记录/项目，显式完整同步保留报错语义；投影线程对畸形状态文件或数据库瞬时错误不退出，最近错误保存在 `Service.last_sync_error`。关闭服务会释放目录扫描句柄。

工作台项目列表逐项目容错：单个登记配置被删除或改坏时，`/api/projects` 仍返回 200，该条目带 `unavailable` 说明且前端不可选；健康项目继续返回 `execution_scope` 与 `config_hash`。创建任务必须携带页面确认过的 `expected_config_hash`，与会话中变化后的配置不一致时返回 409 并要求刷新。

dsh **0.2.2-rc.3** 已补齐原生 headless JSON 事件解析、唯一完成事件、逐步去重计量、权限与代理参数。`totalTokens` 已包含缓存，不重复相加。凭据通过 `extra_env=["DEEPSEEK_API_KEY"]` 显式传给子进程；默认不透传。示例见 `dsh-test.example.toml`。模型测试报告仍需独立真实命令核对，不能因模型执行完成就将测试判通过。

真实分工为 Codex 设计与独立审查、反重力落盘主要实现、dsh 执行固定前端测试。最终 dsh 实跑旧分页 8 场景和新筛选 6 场景，两项退出码均为 0，文件哈希不变；需要临时写盘的 Python 套件仍由外部执行器完成。Windows dsh 沙箱曾遇到 ACL/临时目录限制，没有通过完全权限绕过。

本轮主代理补齐了若干兼容性、Unicode 游标和真实 DOM 边界，不能称为完全无人值守成功。反重力原始超时、预算停止、认证失败及中断恢复记录均保留。详细行为、实际用量、浏览器截图和最终完整回归见上级 `live-validation-2026-11-16/history-search/实施报告.md`。

历史筛选交付时的固定源码完整回归 **322 项全部通过，零跳过**，UTF-8 模式关闭、默认编码 CP936；包含 11 项筛选 Python 契约、其中的 6 个前端异步场景，以及 9 项 dsh 协议回归。该轮原有受保护测试字节保持不变。

## 2026-11-16 CLI 实战可靠性

CLI 现在将 stdout/stderr 实时写入每次尝试的 `live-stdout.log`、`live-stderr.log`，并生成原子 `cli-progress.json`。工作台展示反重力当前工具、相对文件、最后输出时间、已完成响应的计量下界及缓存读取。活动摘要通过 SSE 重放；它不能替代真实测试、审查结论或归档证据。已知子进程环境凭据会跨输出块脱敏；这不保证识别任意未知秘密。

CLI 返回给适配器的 stdout/stderr 分别保留最后 `4*1024*1024` 个解码字符，使用有界分块缓存，避免每次读取都复制整个尾部。中文与 emoji 的截断仍按字符计算，UTF-8 跨块解码、超时返回及流式计量保持原有语义。实时日志的字节上限、脱敏与停止护栏独立生效；完整流中已经观察到的计量不会随返回尾部截断而丢失。

Windows 的 CLI 与受管 worker 先以 `CREATE_SUSPENDED` 创建，再归入 Job，最后恢复初始线程，避免 `.cmd` 在归属建立前已启动后代、超时后留下占用管道的子进程。恢复优先使用 `OpenProcess(PROCESS_SUSPEND_RESUME)` + `ntdll.NtResumeProcess`，失败回退到 Toolhelp 线程枚举；CLI 超时预算只覆盖执行本身，不含 Job 绑定与恢复开销。从挂起创建到归入 Job 之间的任何异常都会终止本次进程；恢复失败会关闭 Job 并结束本次挂起进程，不改变文件/网络权限和调用预算。`execution.json` 另外保留原退出码与解析状态，便于区分执行超时、预算停止、非零退出及清理异常。

离线验收提供失败前诊断留存：CLI 可靠性夹具或 `scripts/verify_delivery.py` 可在临时目录清理前保存脱敏状态、事件、退出码与日志尾部，采集失败不将原测试改成通过。默认不复制配置、提示词和源码树；分类使用明确错误类别/停止原因，证据不足标 unknown，多个原因可同时保留。已知环境凭据名（含 token/passphrase/`_key`/access key 等常见命名，值长度下限 4 字节）、测试显式注册的凭据、可识别令牌前缀与带凭据 URL 会被替换；源根绝对路径在构造脱敏器时注册，避免日志尾部残留路径片段。单个目录不可读或深层 JSON 只登记 issue，不中止整包采集。开关、目录和适用边界见 [DELIVERY.md](DELIVERY.md)。

- **中断计量**：按响应标识去重；超时、取消和 worker 重启保留已报告下界，并继续保留未知部分预扣。同一次 pending 只结算一次；Codex 重复完成事件不会重复计费。这里的 tokens 是 CLI 报告和预算口径，不能直接视为账单。
- **恢复候选**：清理失败的编码 worktree 前，将允许文件内的有限变更封存，绑定任务、配置、基线 commit、CLI 配置、提示词、失效轮次和白名单。校验内容与干净目标后才应用到新 worktree，并重新执行测试。封存失败则保留原 worktree、停止自动重试。已结算用量但尚未封存时崩溃，也能通过持久化工作区记录找回现场。
- **有限执行**：连续工具错误、重复读取、静默、输出过量或已知用量加预留超过预算会停止本次 CLI。重启不会重置当前轮次的调用上限、累计用量或原墙钟期限。显式返工仍使用配置中允许的新轮次，并继续受总修复轮数和总预算限制。

可在 `[autonomy]` 配置 `cli_input_overhead_tokens=32768`、`cli_idle_timeout_s=120`、`cli_max_tool_errors=3`、`cli_max_repeated_reads=4`。空闲超时范围为 1～3600 秒，连续工具错误和重复读取阈值均为 1～20；超出范围时拒绝配置。CLI 的预扣和计量在普通执行模式也生效；恢复代码仍需要明确声明 `allowed_files`。恢复点最多三个文件，单文件 2 MiB、合计 6 MiB，并进一步受 `repair_batch_size` 限制。

Codex 0.169 的 `command_execution`、`file_change` 和 `mcp_tool_call` 活动与反重力、dsh 共用工具错误和重复读取护栏，按调用标识去重；成功文件变更才清空重复读取记录。Shell 重复读取只识别无插值、管道、重定向、使用绝对路径的单文件 `Get-Content`/`gc`/`cat`（含明确的 PowerShell、sh/bash 包装）。此版本的命令事件不含逐次执行目录，相对路径和复杂命令仅显示命令活动，不猜测文件位置或读写行为。MCP 仅按明确的已支持读写工具名分类。工作台只显示工具名和校验后的文件路径，不显示命令、参数、工具输出或推理正文。

Codex 在进程启动前被策略拒绝时可能只有 stderr 日志，没有 JSON 工具事件；现仅按 0.169 真机确认的 router 拒绝前缀计入工具错误。普通 stderr 错误词不触发该规则。护栏在最后一条无换行事件或进程退出时触发，也不会被退出码 0 和最终消息覆盖。

Windows 隔离模式在 `--ignore-user-config` 下保留用户显式配置的 `windows.sandbox`（`unelevated`、`elevated` 或 `mxc`），并将后端纳入执行指纹；未配置时不自行选用，非法配置明确拒绝。该后端用于实施操作系统隔离，报告阶段仍为 `read-only`，不引入桌面 MCP、插件或 hooks。此前只读命令被拒绝的原因是后端随用户配置一起被忽略。2026-11-16 已使用修复后的适配器真实完成两次独立只读调用，并验证重复读取阈值为 1 时在第二次开始后以 `tool_limit` 停止、没有成功报告。探针设置工具 `login=false`，避免受限语言模式下加载 PowerShell profile 的警告；没有修改用户配置。详见上级 `live-validation-2026-11-16/codex-normal-tools/`。文件变更和 MCP 工具生命周期仍以官方版本协议构造事件回归，不将只读命令验收扩展为所有工具的真机证明。

关闭工作台服务时会停止并等待后台投影线程退出，扫描过程也会响应关闭标记，避免服务关闭后仍占用运行状态或 SQLite 文件；未能在关闭期限内回收线程会明确报错。已启动的独立任务进程仍按原有任务控制语义运行。

控制指令的状态校验/入队与 supervisor 终态发布共用每运行的 `.control.lock`。成功 worker 已完成最后检查点但尚未发布终态时，已准入的暂停会在退出交接处补结算，保留完成 attempt 与既有用量；失败/超时不会因此伪装成暂停，取消优先。终态已发布的新暂停请求被拒绝。若旧 manager 仍在退出，继续/审批等已排队指令会等待其释放归属后再交付；不为确认暂停重跑阶段，不增加原预算、期限或自动重启额度。控制门闩最多等待 10 秒，繁忙时明确报错，界面收到入队回执仍须等待实际生效收据。

本轮 **21 项可靠性专项、4 项独立 HTTP/SSE 契约全部通过**。独立真实任务 `tag-normalize-01` 经 Codex 设计、反重力修改一个文件、六项外部测试、Codex 审查完成，未人工补写实现，受保护测试与说明文件哈希不变。墙钟约 83 秒，模型调用及测试计量时长约 66 秒，CLI 保守计量 56,822 tokens，未知预留为 0。

CLI 可靠性交付时的固定源码完整回归 **302 项全部通过，零跳过**，UTF-8 模式关闭、默认文件编码 CP936。该数量包含上述 21 项可靠性专项；4 项 HTTP/SSE 契约单独执行。

测试失败返工、超时恢复和磁盘保存失败由确定性本地 CLI 故障注入验证；真实小任务没有刻意制造这些故障。这个样例证明受控小任务能够完成闭环，复杂项目的长期无人值守成功率仍需更多实战验证。CLI token 限额仍是观察与预扣约束，无法像 API 请求一样强制模型单次输出上限。

本轮配置、原始记录、恢复验证、截图与完整回归结果见上级 `live-validation-2026-11-16/cli-reliability/实施报告.md`。历史分页任务的两次超时记录保持原样。

## 2026-11-15 修复版（当前行为）

本节优先于后面的历史演进记录。保持零第三方依赖、阶段串行和现有适配器接口。

### 千问第四轮报告复核后的修复

该轮完整回归 **300 项**（含 CLI 可靠性专项），验证环境为 `utf8_mode=0`、默认文件编码 CP936。两个报告点名的测试解码错误已复现并修复，原有测试文本读写统一显式UTF-8。

### 千问第五轮自审后的修复（N1/N2/N3）

- **N1 CLI 退出码对齐**：流水线停在 `awaiting_approval` 时，`run/resume/approve/rerun-from` 返回 **11**（与 worker 一致）并提示"未完成：等待人工审批"，不再返回 0 让脚本误判成功。全部完成仍返回 0，失败返回 1。
- **N2 常量公开化**：`artifacts.SKIP_DIRS` 成为公开常量，引擎不再引用私有 `_SKIP_DIRS`。
- **N3 移除全仓提交**：删除 `commit_all`（git add -A），产品与测试统一走 `commit_files` 清单提交，杜绝绕过清单约束的提交路径。

N1/N2/N3 修复后全量 300 项中 299 项一次通过；唯一错误 `test_http_requires_local_session_origin_and_csrf` 为 Windows 偶发连接中断（WinError 10053），单独重跑与整个协作测试文件（15 项）复跑均通过，与本轮改动无关。

- 产物相对路径统一拒绝Windows保留设备名；完整代码块可安全回退名称，补丁目标直接拒绝，原有文件白名单不放宽。本机未复现原报告“整批合法文件丢失”，具体原始结果和可控端到端验证见复核报告。
- 返工提示增加有界相对文件清单，保留只展开点名正文的限制；只在允许范围内清理确已被替代的旧实现，不自动删除范围外文件。
- 测试工作区创建失败明确记录为 `worktree_error`，归档“未执行测试”的失败证据，不按预算错误误报，也不回退运行。
- 直接调用 `load_verdict` 必须显式传 `workdir`；`pass` 仍要求 `issues=[]`，建议写报告正文。

完整证据见上级 `repair-2026-11-15/qwen-round4/修复复核.md`。本轮验证使用真实文件、Git、命令测试与本地HTTP响应，没有追加外部模型调用。

### 本地群聊与可视化工作台

已加入中文 Web 工作台，使用 Python 3.11+ 标准库、SQLite 和本地静态资源，无需安装 npm 或第三方 Web 框架：

```powershell
python -X utf8 -m orchestrator.cli dashboard --config workbench.example.toml --database .workbench/collaboration.sqlite3 --port 8765
```

打开终端输出的本次启动完整链接，点击「新建任务」。此示例只执行真实 Python 断言，最后等待人工审批，不调用模型。`--config` 可重复指定，用于登记实际项目配置。配置决定任务工作区、可写范围、模型、测试命令和预算，网页不能直接增加权限或任意执行 shell 命令。

- **运行证据**：阶段与反馈轮次、真实模型原文、测试命令和输出、结构化审查产物、固定 commit 差异及已报告/预留 token。普通留言与模型调用分开，历史运行只读导入。
- **明确控制**：启动、绑定 attempt/哈希的审批、检查点暂停/继续、所属进程树取消、阶段重跑；指令排队后等待持久化收据，重复提交不会重复批准或重跑。暂停不延长原有效期，取消不撤销已应用提交。
- **有限角色会诊**：已登记配置包含 `openai_compat` 适配器时，可在稳定检查点向开发者、审查者提问。后一个角色读取前一个的实际回复；最多两个角色各发言一次，单次输出最多 600 tokens，总额度 1000–6000 tokens、最长 90 秒。活动父任务先预留并结算额度；历史/终态任务另开分析账本，原 metrics 保持不变。
- **后续任务**：有固定代码 commit 时，可创建关联的新 run 和独立任务仓库；新任务使用新的预算、审批与验收，不改写旧任务。角色建议需要人工写入新任务要求，再走编码、测试与审查。
- **本机使用**：只监听 `127.0.0.1`，写操作需要本次本地会话、同源和 CSRF 校验；文本以纯文本展示，读取产物时检查清单、路径和哈希。浏览器关闭不停止任务；服务重启后用新链接重新连接即可恢复视图。

任务列表只加载摘要；进入房间后按 cursor 分页动态，通过 SSE 和 `Last-Event-ID` 重放。原执行状态与 attempt 证据仍是权威数据，SQLite 投影可以补建。只登记可信项目配置；工作台和 worktree 均不等于执行沙箱。

历史任务支持「加载更早任务」：每页最多 100 条，按创建时间和任务 ID 的稳定游标继续加载，相同时间记录不漏页。7 秒自动刷新保留已加载历史、当前房间和 SSE 连接，并补齐超过一页的新任务；失败后可按原位置重试。关键词、项目与状态筛选作用于服务端全部历史。旧任务即使不在筛选结果内仍可独立恢复房间；浏览器整页刷新后从该筛选的首页重新加载，条件和房间选择按 URL 恢复。

`GET /api/runs` 保留 `runs`、`limit` 和旧 `before` 时间边界，新增 `has_more`、`next_cursor`；客户端应将返回的 `next_cursor` 原样作为下一次 `cursor` 参数，不自行生成或修改。原有内部 `Store.runs` / `Service.list_runs` 返回类型保持兼容。分页实战与异常记录见上级 `live-validation-2026-11-15/pagination-real/实施报告.md`。新增前端异步回归使用 Node.js 内置模块，运行界面仍只需 Python。

分页功能交付后完整回归 **256 项全部通过**，其中新增 15 项分页专项（含 8 个前端异步场景）。真实浏览器验证了 100→200→257 条连续加载、刷新保留房间，以及突发新增 150 条后补齐至 407 条。本轮反重力两次开发超时，最终由主代理接管完成，Codex 找出溢出边界后修复并复审通过，不能将此记录描述为完全无人值守成功。

本机交付时8765已被Claude代理使用，工作台改在 `127.0.0.1:8766` 运行。新启动可用 `--port 8766` 指定空闲端口；不要停止无关服务来抢占默认端口。

首版范围为**本地单用户、有限双角色会诊**。逐 token 回复、多人账号、外部群平台、远程 worker、自动部署及讨论自动采纳尚未实现。它不提供通用开放式群聊，也不把角色意见自动当成一致结论。

实现及真实验收记录见上级目录 `workbench-2026-11-15/实施报告.md`；完整设计见 `../群聊与可视化详细方案.md`。

工作台首版交付时的完整离线回归为 **223 项全部通过**，其中新增协作专项 15 项。真实双角色会诊完成 2 次 API 调用、2,905 tokens；20 个历史任务及单任务一万条事件样例的列表/快照 p95 均小于 300 ms。数据与适用边界见实施报告。

### Codex 设计/审查＋反重力开发

已在独立任务仓库完成真实混合链路：**Codex CLI 0.169.0 设计 → 反重力 CLI 1.5.9 修改源码 → 六项受保护测试 → Codex CLI 审查**。四阶段均 attempt 1、无 supervisor 重启，只改两个允许的源码文件，测试文件哈希不变。此结果验证该受控样例，不代表任意项目的成功率。

```powershell
python -X utf8 -m orchestrator.cli unattended cli-mixed.example.toml --run-id mixed-demo-1 --max-restarts 0 --task "clean_name 去首尾空白并转小写，保留内部空格；parse_count 只做 int 转换，不额外加一。只修改两个源码文件，通过全部六项测试。"
```

该示例会实际调用本机 CLI 并修改 `examples/cli_mixed_demo` 中故意保留缺陷的源码。运行前核对配置中的工具路径、登录、模型与代理；Windows 建议为 Codex 填写原生 `codex.exe` 路径，避免 npm `.cmd` 对复杂参数的二次解释。

- Codex 设计/审查使用只读模式，提示词走 stdin，结果按 attempt 归档。`isolate_config=true` 只继承本机模型连接信息，避开桌面 MCP/插件配置兼容问题；原配置不改写，内联凭据仅在内存中转为子进程环境变量，不进入 argv 或流水线 TOML。
- 反重力 1.5.9 使用 NDJSON 输入和输出、`accept-edits` 模式；使用文件工具实际修改允许文件，没有启用跳过全部权限检查。代理需通过 `proxy_url` 显式配置，地址不能携带凭据。
- CLI 超时会结束本次调用的所属进程树。缺失最终完成事件、失败/不明结果或无法识别的版本不会判为成功。
- Codex 的缓存计数属于输入 token 的子集，不重复相加。反重力的 `total_tokens` 和 `cache_read_tokens` 分列，当前预算采用两者之和的保守口径，并保留 CLI 原始计数。这些计数不能直接当作实际费用。
- CLI harness 自带上下文和工具开销；目前不能像 HTTP API 一样强制每次模型输出的 token 上限，单次超额仍可能发生。使用调用前估算/预留、调用后用量核对、总墙钟上限及有限轮次控制。

CLI 接入时的完整离线回归 **241 项全部通过**，包含新增 18 项 CLI 协议及预算回归。真实记录与完整回归见上级 `live-validation-2026-11-15/cli-mixed/`。这项接入用于流水线角色；工作台的自由议题会诊仍只接 API 后端。

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
- 父目录快照默认关闭。只有显式 watch_root 才扫描；默认仅告警，watch_enforce=true 才据此失败。enforce 模式保留根外文件原位置，只在 `<run>/violations/<stage>/` 保存有界变化元数据，不读取或复制原文件正文。新增/改写/删除根外文件都会按 inputs_hash 持久记录，续跑保持失败且不再调用模型，修正后需 `rerun-from` 显式覆盖。watch_root 落在可写根内部时会记 `watch_disabled` 事件并禁用该监视域。API 读写均校验实际路径边界。
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
- **安全约束**：可写根白名单（执行前校验 workdir + 执行后扫描根外改动）、子进程 env 白名单（API Key 不透传给 CLI）、`--yolo` 须显式开启、审批校验产物哈希、锁占用中止整轮、`.git` 及其 Windows 等价变体在读写两侧统一拒绝。
- **审查完整性**：累积 diff 被截断时按 commit 注入变更文件全文；仍无法完整注入时 pass 转为待人工确认（无人值守判失败）；审查现有代码库的指纹覆盖工作区内容哈希。
- **配置严格性**：顶层/阶段/run_budget 未知字段直接报错，`config_keys` 必须可 JSON 序列化。
- **版本绑定解析**：CLI 解析按工具名前缀白名单匹配，不识别的版本 => `parse_unknown` 禁止判完成。
- **预算上限**：阶段级与 run 级 token **和时长**预算，超限即 rejected；429/5xx 归为限流并指数退避。

## 快速开始

### 1. 离线演示（无需任何 Key/工具）

```powershell
cd agent-orchestrator  # 从仓库根目录进入
python -m unittest discover -s tests        # 完整离线回归（git 可用时需数分钟）
```

> **git 前置（重要）**：代码阶段（worktree/commit/merge/变更探测）依赖 git。
> - git 在 PATH，或在配置 `git_exe` 填绝对路径，或命中兜底（**Windows 注册表 InstallPath** + Program Files 推导）=> Git 相关测试实际执行。**注意**：Windows 上测试包含大量真实 Git 和子进程操作，全量耗时随机器负载变化，可能需要数十分钟。
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

演示脚本退出码与 CLI 对齐：全部阶段完成 0，存在等待审批 11，失败/超时/中断等未完成 1；脚本化调用可按退出码判断，而不是把"未完成"当成功。

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

> 当前工作台提供有界 API 双角色会诊；CLI 接入用于阶段角色执行。自由多轮 CLI 群聊尚未实现，后续是否采用通用框架另行评估。

## 各工具接入与登录

> **默认示例通道仍是 `openai_compat`**。Codex 0.169.0 与反重力 1.5.9 已完成上文所述混合链路真机验收，使用 `cli-mixed.example.toml` 显式启用。
> dsh 0.2.2-rc.3 已验证 headless JSON 和实际前端测试，见 `dsh-test.example.toml`。豆包仍需真实版本和成功输出样本，不能因注册表中有适配器就认定已经验证。

| 工具 | 适配器 type | 无头命令 | 认证 |
|---|---|---|---|
| Codex | `cli_codex` | `codex exec ... --json` | `CODEX_API_KEY`（CI）或交互登录 |
| 豆包 | `cli_doubao` | `doubao -p ...` | `doubao login` 扫码（无 API-key） |
| Antigravity | `cli_agy` | `agy -p ... --output-format json` | 需先交互式登录一次 |
| dsh | `cli_dsh` | `dsh --profile headless --json ... -` | 本机凭据服务或显式 `DEEPSEEK_API_KEY` |
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
tests/           # 479 个离线测试 + fixtures（含真实/合成样本说明）
config.example.toml
pipeline.example.toml   # 推荐流水线：含真测试命令 + 结论文件审查
```

## 测试

```powershell
python -m unittest discover -s tests -v
```

覆盖：配置校验（含未知字段拒绝/JSON 化 config_keys）、双哈希/stale、attempts 隔离/中断扫描、租约锁（死进程/损坏锁/回收竞态）、五阶段离线跑通、三种 kill 点续跑一致、空跑与零净变更拒绝、重试、审批哈希校验（含 code/test 阶段写自身输入）、API mock（正常/401/超时/解析失败）、CLI 版本绑定解析、中文+ANSI+超长输出、env 白名单、可写根越界与 watch 隔离/持久失败、worktree 传递与含空格中文路径、CLI 入口冒烟、预算超限、审查覆盖不完整转人工确认、测试产物不触发重跑、既有非 git 工作区基线、挂起启动性能与清理，以及四个阻断项回归（锁占用中止、审批续跑不覆盖、纯文字拒判完成、已成功 attempt 采纳不重跑）。

**需真实环境联调**（离线测试不覆盖，不假装已验证）：
- 真实 Key 冒烟（百炼/方舟）。
- 各 CLI 安装与登录（豆包扫码/agy 登录）。
- **CLI fixture 的真实性按文件区分**（见 `tests/fixtures/README.md` 与 `cli-capture-provenance.json`）：`codex_0.167.jsonl` 是手写合成样本，只用于旧解析回归；Codex 0.169.0、反重力 1.5.9、dsh 0.2.2-rc.3 的样本为真实抓包（dsh 原始副本未保留独立字节，已在 provenance 注明）。豆包仍需真机抓包核对。

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
- **M6** 429/5xx 归为限流并指数退避；API 编码阶段提取代码块写盘。

L2 审查反馈环（`审查报告-L2反馈环.md` 实测三条通过）后，第三轮实测硬缺陷已修：
- **G1** git 探测加兜底路径 + 配置 `git_exe`；代码阶段缺 git **明确报错**，不再静默降级误判空跑。
- **G2** 越界检测改为执行前后**可写根快照对比**，真正捕获 git diff 清单之外的根外写入。
- **G3** 审查结论不可解析 => **待人工确认**（AWAITING），不再默认放行。
- **G5** CLI 适配器按 `ctx.kind` 为 report/review 阶段写报告文件，同一适配器可兼任 coding 与 review。

第四轮复验（G1/G2/G3/G5 全部确认真生效，零跳过）后修复：
- **N1** `rerun-from` 现在会清未决审批 **并递增失效计数**——只清 pending 不够，否则 `adopt_successful` 会重新采纳旧 success.json，把"没结论的审查"静默当通过。修复后 awaiting 的审查可经 `rerun-from` 真正再审一遍。
- git 兜底改读 **Windows 注册表 InstallPath**（机器无关），去掉硬编码盘符。

第五轮（DeepSeek 真实模型全链实测）修复：
- **API 适配器 review 类型**：此前只判 `kind=="report"`，`review` 掉进代码分支被误当代码提取 → 判空跑。已改为 `kind in ("report","review")`。
- **M0 审查上下文**：审查阶段此前只拿 `commit~1..commit` 末次提交 diff，返工轮里看不到完整实现，误判"实现代码缺失"。改为**相对项目基线的累积全量 diff + 变更文件清单**（`cumulative_diff`）；其他阶段仍用末次摘要。
- **第九轮补充**：累积 diff 超过 600 行时按 commit 注入变更文件全文（有单文件/总量/文件数上限）；仍无法完整注入时，pass 结论不自动采纳——人工模式转待审批，无人值守判失败。审查现有代码库模式把工作区内容哈希纳入 inputs_hash，源码变化后旧 verdict 不再被采纳。

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
- 审查输入指纹包含工作区内容哈希（含 autonomy 逻辑哈希）：源码变化后 `resume` 会重新审查，不再沿用旧 verdict。注意：单文件 12KB、总量 120KB 的注入上限意味着超大代码库的人工"审查全部源码"仍只是部分覆盖；工作区哈希只保证变化会触发重审，不改变该覆盖上限。
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
- **目录快照降级为告警**：默认 `watch_enforce = false`，根外变更只记 `watch_warning` 事件，不判阶段失败（父目录一次无关保存不该让阶段挂）。设 `true` 才当失败：根外文件保留原位置，只在 `<run>/violations/<stage>/` 保存最多 100 条变化的路径、前后大小及时间戳，不读取或复制文件正文；新增/改写/删除记录按 inputs_hash 持久化，续跑保持失败且不再调用模型，需 `rerun-from` 显式重试。证据写入失败也保持阶段失败并记录错误；`watch_root` 在可写根内部会告警并禁用。
- CLI 适配器本身能跑命令，目录快照挡不住，**不假装挡住**。需要硬隔离请用容器或系统级作业对象。

### 既有工作区与配置严格性

- `task_workdir` 已有源码但没有 git 仓库时，首次初始化会把非隐藏/非缓存文件按清单提交为基线（不是空基线），coding worktree 合并不会再因 untracked 文件永久失败；纯 review 流程仍不建仓库。
- 配置顶层、`[[stages]]`、`[run_budget]` 出现未知字段会直接报错（防止 `approval`/`watch_enforce`/`max_tokens` 拼写错误静默失效）；`config_keys` 值必须可 JSON 序列化。
- code 阶段成功后若相对基线没有任何实际内容变化（净零变更），判为本轮失败而不是空跑成功；无人值守与人工模式一致。
- 过期锁回收使用 `.reclaim` sidecar 原子接管；损坏锁 5 秒宽限期后回收；活进程锁仍阻塞。
- 审查返工产生的反馈在 review 通过后保留用于指纹稳定（`control.consumed_feedback`），`rerun-from` 会清除并恢复普通提示词。

### 4. 反馈环收窄
- 返工轮的编码提示词**只含问题条目 + 被点名文件的内容**，不再重贴需求全文和累积 diff。
- 审查**没有文件级问题**时停下等人处理，不自动「全面自查」再烧一轮。
- `max_rounds` 可以保持很小。

### 5. 默认通道
默认示例以 `openai_compat` 为通道；Codex/反重力的显式混合配置已完成本轮真机验收。其余 CLI 适配器仍需真实版本和输出样本，默认不参与流水线。

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
