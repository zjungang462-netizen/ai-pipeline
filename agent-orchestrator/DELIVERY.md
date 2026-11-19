# 干净目录运行与验收

## 当前版本：0.5.4（主线验收与工作台守护）

本版新增 GitHub 离线检查与手动双重完整验收发布、工作台网页进程守护及固定修复场景。操作与恢复边界见 OPERATIONS.md。当前主线全量收集 632 项，fast/quick 129 项、slow 503 项；收集数量不等于验收通过，最终结果以本版发布证据为准。

截至 2026-11-19、提交 `9410862`，当前开发基线在 Windows 11、Python 3.11.9、Git 2.45.1.windows.1、Node.js v24.15.0 上完成离线验收：quick **129 项通过**、用时 **21.593 秒**；full **632 项通过**、零失败/错误/跳过、用时 **333.063 秒**，低于 480 秒观察目标。该轮修复覆盖 CLI 启动失败管道回收、stdin 未完整写入、遥测收尾异常和无换行最终 token 预算复核；未调用真实模型。正式发布仍需在干净检出中执行 `scripts/release.py` 的源码与解压包双重验收。

工作台守护不自动恢复模型任务，活动任务/指令/会诊阻止自动启动，健康检查失败不强杀存活进程。本版保留 0.5.3 的模型与工具功能，当前本机可用性须重新核查，不能沿用历史成功样本推断登录和可执行文件仍有效。

2026-11-18 在加入 Qoder 版本门禁前，本地复验使用 Python 3.11.9：配置检查 16 项、8 份示例离线冒烟通过，quick 127 项用时 25.875 秒，全量 620 项用时 357.812 秒，均零失败/错误/跳过；五个离线固定基准场景的 `expectation_met` 均为 true。这些离线检查未调用模型。此前 full 耗时曾为 1,098.531 秒和 1,751.359 秒，背景负载未隔离，不能据这些样本推断代码性能回归；后续需在相同受控环境重复测量。正式 ZIP 以包内源码验收证据与包旁解压复验结果为准。

2026-11-18 的五项离线固定场景均符合预期（含预期失败的轮次耗尽），不能计入模型完成率。新增一条真实小样本设计通过、编码因本机 Claude 程序 Windows 不兼容失败，已报告 909 tokens 且一次调用计量未知，原记录保留。随后完成本机工具修复与盘点：Claude Code 全局包修复并固定在已验证协议版本 2.1.325；Qwen Code 0.26.0 已安装，匹配现有离线真实样本版本，但本轮未做登录和真实调用；Codex CLI 0.169.1 的 `~/.codex/config.toml` 使用 `custom` provider，路由至 `https://code.shoestravel.xin`、`wire_api=responses`、`requires_openai_auth=false`，代理 token 已配置；`codex login status` 的“未登录”是 OpenAI OAuth 状态，不代表代理凭据不存在。dsh 0.2.2-rc.3 匹配已保存的真实协议样本，本轮只重新确认版本；agy 1.5.10 的版本命令可用，但当前适配器仅有 1.5.9 真实样本，1.5.10 按未知协议拒绝；doubao 未安装，也没有独立 CLI 的验证样本。`doctor` 是静态诊断，不代表版本、登录或协议探测通过。

随后执行一次隔离目录、只读、限时的 Codex 代理探针：适配器记录到一个 completed response 和 19,640 个 CLI 报告 tokens，但因 `budget_limit` 拒绝，未得到可用回复。该探针直接调用适配器，绕过 Engine 的预调用预算门禁；因此不能作为预算闸门验证。代理真实账单金额未知，未重复调用。真实样本不能视为完整成功或通用成功率。

Qoder CLI 1.1.89 在 Windows 全局 Node 24 上曾出现 `UV_HANDLE_CLOSING`，根因未确认。仓库外的 Node 22.23.5 启动入口已通过版本和帮助检查，并用现有登录态完成一次隔离的 Engine 报告任务：协议 1.5.0、首轮完成、返回预期文本、无工具调用、任务工作目录保持为空；模型阶段 55.781 秒，报告 0.301019 credits，token 与美元账单均未知。测试未设本地 token 上限，仍保留单次 90 秒超时和一次尝试。不据此推断启动稳定性、文件工具能力或余额耗尽后的行为。版本探测现在必须以退出码 0 且标准输出匹配 1.1.89 通过；超时、未知版本或失败不发起模型请求，也不回退为旧调用参数。

