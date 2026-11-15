# 多智能体开发流水线编排器 —— 方案评审

评审对象：需求→设计→编码→测试→审查 五阶段流水线（轻量编排引擎 + 可插拔适配器，Python）。
评审日期：2026-11-14。范围：只评审，不含代码实现。

## 结论

骨架成立：编排引擎与适配器分离、产物落盘+哈希作为完成判据、git worktree 隔离、串行执行，这四个方向都对，比再包一层 LLM 框架务实。

但有 **4 个会导致"续跑静默出错 / 假回退 / 隔离失效"的设计缺口**，必须在写代码前补进设计，否则实现出来后在真实失败场景下不会报错、只会给出错误结果。以下按严重度排列。

## 一、必须先补（blocker）

### B1 完成判据缺"输入指纹"，续跑会复用过期产物

现状：以"产物 + 哈希"作为阶段完成的唯一判据。问题：判据只描述输出，不描述输入。只要 prompt 模板、上游产物、相关配置任一变化，已完成阶段仍被判为完成，续跑直接沿用旧产物，且没有任何地方能发现。

修正：完成判据改为二元组 `(inputs_hash, outputs_hash)`。
- `inputs_hash = hash(阶段 prompt 模板 + 上游产物哈希 + 该阶段读取的配置键)`
- 续跑时先比 `inputs_hash`，不匹配则该阶段及其所有下游标记为 `stale`，必须重跑。
- `state.json` 里记录这两项，不要只记产物路径。

### B2 回退（rollback）语义未定义，极易做成假动作

现状："重试、回退"写在 engine 职责里，但没定义回退是"撤销产物"还是"重跑某阶段"。若只把 phase 指针往回拨，产物仍在原地，下游读到的还是旧文件，回退等于没做。

修正（二选一，MVP 建议选后者）：
- 方案 A：回退 = 新 attempt 写入独立目录 + 成功后原子切换 current 指针 + 记录 `supersedes` 链（可审计，但要处理磁盘增长）。
- 方案 B：MVP 不提供回退，只提供"从指定阶段重跑"，并在文档里明确"重跑覆盖，旧产物归档到 `attempts/<n>/`"。

不要留下"按钮能点、语义含糊"的中间态。

### B3 worktree 隔离与"阶段间只传产物文件"互相冲突

现状：每阶段独立 worktree，阶段间只传产物文件。问题：编码阶段改了 12 个源文件，下游"测试"阶段在自己的 worktree 里看不到这些改动，除非把文件逐个复制过去——而复制会丢失删除/重命名、丢失 git 历史，还会与 worktree 的基线冲突。

修正：把"产物"定义为 **commit/branch** 而不是散文件。
- 上游阶段结束：在自身 worktree `git add -A && git commit`，记录 `commit_sha` 与 `branch`。
- 下游阶段：`git worktree add` 时基于上游 `commit_sha` 拉分支，而不是基于主干。
- 只有"报告类产物"（评审意见、测试结果 JSON）才以文件形式传递，并记录哈希。
这样隔离与传递同时成立，且可 diff、可复现。

### B4 重试与原子持久化之间的竞态没有覆盖

现状：原子写（临时文件+改名）解决单文件半写，但没解决"一个阶段多次尝试写同一路径"。kill 在 attempt 2 写入中途，可能与 attempt 1 的产物混在一起；启动时看到文件存在且有哈希，就误判完成。

修正：
- 每次尝试写 `runs/<run_id>/stages/<stage>/attempts/<n>/`，成功后才把 `current` 指针（一个 symlink 或 state 里的路径字段）切到该目录。
- 启动时扫描：存在 attempts 目录但没有 `success.json` 的，一律标记 `interrupted`，不当作完成。
- 完成判据读取的永远是 `current` 指向的目录，不是"最新 mtime 的文件"。

## 二、适配器契约与安全（major）

### M1 `run(prompt, workdir, ctx) -> Result` 太窄

缺少流水线真正需要的字段，后续会在引擎里长出特例分支：

建议 `Result` 至少包含：`ok`、`exit_code`、`stdout`、`stderr`、`changed_files[]`、`commit_sha`、`usage{tokens,cost,duration}`、`error_kind ∈ {timeout, auth, parse_failed, crash, rejected}`。

关键点：`changed_files` 是判断"工具到底干活了没有"的唯一可靠信号。很多 CLI 成功退出但什么都没改，若只信 exit_code，会把空跑判成成功。

### M2 CLI 输出解析必须绑定版本

fixture 钉死解析是对的方向，但输出格式随工具版本变。修正：每条 fixture 标注 `tool_version`，preflight 记录实际版本，解析器按版本分派；版本不认识就降级为"原始文本 + 人工确认"，不允许默认走某个老解析器。

### M3 本机实测：6 个 CLI 全部不在 PATH

审计结果（本次环境）：

| 命令 | 结果 |
|---|---|
| codex / qwen / doubao / agy / qoder / dsh | 均 `not found` |
| git | `not found`（与"项目目录无 Git"一致） |
| python 3.13 + pip 26.1.2 | 可用 |
| dashscope / ark 443 端口 | 可达 |

