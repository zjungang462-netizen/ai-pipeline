# AI Pipeline 项目记忆

更新于 2026-11-25。核对基线为本地 `main` 与 GitHub `origin/main` 的 `697c3c1`，CLI 源码版本 `0.5.9`。本文件记录架构约定和拟议路线，版本历史与既有验收结果见 [WORK_STATUS.md](WORK_STATUS.md)，运行证据和发布口径见 [DELIVERY.md](agent-orchestrator/DELIVERY.md)。

## 接手项目先看什么

- 产品源码在 `agent-orchestrator/`。核心是一个本地优先、可人工审批、可恢复的多模型开发流水线。阶段执行在 `orchestrator/engine.py`，运行状态和证据在 `state.py`、`evidence.py`，模型适配在 `adapters/`，工作台在 `dashboard/` 与 `collaboration/`。
- `state.json` 与 attempt 归档是执行证据。工作台 SQLite 是可重建的投影。现有断点恢复已校验输入指纹和证据，保留预算与期限。工作台 SSE 已支持 `Last-Event-ID`，扩展竞技场时可复用同类游标语义。
- 现有能力包括动态阶段、确定性测试白名单、人工审批、报告导出与归档、历史路由推荐、圆桌讨论，以及隔离 worktree 的 2–3 个候选对比。竞技场采纳已有基线 SHA、干净工作区、`--ff-only` 和采纳回执。worktree 提供 Git 层面的隔离，运行权限还取决于实际执行环境。
- 本机 8766 是独立运行快照，部署版本查进程收据。历史文档中的测试数量只适用于对应验收。快速回归用 `scripts/precommit.py`，离线收敛用 `scripts/benchmark.py`，真实模型基准通过 `--live-config` 显式启用并将证据写在仓库外。

## GitHub 调研记录

样本覆盖当前仓库的四个问题，分别是并行候选生命周期、崩溃恢复、基准口径和 CLI 兼容。以下是读取过的一方文档，GitHub 链接固定在调研时的提交。