Grok Build CLI 1.0.88 已安装，尚未注册 `cli_grok`。当前 CLI 报未认证；普通登录和设备登录均因认证端点连接超时未完成，2026-11-18 复查 OIDC 发现端点仍超时。旧版成功样本不作为该版本的协议证据。恢复认证后，需先验证隔离用户及项目配置（尤其是 Claude 兼容导入），再用当前账户可用的模型取得脱敏的真实协议样本。

## 0.5.3 功能基线（历史）

0.5.3 源码全量收集 **607 项**，fast/quick **127 项**、slow **480 项**；最终以同包 `delivery-metadata/verification.json` 与包旁 `verification-package/verification.json` 的实际结果为准。下方阶段二至四的旧数字和标签为历史冻结版本，不表示本次新增功能的验证。用 `python -m orchestrator.cli --version` 查看版本，包的实际提交与原始字节清单见 `SNAPSHOT.json`。

新建任务默认选择“不限制 token”，并可按已登记适配器指定账户可用模型和受支持的思考选项。模型名由账户/端点决定，系统不虚构“最佳”型号；工具或 API 拒绝参数会保留失败，不自动回退到低配模型。

### 模型与性能设置

工作台创建对话框显示每个适配器的当前模型：API/Codex/Qwen/反重力/Qoder/Claude Code支持本次模型覆盖；Codex 可设 `reasoning_effort=xhigh` 等强度；兼容 API 可设 `reasoning_effort` 与 `thinking=enabled/disabled`，取决于端点支持。启用 thinking 时不同时发送 temperature。Qoder支持模型所提供的low/medium/high/xhigh/max强度，思考模式限auto/adaptive/disabled；Claude Code可设low/medium/high/xhigh/max，不展示API专属thinking/JSON选项。实际能力由工具、账户与型号决定，不自动降级。

### Claude Code工具与DeepSeek模型分离

`cli_claude`精确绑定已验证的Claude Code **2.1.325**。本机连接为`https://api.deepseek.com/anthropic`，模型选择`deepseek-v4-pro[1M]`、思考选项`max`；不是Anthropic原生Claude模型。当前用户配置的Opus别名映射为DeepSeek Pro，默认优先读取该高能力映射，也可显式指定模型/强度，支持`[1M]`上下文后缀。

`inherit_connection=true`仅从`~/.claude/settings.json`读取ANTHROPIC端点、凭据和模型映射及思考设置；不会载入用户hooks/MCP/plugins、apiKeyHelper或改写源配置。凭据只在子进程内存环境，不能写入argv、任务TOML或输入指纹；显式`extra_env`声明的连接变量可以覆盖。`inherit_connection=false`不读取该文件，可配合明确的环境变量名使用。端点、模型/别名映射和effort参与指纹；连接变化不会沿用旧输入哈希，凭据值不入指纹。

采用原生`claude.exe`更适合Windows，示例`claude-deepseek.example.toml`可填写真实路径，npm `claude.cmd`也可配置。使用bare/safe-mode/restricted与strict-mcp-config，显式禁用自定义slash命令、Chrome、会话持久化；只继承连接字段，不调用订阅登录。编码只开放Read/Glob/Grep/Edit/Write、acceptEdits，报告/审查只有读工具且dontAsk；不开放shell/Web/Agent、额外目录或绕过全部权限。CLI内置组件可能仍在init中列出，不能将“关闭自定义组件”解释为所有内置组件从协议消失。

严格要求init版本、唯一末尾result、success/is_error=false/end_turn/completed、无权限拒绝或安全停止、非零退出失败。Claude常把同一响应拆成thinking和tool_use/text多个消息，以message.id取计量最大值去重，最终usage是累计总量，不和分步再次相加。Anthropic兼容计量中input、output、cache_read、cache_creation相加；thinking_tokens是output子集，不重复加。缺少计量时保留unknown，超时仍保存已观察下界。CLI的美元估值未按DeepSeek账单验证，不进入实际费用列。

实际无工具调用与Read/Edit单文件探针均成功，用户设置文件内容哈希不变。随后真实维护任务完成API设计→Claude Code/DeepSeek编码→四项保护测试→独立API审查，四阶段各attempt1、只改一个允许文件，保护测试哈希不变、主代理未补写实现；墙钟178.625秒，总已报告67948tokens，其中Claude工具阶段60296（含33152缓存读取），API设计/审查7652。该样本不代表所有业务任务或后端参数效果；工具接受max且后端有thinking计数，不等于独立证明最大推理深度带来质量提升。

CLI可能输出unrecognized_model警告，因为DeepSeek名称不在其内置Claude型号表，但本次真实响应的model与usage仍显示DeepSeek；不把警告当成功，也不忽略失败退出码。execution记录保存CLI身份与实报模型，stage_usage/metrics/CSV新增backend_model/backend_endpoint，工作台区分配置和实报值，历史记录无字段不根据当前连接猜补。

