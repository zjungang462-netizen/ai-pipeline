# AI Pipeline

## 当前开发入口

GitHub 的 `main` 是后续开发主线，产品源码在 [agent-orchestrator](agent-orchestrator/README.md)，当前功能版本为 **0.5.3 + 工作台导航修复**。

已包含 Claude Code/DeepSeek、Qoder、模型与思考设置、用量导出，以及工作台时间排序、日期折叠、筛选重置、历史阅读保护和任务切换失败恢复。版本号表示功能基线；具体代码以 Git 提交为准。

本地目录说明：

| 目录或分支 | 用途 |
| --- | --- |
| `D:/work/ai-pipeline/agent-orchestrator` | 唯一保留的开发源码目录，对应 GitHub `main` |
| `codex/release-stabilization` | Git 中保留的较早稳定线检查点，无单独展开副本 |
| `review-*`、`live-validation-*`、`repair-*`、`workbench-*` | 本地验收、运行和诊断目录，不是开发主线 |
| `*/runtime` 与旧 ZIP 发布包 | 固定历史快照，不能凭文件时间当作最新源码 |

2026-11-18 已将主线迁回以上 D 盘目录，移除重复工作树。旧验收、发布目录与两份过时工作台运行快照归档在 `D:/work/ai-pipeline-archives/2026-11-18`，逐文件 SHA-256 校验后移除展开副本；保留当前运行快照和真实任务证据。归档含历史本机数据，仅供本地恢复，不上传 GitHub。

本机 8766 工作台仍运行固定验收快照，不能据其页面推断已部署 main 的全部功能。本次整理不更换运行引擎，不迁移历史状态，不改写旧失败记录。

## 验证与运行

2026-11-18 本次归并通过提交前静态检查、8 份示例的离线冒烟和 127 项 quick 回归（33.422 秒），另通过 18 项导航、时间排序、执行范围与设置界面相关回归（30.470 秒）。两组可能重叠，不相加为独立测试总数。本次未重复全量验收，没有新增模型调用。

进入 `agent-orchestrator`，运行：

```powershell
python -m orchestrator.cli --version
python scripts/precommit.py --output <项目外验收目录>
python -m orchestrator.cli dashboard --config workbench.example.toml --port 8766
```

若 8766 已占用，请改用空闲端口。模型使用边界、真实调用结果和历史发布验收见 [DELIVERY.md](agent-orchestrator/DELIVERY.md)。新导航增量的相关测试不能替代整份当前源码的全量验收。凭据、会话入口、运行数据库和日志不提交到 GitHub。
