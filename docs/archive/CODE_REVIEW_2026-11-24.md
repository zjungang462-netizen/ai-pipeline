# ai-pipeline 代码审查报告

**审查对象**：`D:\work\ai-pipeline\agent-orchestrator`，核心包 `orchestrator`（约 15,457 行 Python，85 个 `test_*.py`），以及 dashboard 前端静态脚本。

**审查日期**：2026-11-24  
**审查环境**：Windows 本机，Python 3.13.18，Git 2.45.1.windows.1  
**审查方式**：全项目静态阅读 + 针对性运行验证；审查开始前 `agent-orchestrator` 工作树为空，未修改任何被审代码，仅新增本报告与 findings JSON。

**测试结果**：`python -m unittest discover -s tests -q` → `Ran 771 tests in 687.326s` / `OK`。

---

## 一、总体判断

这个项目的工程质量高于一般“多智能体 demo”：

- attempt 目录、`inputs_hash + outputs_hash` 双哈希、`success.json`/`decision.json` 分离，续跑与证据校验思路完整；
- Git worktree 隔离、按清单提交、基线 commit、失败候选回收，说明作者认真处理过真实返工场景；
- 子进程环境白名单、路径穿越/符号链接/`.git`/Windows 设备名校验、工作台 Host/Origin/CSRF/会话令牌/SSE 上限、远程 Worker HMAC 时间戳、CSV 公式注入转义，安全基线较好；
- 未发现 `shell=True`、`os.system`、`eval`、`pickle`/不安全 YAML 加载；前端基本使用 `textContent`，未发现明显 `innerHTML` XSS；
- 771 个 unittest 全部通过，说明现有主路径有较好的回归覆盖。

但仍有几个会直接让流水线在真实环境停摆或在边界条件下留下残留进程/错误状态的问题。下面按优先级列出，P1/P2 建议先修，P3 属于加固或低频边界。

---

## 二、问题清单（按优先级）

### CR-01 P1｜Windows 非 Git 工作区的基线提交会因 argv 过长失败

**位置**：`orchestrator/artifacts.py:281-299`（`commit_files`），`:166-174`（`ensure_repo`），`orchestrator/collaboration/config_copy.py:73-76`（`derive`）

**证据**：`commit_files` 把全部文件路径直接展开进 argv：

```python
_git(["add", "--", *files], ...)
_git(["diff", "--cached", "--name-only", "--", *files], ...)
_git(["commit", "-q", "--only", "-m", message, "--", *files], ...)
```

`ensure_repo` 在首次初始化非 Git 工作区时，把 `workspace_files()` 找到的**全部文件**传给 `commit_files`；`config_copy.derive` 也会把派生快照的全部文件传入。Windows `CreateProcess` 的命令行上限约为 32767 字符，路径一多就会触发 `WinError 206`。

**复现**：在临时目录创建 2000 个 `src/directoryN/file_XXXX.txt`，调用 `artifacts.ensure_repo(root)`，在 `artifacts.py:292` 的 `git add` 处稳定抛出：

```text
FileNotFoundError [WinError 206] 文件名或扩展名太长。
```

**影响**：非 Git 的大型工作区无法进入 code 阶段；从大型父任务派生子任务时，`derive` 也会在提取后失败。Linux/macOS 同样存在 `E2BIG` 风险。

**建议**：不要用参数数组承载全量路径。改为 `git add --pathspec-from-file=<临时文件> --pathspec-file-nul`，或对首次基线使用受控的 `git add -A -- .`（`ensure_repo` 本来就是“把工作区现有文件作为基线”的语义）。`diff --cached` 与 `commit --only` 同样要改成 pathspec 文件/分批。补一个 Windows 多文件回归测试。

---

### CR-02 P1｜用户开启 Git 提交签名时，引擎提交没有禁用签名

**位置**：`orchestrator/artifacts.py:119-136`（`_git`）；对照 `orchestrator/arena.py:127`