真实工具样本/provenance已脱敏会话与消息ID、路径，并省略思考正文；离线回归不调用模型。修复任务产出的畸形stages/stage_usage摘要边界已整合并新增回归，保持终态metrics优先、未知字段不填零。

### Qoder 1.1.89 真实文件工具链路

先完成官方 Qoder 登录，配置 `exe` 为实际 `qoder` 可执行入口（Windows npm可用 `qodercli.cmd` 绝对路径），不要填桌面启动器。`qoder.example.toml` 提供 API设计/审查、Qoder编码、独立命令测试的配置；会实际修改示例任务目录，运行前核对范围。离线验收只加载配置，不调用模型。

已抓取裸版本 `1.1.89`、JSON成功终态及协议 `1.5.0` 的stream-json工具事件，解析器要求唯一末尾result、success/is_error=false/end_turn、非空结果及无permission_denials，非零退出仍失败。未知版本、缺失/重复/错误终态不会判通过。

真实账户目录包含Ultimate及具体型号；本次实测Ultimate/high/adaptive成功。`thinking=enabled`在该版本明确要求额外thinking-budget；为保持不限制本地token的语义，本接入使用adaptive，不猜预算。控制台Qoder不展示API专属JSON审查选项。

编码只开放Read/Glob/Grep/Edit/Write，使用accept_edits；报告/审查只有Read/Glob/Grep且dont_ask。禁MCP并不加载项目/用户settings，关闭会话持久化和内部请求重试；不开放shell、子代理、网络工具或跳过全部权限。测试由编排器独立执行。CLI仍不是操作系统沙箱；文件白名单及归档证据继续校验。Qoder Read/Edit及工具结果已映射既有重复读取/工具错误停止护栏。

一条真实维护任务（摘要函数处理畸形元数据）完成 API设计→Qoder编码→四项保护测试→独立API审查，四阶段各attempt1，只改一个允许文件，保护测试哈希未变、主代理未补写实现；墙钟104.172秒，API已报告6362tokens，Qoder12.620490993credits。它证明该维护样本的闭环，不代表任意业务成功率。

Qoder真机有实际输出/credits但token计数全0，故token永远按未完整验证处理、unknown_calls累计、保留有限预算预扣；无限token模式仍标incomplete，不能视为免费。`qoder_credits`单列写入state、阶段计量、metrics及CSV/工作台，不和美元或tokens相加。旧metrics缺字段不推测。终态失败也保留可解析的credits，超时尚无最终credits时不猜测金额。

此次另对本机官方Grok Build CLI 1.0.77完成订阅账号登录与无API-Key的`grok-4.7/high`无工具调用；实际模型回显grok-4.7-build，有usage。该历史样本仅为外部CLI试用，未注册Grok流水线适配器或声称真实编码验证。当前本机`grok --version`为1.0.88；本轮只查看版本与帮助，没有模型调用，不能把1.0.77的旧样本当作1.0.88协议验收。CLI报告的美元数不是已核对的订阅账单。

0.5.1 增加兼容 API 的可选 `review_json_mode=true`（控制台“结构化审查 JSON”），只对 review 请求发送 `response_format={"type":"json_object"}`。高思考重放暴露了模型在合法 JSON 后附加说明而无法被严格协议接收的问题；此选项让支持的端点约束输出，解析器仍拒绝 JSON 后额外正文。端点不支持时明确失败，不隐式降级。DeepSeek v4-pro/high/thinking 的真实结构化探针已验证可生成独立 verdict 文件；这不是全面任务成功率保证。

本次选项只接受模型、受支持思考参数、结构化审查开关和不限 token 开关，不接受浏览器提供的路径、命令、端点或凭据。来源配置保持原样，独立副本固化工作目录、提示词引用、配置哈希与本次模型选择。重试同一创建标识不能替换选项；派生任务仍校验父运行绑定与来源配置。

CLI 配置示意：

```toml
[run_budget]
unlimited_tokens = true
max_duration_s = 3600
# 此模式请删除运行、阶段和适配器中的 max_tokens。
```

不限 token 时不发送编排器的输出 token 上限，也不按本地估算额度拦截；服务商仍可能有默认上限、上下文窗口及费率。墙钟、阶段超时、重试/反馈轮次、审批和文件白名单保持。用量继续记录，缺失计量保留 unknown，不能把无限模式中的未知调用当免费。未勾选时沿用原配置限额；独立会诊仍使用其明确的有限预算。

### 用量导出与工作台

```powershell
python -m orchestrator.cli export-usage --state-dir <状态目录> --output <仓库外文件.csv>
```

