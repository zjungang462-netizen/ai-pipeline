# AI Pipeline：本地稳定线检查点

当前目录位于 `codex/release-stabilization`，用于保存较早稳定线及本机尚未合入主线的改动，不是最新功能全集。

最新开发入口是 GitHub 的 `main`，功能基线为 **0.5.3 + 工作台导航修复**。本机对应源码目录为：

`C:/Users/张军刚/.codex/worktrees/phase3-maintenance/ai-pipeline/agent-orchestrator`

本分支保留了适配器登记调整、导航失败恢复和本机审查记录，便于追溯和后续逐项合并。报告内测试数字对应当时源码，不代表本检查点重新完成全量验收。

`review-*`、`live-validation-*`、`repair-*`、`workbench-*`、其中的 `runtime` 及旧 ZIP 均为本地证据或固定运行快照，不按目录时间判定新版本。8766 工作台仍运行其固定验收快照；本次提交不切换运行引擎，也不改写历史状态。

后续开发以 GitHub `main` 的 Git 提交为准。凭据、会话入口、运行数据库和日志不提交。