**证据**：`_git` 只注入了 `core.hooksPath` 来禁用 hook，**没有**设置 `commit.gpgsign=false`。如果用户的全局或仓库配置里有 `commit.gpgsign = true`，引擎的 `git commit` 会尝试签名；而 `_git` 的 `build_subprocess_env()` 又会剥离 `GNUPGHOME`、`SSH_AUTH_SOCK` 等签名所需环境，最终失败或交互挂起。

**复现**：临时仓库中设置 `commit.gpgsign=true`、`gpg.program=definitely-not-gpg`，修改文件后调用 `commit_files`，得到：

```text
GitError: git commit -q --only -m signed -- a.txt 失败:
error: cannot spawn definitely-not-gpg: No such file or directory
```

同一项目的 `arena.py:127` 已经显式使用 `-c commit.gpgsign=false`，说明这里属于遗漏，而不是设计选择。

**影响**：所有 code 阶段的提交、baseline 提交、worktree 合并都可能失败；在启用签名的企业环境中是直接阻断。

**建议**：在 `artifacts._git` 的公共命令前缀中统一加入 `-c commit.gpgsign=false`（或 `GIT_CONFIG_COUNT` 方式），并在 `ensure_repo`/`commit_files` 路径补测试。若未来要保留用户签名，应单独提供显式开关，而不是继承用户全局配置。

---

### CR-03 P2｜adapter options 没有 schema，拼错的键会被静默忽略

**位置**：`orchestrator/config.py:230-241`，`orchestrator/adapters/factory.py:13-24`，`orchestrator/doctor.py:100-111`

**证据**：`parse_config` 对 adapter 只校验 `type` 与 `openai_compat` 的 `base_url`/`model`/`api_key_env` 是否存在；其余 options 原样放进 `options = {k: v for k, v in opts.items() if k != "type"}`。`factory.build_adapter` 用 `.get` 读取已知键，未知键不会报错，拼错 `max_tokens` 为 `max_token` 会被静默忽略；`temperature` 传字符串可以过配置加载，直到构建适配器时才抛 `ValueError`。这与 `config.py:190` 注释“未知键必须明确拒绝，避免限额无声失效”直接矛盾。

**复现**：临时配置写入 `temperature = "not-a-number"`、`max_token = 123`：

```text
config loaded; options= {'temperature': 'not-a-number', 'max_token': 123, ...}
build_adapter failed: ValueError could not convert string to float: 'not-a-number'
```

只写 `max_token` 时则连 `build_adapter` 都不会报错，适配器得到的 `max_tokens` 为 `None`，实际退回到 `autonomy.output_tokens`/阶段额度；用户专门配置的上限被静默忽略。

**影响**：配置上限可能被无声忽略，错误值在启动或运行期才失败；`base_url` 的 `file://`、带 userinfo 的 URL 在直接加载配置时也不会被 `parse_config` 拒绝（`doctor` 和工作台注册各自校验，入口不一致）。

**建议**：为每种 adapter type 建立 options schema（允许键、类型、范围、枚举、URL scheme/host/无凭据），在 `parse_config` 层统一拒绝未知键和非法值，并让 `doctor`/`model_settings`/工作台复用同一份 schema，避免三套校验漂移。

---

### CR-04 P2｜测试/部署的进程树清理不是强保证

**位置**：`orchestrator/testrun.py:82-103`，`orchestrator/deploy.py:129-154`，`orchestrator/supervisor.py:220-235`

**证据**：

- `testrun` 超时后 Windows 先 `taskkill /PID /T /F`，失败则只 `proc.kill()` 杀直接子进程；POSIX `os.killpg` 只捕获 `ProcessLookupError`，其他 `OSError` 可能直接冒出；`proc.wait(timeout=5)` 超时也不会再确认后代是否退出。
- `deploy.execute_deploy` 使用 `subprocess.run(..., timeout=...)`，Python 超时只保证杀掉直接进程，不保证其子进程/孙进程退出。
- `supervisor._terminate` 同样以 `taskkill` + `killpg` 为主，失败路径没有二次校验。

**影响**：测试或部署命令若启动守护进程、子 shell、端口服务，超时后可能继续持有端口、文件锁或写工作区；后续阶段会莫名失败，且需要人工清理。