可加 `--run-id`。CSV 分 `run_total` 和 `stage` 行，不能将两种行再次相加；tokens 是预算计量（已含预留），已报告/预留单列。终态优先读取不可变 metrics，新版 metrics 冻结阶段 usage、工具、配置模型与思考强度；旧记录缺字段时留空/unknown，不依据当前配置补推。`billing_cost` 始终留空并标 `not_reported`，没有服务商账单数据时不拿 token 或公开价格冒充实际费用。输出独占创建，不覆盖现有文件或状态根；不读取模型正文。

预算使用达到 80% 给只读提示，不重复加上预留，也不自动放宽。不限 token 模式隐藏 token 进度条，继续显示实际用量和时长提示；未记录的期限不伪装成已过期。

模板按项目/名称保存任务文字与配置哈希，最多 100 项；使用只填充表单，不启动执行，配置变化时禁用。两运行对比为用量/状态摘要，不做质量结论；配置不同或未知会显著标注。失败看板按主键逐页扫描（最多100/页），不受首页历史上限影响、不加载产物正文，展示本页/累计范围及元数据错误；一个运行可以有多个明确类别。SSE 自动重连保持原游标回放，旧房间的回调不能覆盖当前房间，坏事件忽略。

### 一键本地发布

```powershell
python scripts/release.py --output D:/work/<全新发布目录>
```

要求干净的已提交 HEAD、仓库外全新目录。步骤：禁用换行转换导出 Git 对象并逐文件比对 → 新源码目录完整离线验收 → 生成 ZIP/SNAPSHOT、结构化证据与最近最多30个提交的自动 CHANGELOG → 独立解压该 ZIP 再做完整验收 → 复核 HEAD/工作树及全部源码字节 → 发布正式 ZIP、`.sha256` 与 `release-result.json`。两份验证都必须 full、非零测试数、零失败/错误/跳过、指纹不变且数量一致；不提供跳过全量的 CLI 选项。第二次验收证据在包旁保存，避免修改已经复验的 ZIP。任一步失败保留候选与证据，不生成正式包；不自动推送远端、修改旧标签或覆盖旧包。

发布脚本不收集工作树、凭据、状态、数据库、原始模型/回归日志；只包含该提交下跟踪源码与显式结构化发布证据。失败清理/平台边界沿用下文；此前有限平台矩阵不能当作本版所有组合全量验证。豆包CLI仍未核实，跨业务项目基线仍需补齐。

## 以下为先前阶段记录

阶段四冻结版包含测试输入完整性、失败 attempt 计量、可配置审查覆盖、真实协议与维护缺口修复；该版本全量 **555 项通过**。下述阶段三数字同为已完成的历史冻结基线。

2026-11-17 阶段三开发源码的全量离线验收为 **519 项通过，零失败/错误/跳过**；分层为 fast **95** 项、slow **424** 项，互斥并完整覆盖 full。阶段二的 514 项为历史基线。下文 `delivery-2026-11-16-round9` 的 479 项与 SHA 清单描述上次冻结发布包；新命令需使用最新源码，未改写旧包或标签。

## 环境

- Python 3.11 或更高版本；运行代码使用标准库，无需 pip 安装。
- Git：代码阶段的 worktree/提交与完整 Git 测试需要。
- Node.js：完整测试中的前端异步契约需要，无需 npm 安装。仅启动工作台不需要 Node.js。
- 真正调用模型时另装相应 CLI 或在环境变量中配置 API Key；离线验收不调用模型或网络服务。

本次标签为 `delivery-2026-11-16-round9`，完整离线验收 **479 项通过，0 失败、0 错误、0 跳过**，134 个验收范围文件前后指纹一致。实际提交、全部交付源文件 SHA-256 和本次验收摘要记录在包内 `SNAPSHOT.json`；原始结构化结果见 `delivery-metadata/verification.json`。包旁的 `<包名>.sha256` 校验压缩包整体字节。包自身的哈希放在包外，避免自引用。

本次验证环境为 Windows、Python 3.11.9、Git 2.45.1.windows.1、Node.js v24.15.0。Python 3.12/3.13 与 Linux/POSIX 尚未完成交付矩阵验收；Python 3.11+ 是最低版本要求，不代表这些组合均已验证。

解压源码包或检出交付分支后，进入 `agent-orchestrator`。命令中的 `python`、`git`、`node` 应能在当前 PATH 解析；命令行包含中文时建议使用 UTF-8 终端。

## 离线启动

```powershell
python -m orchestrator.cli --help
python -m orchestrator.cli dashboard --config workbench.example.toml --port 8766
```

