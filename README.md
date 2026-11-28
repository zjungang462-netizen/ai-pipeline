> **Kiroween 2026 Judges & English Readers**: Start with **[START-HERE.md](START-HERE.md)** for the 1-command zero-key verification (`verify_judge_demo.py`) and interactive English workbench guide, or read **[README.en.md](README.en.md)** for the full `Frankenstein` architecture and `/.kiro` integration overview.

# AI Pipeline (`agent-orchestrator`)

基于证据链、确定性测试门禁与人工哈希审批的多智能体研发流水线编排器与本机协作工作台（当前版本 **v0.5.10**，Python 3.11+ 标准库零第三方运行时依赖）。

---

## 核心特性

- **异构模型与 CLI 编排（Frankenstein Stitch）**：统一编排 `openai_compat` API、`cli_claude`、`cli_codex`、`cli_qwen`、`cli_qoder`、`cli_agy`、`cli_dsh` 及容器隔离的 `cli_grok` / `cli_doubao` 共 10 种执行器。
- **不轻信模型自述的硬核验证**：模型退出码 `0` 不代表任务完成；编码阶段强制 Git 隔离 worktree 与非空净变更校验，测试阶段由引擎直接执行固定 `argv` 命令取证，未通过自动携带失败证据打回重修（有界轮次与停滞熔断）。
- **SHA-256 证据封存与审批门禁**：每次尝试封存输入指纹、`manifest.json`、红绿代码 Diff 与结构化 `review_verdict.json`；人工审批、断点续跑与受控部署均严格校验产物哈希与提交 SHA。
- **本机实时协作工作台**：内置零依赖 HTTP/SSE 双语工作台（支持中文 / English 无损切换与 `🎃 Kiroween` 主题）、多模型只读圆桌交叉质询、隔离 Worktree 双模型竞技场（Best-of-N）及历史路由自优化推荐。
- **深度 Kiro 规范驱动工程**：完整包含 [`.kiro/specs/`](.kiro/specs/) 模块架构规格、[`architecture-standards.md`](.kiro/steering/architecture-standards.md) 架构护栏与 [`verify-orchestrator-tests.json`](.kiro/hooks/verify-orchestrator-tests.json)（及 [`.kiro.hook`](.kiro/hooks/verify-orchestrator-tests.kiro.hook)）保存后自动回归钩子。

---

## 零配置快速体验（无需任何 API Key）

在仓库根目录一键运行五阶段闭环自动核验（包含「首次实现错误 → 真实单测打回 → 二次修复通过 → 脚本化 AST 审查 → 人工审批放行 → 幂等续跑」全链路）：

```powershell
python -X utf8 agent-orchestrator/scripts/verify_judge_demo.py
```

启动本机可视化协作工作台（英文界面可加 `--lang en`）：

```powershell
cd agent-orchestrator
python -X utf8 -m orchestrator.cli dashboard --config kiroween-demo.example.toml --port 8766 --open
```

> 若 `8766` 端口已占用，请改用任意空闲端口（如 `--port 8776`）。工作台仅监听本机回环地址并使用一次性启动令牌建立同源会话；凭据、运行数据库与日志均不提交到仓库。

---

## 工程验证与目录导航

进入 `agent-orchestrator` 目录可执行完整静态检查与分层离线验收：

```powershell
python -m orchestrator.cli --version
python scripts/generate_config_docs.py --check
python scripts/precommit.py --checks-only --base-ref HEAD
python scripts/verify_delivery.py --quick --redact-logs --output <仓库外输出目录>
```

| 文档 / 目录 | 说明 |
| --- | --- |
| [START-HERE.md](START-HERE.md) | 评委与新用户 60 秒零配置上手指南（含 CLI 退出码 `11` 审批门禁说明与浏览器演练步骤） |
| [README.en.md](README.en.md) | 英文完整架构文档、适配器能力矩阵、Kiro 集成说明与 Docker 启动方式 |
| [agent-orchestrator/README.md](agent-orchestrator/README.md) | 编排器产品文档与自动生成的配置参数/适配器能力参考表 |
| [agent-orchestrator/OPERATIONS.md](agent-orchestrator/OPERATIONS.md) | 守护运行、状态备份恢复、受控部署与容器运行时运维手册 |
| [agent-orchestrator/DELIVERY.md](agent-orchestrator/DELIVERY.md) | 离线确定性验收与真机模型实测边界声明 |
| [PROJECT_MEMORY.md](PROJECT_MEMORY.md) / [WORK_STATUS.md](WORK_STATUS.md) | 架构演进记忆、阶段性进展记录与维护基准（历史过程文档归档于 [`docs/archive/`](docs/archive/)） |