**建议**：把 `process_tree` 的 Windows Job Object 能力复用到 test/deploy/supervisor；POSIX 统一 `start_new_session=True` + `killpg`，并捕获所有 `OSError`；超时后做“进程树是否为空”的校验，记录未退出的 PID，必要时返回明确的 `orphaned_processes` 诊断。

---

### CR-05 P2｜`StageLock.stale_s` 实际未使用，PID 复用会让锁永久有效

**位置**：`orchestrator/state_io.py:111-132`；`orchestrator/maintenance.py:102`

**证据**：`StageLock.__init__` 保存了 `self.stale_s`，文档也写“持有进程已经不在时立即回收，不等 24 小时”，但 `_content_stale()` 只调用 `_pid_alive(data.get("pid"))`，**从未读取 `stale_s`**。如果原持有进程已死亡，而 PID 被系统复用给一个无关的存活进程，`_pid_alive` 会一直返回真，锁就被视为永久有效。`maintenance._run_actions` 进一步只判断 `.run.lock`/`.supervisor.lock`/`.control.lock` 文件是否存在，不检查 PID，因此清理也会被卡住。

**影响**：阶段/运行出现永久 `lock_busy`，用户只能手工删锁；长期运行的 Windows 机器上 PID 复用并不罕见。

**建议**：锁内容增加进程启动时间/随机 token 与创建时间；`_content_stale` 同时按 `stale_s` 和 PID 身份判断；`maintenance` 对锁做 PID 探测而不是只看文件存在；补一个“锁文件存在但持有 PID 已复用”的回归测试。

---

### CR-06 P3｜`Service.active` / `active_topics` 只增不减，长期运行会累积进程对象

**位置**：`orchestrator/collaboration/service.py:289-307`，`:459-481`

**证据**：`launch` 只检查 `self.active[key].poll() is None`，完成后不 `pop`；`launch_discussion` 对 `active_topics` 同样只写不删。`close` 也没有清理这两个映射。

**影响**：工作台长时间运行时，每个已完成运行/讨论都保留一个 `Popen` 对象及其引用；属于缓慢的内存/句柄增长，不影响单次结果但会影响长期稳定性。

**建议**：在 `launch` 入口及健康检查/后台同步中清理 `poll() is not None` 的条目；使用有界字典或 `weakref`；讨论结束后同样移除。

---

### CR-07 P3｜上游报告/提示词文件先整文件读入内存，再截断

**位置**：`orchestrator/prompt_context.py:30-32`，`:54-60`

**证据**：

```python
return p.read_text(encoding="utf-8")
...
content = rf.read_text(encoding="utf-8", errors="replace")
if len(content) > _UPSTREAM_REPORT_LIMIT:
    content = content[:_UPSTREAM_REPORT_LIMIT] + ...
```

截断发生在完整读取之后。一个由 CLI/模型生成的超大 `design.md`、`review.md` 或 prompt 文件会先完整进入内存，再被截断；文件多大就分配多大。

**影响**：大报告可能造成内存峰值、拖慢 prompt 组装，严重时 worker 被 OOM 杀掉。

**建议**：`read(_UPSTREAM_REPORT_LIMIT + 1)` 或按字节流读取；`_read_prompt` 也加文件大小上限（例如 2 MiB），超限直接报配置错误，而不是静默截断。

---

### CR-08 P3｜`openai_compat` 非流式响应没有读取上限

**位置**：`orchestrator/adapters/openai_compat.py:173-178`

**证据**：非流式路径直接 `body = resp.read().decode("utf-8")`；流式路径有 SSE 行/字节上限，但非流式没有对应上限，之后还会 `json.loads(body)` 并保留 body 用于 trace/错误。

**影响**：异常端点或错误代理返回超大响应时，单次调用可能耗尽内存；错误 body 还会被写进 attempt trace。

**建议**：`resp.read(_MAX_BODY_BYTES + 1)`，超过上限返回 `PARSE_FAILED`/`RESPONSE_TOO_LARGE`；同时限制写入 trace 的 body 长度。

---

### CR-09 P3｜畸形 state 只校验到 `stages` 容器，单个 stage 值仍会让列表接口 500