在浏览器打开终端打印的本次会话链接。端口被占用时改用其他空闲端口。工作台示例只运行真实 Python 断言，最后等待人工审批，不需要模型凭据。

同一个示例可直接用 CLI 执行：

```powershell
python -m orchestrator.cli run workbench.example.toml --task "离线交付验收" --run-id delivery-demo
python -m orchestrator.cli status workbench.example.toml --run-id delivery-demo
python -m orchestrator.cli approve workbench.example.toml --run-id delivery-demo --stage acceptance
python -m orchestrator.cli resume workbench.example.toml --run-id delivery-demo
```

第一条 run 的预期退出码是 **11**，表示等待审批；approve 后及完成任务 resume 的退出码为 0，三个阶段各一个 attempt。选择新的 run-id 可开始另一条独立验收。示例状态与数据库存放于 `.workbench/`。

## 完整离线验证

```powershell
python scripts/verify_delivery.py --output ../delivery-verification
```

验证脚本检查运行环境、六份示例的加载和提示词引用、脱敏样本指纹、CLI 帮助、离线示例 run/status/approve/resume，以及本机临时 HTTP 端口上的登录和项目列表。随后执行原有全部 unittest（包括 Node.js 前端契约）。失败、错误或跳过均使验收返回非零。源码前后指纹覆盖 `orchestrator/`、`tests/`、`prompts/`、`scripts/`、`demo/`、示例配置与交付说明；验收期间改动其中任一文件都会使 `source_unchanged` 为假。

运行结束在指定输出目录保存 `verification.json`、`full-regression.log`、`smoke.json` 和源码前后指纹。输出目录应在项目目录之外。脚本不下载依赖，不读取登录凭据，不运行 API/CLI 模型示例。`verification.json` 中的失败/错误/跳过正文按已知环境凭据与可识别令牌/URL 规则脱敏；`full-regression.log` 由 unittest 流直接写入，**保持原始内容、不做脱敏**，只应在受控本机环境保存。

失败时，验证入口会在临时目录清理前把有界诊断包写入输出目录的 `failure-evidence/`，并在最终 JSON 中登记路径或采集错误。包包含状态/事件摘要、执行退出码、已知用量和日志尾部；不复制配置、提示词、源码 worktree、Git 或恢复候选。已知环境凭据名（含 token/passphrase/`_key`/access key 等常见命名，值长度下限 4 字节）、测试显式注册的凭据、可识别令牌前缀（`sk-`/`ghp_`/`AKIA`/`npm_`/`hf_`/`glpat-` 等）及带凭据 URL 会被替换，源根绝对路径也会在尾部读取前注册替换；单个目录不可读或深层 JSON 只登记 issue，不中止整包。不保证识别任意未知秘密。诊断包仅用于定位，不可用作完成、审批或恢复证据。

直接用 unittest 运行 CLI 可靠性、登录早停及遥测边界测试，也会通过 `EvidenceTestCase` 在失败后自动留存，默认目录为 `.test-failures/`（已忽略）。可用环境变量 `ORCHESTRATOR_TEST_EVIDENCE` 指定持久输出目录，必须位于待清理测试根目录之外。成功和预期失败不会生成包；采集失败不掩盖原测试失败，仍继续原清理。无法从测试对象发现临时根目录、源文件已清理或不可读时，包会报告缺失/unknown，不猜测原因；解释器被强制终止前尚未触发回调的失败、以及类级 `setUpClass` 失败无法保证采集。

若只需执行现有测试：

```powershell
python -m unittest discover -s tests -v
```

Windows 上测试会真实创建临时文件、Git 仓库和进程树，耗时受负载影响。不要把运行中的测试进程无输出视为已通过，也不要用单项复验替代失败的完整批次。

## 模型示例

`config.example.toml` / `pipeline.example.toml` 使用环境变量 `DEEPSEEK_API_KEY`；它们包含设计审批。`unattended.example.toml` 显式定义预授权文件和预算。`cli-mixed.example.toml` 需要已验证版本的 Codex 与反重力及本机登录；`dsh-test.example.toml` 需要 dsh 及显式允许的 `DEEPSEEK_API_KEY`。先复制模板、配置工作目录和工具路径，再按 README 启动；不要把真实凭据写入 TOML 或提交仓库。`demo/run_api_demo.py` 退出码与 CLI 对齐：全部阶段完成 0、等待审批 11、失败/超时/中断等未完成 1。

## 配置诊断与状态维护（阶段二）

```powershell
python -m orchestrator.cli doctor workbench.example.toml --json
python -m orchestrator.cli clean --state-dir .workbench/demo-state --dry-run --older-than 30
```

