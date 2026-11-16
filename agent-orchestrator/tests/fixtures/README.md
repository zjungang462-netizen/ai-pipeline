# 测试样本（fixtures）说明

## 现状（如实标注）

| 文件 | 来源 | 状态 |
|---|---|---|
| `codex_0.167.jsonl` | **手写**，模拟 codex `--json` 事件流 | ⚠️ 合成样本，非真实抓包 |
| `qwen_0.5.json` | **手写**，模拟 qwen `--output-format json` | ⚠️ 合成样本，版本号 0.5 与真实不符 |
| `qwen_0.26.0_real.json` | 2026-11-17 本机 Qwen Code 0.26.0，通过 stdin 的真实调用 | 真实 JSON 事件数组及唯一成功 result，用量 7282；后端为 DeepSeek 兼容端点，不是 Qwen 模型；未验证工具执行 |
| `qoder_1.1.89_auth_stderr.txt` | 2026-11-17 本机 Qoder 1.1.89 非交互探针 | 真实未登录失败，未获得模型成功结果；不会放开成功版本白名单 |
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

Qwen 已在独立试验目录安装并抓包；豆包与 Qoder 的成功 CLI 样本仍待实际登录与调用。真实 fixture 应这样获取：

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

**当前真实验收覆盖 Codex CLI 0.169.0 与反重力 CLI 1.5.9；不能据此宣称其余 CLI 或其他版本均已验证。**