| 已核对的做法 | 可移植到本项目的做法 | 来源 |
| --- | --- | --- |
| 按任务分配 worktree，定义后台运行的持久化状态和派生状态 | 竞技场登记为后台运行，候选进度和失败可以独立查询，页面重开后继续查看 | [OrchestratorInc/agent-orchestrator](https://github.com/OrchestratorInc/agent-orchestrator/blob/2fe8d90dfb38eb7cb3c187321fd7fc5bf9c3fd22/docs/architecture.md) |
| 以持久化目录和会话 ID 重载对话 | 为支持恢复的讨论席位保存会话标识和上下文摘要，明确区分查看旧讨论与继续执行 | [OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk/blob/a053dbc47bfd98e96c621fa1b1e8d154abb1154c/examples/01_standalone_sdk/10_persistence.py) |
| 按步骤保存检查点，支持故障恢复与人工介入 | 在产生 Git 变更前后留独立收据，用故障注入验证中断后的判定 | [LangGraph](https://github.com/langchain-ai/langgraph/blob/f96105368f9e9385d4c9e38bffaa457aba17f095/README.md) · [Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers) |
| 单样本结果和评分可追溯，失败或缺失样本可按原选择重试 | 统一离线夹具、真实任务和候选对比的结果字段，保存每次尝试及失败原因 | [Inspect AI 评测集](https://github.com/UKGovernmentBEIS/inspect_ai/blob/2f8d8ec11a184c92ee858040302597035f9223c7/docs/eval-sets.qmd) · [评测日志](https://github.com/UKGovernmentBEIS/inspect_ai/blob/2f8d8ec11a184c92ee858040302597035f9223c7/docs/eval-logs.qmd) |
| 协商协议版本和可选能力，按声明支持会话加载、回放或重连 | 先为一个已接入工具验证可选能力和降级行为，保留当前适配器的证据及预算口径 | [Agent Client Protocol](https://github.com/agentclientprotocol/agent-client-protocol/blob/d3ac0e6d8b1132c9a749a4210f04bb788c9a75a4/README.md) · [会话规范](https://agentclientprotocol.com/protocol/session-setup) |

## 已落实的两项 P0

- **Windows Git 基线。** 自动提交通过标准输入传递 NUL 分隔的字面文件清单，并显式关闭本次提交的 GPG 签名。此前在临时仓库中，600 个路径的文件参数组合触发了 Windows 错误 206，启用缺失的签名程序也会导致基线失败。回归覆盖超过 Windows 命令行长度上限的清单、中文及空格文件名、空基线和已有的无关暂存项。进程树退出和锁持有者身份仍是单独的可靠性议题。
- **竞技场采纳。** 在从封存候选 SHA 检出的独立工作树执行白名单测试。采纳要求生成成功、变更属于声明的文件范围且测试通过。审批请求带上基线 SHA、候选 SHA 与验收证据 SHA-256。合入前写入采纳意向，最终回执写入中断时，同一分支的原候选可通过重试补齐收据。未声明改动范围、无测试或旧版无证据的记录均只供对比。

## 已落实的后台竞技场

- 启动接口返回 `202` 与持久化任务 ID。同一项目中的同一幂等请求返回原任务，进度通过 SSE 按游标补齐，断线时可用快照复查。工作台重开后可查看近期任务。
- 候选状态和完成结果分别落盘。正常取消会停止运行中的 CLI 或测试，并回收竞技场工作树。后台进程异常退出后保留已有候选结果，记录标记为已中断，由用户决定是否发起新任务。

## 已验证的配对评测

- `scripts/paired_benchmark.py` 在相同基线、保护测试、预算与公共设计和审查角色下比较两个编码候选。2026-11-25 完成三个任务各三次的离线验证，共 18 个样本。报告保留完成、失败与阻塞，未知用量调用单列。
- Windows 单工作进程对照中基线与竞技场工作树均超出 120 秒。已加入历史耗时均衡分片，四进程复跑最快 98.97 秒。同法另一次耗时 124.56 秒，时限稳定性仍需观察。见 [WORK_STATUS.md](WORK_STATUS.md)。

## 真实配对评测结论

- 2026-11-25 的 18 个真实样本使用统一的 120,000 本地计量 token 预算。Codex CLI `0.170.0` 的候选完成 7/9，反重力 CLI `1.5.17` 的候选完成 0/9。三个失败原因集中在公共设计超时、反重力编码超时和编码后 token 超限。现有历史只有一条匹配的编码记录，影子对照返回 `insufficient_history`。逐次证据见 [WORK_STATUS.md](WORK_STATUS.md)。

## 编码路由对照入口

- `route-recommend --paired-results` 从逐样本证据重算配对结果，按独立终态运行计算历史覆盖。以每个候选至少两条为准，Codex 还差两条，反重力还差一条。操作入口见 [OPERATIONS.md](agent-orchestrator/OPERATIONS.md)。

## 独立任务路由对照

- 用标签规范化和站点读数汇总积累四条真实编码运行记录。Codex `0.170.0` 完成 2/2，反重力 `1.5.17` 因后端空会话 ID 完成 0/2。独立任务的排序与 18 次配对评测一致，既有项目历史仍有覆盖缺口。版本、预算和逐次结果见 [WORK_STATUS.md](WORK_STATUS.md)。

## 反重力复测

- 固定反重力 CLI `1.5.17`，显式代理和模型组合在复测中成功建立会话。两道独立任务均执行了编码文件工具，并在 `120,000` 计量预算下中止。标签规范化在 `180,000` 预算的单次诊断中全流程通过，最终报告 `113,356` tokens。会话探针、原预算复跑和诊断证据见 [WORK_STATUS.md](WORK_STATUS.md)。

## 后续推进顺序与验收

1. **P1 · 继续积累真实项目路由历史。** 独立任务已验证两种候选的当前排序。既有项目历史的最低覆盖还差 Codex 两条和反重力一条，后续用同配置的终态运行复核。
2. **P2 · 小范围试验协议与会话恢复。** 验证一个现有 CLI 的 ACP 版本和能力协商，先接只读讨论。验收覆盖可重连、仅可回放、完全不支持三种路径，以及工具更新后的契约回归。沿用现有的阶段审批、预算和工程证据。

进度视图沿用现有的候选状态和采纳收据。路由策略以固定任务上的可比较结果为输入。

## 验收记录

2026-11-25 在实现 P0 前，Windows 上的竞技场和崩溃恢复两组离线单测共 15 项通过。当前实现的验收见 [WORK_STATUS.md](WORK_STATUS.md)。