`doctor` 退出 0 表示无错误，1 表示诊断有错误；目录尚不存在但可创建等情况只告警。它复用配置加载器的全部字段与约束，集中列出未知字段和拼写建议，并检查本机路径、提示词文件、命令入口、适配器构造与凭据环境变量、预算/autonomy 关系和 watch 设置。不创建工作区、不执行测试/模型/登录命令、不联网；权限检查是静态访问检查，不能代替运行时 ACL 或工具连通性验证。配置存在结构错误时先修复校验错误再重跑，以继续后续路径检查。

`clean` 默认只输出 JSON 预览，包括候选路径、字节数、保留理由和错误；`--apply` 才实际删除。`--older-than` 单位为天，必须为正数。保留当前/最新 attempt、成功/decision/审批证据、恢复候选、待审批或活动运行、含链接/junction 的目录及无法可靠解析的运行状态；不删除 `state.json`、`metrics.json` 或有效 worktree。只有整棵目录都已过期才清理失败 attempt 或隔离目录，删除前获取运行所有权锁并重新检查状态、目录指纹及绝对路径边界。目录变化或锁繁忙时保留并报错；可重新预览后处理。

可附加 `--repo <任务仓库>` 检查失效 worktree 登记；只有 Git 将淘汰的全部登记都指向该状态根内且目录已消失时，才允许 `git worktree prune`。外部登记、仍存在的目录或不可识别的 Git 输出会拒绝自动处理。`--test-failures <诊断目录>` 可一并预览/清理诊断包，`--max-evidence-mib 100` 指定占用上限（至少 2 MiB）。实际历史目录先 dry-run 核对，不要把共享目录当作专属状态根。

## 快速与提交前门禁（阶段二）

```powershell
python scripts/verify_delivery.py --quick --redact-logs --output ../quick-verification
python scripts/precommit.py --output ../precommit-verification
```

`--quick` 保留完整示例/CLI/HTTP 冒烟，执行固定核心集合，覆盖配置、引擎状态、预算、适配器、工作台执行范围及新增运维门禁；目标总耗时不超过 120 秒，超时预算、失败、错误或跳过均返回非零。`verification.json` 写入 `mode=quick` 和预算结果，不能替代 `mode=full` 的完整验收；quick 的日志名为 `quick-regression.log`，完整验收仍为 `full-regression.log`。

`precommit.py` 先对暂存区与工作树做 `git diff --check`，验证受 Git 管理/未忽略的流水线 TOML，再扫描新增或改动文本中的常见令牌、含凭据 URL、私钥头和直接写入的凭据。暂存区与工作树分别读取，防止已暂存凭据被工作树的干净版本掩盖。只输出位置和类别，不回显凭据；大于 2 MiB 的文本拒绝跳过扫描，二进制不扫描。受控夹具应使用明确的运行时构造，不提交可被误用的真实凭据；扫描不是任意秘密的识别保证。静态检查通过后调用同一个官方 `--quick --redact-logs` 入口；`--checks-only` 仅用于静态诊断，不代表完整提交前门禁通过。脚本不自动安装 Git hook。

## 配置与测试维护（阶段三）

配置 dataclass 的字段元数据统一声明默认值、数值上下限与说明；严格校验、doctor 字段集合、README 配置表和六份示例注释复用这些声明。生成示例注释不改变 TOML 解析值；生成文件的漂移会使提交前检查失败。历史过程与旧测试数字已移至 HISTORY。

```powershell
python scripts/generate_config_docs.py --check
python scripts/verify_delivery.py --suite fast --redact-logs --output ../fast-verification
python scripts/verify_delivery.py --suite slow --redact-logs --output ../slow-verification
python scripts/verify_delivery.py --suite full --redact-logs --output ../full-verification
```

`--quick` 与 `--suite fast` 使用同一组测试；slow 为完整发现集合减去 fast，因此新增测试不会因未分类而漏出 full。`--quick` 与 `--suite` 互斥。每次仍执行原有冒烟，并额外保存 `test-ids.json`、`test-durations.json`、`slow-tests.json`；默认将至少 2 秒的用例列入慢表，可用正数 `--slow-threshold` 调整。

fast 的 120 秒门限仍影响退出码；full 的 480 秒是性能观察目标，以 `full_within_target` 记录，不把机器负载导致的变慢冒充功能失败。流式日志夹具改用观察握手结束，保留原有执行中读取、凭据替换和部分用量断言；不再为该观察测试固定等到超时。

后台同步的大批量测试数据在准备阶段复用连接并使用 NORMAL 同步级别，准备结束会验证新连接恢复原默认值；测试观察、控制与投影仍执行产品默认事务。现有测试断言保持不变，避免为每个夹具记录反复开关 WAL 和同步准备数据。

