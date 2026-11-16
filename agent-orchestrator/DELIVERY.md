# 干净目录运行与验收

## 环境

- Python 3.11 或更高版本；运行代码使用标准库，无需 pip 安装。
- Git：代码阶段的 worktree/提交与完整 Git 测试需要。
- Node.js：完整测试中的前端异步契约需要，无需 npm 安装。仅启动工作台不需要 Node.js。
- 真正调用模型时另装相应 CLI 或在环境变量中配置 API Key；离线验收不调用模型或网络服务。

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

验证脚本检查运行环境、六份示例的加载和提示词引用、脱敏样本指纹、CLI 帮助、离线示例 run/status/approve/resume，以及本机临时 HTTP 端口上的登录和项目列表。随后执行原有全部 unittest（包括 Node.js 前端契约）。失败、错误或跳过均使验收返回非零。

运行结束在指定输出目录保存 `verification.json`、`full-regression.log`、`smoke.json` 和源码前后指纹。输出目录应在项目目录之外。脚本不下载依赖，不读取登录凭据，不运行 API/CLI 模型示例。

若只需执行现有测试：

```powershell
python -m unittest discover -s tests -v
```

Windows 上测试会真实创建临时文件、Git 仓库和进程树，耗时受负载影响。不要把运行中的测试进程无输出视为已通过，也不要用单项复验替代失败的完整批次。

## 模型示例

`config.example.toml` / `pipeline.example.toml` 使用环境变量 `DEEPSEEK_API_KEY`；它们包含设计审批。`unattended.example.toml` 显式定义预授权文件和预算。`cli-mixed.example.toml` 需要已验证版本的 Codex 与反重力及本机登录；`dsh-test.example.toml` 需要 dsh 及显式允许的 `DEEPSEEK_API_KEY`。先复制模板、配置工作目录和工具路径，再按 README 启动；不要把真实凭据写入 TOML 或提交仓库。

## 原始验证与历史记录

收口前的 388 项通过来自当前源码的一次完整回归。交付包的可复现验收使用上面的脚本和同包附带的测试；具体文件指纹以 `verification.json` 与压缩包 SHA-256 为准。原始性能测量和模型执行日志保留在开发机被忽略的实测目录，不是启动或测试依赖。