**位置**：`orchestrator/collaboration/projection.py:64-76`，`:113`，`:148`；`orchestrator/collaboration/run_listing.py:155`

**证据**：`_check_state_shape` 只检查 `stages` 是 dict，不检查每个 stage 的值必须是 dict；`lifecycle` 随后执行 `s.get('status')`，`SummaryCache.read` 只捕获 `ValueError`，不捕获 `AttributeError`。`project` 也直接 `state.get('stages', {}).items()` 并访问 stage 字段。

**触发**：手工编辑、旧版本迁移或外部写入导致 `{"stages": {"review": "done"}}` 这类 JSON 合法但结构错误的状态；`/api/runs` 整页会因一个坏 run 返回 500，房间快照也可能失败。

**建议**：在 `_check_state_shape` 中递归校验每个 stage 的 `status`、`current_attempt`、`inputs_hash` 等字段类型；在 `SummaryCache`/`run_listing` 边界同时捕获 `AttributeError`/`TypeError`，降级为 `unknown`，不要让单条坏状态影响整页。

---

### CR-10 P3｜`apply_raw_updates` 回滚可能二次失败并掩盖原始错误

**位置**：`orchestrator/adapters/workspace_sync.py:82-89`

**证据**：

```python
except OSError:
    for key in applied:
        replacer(relative_target(workdir, key), original[key])
    raise
```

回滚路径自身再次调用 `replacer`；如果磁盘满、权限变化或目标被占用，回滚再次抛 `OSError`，会覆盖原始错误，且只恢复了一部分文件。若 update 列表包含 `editable_files` 中原本不存在的新文件，`original[key]` 还会抛 `KeyError`。

**影响**：远程 Worker 的文件回写失败时，工作区可能停留在半更新状态，调用方看到的错误也不是最初原因。

**建议**：写入前把所有原始内容（含“不存在”标记）收集完整；回滚使用 best-effort 并聚合错误，最终抛出包含原始异常和回滚异常的复合错误；优先 temp 文件 + 批量原子替换。

---

### CR-11 P3｜Windows 设备名可作为项目标识

**位置**：`orchestrator/collaboration/project_manager.py:16`，`:235`，`:276-277`

**证据**：`_PROJECT_NAME_RE = r'^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$'` 允许 `CON`、`PRN`、`NUL`、`COM1`、`LPT1` 等 Windows 保留设备名；随后会生成 `managed_projects/CON.toml` 与 `managed_projects/state-CON`。Windows 上这些路径可能被解析为设备，导致创建失败或出现异常文件。

**影响**：Windows 工作台注册项目时失败；错误信息可能不直观。

**建议**：复用 `safety` 中已有的 Windows 保留名判断，拒绝 `CON`/`PRN`/`AUX`/`NUL`/`COM1-9`/`LPT1-9` 以及结尾的点/空格；补 Windows 单测。

---

### CR-12 P3｜`config_copy.derive` 复用已存在的派生工作区，可能带进旧文件

**位置**：`orchestrator/collaboration/config_copy.py:55-76`

**证据**：`workspace.mkdir(parents=True, exist_ok=True)` 后直接解压新的 `source.zip`，没有清理目录。`run_id` 是确定性的，如果上一次派生在解压后、写 job 前失败，下一次同 idem 重试会复用含有旧文件的目录；随后 `ensure_repo` 会把旧文件一并作为基线提交，派生源码不再等于父 commit。另有一个纯性能问题：`ensure_repo` 已经提交了全部文件，紧接着的 `commit_files(inputs)` 通常无改动，却会再次扫描全工作区。

**影响**：派生任务的代码基线可能包含父 commit 中不存在的旧文件，破坏可复现性。

**建议**：在临时目录解压并校验后再原子 rename 为 `workspace`；或写入 `.derived-owned` 标记并只清理自己创建的目录；`ensure_repo` 后跳过重复 `commit_files`。

---

### CR-13 P3｜`Service.snapshot` 对 `job.max_duration_s` 为 `null` 没有防御

**位置**：`orchestrator/collaboration/service.py:151`，`:181`