## 状态计量与审查覆盖（阶段四）

attempt 目录分配后立即写失败占位记录；后续成功、恢复或人工批准更新同一编号，失败/超时/崩溃不再漏计。调用前预算拒绝尚未分配 attempt，不算一次执行。metrics 的 `retries` 按每阶段实际编号数减一求和，包含反馈轮次中的额外尝试；新增 `attempts_total`、`failed_attempts` 与口径说明。已有终态 metrics 保持不可变，不改写历史账本。

`[review_coverage]` 默认 diff 600 行、变更文件 24,000 字节/总量 180,000 字节/60 文件；现有代码库默认 12,000/120,000 字节。字段与安全上限见生成配置表。`incomplete=manual` 在人工模式待审批、无人值守失败；`fail` 直接失败；`allow` 是显式接受未完整审查风险，doctor 会警告，证据仍标记不完整。策略变化参与输入指纹，旧 pass 不沿用。已有代码库超限/二进制/不可读源码也纳入缺失清单。

每次审查 attempt 保存 `review-coverage.json`，success/decision 带覆盖摘要与未覆盖文件；覆盖指已注入提示词的文件。API 仅能依据这些内容审查，CLI 虽可能额外读取文件，本记录不认证其工具访问是否完整。审查未覆盖列表也随无人值守提示词缓存恢复，避免重启后丢失限制。

Qwen Code 0.26.0 已从官方 npm 固定版本安装到独立试验目录，真实 stdin 调用返回 JSON 事件数组和 7,282 tokens。版本、成功终态、用量与 provenance 已纳入离线回归；实际后端为现有 DeepSeek 兼容 API，不证明官方 Qwen 模型、工具执行或其它 0.26.x 版本。旧合成样本只保留为旧协议回归。

阶段四当时Qoder 1.1.89只完成未登录失败；该失败样本继续保留。当前0.5.2已新增登录后的真实成功/文件工具样本与维护闭环，范围见上文。豆包CLI仍未核实，桌面应用不能替代CLI，不捏造成功样本。

真实仓库维护试点在独立项目中执行设计→编码→外部测试→独立审查，固定 DeepSeek API 后端、每任务 100,000 tokens/360 秒、零自动重启。三个既有缺口均在修改前复现，受保护测试哈希未变，首轮完整成功 **1/3**；另外两项外部测试虽通过，仍分别因补丁协议与审查失败记录为未完成。已报告 tokens 合计 62,665，未知调用为 0；API 未提供真实账单费用，费用留空。首轮没有人工补写实现。后续主代理审查并整合修复不改变首轮结果。这是三个本仓库维护任务样本，尚不是跨业务项目成功率基线。

整合的维护修复包括：doctor 识别内联文字末尾的 Markdown 文件名；提交前扫描支持带 BOM 的 UTF-16 脚本并拒绝损坏编码；诊断包保存安全的保留容量摘要，只留数字及有界原因代码，避免异常中的绝对路径泄漏。

## 平台验证范围（阶段四）

| 平台/解释器 | 已验证范围 | 未覆盖 |
|---|---|---|
| Windows / Python 3.11.9 | 官方 full 全量、只读审查、真实测试/续跑、工作台、CLI 成功/超时 | 其它负载/工具版本的保证 |
| Windows / Python 3.12.10 | 官方嵌入式解释器，目标范围探针：只读审查、真实测试/续跑、工作台、CLI 受管进程及入口 | 该解释器的全量离线回归 |
| Windows / Python 3.13.17 | 同上 | 该解释器的全量离线回归 |
| Linux / Python 3.13.18 | 只读源码挂载、禁外网的容器探针；只读审查、真实测试/续跑、HTTP、POSIX 受管 CLI 成功/超时 | 完整套件、真实编码工具、Windows Job/沙箱特性 |

额外解释器仅放在独立验证目录，未修改系统 Python/PATH。Linux 镜像以记录的摘要固定来源；容器只读根文件系统，临时目录可写，源码不变。Windows 为优先验证环境，不能据有限探针宣称所有 POSIX 路径已覆盖。

## 日志脱敏与诊断保留（阶段二）

完整验收也支持 `--redact-logs`，默认关闭。开启后对回归日志流和 CLI 冒烟日志替换已知环境凭据、可识别令牌及含凭据 URL；跨 write/flush 边界保留未完成片段，超过 64 Ki 字符的单行整体省略，防止无限行缓存。结构化 `verification.json` 无论开关都保持原有脱敏规则。脱敏不改变测试状态或退出码，也不保证识别任意未知秘密。

