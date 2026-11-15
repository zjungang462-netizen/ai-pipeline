# 测试样本（fixtures）说明

## 现状（如实标注）

| 文件 | 来源 | 状态 |
|---|---|---|
| `codex_0.167.jsonl` | **手写**，模拟 codex `--json` 事件流 | ⚠️ 合成样本，非真实抓包 |
| `qwen_0.5.json` | **手写**，模拟 qwen `--output-format json` | ⚠️ 合成样本，版本号 0.5 与真实不符 |

## 重要说明（评审 M4）

- 这些 fixture 是**手写合成样本**，用于离线验证解析器逻辑，**不是真实工具抓包**。
- 真实 Qwen Code 当前为 **0.26.x**（非 0.5）；真实 codex 版本以本机 `codex --version` 为准。
- 版本白名单已收紧为"工具名前缀"匹配（`cli_qwen.py` / `cli_codex.py` 等），裸 `0.` 不再放行；
  不识别的工具版本 => `parse_unknown`，禁止判完成。

## 待真机抓包替换（需用户环境）

本机已安装 codex/agy/dsh，qwen/doubao/qoder 未安装。真实 fixture 应这样获取：

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

**在真实抓包替换前，CLI 解析逻辑仅经合成样本验证，真实工具联调属未验证项。**