**证据**：`deadline = job.get('started_at', 0) + job.get('max_duration_s', 0)`；如果旧格式/外部导入的 `job.json` 中 `max_duration_s` 是 `null`，`0 + None` 会抛 `TypeError`，房间快照/接口 500。`job.get('config_hash')` 对 `job` 本身的形状也没有独立防线（`_check_state_shape` 只校验 job 是 dict）。

**影响**：单条 legacy/损坏 job 可能让对应房间无法打开。

**建议**：统一数值归一化函数（`None`/非数 → 0 或 `not_recorded`），并把 `config_integrity` 的 `job` 字段访问放进显式形状校验。

---

### CR-14 P3｜`preflight_cli` 用默认环境执行版本探测，潜在泄露全部 API Key

**位置**：`orchestrator/safety.py:221-244`；白名单实现 `:180-194`

**证据**：`subprocess.run([exe, *version_args], ...)` 没有传 `env=`，默认继承完整 `os.environ`，包括 `OPENAI_API_KEY`、`DEEPSEEK_API_KEY` 等；而项目在 `build_subprocess_env` 中明确做了白名单。当前内置 CLI 适配器都覆盖了 `preflight` 并使用了白名单，所以这条路径目前主要是潜在风险，但它违反了项目自述的 M4 约束，一旦新适配器忘覆盖就会暴露。

**影响**：版本探测命令或第三方 CLI 可能看到与自身无关的凭据；若 CLI 被替换/被污染，存在外泄面。

**建议**：`env=build_subprocess_env()`；如果这个函数确实没有调用方，直接删除或改为仅内部使用；补一个设置假密钥环境变量并断言子进程看不到的测试。

---

### CR-15 P3｜远程 Worker 的幂等缓存只记 job_id，重试会永久 409

**位置**：`orchestrator/remote_worker.py:260-273`（服务端），客户端 `:85-130`

**证据**：服务端在调用 `_executor` **之前**就把 `job_id` 写入 `_seen_jobs`；如果 executor 抛 `ValueError/KeyError/TypeError`，job_id 仍留在集合中，客户端重试同一 `job_id` 会一直收到 `409 duplicate_job_id`。即使第一次执行成功但响应在网络中丢失，重试也只会得到 409，客户端拿不到 receipt。当前 `RemoteWorkerServer` 的 executor 还是 `_reject_unconfigured`（功能未真正接入 Engine），所以这是协议层/未来路径问题。

**影响**：远程任务一旦遇到一次异常或回执丢失，就无法通过重试恢复，只能重启 Worker/换 job_id。

**建议**：按 `job_id` 缓存完整 receipt（成功结果或失败原因），重复请求返回相同 receipt；只在执行有明确结果后标记“已处理”；设置 TTL/容量上限；客户端把 409 当作可查询回执而不是永久失败。

---

## 三、建议修复顺序

1. **第一批（当天可做）**：CR-01 argv 超限、CR-02 提交签名、CR-03 adapter schema。三处都在配置/启动路径，改动集中，能直接消除两类环境阻断和一类配置静默失效。
2. **第二批（下一轮迭代）**：CR-04 进程树清理、CR-05 StageLock 身份校验。先把残留进程和永久锁的运维风险收掉。
3. **第三批（按需加固）**：其余 P3。优先做 CR-09 畸形状态降级与 CR-07/CR-08 有界读取，因为这两个直接影响工作台可用性和 worker 内存。

---

## 四、可优化项（非缺陷，但值得做）