失败诊断包默认保留 30 天、目录总量目标 100 MiB，每次采集完成后在目录锁下淘汰过期/最旧包，保留本次新包。只管理含 `evidence.json` 的诊断目录，手动文件、链接和带受保护标记的目录不自动删除；它们可能导致无法达到总量目标，此时在预览中报告。保留规则也适用于 `ORCHESTRATOR_TEST_EVIDENCE` 指定目录和验收的 `failure-evidence/`。主回归日志不在自动淘汰范围内：本机保留 30 天，归档结构化验收 JSON 与源码/包指纹后再按实际需要人工删除原日志。正式交付的成功与审批证据不受诊断保留策略影响。

## 升级与回滚注意事项

- **严格配置校验**：未知顶层、阶段和 `run_budget` 字段现在直接报错。升级前对照同包示例修正拼写和字段位置，不能依赖旧版静默忽略未知键。
- **根外写入证据**：当前开发版的 `watch_enforce=true` 保留根外文件原位，只在 `<run>/violations/<stage>/` 保存有界变化元数据，不读取、复制或移动原文件正文；快照无法可靠确认写入者。违规仍按输入指纹持久判失败，修正问题后用 `rerun-from` 显式重试。`watch_root` 位于可写根内时禁用该监视域并记录 `watch_disabled`。旧冻结包的隔离搬移行为只作为历史记录。
- **审查覆盖不完整**：diff 超过 600 行时尝试按 commit 注入变更文件全文；仍无法覆盖时，`pass` 在人工模式下转待审批，无人值守模式下判失败。人工确认前应核查未覆盖的变更。
- **审批校验变化**：code/test 阶段校验申请时冻结的输入指纹、attempt 归档及 evidence；report/review 阶段仍拒绝输入变化后的审批。等待审批退出码为 11，不能作为任务成功处理。
- **状态兼容与备份**：当前保存的 `state.json` 使用 `schema_version=2`，包含顶层 `test_artifacts`、`test_artifact_hashes` 以及 `control.consumed_feedback`、`control.write_violations`。缺失字段按空值读取；没有提供向旧版本回滚的迁移工具。升级前及回滚前先停止写入该状态目录的 worker/supervisor/工作台，备份整个 `state_dir`（含 attempts、审批与隔离证据）；回滚时使用与旧代码配套的备份，不让旧代码覆盖新版状态。不要只修改 `schema_version` 或删字段来冒充迁移。
- **测试输入与缓存**：输出按内容哈希绑定，输出改变或丢失会重新执行测试；只有路径、没有哈希的旧记录不再自动受信。测试期间既有输入被改写或删除会拒收，保留真实命令退出码和输入变化原因；既有生成文件若没有可信输出绑定，也按输入保守处理。
- **既有工作区与脚本**：非 Git 工作区首次 coding 会提交已有文件作为本地基线；零净变更 code 阶段现在判失败。`demo/run_api_demo.py` 的未完成退出码为 11 或 1，调用方应检查退出码。

## 包内容与独立核验

源码包只包含标签提交下 `agent-orchestrator/` 的跟踪文件，另附生成的 `SNAPSHOT.json` 与 `delivery-metadata/`。源文件直接取自 Git 对象，不经过检出换行转换；`SNAPSHOT.json` 的 `files` 以仓库相对路径记录源文件 SHA-256，`metadata_files` 记录附带验收证据的 SHA-256。这两个清单不包含清单自身；完整 ZIP 的字节由包外 `.sha256` 校验。

`delivery-metadata/` 包含本次 `verification.json`、`smoke.json`、`source-before.json`、`source-after.json` 和 `test-durations.json`；未脱敏的主回归日志仅保留在开发机。`live-validation-*`、`review-*`、本机配置/凭据、运行状态、数据库、缓存、worktree 和 Git 数据不入包。开发机审查与实测目录继续按现有忽略规则保留，不入库；交付的启动与回归不依赖它们。

下载或复制后先用 `Get-FileHash <源码包.zip> -Algorithm SHA256` 对照包旁 `.sha256`。解压到新的仓库外目录后，用 `SNAPSHOT.json` 核对源文件与证据清单，再执行上面的完整离线验证；验收会生成新的运行状态和缓存，不应将它们重新混入发布包。

## 原始验证与历史记录

旧交付基线 `e2a4535` 的 388 项通过以及 `validation/development-baseline.json` 是历史记录，本次发布以包内验收证据为准。README 各轮历史数字与开发机 `review-2026-11-16/修复复验报告.md` 仅用于追溯。原始性能测量、主回归日志和模型执行日志保留在开发机证据目录，不是启动或测试依赖。