含义：
- preflight 不能只 `shutil.which("codex")`，必须支持配置里写绝对路径（Windows 上 npm/pnpm 全局包常在 `%APPDATA%\npm`，不在 PATH）。
- worktree 隔离依赖 git；本机 git 不在 PATH，要么写死路径，要么 preflight 明确报错，不能"以为有"。
- 结论只代表这台机器，不代表目标机器；preflight 输出要落盘成报告，作为每次运行的环境证据。

### M4 密钥泄漏面：子进程 env 继承

"密钥只走环境变量"是正确起点，但子进程默认继承父进程全部环境变量。被测 CLI 可能把 env 或自身日志写到磁盘/上报，Key 就出去了。

修正：adapter 启动子进程时用**白名单 env**（PATH、HOME、LANG、必要的工具凭据），百炼/方舟 Key 只注入 `openai_compat` 适配器，不注入 CLI 适配器；若某 CLI 自身需要 Key，单独配置项显式开启并告警。

### M5 `--yolo` 之外还需要可写根白名单

危险模式开关很好，但"显式开启+告警"只解决人的知情，不解决越界。应再加一层硬约束：可写根 = 当前 worktree + `runs/<run_id>/`；越界写入（父目录、`.git`、系统目录、项目外）直接拒绝，并在执行后扫描 `git status` 之外的文件变更。

### M6 审批检查点要能"对得上产物"

审批暂停→进程退出→人第二天点通过，中间产物可能已被改动。修正：`pending_approvals` 持久化 `{stage, outputs_hash, requested_at, requested_by}`；恢复时校验当前产物哈希与申请时一致，不一致就要求重新审批，而不是直接放行。

## 三、步骤与验证（major/minor）

### S1 第 1 步的"kill 续跑一致"缺可测口径

"结果一致"要落成断言：固定输入 → 期望 `事件序列` + `产物哈希集合` 完全相等。kill 点至少覆盖三种：①写产物中途；②临时文件已写、未改名；③改名完成、checkpoint 未落盘。

### S2 第 5 步"CLI 失败回退 API"需要写明不等价

同一 prompt 换通道，模型不同、产物必然不同。回退只能保证"链路继续"，不能保证"结果等价"。修正：结果里记录 `channel` 与 `fallback_reason`，并在最终产物里标注"本阶段由 API 通道完成"，避免事后误判为同一基线。

### S3 缺成本与时长上限

五阶段串行 + 重试，真实付费工具下很容易失控。建议每阶段配置 `max_tokens`、`max_duration`、`max_attempts`，并在 run 级设总预算；超限即 `rejected` 而不是继续重试。

### S4 依赖与"离线可测"目标不一致

现状：pytest / pyyaml / pydantic 均未安装（pip 可用、网络可达）。"pytest 离线钉死"当前不成立。

两个可执行方案：
- 方案 A（要联网一次）：`pip install pytest pyyaml pydantic` + `requirements-dev.txt` 锁版本。
- 方案 B（真离线，推荐）：校验层用 `dataclass` + 手写校验函数替代 pydantic，配置用 `tomllib`（3.11+ 内置）或 JSON 替代 YAML。这样 `pytest` 是唯一外部依赖，甚至可退回 `unittest` 做到零依赖。

原方案写 YAML+pydantic，会与"可离线测试"的目标互相拖累。

## 四、验证覆盖缺口（必须补用例）

现有清单（配置校验、状态机/续跑、fixture 解析、重试回退、mock e2e）方向对，但缺这些真正会出事的场景：

1. 输入指纹变化 → 下游必须 stale（对应 B1）。
2. 同阶段两个进程同时跑 → 必须有租约/锁，只允许一个写（对应 B4）。
3. 审批通过时产物已被改动 → 必须拒绝放行（对应 M6）。
4. 下游 worktree 能看到上游 commit，但看不到未提交改动（对应 B3）。
5. CLI 退出 0 但 `changed_files` 为空 → 不得判完成（对应 M1）。
6. 子进程 env 不含 API Key 的断言（对应 M4）。
7. 输出含中文 + ANSI 颜色码 + 超长截断 → 解析不崩（现有清单未提）。
8. 只读文件系统/路径含空格与中文的 worktree 创建（Windows 上必踩）。

## 五、范围建议

MVP 建议砍两项：**回退（B2）** 与 **CLI→API 自动回退（S2）**，留到 v2。

理由：这两项是失败模式最多、最难测、也最容易做出"看起来能用"的部分；先保证"串行 + 断点续跑 + 审批 + 双 CLI + 纯 API demo"四条主线在异常场景下行为正确，收益远大于多两个功能开关。

其余判断支持：并行留到后期（并行会立刻引出 worktree 产物合并冲突，串行是正确选择）；WorkBuddyAI 不纳入正确，README 里写明"未来若提供 CLI，Adapter 契约不变即可接入"；解析失败降级原始文本正确，但要同时标 `unknown`，禁止据此判完成。

## 附：本次评审用到的环境事实

- `codex / qwen / doubao / agy / qoder / dsh / git` 均不在 PATH（本机 PowerShell 探测）。
- Python 3.13.16，pip 26.1.2；`pytest`、`yaml`、`pydantic` 导入失败。
- `dashscope.aliyuncs.com:443` 与 `ark.cn-beijing.volces.com:443` TCP 可达。
- 以上仅为本机结论，目标机器需由 preflight 重新探测并落盘。
