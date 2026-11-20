# 测试样本（fixtures）说明

## 现状（如实标注）

2026-11-20 新增四份真实文件工具夹具：codex_0.170.0_tools_real.jsonl、agy_1.5.12_tools_real.jsonl、agy_1.5.13_tools_real.jsonl、qwen_0.26.0_tools_real.jsonl。每份均真实读取并修改 probe.py，通过仓库外断言，未修改用户连接配置；路径、会话标识与思考正文机械脱敏。Qwen 样本使用 safe/bare 与显式 exclude-tools，init 工具清单仅 read_file/edit；反重力 init 保留完整内置工具清单，不伪称只开放文件工具。1.5.13 在独立可执行文件副本上采集，前后二进制指纹相同。仅单文件协议探针通过不等于 Engine 业务闭环或跨项目成功率，后续维护证据单独记录。

| 文件 | 来源 | 状态 |
|---|---|---|
| `codex_0.167.jsonl` | **手写**，模拟 codex `--json` 事件流 | ⚠️ 合成样本，非真实抓包 |
| `qwen_0.5.json` | **手写**，模拟 qwen `--output-format json` | ⚠️ 合成样本，版本号 0.5 与真实不符 |
| `qwen_0.26.0_real.json` | 2026-11-17 本机 Qwen Code 0.26.0，通过 stdin 的真实调用 | 真实 JSON 事件数组及唯一成功 result，用量 7282；后端为 DeepSeek 兼容端点，不是 Qwen 模型；未验证工具执行 |
| `qoder_1.1.89_auth_stderr.txt` | 2026-11-17 本机 Qoder 1.1.89 非交互探针 | 真实未登录失败，保留作认证失败回归 |
| `qoder_1.1.89_real.json` | 2026-11-17 登录后本机 Qoder stdin调用 | 真实成功终态、credits与全零token计数，后者按未知处理 |
| `qoder_1.1.89_tools_real.jsonl` | 2026-11-17 Qoder Ultimate/high/adaptive | 真实Read/Edit单文件修改、init协议1.5.0与成功终态，标识/路径已脱敏 |
| `claude_2.1.325_result_real.jsonl` | 2026-11-17 Claude Code经用户DeepSeek兼容API | 无工具成功终态，模型DeepSeek Pro[1M]、max选项；不是原生Claude模型；思考正文省略 |
| `claude_2.1.325_tools_real.jsonl` | 2026-11-17 Claude Code/DeepSeek Read/Edit | 真实单文件工具修改与累计usage；thinking为output子计数，标识/路径已脱敏 |
| `codex_0.169_real.jsonl` | 2026-11-15 本机 Codex CLI 0.169.0 实际执行 | 真实 JSONL，脱敏 thread_id |
| `agy_1.5.9_real.jsonl` | 2026-11-15 本机反重力 CLI 1.5.9 实际执行 | 真实 stream-json，脱敏会话标识与 cwd |
| `codex_0.169_policy_stderr.txt` | 2026-11-16 本机 Codex CLI 0.169.0 只读探针 | 真实策略拒绝日志，命令与本机路径已脱敏；命令未实际执行 |
| `codex_0.169_tools_real.jsonl` | 2026-11-16 本机 Codex CLI 0.169.0，保留 Windows 沙箱后端 | 真实两次只读调用，保留命令生命周期、退出码与输出，脱敏会话/shell/工作区路径 |
| `dsh_0.2.2-rc.3_real.jsonl` | 2026-11-16 本机 dsh 0.2.2-rc.3 headless JSON 探针（`DSH_PROBE_OK_20261116`） | 真实 headless 事件流，脱敏 sessionId/cwd；原始抓包未保留独立副本 |

新增真实样本的来源指纹和脱敏字段见 `cli-capture-provenance.json`。原始证据保留在上级 `live-validation-2026-11-15/cli-mixed/probes/`，离线回归不调用模型。完整混合链路已实际完成 Codex 设计→反重力编辑→六项真实测试→Codex 审查；具体模型及用量口径见该目录实施报告。

2026-11-16 工具活动加固的原始探针保存在 `live-validation-2026-11-16/codex-tool-activity/`。早期命令、文件修改和 MCP 工具事件测试为**构造事件**，依据 [Codex rust-v0.169.0 协议定义](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/exec/src/exec_events.rs)，不冒充真实成功抓包。探针最终消息和进程成功退出不代表其中命令已获准执行。

后续 `live-validation-2026-11-16/codex-normal-tools/` 已定位 Windows 隔离配置缺失，并完成正常命令两次读取与预设重复读取阈值停止的真机验收。`codex_0.169_tools_real.jsonl` 来源为该轮正常只读工具抓包；原有构造事件继续用于错误/边界验证，文件修改和 MCP 没有新增真机成功样本。

`codex_0.169_policy_stderr.txt` 的哈希按原始文件字节计算；`.gitattributes` 对此样本关闭换行转换，保持跨平台检出后的指纹一致。

交付收口时，`codex_0.169_real.jsonl` 与 `agy_1.5.9_real.jsonl` 恢复为来源登记的 LF 字节；其差异仅为 CRLF→LF，内容和来源哈希不变。全部 JSONL 样本关闭 Git 换行转换；原本使用 CRLF 的样本保留原始字节，并按 CR 行尾进行空白检查。

## 重要说明（评审 M4）

- 原有 `codex_0.167.jsonl`、`qwen_0.5.json` 是**手写合成样本**，保留用于旧解析逻辑回归，不代表对应工具已经真机验收。
- Qwen Code 0.26.0 的裸版本号、stdin 与 JSON 事件数组已真实抓取并精确绑定；其它版本、工具执行、官方 Qwen 模型后端仍未由本样本证明。
- 版本白名单已收紧为"工具名前缀"匹配（`cli_qwen.py` / `cli_codex.py` 等），裸 `0.` 不再放行；
  不识别的工具版本 => `parse_unknown`，禁止判完成。

## 待真机抓包替换（需用户环境）

Qwen和Qoder已在独立试验目录安装并抓包；豆包独立官方CLI仍未核实，不使用猜测命令冒充真实接入。新增真实fixture需登记provenance；Qoder零token+非零credits不证明免费或用量完整。真实fixture示例：

```powershell
# codex（本机可用）：只读分析任务，抓 --json 原始输出
codex exec "列出当前目录文件" --sandbox read-only --skip-git-repo-check --json > codex_real.jsonl

# qwen（需先 npm 安装并配置 Key）：
qwen -p "你好" --output-format json > qwen_real.json
```

抓到真实输出后：
1. 用真实内容替换对应 fixture，文件名带真实版本号。
2. 在解析测试里绑定该真实版本，确认解析器正确提取。
3. 若真实格式与现有解析器不符，按真实格式调整解析器（而非改 fixture 迁就解析器）。

2026-11-20 新增 `claude_2.1.328_tools_real.jsonl`，来自固定旧提交的真实自身维护任务。保留 Read/Edit、消息计量及成功终态，机械脱敏路径与标识符，省略思考正文和增量思考计数事件。原始运行当时因适配器尚不接受新版本而失败，该夹具不能把原失败改记为成功。2.1.325 与 2.1.328 分别精确绑定 CLI 和 init 版本；其它版本在模型调用前拒绝。provenance 记录原始输出和夹具 SHA-256。

真实覆盖以本页逐项说明和 provenance 为准，版本探测通过不等于认证、文件工具或完整流水线通过。
