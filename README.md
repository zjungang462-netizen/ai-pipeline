# AI Pipeline

## 当前开发入口

GitHub 的 `main` 是后续开发主线，产品源码在 [agent-orchestrator](agent-orchestrator/README.md)，当前开发版本为 **0.5.6**。自动检查、守护运行与基准使用见 [OPERATIONS.md](agent-orchestrator/OPERATIONS.md)。

2026-11-20 的发布、工作台升级、状态恢复、自身维护基准与后续阻塞见 [WORK_STATUS.md](WORK_STATUS.md)。冻结包以实际发布提交和 SHA-256 为准，根目录 CI 修正与产品源码验收分开记录。

已包含 Claude Code/DeepSeek、Qoder、模型与思考设置、用量导出，以及工作台时间排序、日期折叠、筛选重置、历史阅读保护和任务切换失败恢复。版本号表示功能基线；具体代码以 Git 提交为准。

本地目录说明：

| 目录或分支 | 用途 |
| --- | --- |
| `D:/work/ai-pipeline/agent-orchestrator` | 唯一保留的开发源码目录，对应 GitHub `main` |
| `codex/release-stabilization` | Git 中保留的较早稳定线检查点，无单独展开副本 |
| `review-*`、`live-validation-*`、`repair-*`、`workbench-*` | 本地验收、运行和诊断目录，不是开发主线 |
| `*/runtime` 与旧 ZIP 发布包 | 固定历史快照，不能凭文件时间当作最新源码 |

2026-11-18 已将主线迁回以上 D 盘目录，移除重复工作树。旧验收、发布目录与两份过时工作台运行快照归档在 `D:/work/ai-pipeline-archives/2026-11-18`，逐文件 SHA-256 校验后移除展开副本；保留当前运行快照和真实任务证据。归档含历史本机数据，仅供本地恢复，不上传 GitHub。

本机 8766 工作台运行独立固定验收快照，部署版本以本地进程收据为准，不能仅据当前源码版本推断已经上线。历史状态与旧失败记录保留。

## 验证与运行

2026-11-18 性能优化后、加入 Qoder 版本门禁前，工作树通过配置静态检查 16 项、8 份示例离线冒烟、127 项 quick 回归（25.875 秒）及 620 项全量回归（357.812 秒，零失败/错误/跳过）。五个离线固定基准场景均符合预期，这些检查没有模型调用。Qoder 真机复验、Grok 认证阻塞与 Codex 代理探针见 [DELIVERY.md](agent-orchestrator/DELIVERY.md)。正式发布以源码与解压包的双重完整验收为准。

进入 `agent-orchestrator`，运行：

```powershell
python -m orchestrator.cli --version
python scripts/precommit.py --output <项目外验收目录>
python -m orchestrator.cli dashboard --config workbench.example.toml --port 8766
```

若 8766 已占用，请改用空闲端口。模型使用边界、真实调用结果和历史发布验收见 [DELIVERY.md](agent-orchestrator/DELIVERY.md)。新导航增量的相关测试不能替代整份当前源码的全量验收。凭据、会话入口、运行数据库和日志不提交到 GitHub。
