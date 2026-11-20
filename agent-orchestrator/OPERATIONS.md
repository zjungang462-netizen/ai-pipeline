# 主线验收、工作台守护与自动修复基准

## GitHub 检查

`Offline Checks` 对 main 推送和 PR 执行 Windows/Python 3.11、3.13 的配置与提交差异凭据扫描、quick、导航和服务恢复回归，以及无模型的固定场景基准。不会读取或配置模型密钥。证据作为短期 artifact 保存。流水线成功只表示所执行的平台和集合通过。

`Verified Package` 需要在 GitHub Actions 手动触发，执行现有 release.py：冻结干净 HEAD，源码与最终 ZIP 分别完整验收，通过后才生成正式包和校验值。无需 GitHub 写权限，不自动发布 Release 或推送标签。工作流定义提交不等于远端任务已通过，需查看实际 Actions 结果。

## 本地工作台守护

在产品目录执行：

```powershell
python -m orchestrator.workbench_service run --config workbench.example.toml --database ../service-data/workbench.sqlite3 --directory ../service-data/monitor --port 8766
```

守护器前台运行；需后台时使用本机进程管理器或 PowerShell `Start-Process -WindowStyle Hidden`，并将输出重定向到私密本地日志。目录、数据库与端口按实际部署配置，8765 保留给本机代理。它不是 Windows 系统服务，不承诺系统重启或守护器本身被强制结束后自动启动。

守护器持租约锁，子进程退出非零时最多重启 3 次（上限可设 0..5），记录每次 PID、退出码、日志和健康状态。会话只保存在目录内“工作台入口.md”，重启会换会话。`GET /api/health` 返回状态、版本和当前 PID，不返回任务或会话信息，并保留 Host 校验。

启动/重启前只读检查数据库，存在活动 managed 运行、排队命令或会诊则退出为 blocked，须人工核查。守护子进程显式关闭自动任务恢复；新建任务或明确操作仍使用原引擎和审批。直接使用旧 dashboard 命令时保留原恢复语义。

连续健康检查失败会记 unhealthy，保留仍存活的网页进程供排查，不因未知状态启动第二个实例。正常退出不重启。停止时在守护目录创建名为 `stop` 的文件；只终止守护器自己创建的网页子进程，独立任务进程保持。再次启动前确认旧进程退出并移除 stop。超限、blocked、unhealthy 均须查看 service.json 与 events.jsonl，不自动重置预算、重建任务或清除失败结论。

## 固定任务集

```powershell
python scripts/benchmark.py --output ../benchmark-offline-unique
python scripts/benchmark.py --output ../benchmark-live-unique --live-config claude-deepseek.example.toml
```

输出目录必须全新。默认五个场景使用明确标记的脚本适配器，真实执行 Git 提交、独立测试和严格结论机制，覆盖首次通过、测试返工、审查返工、中断续跑、轮次耗尽。最后一项预期失败，`expectation_met` 表示护栏符合预期，不能把五项都称为业务成功。

`--live-config` 显式启用付费模型调用；沿用选定配置的 report/code/review 角色适配器，在新建的小型任务工作区运行，绝不续跑原任务。编码阶段注入 app.py 当前内容，输出协议由引擎统一指定，不额外要求完整文件块与补丁同时出现。独立测试由引擎执行。单样本墙钟预算 240 秒、单阶段/适配器最多 120 秒，保留最多两轮反馈；当前基准显式不限本地 token，不是费用硬上限。API 审查开启 JSON 对象约束，不支持时保留失败，不自动换工具或模型。

results.json 记录模式、各阶段终态、返工事件、attempt 数、耗时、已报告/未知用量、保护测试哈希和人工实现介入数。保留原始状态、测试输出、审查文件和脱敏日志；配置只写环境变量名，禁止写凭据。离线场景与真实样本分开统计，小样本不能推断通用业务成功率，也不把预算预留或 CLI 估值当实际账单。

## 状态备份与恢复

先停止会向该状态根写入的工作台、worker、supervisor 和调优进程，暂停新任务创建，再执行：

```powershell
python -m orchestrator.cli state-check --state-dir D:/data/state
python -m orchestrator.cli state-backup --state-dir D:/data/state --output D:/backups/state-before-upgrade
python -m orchestrator.cli state-restore --backup D:/backups/state-before-upgrade --output D:/data/restored-state
```

备份包含整个状态根的 attempts、审批、metrics、失败及恢复证据，manifest 记录文件集合、空目录、大小和 SHA-256；复制前后复核原目录，变动则不发布备份。已有运行、控制、会诊或调优锁会阻止备份。源目录与备份不能互相包含，输出父目录须存在，既有目标永不覆盖。备份按原始字节保留未知 schema 和损坏状态，兼容检查单独报告失败，不把备份成功当成可续跑。

恢复先校验完整文件集合和哈希，再写入全新目录；不改 schema、用量、期限、审批、失败结论或绝对路径，不自动启动任务。状态中的绝对路径仍指向原位置，新目录用于验证或离线恢复演练。实际原位恢复须先停写并另行保留当前状态，再将已校验副本部署回相同位置；工作区、Git 对象与外部 worktree 必须单独备份，不包含在状态目录恢复承诺中。

旧版 schema 2 的内嵌事件可直接读取。首次保存时迁移到 schema 3：先同步 `events.jsonl` 中的新增事件，再原子替换 `state.json`，以快照中的字节位置为提交边界。恢复与工作台只读取已提交的前缀，下一次追加会截去未提交尾部；已提交前缀缺失或不完整时拒绝续跑。备份需包含两个文件；回滚旧代码时使用升级前的备份。

独立审查 Git 工作区时，引擎会先核对审查范围及工作区原始文件字节。匹配提交树才使用带协议号的 tree ID；有未提交修改、额外文件或换行转换时继续使用原文哈希。首次命中新协议的独立审查会重新生成结论。测试阶段的输入保护和已有提交证据的校验方式保持原样。

Qwen、Qoder、Claude 共用已验证版本的流式终态表；反重力、Codex、DSH 共用其已允许的 JSONL 解码。各工具的启动参数、权限、完成判据和用量口径仍由适配器定义。

工作台的事件分页和实时流只更新运行投影与 CLI 活动。房间快照按状态版本短暂复用产物清单，实际打开产物时仍实时校验哈希。指标文件和 CLI 进度按各自的文件变更刷新，无需等待运行状态提交。

## 自身维护基准

```powershell
python scripts/self_benchmark.py --output D:/benchmarks/self-maintenance-new --live-config claude-deepseek.example.toml --ref 7ee4dbd
```

该命令显式调用真实模型。固定旧提交导出三份隔离源码，复现 reporting 模块的超大整数、畸形 usage_total 和畸形 job 输入问题，逐项运行设计、编码、保护测试与独立审查。设计请求显式附源码，记录基准执行器的文件指纹。每项仅允许修改 reporting.py，现有测试及仓库外验收脚本受哈希保护；首测未复现缺陷则拒绝算作修复。单项预算为 120000 本地计量 tokens、单次输出额度 16384、600 秒、两轮返工；CLI 未知计量仍预留，不承诺等于账单硬上限。结果记录固定提交、模型用量、attempt、返工、成功和失败，保留原始证据，不修改开发主线。三个同项目小样本不能外推跨项目业务成功率。