1. **adapter schema 单一来源**：把 CR-03 的 options schema 同时供 `parse_config`、`doctor`、`model_settings`、工作台注册使用，避免“配置能加载、doctor 报错、运行时才炸”的三套逻辑漂移。
2. **Git argv 与签名统一封装**：把 `git add`/`commit`/`diff` 的 pathspec 文件和 `commit.gpgsign=false` 收敛到 `artifacts._git_commit` 一处，避免 `arena`/`artifacts` 两套 Git 行为再次分叉。
3. **进程管理复用**：把 `process_tree` 的 Job Object 封装提供给 `testrun`、`deploy`、`supervisor`，统一超时/取消/残留诊断，而不是各自维护 `taskkill`/`killpg` 分支。
4. **有界读取**：`prompt_context`、`openai_compat` 非流式、`read_json`/`artifact` 预览统一使用“读取上限 + 超限失败”，避免大文件/大响应造成内存峰值。
5. **投影性能**：`projection.artifact_list` 每次请求 glob 所有 attempt；可改为按 stage/attempt 的 mtime 增量缓存，至少避免在 `/api/runs` 与房间打开时重复全扫。
6. **事件日志增长**：`state.events` 随运行轮次增长，`metrics` 又依赖完整事件；建议按阶段/轮次聚合后压缩旧事件，保留审计所需的最小摘要。
7. **状态形状校验**：在 `RunState.load` 或 `projection._check_state_shape` 做一次完整 stage/usage/approval 结构校验，坏状态返回 400/unknown，而不是在 UI 层异常。
8. **测试矩阵**：增加 Windows 专属用例：多文件 argv 长度、`commit.gpgsign`、`CON` 项目名、PID 复用、超时进程树、null `max_duration_s`、畸形 stage 值、派生目录重试。

---

## 五、验证记录

### 已完成的验证

- `python -m unittest discover -s tests -q`：771 个用例全部通过，耗时 687.326s。
- Windows argv 超限：2000 个文件触发 `FileNotFoundError [WinError 206]`（`artifacts.py:292`）。
- Git 签名：临时仓库 `commit.gpgsign=true` + 无效 `gpg.program`，`commit_files` 抛 `GitError ... cannot spawn ...`。
- adapter options：`temperature="not-a-number"` + `max_token=123` 可通过 `load_config`，构建适配器时才失败；`max_token` 拼写错误被静默忽略。
- 被审代码未修改；本次只新增报告文件。

### 未覆盖/限制

- 未对 `docs/`、示例 TOML、全部前端交互做逐行审阅；前端只做了 XSS/路径相关抽检。
- 未做依赖供应链审计（项目主要使用标准库，但没有锁文件/依赖清单可审）。
- 未对远程 Worker 的真实执行链路做端到端压测（当前 executor 未接入）。
- 全量测试通过不等于上述 Windows/签名/畸形状态边界已覆盖；建议把第五节列出的复现脚本转成正式测试。

---

## 六、缺陷 JSON 数组

以下数组按优先级排序，最多 15 条；`status` 为 `reproduced` 的条目已在本机验证，`static` 为静态代码确认，`latent`/`protocol` 为当前未触发或协议层问题。

```json
[
  {
    "id": "CR-01",
    "priority": "P1",
    "title": "Windows 非 Git 工作区基线提交因 argv 过长失败",
    "type": "bug",
    "status": "reproduced",
    "confidence": "high",
    "locations": ["orchestrator/artifacts.py:281-299", "orchestrator/artifacts.py:166-174", "orchestrator/collaboration/config_copy.py:73-76"],
    "trigger": "首次初始化含大量文件的非 Git 工作区，或派生大型快照；git add/diff/commit 的路径参数超过 Windows 32767 字符命令行限制",
    "impact": "code 阶段在 prepare 阶段失败；Linux/macOS 存在 E2BIG 风险",
    "recommendation": "使用 git --pathspec-from-file --pathspec-file-nul 或受控的 add -A；补 Windows 多文件回归测试"
  },
  {
    "id": "CR-02",
    "priority": "P1",
    "title": "引擎 Git 提交未禁用 commit.gpgsign",
    "type": "bug",
    "status": "reproduced",
    "confidence": "high",
    "locations": ["orchestrator/artifacts.py:119-136", "orchestrator/arena.py:127"],
    "trigger": "用户全局/仓库配置 commit.gpgsign=true，且签名程序或 agent 不可用",
    "impact": "baseline、code 阶段提交、worktree 合并全部失败或挂起",
    "recommendation": "在引擎提交前缀统一加 -c commit.gpgsign=false；与 arena 行为保持一致"
  },
  {
    "id": "CR-03",
    "priority": "P2",
    "title": "adapter options 缺少 schema，未知键和错误类型不报错",
    "type": "bug",
    "status": "reproduced",
    "confidence": "high",
    "locations": ["orchestrator/config.py:230-241", "orchestrator/adapters/factory.py:13-24", "orchestrator/doctor.py:100-111"],
    "trigger": "拼错 max_tokens，或 temperature/max_tokens/timeout_s 传错类型，或 base_url 使用 file://、带凭据 URL",
    "impact": "用户配置的 adapter 上限被静默忽略，错误值可能到运行期才失败；配置入口校验不一致",
    "recommendation": "按 adapter type 建立 options schema，统一 parse_config/doctor/model_settings/工作台校验"
  },
  {
    "id": "CR-04",
    "priority": "P2",
    "title": "测试/部署超时进程树清理不是强保证",
    "type": "bug",
    "status": "static",
    "confidence": "high",
    "locations": ["orchestrator/testrun.py:82-103", "orchestrator/deploy.py:129-154", "orchestrator/supervisor.py:220-235"],
    "trigger": "测试或部署命令启动守护进程/子进程后超时，taskkill 失败或 POSIX killpg 权限错误",
    "impact": "后代进程继续持有端口/文件/工作区，后续阶段失败且需人工清理",
    "recommendation": "复用 process_tree Job Object，统一 start_new_session/killpg；超时后校验进程树为空并记录 orphaned_pids"
  },
  {
    "id": "CR-05",
    "priority": "P2",
    "title": "StageLock.stale_s 未使用，PID 复用导致永久锁",
    "type": "robustness",
    "status": "static",
    "confidence": "high",
    "locations": ["orchestrator/state_io.py:111-132", "orchestrator/maintenance.py:102"],
    "trigger": "持锁进程死亡后 PID 被无关存活进程复用；或 maintenance 只看到锁文件存在",
    "impact": "阶段/运行永久 lock_busy，清理被阻塞，只能手工删锁",
    "recommendation": "锁记录进程启动时间/token，按 stale_s 与 PID 身份共同判断；maintenance 探测 PID 而不是只看文件"
  },
  {
    "id": "CR-06",
    "priority": "P3",
    "title": "Service.active/active_topics 只增不减",
    "type": "resource-leak",
    "status": "static",
    "confidence": "high",
    "locations": ["orchestrator/collaboration/service.py:289-307", "orchestrator/collaboration/service.py:459-481"],
    "trigger": "工作台长期运行并完成多个运行/讨论",
    "impact": "Popen 对象及引用缓慢累积，内存/句柄增长",
    "recommendation": "完成时 pop，健康检查/后台同步周期清理，使用有界映射"
  },
  {
    "id": "CR-07",
    "priority": "P3",
    "title": "上游报告和 prompt 文件先全量读入再截断",
    "type": "resource",
    "status": "static",
    "confidence": "high",
    "locations": ["orchestrator/prompt_context.py:30-32", "orchestrator/prompt_context.py:54-60"],
    "trigger": "CLI/模型生成超大 design.md/review.md 或 prompt 文件",
    "impact": "内存峰值、prompt 组装变慢，严重时 OOM",
    "recommendation": "read(limit+1)，prompt 文件设硬上限并报配置错误"
  },
  {
    "id": "CR-08",
    "priority": "P3",
    "title": "openai_compat 非流式响应读取无上限",
    "type": "resource",
    "status": "static",
    "confidence": "high",
    "locations": ["orchestrator/adapters/openai_compat.py:173-178"],
    "trigger": "端点/代理返回超大 JSON 响应",
    "impact": "单次调用内存耗尽；错误 body 写入 trace",
    "recommendation": "resp.read(MAX+1)，超限返回 PARSE_FAILED，并限制 trace body"
  },
  {
    "id": "CR-09",
    "priority": "P3",
    "title": "畸形 state 未校验单个 stage 值，列表接口可能 500",
    "type": "robustness",
    "status": "static",
    "confidence": "high",
    "locations": ["orchestrator/collaboration/projection.py:64-76", "orchestrator/collaboration/projection.py:113", "orchestrator/collaboration/projection.py:148", "orchestrator/collaboration/run_listing.py:155"],
    "trigger": "JSON 合法但 stages 的值为字符串/数组等非 dict",
    "impact": "一个坏 run 导致 /api/runs 整页 500，房间快照失败",
    "recommendation": "递归校验每个 stage 字段；SummaryCache/run_listing 捕获 AttributeError/TypeError 并降级 unknown"
  },
  {
    "id": "CR-10",
    "priority": "P3",
    "title": "workspace_sync 回滚可能二次失败并掩盖原错误",
    "type": "robustness",
    "status": "static",
    "confidence": "high",
    "locations": ["orchestrator/adapters/workspace_sync.py:82-89"],
    "trigger": "远程文件回写过程中 replacer 失败，回滚再次失败或 original 缺少新文件 key",
    "impact": "工作区半更新，调用方看到错误的异常",
    "recommendation": "记录不存在标记，best-effort 回滚并聚合异常；优先 temp+批量原子替换"
  },
  {
    "id": "CR-11",
    "priority": "P3",
    "title": "Windows 设备名可作为项目标识",
    "type": "bug",
    "status": "static",
    "confidence": "high",
    "locations": ["orchestrator/collaboration/project_manager.py:16", "orchestrator/collaboration/project_manager.py:235", "orchestrator/collaboration/project_manager.py:276-277"],
    "trigger": "项目名使用 CON/PRN/NUL/COM1/LPT1 等 Windows 保留名",
    "impact": "managed_projects 下配置/状态路径创建失败或出现特殊文件",
    "recommendation": "复用 safety 保留名判断并拒绝 Windows 设备名/结尾点空格"
  },
  {
    "id": "CR-12",
    "priority": "P3",
    "title": "config_copy.derive 复用已存在的派生工作区",
    "type": "bug",
    "status": "static",
    "confidence": "medium",
    "locations": ["orchestrator/collaboration/config_copy.py:55-76"],
    "trigger": "同一确定性 run_id 的派生任务在上次解压后失败并重试",
    "impact": "派生源码带入父 commit 中不存在的旧文件，破坏可复现性；重复扫描开销",
    "recommendation": "临时目录解压校验后原子 rename，或只清理自有目录；跳过 ensure_repo 后的重复 commit_files"
  },
  {
    "id": "CR-13",
    "priority": "P3",
    "title": "Service.snapshot 对 job.max_duration_s=null 无防御",
    "type": "robustness",
    "status": "static",
    "confidence": "medium",
    "locations": ["orchestrator/collaboration/service.py:151", "orchestrator/collaboration/service.py:181"],
    "trigger": "legacy/外部导入 job.json 的 started_at/max_duration_s 为 null 或缺失",
    "impact": "房间快照 TypeError，接口 500",
    "recommendation": "统一数值归一化，None 视为 0/not_recorded，并纳入 state shape 校验"
  },
  {
    "id": "CR-14",
    "priority": "P3",
    "title": "preflight_cli 版本探测继承完整环境，潜在泄露 API Key",
    "type": "security",
    "status": "latent",
    "confidence": "high",
    "locations": ["orchestrator/safety.py:221-244", "orchestrator/safety.py:180-194"],
    "trigger": "新增 CLI adapter 未覆盖 preflight，或直接调用 preflight_cli",
    "impact": "版本探测/第三方 CLI 可读取全部 API key/token，违反环境白名单约束",
    "recommendation": "传入 build_subprocess_env()；删除未使用路径或补回归测试"
  },
  {
    "id": "CR-15",
    "priority": "P3",
    "title": "远程 Worker 幂等缓存仅记 job_id，重试永久 409",
    "type": "protocol",
    "status": "protocol",
    "confidence": "high",
    "locations": ["orchestrator/remote_worker.py:260-273", "orchestrator/remote_worker.py:85-130"],
    "trigger": "executor 抛 ValueError/KeyError/TypeError，或首次执行成功但回执丢失后重试",
    "impact": "客户端无法拿到结果，任务不可恢复，只能重启 Worker/换 job_id",
    "recommendation": "按 job_id 缓存完整 receipt/错误，重复请求返回同一结果；只在有结果后标记已处理；加 TTL"
  }
]
```
