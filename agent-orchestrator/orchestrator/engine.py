"""编排引擎（评审 B1/B2/B4/M6/S3）。

- 串行阶段状态机；完成判据 = (inputs_hash, outputs_hash)。
- inputs_hash 变化 => 该阶段及下游 stale。
- attempt 隔离 + 成功后原子切 current；租约锁防双进程。
- 审批持久化 outputs_hash，恢复时校验。
- 预算超限 => rejected。无回退（MVP），rerun-from 重跑覆盖。
"""
from __future__ import annotations

import json
import os
import shutil
import time
import threading
import re
import uuid
from dataclasses import asdict
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from . import artifacts
from .config import Config
from .contracts import ErrorKind, Result, RunContext, StageSpec, StageStatus
from .state import RunState, StageLock, atomic_write_json, compute_inputs_hash, sha256_text
from .safety import WriteRootGuard, WriteRootViolation, relative_target
from .adapters.base import Adapter
from .autonomy import AutonomousControls


# G6：单个上游报告注入 prompt 的字符上限（超出截断并标注全文路径）
_UPSTREAM_REPORT_LIMIT = 20000


class BudgetExceeded(RuntimeError):
    pass


class StageArchiveError(RuntimeError):
    """产物归档/合并失败 => 阶段级失败，可重试，不中断整轮。"""


class Engine(AutonomousControls):
    def __init__(self, config: Config, adapters: dict[str, Adapter],
                 run_id: str, task: str,
                 adapter_factory: Callable[[str], Adapter] | None = None,
                 control=None):
        self.config = config
        self.adapters = adapters
        self.run_id = run_id
        self.task = task
        self.state = RunState(config.state_dir, run_id)
        self.state.task = task
        self._adapter_factory = adapter_factory
        self.control = control
        self._owned = False
        self._owner_thread = None
        self._prompts: dict[str, str] = {}
        self._reports: dict[str, list[Path]] = {}
        # 审查上下文覆盖不完整（diff 截断且无法注入全部变更文件）=> 记录未纳入清单，
        # pass 结论不得直接当作完成，只能交人工确认/在无人值守下失败。
        self._review_missing: dict[str, list[str]] = {}
        # G1：应用配置的 git 路径后再探测（PATH 无 git 时走兜底）
        artifacts.set_git_exe(config.git_exe)
        self._git_ready = artifacts.git_available()

    @contextmanager
    def _ownership(self):
        """先取得写入所有权，再读取状态，避免旧内存覆盖新状态。"""
        if self._owned and self._owner_thread == threading.get_ident():
            yield True
            return
        locks = [StageLock(self.state.run_dir / ".run.lock")]
        if any(s.kind in ("code", "test") for s in self.config.stages):
            ws = self.config.task_workdir
            locks.append(StageLock(ws.parent / f".{ws.name}.pipeline.lock"))
        held = []
        try:
            for lock in locks:
                if not lock.acquire():
                    yield False
                    return
                held.append(lock)
            self._owned = True
            self._owner_thread = threading.get_ident()
            yield True
        finally:
            if self._owner_thread == threading.get_ident():
                self._owned = False
                self._owner_thread = None
            for lock in reversed(held):
                lock.release()

    # ---- 初始化 / 续跑 ----
    def prepare(self) -> None:
        loaded = self.state.load()
        if not loaded:
            for s in self.config.stages:
                self.state.stage(s.name)
            self.state.save()
        else:
            # 续跑保留已持久化的 task；仅当本次显式传入非空 task 时才更新
            if self.task:
                self.state.task = self.task
            else:
                self.task = self.state.task
        has_cli=any(getattr(self.adapters.get(s.adapter),'channel','').startswith('cli_') for s in self.config.stages)
        if has_cli:
            from .recovery import config_hash
            identity={'task_hash':sha256_text(self.task),'config_hash':config_hash(self.config)}
            if self.state.control.get('cli_identity',identity)!=identity:
                raise ValueError('同一 CLI 运行不可更换已固定的任务或配置')
            self.state.control['cli_identity']=identity
            jobfile=self.state.run_dir/'job.json'
            deadline=self.state.control.get('cli_deadline')
            if jobfile.is_file():
                job=json.loads(jobfile.read_text(encoding='utf8'))
                if job.get('task_hash')!=sha256_text(self.task) or (self.config.source_hash and job.get('config_hash')!=self.config.source_hash):
                    raise ValueError('同一 CLI 运行不可更换已固定的任务或配置')
                candidate=job['started_at']+job['max_duration_s']
                deadline=min(deadline,candidate) if deadline is not None else candidate
            elif deadline is None and self.config.run_budget.max_duration_s is not None:
                started=min((e['ts'] for e in self.state.events if isinstance(e.get('ts'),(float,int))),default=time.time())
                deadline=started+self.config.run_budget.max_duration_s
            if deadline is not None:self.state.control['cli_deadline']=deadline
            trackers=self.state.autonomy.setdefault('cli_attempts',{})
            for spec in self.config.stages:
                if getattr(self.adapters.get(spec.adapter),'channel','').startswith('cli_') and spec.name not in trackers:
                    calls=sum(e['kind']=='model_call_started' and e.get('stage')==spec.name for e in self.state.events)
                    cycle=str(self.state.invalidate_nonce.get(spec.name,0))
                    trackers[spec.name]={'total':calls,'cycles':{cycle:calls}}
            self.state.save()
        if self.config.autonomy.enabled or has_cli:
            self._recover_pending_calls()
        # 启动扫描 interrupted（评审 B4）
        interrupted = self.state.scan_interrupted()
        for name in interrupted:
            self.state.log_event("interrupted", name)
        if interrupted:
            self.state.save()
        # 仅当存在代码阶段时才建 git 仓库；纯审查现有代码库不改动被审项目
        has_code = any(s.kind == "code" for s in self.config.stages)
        if self._git_ready and has_code:
            # 既有非 git 工作区首次初始化时把已有文件提交为基线，避免 coding
            # worktree 合并回主线时因 untracked 文件永久失败（P2-8）。
            artifacts.ensure_repo(self.config.task_workdir, exclude=self.config.state_dir)
            if self.config.autonomy.enabled and not loaded:
                self._bootstrap_declared_files()
            if not loaded:
                self.state.control['project_baseline'] = artifacts.head_sha(self.config.task_workdir)
                self.state.save()

    def _read_prompt(self, spec: StageSpec) -> str:
        p = Path(spec.prompt)
        if not p.is_absolute():
            p = self.config.base_dir / p
        if p.is_file():
            return p.read_text(encoding="utf-8")
        return spec.prompt  # 允许直接内联 prompt 文本

    def _upstream_context(self, index: int,
                          spec: StageSpec) -> tuple[str, list[Path]]:
        """收集上游产物内容（评审 B1）：报告全文 + 代码 diff。

        spec.kind=review 时代码给累积全量（修复 M0）；若 diff 被截断，再按
        commit 注入变更文件全文（有上限），确保默认 API 通道的审查模型不会
        对未注入的实现给 pass。仍无法完整注入时记录到 _review_missing。
        """
        current_kind = spec.kind
        parts: list[str] = []
        reports: list[Path] = []
        for up in self.config.stages[:index]:
            st = self.state.stage(up.name)
            cdir = self.state.current_attempt_dir(up.name)
            if cdir is None:
                continue
            # 报告类：读 attempt 目录里归档的报告文件
            for rf in sorted(cdir.glob("*.md")):
                content = rf.read_text(encoding="utf-8", errors="replace")
                # G6：上游全文注入需有上限，否则 prompt 无界膨胀、真跑烧钱
                if len(content) > _UPSTREAM_REPORT_LIMIT:
                    content = (content[:_UPSTREAM_REPORT_LIMIT]
                               + f"\n...（报告截断，原 {len(content)} 字符；"
                                 f"全文见 {rf}）")
                parts.append(f"### 上游[{up.name}] 报告 {rf.name}\n{content}")
                reports.append(rf)
            if up.kind == "test":
                record = cdir / "test_record.json"
                if record.is_file():
                    with record.open(encoding="utf-8") as stream:
                        content = stream.read(_UPSTREAM_REPORT_LIMIT)
                    parts.append(f"### 上游[{up.name}] 测试执行记录\n{content}")
                    reports.append(record)
            # 代码类：审查阶段给累积全量 diff（修复 M0：否则只看到末次提交，
            # 返工轮里会误判"实现代码缺失"）；其他阶段给末次变更摘要即可。
            if up.kind == "code" and st.commit_sha and self._git_ready:
                if current_kind == "review":
                    diff, total_lines, truncated = artifacts.cumulative_diff_info(
                        self.config.task_workdir, st.commit_sha,
                        base_sha=self.state.control.get('project_baseline'))
                    label = "代码全量（相对项目基线累积）"
                    if truncated:
                        changed = artifacts.changed_files_at(
                            self.config.task_workdir, st.commit_sha,
                            base_sha=self.state.control.get('project_baseline'))
                        body, missing = artifacts.review_file_context(
                            self.config.task_workdir, st.commit_sha, changed)
                        if missing:
                            self._review_missing[spec.name] = missing
                        if body:
                            diff = (f"{diff}\n\n...（累积 diff 超过 600 行已截断，"
                                    f"以下为按 commit 注入的变更文件全文，共 {len(changed)} 个文件）\n\n{body}")
                else:
                    diff = artifacts.diff_summary(self.config.task_workdir, st.commit_sha)
                    label = "代码变更"
                if diff:
                    parts.append(f"### 上游[{up.name}] {label}\n{diff}")
        # 审查现有代码库：无上游 code 阶段产物时，直接读工作区现有源码
        if current_kind == "review":
            has_upstream_code = any(
                u.kind == "code" and self.state.stage(u.name).commit_sha
                for u in self.config.stages[:index])
            if not has_upstream_code:
                existing = artifacts.existing_code_context(self.config.task_workdir)
                if existing:
                    parts.append(f"### 被审查代码库（工作区现有源码）\n{existing}")
        return "\n\n".join(parts), reports

    def _render_prompt(self, spec: StageSpec, index: int) -> str:
        """渲染模板：注入 task / upstream / feedback（评审 B1 + L2）。

        用 replace 避免误伤其他花括号。feedback 为该阶段收到的审查反馈文本。
        """
        template = self._read_prompt(spec)
        feedback = self._feedback(spec.name)
        recovery = self.state.autonomy.get('recovery_hints', {}).get(spec.name, '')
        if recovery:
            feedback = (feedback + '\n\n' + recovery).strip()
        # 返工轮（有反馈）+ 代码阶段 => 只给问题条目与相关文件，
        # 不重贴需求全文和累积 diff，省轮次与 token。
        if feedback and spec.kind == "code":
            upstream = self._rework_context(feedback)
            reports = []
        else:
            upstream, reports = self._upstream_context(index, spec)
        self._reports[spec.name] = reports
        task = self.task[:2000] if feedback and spec.kind == "code" else self.task
        values = {'task': task, 'upstream': upstream or '（无上游产物）',
                  'feedback': feedback or '（无审查反馈）'}
        # 单次替换，不再改写任务和源码中的字面占位符。
        rendered = re.sub(r'\{(task|upstream|feedback)\}', lambda m: values[m.group(1)], template)
        # 模板未含 {feedback} 占位符但有反馈时，追加到末尾，确保反馈不被丢弃
        if feedback and "{feedback}" not in template:
            rendered += "\n\n" + feedback
        cli = getattr(self.adapters.get(spec.adapter), 'channel', '').startswith('cli_')
        if spec.context_files and (spec.kind == 'code' or cli):
            label = '当前声明文件原文（只读上下文）' if cli else '当前文件原文（仅用于构造精确补丁）'
            rendered += '\n\n' + label + '：\n' + self._source_context(spec)
        if self.config.autonomy.enabled:
            if spec.kind == 'code':
                rendered += (f'\n\n执行约束：本轮最多修改 {self.config.autonomy.repair_batch_size} 个文件。'
                             f'只可修改 {self.config.autonomy.allowed_files}；不要修改测试或其他文件。')
                rendered += ('直接修改当前任务工作区的允许文件，必须产生真实文件变更；最后简述实际修改。'
                             if cli else '优先修复失败证据中最靠前的一类问题，输出 # patch: 路径和 edits JSON；'
                             '目标原文必须唯一匹配，不要输出解释。')
            elif spec.kind == 'review':
                rendered += ('\n\n请先给出唯一的 review_verdict.json 结论（verdict 与 issues），'
                             '随后至多 200 字说明，不输出分析过程。')
        return rendered

    def _rework_file_inventory(self, max_files: int = 160, max_chars: int = 6000,
                               max_directories: int = 256) -> str:
        """有上限的相对文件清单，不读取正文，不跟随目录链接或进入状态/依赖树。"""
        root = self.config.task_workdir.resolve()
        state_root = self.config.state_dir.resolve()
        excluded = state_root if state_root != root else state_root / 'runs'
        lines, count, size, visited, truncated = [], 0, 0, 0, False
        def ignore_error(error):
            nonlocal truncated
            truncated = True
        for directory, directories, files in os.walk(root, followlinks=False, onerror=ignore_error):
            visited += 1
            if visited > max_directories:
                truncated = True
                break
            kept = []
            for name in sorted(directories):
                if name.startswith('.') or name in artifacts.SKIP_DIRS:
                    continue
                path = Path(directory) / name
                try:
                    resolved = path.resolve()
                    if (path.is_symlink() or resolved != path.absolute() or not resolved.is_relative_to(root)
                            or (excluded.is_relative_to(root) and resolved.is_relative_to(excluded))):
                        continue
                except OSError:
                    truncated = True
                    continue
                kept.append(name)
            directories[:] = kept
            for name in sorted(files):
                if name.startswith('.'):
                    continue
                path = Path(directory) / name
                rel = path.relative_to(root).as_posix()
                try:
                    if path.is_symlink() or not relative_target(root, rel).is_file():
                        continue
                except (OSError, WriteRootViolation):
                    continue
                line = '- ' + json.dumps(rel, ensure_ascii=False)
                if count >= max_files or size + len(line) + 1 > max_chars:
                    truncated = True
                    break
                lines.append(line)
                count += 1
                size += len(line) + 1
            if truncated:
                break
        detail = '\n'.join(lines) if lines else '（未列出普通文件；不代表工作区为空）'
        if truncated:
            detail += '\n（清单已截断或存在不可读目录；未列出不代表文件不存在。）'
        return '### 当前工作区相对文件清单（只供定位，不授予写权限）\n' + detail

    def _rework_context(self, feedback: str, max_file_bytes: int = 8000) -> str:
        """轻量文件清单加被点名文件正文，保持结构可见并控制正文范围。"""
        import re
        names: list[str] = []
        for m in re.finditer(r"`([^`]+)`", feedback):
            names.append(m.group(1).strip())
        for m in re.finditer(r"([\w./\\-]+\.[A-Za-z0-9]+)\s*[:：]", feedback):
            names.append(m.group(1).strip())
        parts: list[str] = []
        seen: set[str] = set()
        root = self.config.task_workdir
        for name in names:
            rel = name.replace("\\", "/")
            if rel in seen:
                continue
            seen.add(rel)
            try:
                fp = relative_target(root, rel)
            except WriteRootViolation:
                continue
            if not fp.is_file():
                continue
            try:
                with fp.open(encoding="utf-8", errors="replace") as stream:
                    text = stream.read(max_file_bytes + 1)
            except OSError:
                continue
            if len(text) > max_file_bytes:
                text = text[:max_file_bytes] + f"\n...（截断，原 {len(text)} 字符）"
            parts.append(f"### 待修复文件: {rel}\n```\n{text}\n```")
        if not parts:
            parts.append('（反馈未点名可读取的文件，请按清单与反馈定位，勿猜测缺失正文。）')
        guidance = ('先在现有实现上修复，避免另建重复实现。只有确认旧文件已被替代且位于本轮允许修改范围内时才清理；'
                    '范围外旧文件只报告，不擅自删除测试或其他文件。')
        return self._rework_file_inventory() + '\n\n' + guidance + '\n\n' + '\n\n'.join(parts)

    def _upstream_hashes(self, index: int) -> list[str]:
        # 只用上游 outputs_hash（纯内容哈希，稳定）；commit_sha 含提交时间戳，
        # 仅作产物指针，不参与 inputs_hash，否则续跑会因 SHA 漂移误判重跑。
        hashes: list[str] = []
        for up in self.config.stages[:index]:
            st = self.state.stage(up.name)
            if st.outputs_hash:
                hashes.append(st.outputs_hash)
        return hashes

    def _inputs_hash(self, spec: StageSpec, index: int, prompt_override: str | None = None) -> str:
        # 渲染结果计入 inputs_hash（评审 B1）：上游产物变了 => 指纹变 => 下游 stale。
        config_keys = dict(spec.config_keys)
        adapter = self.config.adapters.get(spec.adapter)
        config_keys["__execution__"] = {
            "protocol": 2, "kind": spec.kind,
            "adapter": {"name": adapter.name, "type": adapter.type,
                        "options": adapter.options} if adapter else None,
            "timeout_s": spec.timeout_s, "argv": spec.test_cmd,
            "workdir": str(self.config.task_workdir),
            "feedback_to": spec.feedback_to, "max_rounds": spec.max_rounds,
            "allow_yolo": self.config.allow_yolo,
        }
        fingerprint = getattr(self.adapters.get(spec.adapter), 'input_fingerprint', None)
        if callable(fingerprint):
            config_keys['__cli_profile__'] = fingerprint()
        # L2：反馈返工使相关阶段失效。invalidate_nonce 仅在反馈触发返工时递增，
        # 执行中与续跑时取值相同（已持久化），故指纹稳定；递增后旧 attempt 不再被采纳。
        nonce = self.state.invalidate_nonce.get(spec.name, 0)
        if nonce:
            config_keys["__feedback_nonce__"] = nonce
        if spec.context_files or spec.precheck_cmd:
            config_keys['__sources__'] = spec.context_files
            config_keys['__precheck__'] = spec.precheck_cmd
        if spec.kind == 'code' and self._feedback(spec.name):
            config_keys['__rework_context_protocol__'] = 2
        review_no_upstream = (
            spec.kind == "review"
            and not any(self.state.stage(u.name).commit_sha for u in self.config.stages[:index]))
        if review_no_upstream:
            # 审查现有代码库：输入是工作区源码本身。把工作区内容哈希并列
            # 参与指纹与 autonomy 逻辑哈希，源码变化后旧 verdict 不再被采纳。
            if getattr(self, '_review_workspace_hash', None) is None:
                self._review_workspace_hash = artifacts.workspace_hash(
                    self.config.task_workdir, exclude=self.config.state_dir)
            config_keys["__review_workspace__"] = self._review_workspace_hash
        if spec.kind == "test":
            if not any(self.state.stage(u.name).commit_sha
                       for u in self.config.stages[:index]):
                # 测试命令会写工作区（junit/coverage/快照等）。这些产物属于
                # 本次测试的输出而不是被测输入；若计入指纹，续跑时 hash 会
                # 随产物变化而改变，导致每次 resume 都重跑测试（P2-1）。
                config_keys["__workspace__"] = artifacts.workspace_hash(
                    self.config.task_workdir, exclude=self.config.state_dir,
                    ignore=set(self.state.test_artifacts.get(spec.name, [])))
        logical = None
        if self.config.autonomy.enabled and spec.kind != 'test':
            policy=asdict(self.config.autonomy)
            if not getattr(self.adapters.get(spec.adapter),'channel','').startswith('cli_'):
                policy={k:v for k,v in policy.items() if not k.startswith('cli_')}
            config_keys['__autonomy__'] = policy
            logical = compute_inputs_hash(self._read_prompt(spec) + self.task + self._feedback(spec.name)
                                          + self.state.autonomy.get('recovery_hints', {}).get(spec.name, ''),
                                          self._upstream_hashes(index), config_keys)
            saved = self.state.autonomy.setdefault('execution_inputs', {}).get(spec.name)
            if prompt_override is None and saved and saved['logical_hash'] == logical:
                self._prompts[spec.name] = saved['prompt']
                return saved['inputs_hash']
        if spec.kind == "test":
            prompt = ""
        else:
            prompt = prompt_override if prompt_override is not None else self._render_prompt(spec, index)
            self._prompts[spec.name] = prompt
        inputs_hash = compute_inputs_hash(prompt, self._upstream_hashes(index), config_keys)
        if logical:
            self.state.autonomy['execution_inputs'][spec.name] = {
                'logical_hash': logical, 'inputs_hash': inputs_hash, 'prompt': prompt}
        return inputs_hash

    def _account_usage(self, result: Result, stage: str = "", call_id: str | None = None) -> None:
        """调用一旦发生就入账，失败和超限也算。续跑才能看见已经花掉的用量。"""
        unknown = self._settle_call(result, stage, call_id) if call_id else False
        tokens = result.usage.get('known_tokens',0) if unknown else result.usage.get('tokens') or 0
        self.state.usage_total["tokens"] += tokens
        self.state.usage_total["duration_s"] += result.usage.get("duration_s") or 0.0
        if stage:
            used = self.state.stage_usage.setdefault(stage, {"tokens": 0, "duration_s": 0.0})
            used['tokens'] += tokens
            used['duration_s'] += result.usage.get('duration_s') or 0
        self.state.save()

    def _check_budget(self, spec: StageSpec, result: Result) -> None:
        """用量已经计入 usage_total。阶段上限仍按这一次调用判断。"""
        b = self.config.run_budget
        used = self.state.usage_total
        stage_used = self.state.stage_usage.get(spec.name, result.usage)
        tokens = stage_used.get("tokens") or 0
        duration = stage_used.get("duration_s") or 0.0
        if b.max_tokens is not None and used["tokens"] > b.max_tokens:
            raise BudgetExceeded(f"run 总 token 超限: {used['tokens']} > {b.max_tokens}")
        if spec.max_tokens is not None and tokens > spec.max_tokens:
            raise BudgetExceeded(f"阶段 {spec.name} token 超限: {tokens} > {spec.max_tokens}")
        # 时长预算（评审 M3）
        if b.max_duration_s is not None and used["duration_s"] > b.max_duration_s:
            raise BudgetExceeded(
                f"run 总时长超限: {used['duration_s']:.0f}s > {b.max_duration_s}s")
        if spec.max_duration_s is not None and duration > spec.max_duration_s:
            raise BudgetExceeded(
                f"阶段 {spec.name} 时长超限: {duration:.0f}s > {spec.max_duration_s}s")

    def _before_call(self, spec: StageSpec, prompt: str = "") -> float:
        estimate = max(1, len(prompt) // 3) if spec.kind != "test" else 0
        run = self.state.usage_total
        stage = self.state.stage_usage.get(spec.name, {})
        for label, used, max_tokens, max_duration in (
                ("run", run, self.config.run_budget.max_tokens,
                 self.config.run_budget.max_duration_s),
                (f"阶段 {spec.name}", stage, spec.max_tokens, spec.max_duration_s)):
            if max_tokens is not None and used.get("tokens", 0) + estimate > max_tokens:
                raise BudgetExceeded(f"{label} token 预算不足，调用前拦截")
            if max_duration is not None and used.get("duration_s", 0) >= max_duration:
                raise BudgetExceeded(f"{label} 时长预算耗尽，调用前拦截")
        remaining = [float(spec.timeout_s)]
        deadline=self.state.control.get('cli_deadline')
        if deadline is not None:
            if deadline<=time.time():raise BudgetExceeded('原 CLI 运行有效期已过，不能通过续跑延长')
            remaining.append(deadline-time.time())
        for used, limit in ((run, self.config.run_budget.max_duration_s),
                            (stage, spec.max_duration_s)):
            if limit is not None:
                remaining.append(limit - used.get("duration_s", 0))
        return min(remaining)

    def _reject_budget(self, spec: StageSpec, error: Exception) -> StageStatus:
        st = self.state.stage(spec.name)
        st.status, st.error = StageStatus.FAILED, str(error)
        self.state.log_event("rejected", spec.name, reason=str(error))
        self.state.save()
        return StageStatus.FAILED

    # ---- 单阶段执行 ----
    def _run_stage(self, spec: StageSpec, index: int) -> StageStatus:
        if self._owned and self._owner_thread == threading.get_ident():
            return self._run_stage_owned(spec, index)
        with self._ownership() as owned:
            if not owned:
                return StageStatus.RUNNING
            self.prepare()
            return self._run_stage_owned(spec, index)

    def _approval_checkpoint(self, spec: StageSpec) -> StageStatus:
        from .evidence import approved
        st = self.state.stage(spec.name)
        directory = self.state.current_attempt_dir(spec.name)
        record = {"n": st.current_attempt, "inputs_hash": st.inputs_hash,
                  "outputs_hash": st.outputs_hash}
        if not spec.approval or (directory and approved(directory, record)):
            return StageStatus.DONE
        self.state.pending_approvals[spec.name] = {
            **record, "stage": spec.name, "requested_at": time.time(),
            "requested_by": "engine",
        }
        st.status = StageStatus.AWAITING_APPROVAL
        self.state.save()
        if self.config.autonomy.enabled and spec.name in self.config.autonomy.auto_approve:
            if self._approve_owned(spec.name):
                self.state.log_event('policy_approved', spec.name)
                self.state.save()
                return StageStatus.DONE
            return self._reject_budget(spec, RuntimeError('预授权审批的产物校验失败'))
        return StageStatus.AWAITING_APPROVAL

    def _run_stage_owned(self, spec: StageSpec, index: int) -> StageStatus:
        st = self.state.stage(spec.name)

        # 测试阶段：真实执行命令，不调用模型（确定性）
        if spec.kind == "test":
            return self._run_test_stage(spec, index)

        # G1：代码阶段依赖 git（变更探测/worktree/commit）。缺 git 明确报错，
        # 绝不静默降级把"文件写成功"误判成"空跑"。
        if spec.kind == "code" and not self._git_ready:
            st.status = StageStatus.FAILED
            st.error = ("代码阶段需要 git，但未探测到可用的 git。"
                        "请把 git 加入 PATH，或在配置 git_exe 填绝对路径"
                        "（如 D:\\Program Files\\Git\\cmd\\git.exe）。")
            self.state.log_event("rejected", spec.name, reason="git_unavailable")
            self.state.save()
            return StageStatus.FAILED

        inputs_hash = self._inputs_hash(spec, index)

        # 未决审批优先（评审 B3）：有 pending 就直接返回 AWAITING，绝不重跑覆盖。
        if spec.name in self.state.pending_approvals:
            self.state.log_event("approval_pending", spec.name)
            return StageStatus.AWAITING_APPROVAL

        # 完成判据：双哈希匹配则跳过（评审 B1）
        if self.state.is_complete(spec.name, inputs_hash):
            self.state.log_event("skip_done", spec.name)
            return self._approval_checkpoint(spec)

        # 审查耗尽后的失败要保持失败。旧记录没有 verdict 字段时，靠状态和错误文本认。
        if self._exhausted_review_stands(spec, inputs_hash):
            self.state.log_event("review_failed_kept", spec.name)
            self.state.save()
            return StageStatus.FAILED

        replay = self.state.terminal_replay(spec.name, inputs_hash)
        if replay == "failed":
            self.state.log_event("review_failed_kept", spec.name)
            self.state.save()
            return StageStatus.FAILED
        if replay == "unparsed":
            if self.config.autonomy.enabled:
                self.state.pending_approvals.pop(spec.name, None)
                st.status = StageStatus.FAILED
                st.error = '无人值守审查结论仍无法解析，已停止'
                self.state.save()
                return StageStatus.FAILED
            self.state.log_event("approval_pending", spec.name)
            self.state.save()
            return StageStatus.AWAITING_APPROVAL

        # 采纳已成功的 attempt（评审 M1）：success.json 指纹匹配 => 不重跑。
        adopted = self.state.adopt_successful(spec.name, inputs_hash)
        if adopted is not None:
            self.state.log_event("skip_adopted", spec.name, n=adopted)
            return self._approval_checkpoint(spec)

        if st.status == StageStatus.DONE and st.inputs_hash != inputs_hash:
            self.state.mark_stale_downstream([s.name for s in self.config.stages], index)

        try:
            self._run_precheck(spec)
        except BudgetExceeded as error:
            return self._reject_budget(spec, error)
        inputs_hash = self._inputs_hash(spec, index)

        lock = StageLock(self.state.stage_dir(spec.name) / ".lock")
        if not lock.acquire():
            self.state.log_event("lock_busy", spec.name)
            return StageStatus.RUNNING  # 另一进程在跑 => run() 会中止整轮
        try:
            return self._run_attempts(spec, index, inputs_hash)
        finally:
            lock.release()

    def _run_test_stage(self, spec: StageSpec, index: int) -> StageStatus:
        """测试阶段：在 worktree/工作区真实执行 test_cmd，退出码即判定。

        非零退出码 => 失败，并把执行记录直接作为反馈回传给 feedback_to，
        不经审查模型复述。通过则正常完成。
        """
        from .testrun import run_test_cmd, write_record
        from .review import format_test_feedback
        st = self.state.stage(spec.name)
        if spec.name in self.state.pending_approvals:
            return StageStatus.AWAITING_APPROVAL

        # 指纹纳入命令本身与上游产物：命令或代码变了就重跑
        inputs_hash = self._inputs_hash(spec, index)
        if self.state.is_complete(spec.name, inputs_hash):
            self.state.log_event("skip_done", spec.name)
            return self._approval_checkpoint(spec)
        replay = self.state.terminal_replay(spec.name, inputs_hash)
        if replay:
            return StageStatus.FAILED if replay == "failed" else StageStatus.AWAITING_APPROVAL
        if self.state.adopt_successful(spec.name, inputs_hash) is not None:
            return self._approval_checkpoint(spec)
        try:
            timeout = self._before_call(spec)
        except BudgetExceeded as error:
            return self._reject_budget(spec, error)

        lock = StageLock(self.state.stage_dir(spec.name) / ".lock")
        if not lock.acquire():
            self.state.log_event("lock_busy", spec.name)
            return StageStatus.RUNNING
        try:
            n, attempt_dir = self.state.new_attempt_dir(spec.name)
            st.status = StageStatus.RUNNING
            self.state.log_event("test_start", spec.name, n=n,
                                 cmd=" ".join(spec.test_cmd))
            self.state.save()

            workdir = self.config.task_workdir
            upstream_sha = next((self.state.stage(s.name).commit_sha
                                 for s in reversed(self.config.stages[:index])
                                 if self.state.stage(s.name).commit_sha), None)
            worktree = None
            setup_started = time.monotonic()
            # 无上游 commit 时测试直接跑在任务工作区：记录测试自身写出的文件，
            # 下次指纹计算忽略它们，避免 resume 因新产物永不收敛（P2-1）。
            before_workspace = None
            if not (upstream_sha and self._git_ready):
                before_workspace = artifacts.workspace_file_hashes(
                    workdir, exclude=self.config.state_dir)
            try:
                if upstream_sha and self._git_ready:
                    worktree = self.state.stage_dir(spec.name) / "worktree" / str(n)
                    artifacts.add_worktree(workdir, upstream_sha,
                                           f"verify/{spec.name}/{self.run_id}/{n}", worktree)
                    workdir = worktree
                record = run_test_cmd(spec.test_cmd, workdir, timeout_s=timeout)
                if before_workspace is not None:
                    self._record_test_artifacts(spec, before_workspace)
            except artifacts.GitError as error:
                message = '隔离测试工作区创建失败，未执行测试：' + str(error)
                record = {'ok': False, 'exit_code': -1, 'cmd': list(spec.test_cmd),
                          'duration_s': time.monotonic() - setup_started, 'output': message,
                          'error': message, 'failure_ids': ['worktree_creation_failed'], 'truncated': False}
                write_record(record, attempt_dir)
                from .evidence import seal
                outputs_hash = seal(attempt_dir)
                self._account_usage(Result(ok=False, usage={'duration_s': record['duration_s']}), spec.name)
                self.state.log_event('worktree_error', spec.name, n=n, reason=message)
                self.state.commit_success(spec.name, n, attempt_dir, inputs_hash, outputs_hash,
                                          None, None, verdict='fail', status=StageStatus.FAILED, error=message)
                return StageStatus.FAILED
            finally:
                self._cleanup_worktree(worktree)
            rec_path, out_path = write_record(record, attempt_dir)
            from .evidence import seal
            outputs_hash = seal(attempt_dir)
            usage = Result(ok=record["ok"], usage={"duration_s": record["duration_s"]})
            self._account_usage(usage, spec.name)
            try:
                self._check_budget(spec, usage)
            except BudgetExceeded as error:
                return self._reject_budget(spec, error)

            if record["ok"]:
                self.state.log_event("test_pass", spec.name, n=n,
                                     duration_s=record["duration_s"])
                self.state.commit_success(spec.name, n, attempt_dir, inputs_hash,
                                          outputs_hash, None, None, verdict="pass")
                self._consume_feedback(spec.name)
                return self._approval_checkpoint(spec)

            # 失败：记录留证 + 直接作为反馈
            rounds_used = self.state.review_rounds.get(spec.name, 0) + 1
            self.state.review_rounds[spec.name] = rounds_used
            err = record.get("error") or f"测试失败（退出码 {record['exit_code']}）"
            self.state.log_event("test_fail", spec.name, n=n,
                                 exit_code=record["exit_code"], round=rounds_used)
            stopped = self._feedback_limit(spec, record.get('output', '')) if spec.feedback_to else ''
            exhausted = (not spec.feedback_to) or rounds_used >= spec.max_rounds or bool(stopped)
            if exhausted:
                self.state.commit_success(
                    spec.name, n, attempt_dir, inputs_hash, outputs_hash,
                    None, None, verdict="fail", status=StageStatus.FAILED, error=stopped or err)
                return StageStatus.FAILED

            self.state.feedback[spec.feedback_to] = format_test_feedback(
                record, rounds_used)
            if self.config.autonomy.enabled:
                self.state.autonomy.setdefault('failures', {})[spec.name] = {
                    'failure_ids': record.get('failure_ids', []), 'round': rounds_used,
                    'output_hash': sha256_text(record.get('output', ''))}
            target_idx = self.config.stage_index(spec.feedback_to)
            for name in [s.name for s in self.config.stages][target_idx:]:
                stg = self.state.stage(name)
                stg.status = StageStatus.PENDING
                stg.current_attempt = None
                stg.inputs_hash = None
                self.state.invalidate_nonce[name] = \
                    self.state.invalidate_nonce.get(name, 0) + 1
            self.state.commit_success(
                spec.name, n, attempt_dir, inputs_hash, outputs_hash,
                None, None, verdict="fail", status=StageStatus.PENDING, error=err)
            self.state.log_event("test_feedback", spec.name, round=rounds_used,
                                 target=spec.feedback_to)
            self.state.save()
            return StageStatus.REVIEW_FEEDBACK
        finally:
            lock.release()

    def _record_test_artifacts(self, spec: StageSpec, before: dict[str, str]) -> None:
        """把测试命令新建/改写的文件记入 state.test_artifacts（有上限）。"""
        after = artifacts.workspace_file_hashes(
            self.config.task_workdir, exclude=self.config.state_dir)
        changed = sorted({rel for rel, digest in after.items()
                          if before.get(rel) != digest})
        known = set(self.state.test_artifacts.get(spec.name, []))
        merged = sorted(known | set(changed))
        limit = 500
        if len(merged) > limit:
            merged = merged[:limit]
            self.state.log_event("test_artifacts_truncated", spec.name,
                                 total=len(known | set(changed)))
        self.state.test_artifacts[spec.name] = merged
        if changed:
            self.state.log_event("test_artifacts", spec.name, files=changed[:20],
                                 count=len(changed))
        self.state.save()

    def _stage_base_sha(self, index: int, kind: str = "code") -> str | None:
        """代码阶段的基线 = 最近上游代码阶段的 commit（评审 B3：基于上游 commit）。

        非代码阶段不需要基线，也不触碰 git（纯报告/审查流程可能没有仓库）。
        """
        if not self._git_ready or kind != "code":
            return None
        for up in reversed(self.config.stages[:index]):
            sha = self.state.stage(up.name).commit_sha
            if sha:
                return sha
        if not self.config.task_workdir.is_dir():
            return None
        return artifacts.head_sha(self.config.task_workdir)

    def _run_attempts(self, spec: StageSpec, index: int, inputs_hash: str) -> StageStatus:
        st = self.state.stage(spec.name)
        adapter = self.adapters[spec.adapter]
        base_sha = self._stage_base_sha(index, spec.kind)
        last_error = ""
        violation = self._write_violation_stands(spec, inputs_hash)
        if violation:
            st.status = StageStatus.FAILED
            st.error = violation
            self.state.log_event("write_violation_kept", spec.name)
            self.state.save()
            return StageStatus.FAILED
        cli=getattr(adapter,'channel','').startswith('cli_')
        for attempt in range(1, spec.max_attempts + 1):
            if cli:
                cycle=str(self.state.invalidate_nonce.get(spec.name,0))
                used=self.state.autonomy.get('cli_attempts',{}).get(spec.name,{}).get('cycles',{}).get(cycle,0)
                if used>=spec.max_attempts:
                    last_error='本轮 CLI 重试次数已耗尽，续跑不重置调用次数'
                    self.state.log_event('cli_retry_exhausted',spec.name,used=used,limit=spec.max_attempts)
                    break
            if self.control:
                self.control.checkpoint(self)
            n, attempt_dir = self.state.new_attempt_dir(spec.name)
            st.status = StageStatus.RUNNING
            self.state.log_event("attempt_start", spec.name, n=n)
            self.state.save()

            # 代码阶段在独立 worktree 执行（基于上游 commit 拉分支，评审 B3）；
            # 报告阶段在主工作区（报告落 runs/，不进 git）。
            worktree = None
            if spec.kind == "code" and self._git_ready and base_sha:
                branch = f"stage/{spec.name}/{self.run_id}/{n}"
                worktree = self.state.stage_dir(spec.name) / "worktree" / str(n)
                try:
                    artifacts.add_worktree(self.config.task_workdir, base_sha, branch, worktree)
                    workdir = worktree
                except artifacts.GitError as e:
                    last_error = f"worktree 隔离失败: {e}"
                    self.state.log_event("attempt_fail", spec.name, reason=last_error)
                    self._cleanup_worktree(worktree)
                    continue
            else:
                workdir = self.config.task_workdir

            restored=None
            if cli and spec.kind=='code':
                try:
                    from .recovery import restore
                    restored=restore(self,spec,worktree,base_sha,n,attempt_dir)
                except (OSError,ValueError,RuntimeError) as error:
                    st.status=StageStatus.FAILED
                    st.error='恢复点校验失败：'+str(error)
                    self.state.log_event('recovery_rejected',spec.name,n=n,reason=st.error)
                    self.state.save()
                    self._cleanup_worktree(worktree)
                    return StageStatus.FAILED

            if attempt > 1 and self.config.autonomy.enabled:
                inputs_hash = self._inputs_hash(spec, index)
            prompt = self._prompts.get(spec.name)
            if prompt is None:
                prompt = self._render_prompt(spec, index)
            if restored:
                bodies=[]
                for name in restored['files']:
                    target=relative_target(workdir,name)
                    body=target.read_text(encoding='utf8',errors='replace')[:8000] if target.is_file() else '（文件在恢复点中已删除）'
                    bodies.append(f'<file path="{name}">\n{body}\n</file>')
                prompt+='\n\n已验证恢复点已应用；以下为当前隔离工作区原文，优先于旧基线，不要重复覆盖已完成修改：\n'+'\n'.join(bodies)
                inputs_hash=self._inputs_hash(spec,index,prompt_override=prompt)
            try:
                timeout = self._before_call(spec, prompt)
            except BudgetExceeded as error:
                self._cleanup_worktree(worktree)
                return self._reject_budget(spec, error)
            ctx = RunContext(
                task=self.task, run_id=self.run_id, stage=spec.name,
                workdir=workdir, runs_dir=self.state.run_dir,
                upstream_commits=[self.state.stage(u.name).commit_sha
                                  for u in self.config.stages[:index]
                                  if self.state.stage(u.name).commit_sha],
                upstream_reports=self._reports.get(spec.name, []),
                allow_yolo=self.config.allow_yolo, timeout_s=timeout,
                kind=spec.kind, attempt_dir=attempt_dir,
                feedback=self.state.feedback.get(spec.name, ""),
                base_commit=base_sha,
                idle_timeout_s=self.config.autonomy.cli_idle_timeout_s,
                max_tool_errors=self.config.autonomy.cli_max_tool_errors,
                max_repeated_reads=self.config.autonomy.cli_max_repeated_reads,
            )
            if self.config.autonomy.enabled or cli:
                try:
                    ctx.output_tokens, ctx.token_reservation = self._call_limits(spec, adapter, prompt)
                except BudgetExceeded as error:
                    self._cleanup_worktree(worktree)
                    return self._reject_budget(spec, error)
            if spec.kind == "review":
                # 兼容旧适配器的固定输出位置，但旧文件绝不能属于新 attempt。
                for old in (self.state.run_dir / "review_verdict.json",
                            self.state.run_dir / f"{spec.name}_review_verdict.json"):
                    old.unlink(missing_ok=True)

            # 可写根守卫。默认监视工作区父目录，才能看见写到旁边的文件。
            # watch_root = "off" 关闭根外扫描；其它值则改用该目录。
            # 落在可写根内部的监视域不会产生任何根外文件，静默空转等于没有
            # 保护：显式告警并禁用它（P2-6）。
            roots = [self.config.task_workdir, self.state.run_dir]
            wr_cfg = (self.config.watch_root or "").strip()
            if wr_cfg.lower() == "off":
                watch = list(roots)
            elif wr_cfg:
                wr = Path(wr_cfg)
                watch = [wr if wr.is_absolute() else self.config.base_dir / wr]
            else:
                watch = None
            if watch:
                guard_roots = [Path(r).resolve() for r in roots]
                usable = []
                for candidate in watch:
                    resolved = Path(candidate).resolve()
                    if any(resolved == r or r in resolved.parents for r in guard_roots):
                        self.state.log_event("watch_disabled", spec.name,
                                             reason="watch_root 位于可写根内部，无法捕获根外写入",
                                             path=str(resolved))
                        self.state.save()
                        continue
                    usable.append(resolved)
                watch = usable or None
            guard = WriteRootGuard(roots, watch_roots=watch)
            try:
                guard.check(workdir)
            except WriteRootViolation as e:
                st.status = StageStatus.FAILED
                st.error = str(e)
                self.state.log_event("rejected", spec.name, reason=str(e))
                self.state.save()
                self._cleanup_worktree(worktree)
                return StageStatus.FAILED

            # G2：执行前对监视域拍快照，执行后对比，捕获根外写入
            before_snap = guard.snapshot()
            call_id = self._reserve_call(spec, n, ctx)
            ctx.call_id=call_id or f'{spec.name}/{n}'
            limits=[maximum-used.get('tokens',0)+ctx.token_reservation
                    for used,maximum in ((self.state.usage_total,self.config.run_budget.max_tokens),
                                          (self.state.stage_usage.get(spec.name,{}),spec.max_tokens)) if maximum is not None]
            ctx.remaining_tokens=min(limits) if limits else None
            t0 = time.monotonic()
            try:
                result = adapter.run(prompt, workdir, ctx)
            except Exception as e:  # 适配器崩溃
                result = Result(ok=False, exit_code=-1, error_kind=ErrorKind.CRASH,
                                stderr=str(e), channel=adapter.channel)
                if self.config.autonomy.enabled or cli:
                    result.usage['usage_unknown'] = True
            duration = time.monotonic() - t0
            result.usage.setdefault("duration_s", duration)

            # 目录快照是启发式，**默认只告警**：父目录里一次无关保存不该判阶段失败。
            # 真正的写入约束是 API 适配器的相对路径校验 + git 按清单提交。
            # 需要把根外变更当失败时显式配 watch_enforce = true；
            # 硬隔离请用容器/系统级作业对象，而不是父目录 os.walk。
            after_snap = guard.snapshot()
            outside = guard.diff_outside(before_snap, after_snap)
            if outside and not self.config.watch_enforce:
                self.state.log_event("watch_warning", spec.name, files=outside[:10])
                self.state.save()
                outside = []
            elif outside:
                # 不删除无归属文件：新文件移入本运行的隔离目录留证，已有文件
                # 只记录。越界记录按 inputs_hash 持久化，续跑不能把同一输入
                # 的违规“洗白”成通过（P2-6）。
                moved = self._quarantine_new_outside(spec, before_snap, outside)
                self.state.control.setdefault('write_violations', {})[spec.name] = {
                    'inputs_hash': inputs_hash, 'files': outside[:20],
                    'quarantined': moved[:20], 'at': time.time()}
                self.state.log_event("watch_enforce", spec.name,
                                     files=outside[:10], quarantined=moved[:10])
                self.state.save()
                result.ok = False
                result.error_kind = ErrorKind.REJECTED
                result.stderr = (result.stderr + f" [可写根外变更: {outside[:5]}]").strip()

            # 代码阶段：检测实际变更文件（评审 B4），归一化为相对路径供按清单提交
            bad_paths: list[str] = []
            if spec.kind == "code" and self._git_ready and result.ok:
                if not result.changed_files:
                    result.changed_files = artifacts.changed_files_since(
                        workdir, base_sha or artifacts.head_sha(workdir))
                result.changed_files = artifacts.normalize_rel(workdir, result.changed_files)
                bad_paths = artifacts.unsafe_rel_paths(result.changed_files)
                if bad_paths:
                    artifacts.remove_git_metadata_files(workdir, bad_paths)
                    result.ok = False
                    result.error_kind = ErrorKind.REJECTED
                    result.stderr = (result.stderr + f" [拒绝越界或 .git 路径: {bad_paths[:5]}]").strip()
                if not bad_paths:
                    scope_error = self._check_code_scope(spec, result, workdir,
                                                         base_sha or artifacts.head_sha(workdir))
                    if scope_error:
                        bad_paths = [scope_error]
                        result.ok = False
                        result.error_kind = ErrorKind.REJECTED
                        result.stderr = scope_error

            # 预算（评审 S3/M3）：先入账再判断，超限的这次也算进用量
            self._account_usage(result, spec.name, call_id)
            self._save_attempt_trace(spec, attempt_dir, prompt, result)
            if cli and spec.kind=='code' and not result.ok:
                if not self._finish_cli_worktree(spec,worktree,base_sha,n,attempt_dir,result.error_kind.value):
                    return StageStatus.FAILED
            try:
                self._check_budget(spec, result)
            except BudgetExceeded as e:
                if cli and spec.kind=='code' and result.ok:
                    if not self._finish_cli_worktree(spec,worktree,base_sha,n,attempt_dir,'budget_exceeded'):
                        return StageStatus.FAILED
                st.status = StageStatus.FAILED
                st.error = str(e)
                self.state.log_event("rejected", spec.name, reason=str(e))
                self.state.save()
                self._cleanup_worktree(worktree)
                return StageStatus.FAILED

            # 越界不重试。重试会让下一次干净的调用把阶段判成成功，根外文件还在。
            if outside or bad_paths:
                st.status = StageStatus.FAILED
                st.error = result.stderr
                self.state.log_event("rejected", spec.name, reason=result.stderr)
                self.state.save()
                self._cleanup_worktree(worktree)
                return StageStatus.FAILED

            # 空跑 / 解析未知 => 不得判完成（评审 B4：按 kind 收紧）
            if result.ok and (not result.is_meaningful(spec.kind) or result.parse_unknown):
                result.ok = False
                result.error_kind = ErrorKind.REJECTED
                result.stderr = (result.stderr + " [空跑或解析未知，拒绝判完成]").strip()

            if not result.ok:
                last_error = result.stderr or result.error_kind.value
                self.state.log_event("attempt_fail", spec.name, n=n,
                                     error_kind=result.error_kind.value)
                self.state.save()
                if cli and spec.kind=='code':
                    record=self.state.control.get('cli_worktrees',{}).get(ctx.call_id,{})
                    if record.get('status')=='active':
                        if not self._finish_cli_worktree(spec,worktree,base_sha,n,attempt_dir,result.error_kind.value):
                            return StageStatus.FAILED
                self._cleanup_worktree(worktree)
                if self.config.autonomy.enabled:
                    if result.error_kind in (ErrorKind.AUTH, ErrorKind.REJECTED):
                        break
                    self._recovery_hint(spec, last_error)
                if cli and result.parse_unknown:break
                # 限流退避（评审 M6）：429/5xx 指数退避后再试
                if result.error_kind == ErrorKind.RATE_LIMIT and attempt < spec.max_attempts:
                    time.sleep(min(2 ** attempt, 30))
                continue

            # 成功：归档产物到 attempt 目录（代码阶段在 worktree 提交并合并回主线）
            try:
                outputs_hash = self._archive(spec, result, attempt_dir, base_sha, worktree)
            except (StageArchiveError, artifacts.GitError) as e:
                last_error = str(e)
                self.state.log_event("attempt_fail", spec.name, n=n, error_kind="archive")
                self.state.save()
                if cli and spec.kind=='code':
                    if not self._finish_cli_worktree(spec,worktree,base_sha,n,attempt_dir,'archive_failed'):
                        return StageStatus.FAILED
                self._cleanup_worktree(worktree)
                continue
            self.state.log_event("attempt_done", spec.name, n=n, outputs_hash=outputs_hash)
            if cli and spec.kind=='code':
                self._finish_cli_worktree(spec,worktree,base_sha,n,attempt_dir,'archived',archived=True)
                recovery=self.state.control.get('recoveries',{}).get(spec.name)
                if recovery:recovery['status']='consumed'
            self._cleanup_worktree(worktree)

            # 审查先定结论，再决定这条记录能不能被续跑采纳。
            if spec.kind == "review":
                fb_status = self._handle_review(
                    spec, result, attempt_dir, n, inputs_hash, outputs_hash)
                if (fb_status == StageStatus.AWAITING_APPROVAL and self.config.autonomy.enabled
                        and self.state.pending_approvals.get(spec.name, {}).get('requested_by') == 'review_unparsed'
                        and attempt < spec.max_attempts):
                    self.state.pending_approvals.pop(spec.name, None)
                    st.status = StageStatus.PENDING
                    self._recovery_hint(spec, st.error or '没有给出合法 JSON 结论')
                    continue
                if fb_status is not None:
                    if fb_status == StageStatus.AWAITING_APPROVAL and self.config.autonomy.enabled:
                        self.state.pending_approvals.pop(spec.name, None)
                        st.status = StageStatus.FAILED
                        self.state.save()
                        return StageStatus.FAILED
                    return fb_status
            else:
                self.state.commit_success(spec.name, n, attempt_dir, inputs_hash,
                                          outputs_hash, result.commit_sha, result.branch)

            # 审批检查点（评审 M6）
            return self._approval_checkpoint(spec)

        st.status = StageStatus.FAILED
        st.error = last_error
        self.state.log_event("stage_failed", spec.name, error=last_error)
        self.state.save()
        return StageStatus.FAILED

    def _exhausted_review_stands(self, spec: StageSpec, inputs_hash: str) -> bool:
        """同一输入上的审查耗尽结果保持失败，续跑不改判通过、也不再调用模型。"""
        if spec.kind != "review":
            return False
        st = self.state.stage(spec.name)
        if st.status != StageStatus.FAILED or st.inputs_hash != inputs_hash:
            return False
        return bool(st.error) and "仍未通过" in st.error

    def _quarantine_new_outside(self, spec: StageSpec, before: dict,
                                outside: list[str]) -> list[str]:
        """把本次新写出的根外文件移入运行目录隔离区（可恢复，不直接删除）。

        改写过的既有根外文件留在原地并由持久化越界记录阻止续跑重试。
        返回实际移动的原始路径列表。
        """
        target = self.state.run_dir / "violations" / spec.name
        moved: list[str] = []
        for path in outside:
            if path in before:
                continue
            source = Path(path)
            try:
                if not source.is_file():
                    continue
                target.mkdir(parents=True, exist_ok=True)
                destination = target / f"{uuid.uuid4().hex[:8]}-{source.name}"
                shutil.move(str(source), str(destination))
                moved.append(path)
            except OSError:
                continue
        return moved

    def _write_violation_stands(self, spec: StageSpec, inputs_hash: str) -> str:
        """同一输入上已记录的根外写入 => 保持失败，不再调用模型。"""
        violation = self.state.control.get('write_violations', {}).get(spec.name)
        if isinstance(violation, dict) and violation.get('inputs_hash') == inputs_hash:
            return ('同一输入上已发生可写根外变更，续跑拒绝重试；'
                    '修正根外文件后可用 rerun-from 显式覆盖: '
                    + str(violation.get('files', [])[:5]))
        return ''

    def _handle_review(self, spec: StageSpec, result: Result, attempt_dir: Path,
                       n: int, inputs_hash: str, outputs_hash: str) -> StageStatus | None:
        """L2：解析审查结论。不通过且轮次未尽 => 写反馈、标目标阶段待返工。

        返回 None 表示审查通过（记录已写成 pass）；返回 REVIEW_FEEDBACK 表示
        需返工；返回 AWAITING_APPROVAL 表示结论不可解析需人工确认。
        失败和不可解析都写成带 verdict 的记录，续跑不能把它们采纳成通过。
        """
        from .review import load_verdict, verdict_path, format_feedback, VerdictError
        # 只认独立结论文件；Markdown 报告不参与判定。
        # 优先本 attempt 目录内的结论，其次 run 级固定路径。
        candidates = [attempt_dir / "review_verdict.json"]
        verdict = None
        verr = ""
        for cand in candidates:
            try:
                verdict = load_verdict(cand, workdir=self.config.task_workdir)
                break
            except VerdictError as e:
                verr = str(e)
        rounds_used = self.state.review_rounds.get(spec.name, 0) + 1
        self.state.review_rounds[spec.name] = rounds_used

        missing = self._review_missing.get(spec.name, [])
        if verdict is not None and verdict.passed and missing:
            # 审查模型只看到部分实现时，pass 不能当作完成：交人工确认；
            # 无人值守下沿用 AWAITING→FAILED 的保守语义。
            reason = (f"审查上下文覆盖不完整：{len(missing)} 个变更文件未注入提示词"
                      f"（{missing[:5]}）；通过结论不能自动采纳")
            self.state.pending_approvals[spec.name] = {
                "stage": spec.name, "outputs_hash": outputs_hash,
                "n": n, "inputs_hash": inputs_hash,
                "requested_at": time.time(), "requested_by": "review_incomplete",
            }
            self.state.log_event("review_incomplete", spec.name, round=rounds_used,
                                 missing=missing[:20])
            self.state.commit_success(
                spec.name, n, attempt_dir, inputs_hash, outputs_hash,
                result.commit_sha, result.branch, verdict="unparsed",
                status=StageStatus.AWAITING_APPROVAL, error=reason)
            return StageStatus.AWAITING_APPROVAL

        if verdict is None:
            self.state.pending_approvals[spec.name] = {
                "stage": spec.name, "outputs_hash": outputs_hash,
                "n": n, "inputs_hash": inputs_hash,
                "requested_at": time.time(), "requested_by": "review_unparsed",
            }
            self.state.log_event("review_unparsed", spec.name, round=rounds_used,
                                 reason=verr)
            self.state.commit_success(
                spec.name, n, attempt_dir, inputs_hash, outputs_hash,
                result.commit_sha, result.branch, verdict="unparsed",
                status=StageStatus.AWAITING_APPROVAL, error=verr or None)
            return StageStatus.AWAITING_APPROVAL

        if verdict.passed:
            self._consume_feedback(spec.feedback_to)
            self.state.log_event("review_pass", spec.name, round=rounds_used)
            self.state.commit_success(
                spec.name, n, attempt_dir, inputs_hash, outputs_hash,
                result.commit_sha, result.branch, verdict="pass")
            return None

        # 不通过
        stopped = self._feedback_limit(spec, format_feedback(verdict, rounds_used)) if spec.feedback_to else ''
        if not spec.feedback_to or rounds_used >= spec.max_rounds or stopped:
            error = f"审查 {rounds_used} 轮仍未通过（上限 {spec.max_rounds}）"
            self.state.log_event("review_exhausted", spec.name,
                                 round=rounds_used, max_rounds=spec.max_rounds)
            self.state.commit_success(
                spec.name, n, attempt_dir, inputs_hash, outputs_hash,
                result.commit_sha, result.branch, verdict="fail",
                status=StageStatus.FAILED, error=stopped or error)
            return StageStatus.FAILED

        # 没有文件级问题 => 机器无法执行返工，停下等人处理，
        # 不再自动"全面自查"白烧一轮。
        if not verdict.located_issues():
            reason = "审查不通过但未给出文件级问题，需人工处理"
            self.state.pending_approvals[spec.name] = {
                "stage": spec.name, "outputs_hash": outputs_hash,
                "n": n, "inputs_hash": inputs_hash,
                "requested_at": time.time(), "requested_by": "review_no_location",
            }
            self.state.log_event("review_no_location", spec.name, round=rounds_used)
            self.state.commit_success(
                spec.name, n, attempt_dir, inputs_hash, outputs_hash,
                result.commit_sha, result.branch, verdict="unparsed",
                status=StageStatus.AWAITING_APPROVAL, error=reason)
            return StageStatus.AWAITING_APPROVAL

        # 轮次未尽 => 只把文件级条目作为反馈，标目标阶段及其下游待返工
        feedback_text = format_feedback(verdict, rounds_used)
        self.state.feedback[spec.feedback_to] = feedback_text
        target_idx = self.config.stage_index(spec.feedback_to)
        names = [s.name for s in self.config.stages]
        for name in names[target_idx:]:
            stg = self.state.stage(name)
            stg.status = StageStatus.PENDING
            stg.current_attempt = None
            stg.inputs_hash = None
            # 递增失效计数 => inputs_hash 变化 => 旧 attempt 不被采纳，真正重跑
            self.state.invalidate_nonce[name] = self.state.invalidate_nonce.get(name, 0) + 1
        self.state.log_event("review_feedback", spec.name,
                             round=rounds_used, target=spec.feedback_to,
                             issues=len(verdict.issues))
        self.state.save()
        return StageStatus.REVIEW_FEEDBACK

    def _cleanup_worktree(self, worktree: Path | None) -> None:
        if worktree is not None and self._git_ready:
            if not artifacts.remove_worktree(self.config.task_workdir, worktree):
                self.state.log_event("worktree_cleanup_failed", "",
                                     path=str(worktree))
                self.state.save()

    def _archive(self, spec: StageSpec, result: Result, attempt_dir: Path,
                 base_sha: str | None, worktree: Path | None = None) -> str:
        """归档产物，计算 outputs_hash。

        outputs_hash 只基于产物**内容**（文件内容哈希 + 文本），不含 commit SHA/
        路径/时间戳，保证续跑时内容相同则哈希相同（评审 S1）。commit SHA 单独
        存为产物指针（代码类产物 = commit/branch，评审 B3）。
        """
        import shutil
        from .state import sha256_file
        commit_sha = None
        content_hashes: dict[str, str] = {}
        if spec.kind == "code" and self._git_ready:
            repo = self.config.task_workdir
            # 按变更清单提交（评审 B4），不用 git add -A 全仓
            files = result.changed_files
            if worktree is not None:
                wt_sha = artifacts.commit_files(worktree, files, f"{spec.name}: {self.task[:40]}")
                branch = artifacts._git(["rev-parse", "--abbrev-ref", "HEAD"], worktree).stdout.strip()
                if wt_sha is None and artifacts.head_sha(worktree) == base_sha:
                    raise StageArchiveError("代码未发生变化，无法视为一次修复")
                try:
                    commit_sha = artifacts.merge_branch(repo, branch)
                except artifacts.GitError as e:
                    # 快进合并失败（主线已分叉）=> 本阶段失败，不中断整轮
                    raise StageArchiveError(f"合并 {branch} 失败: {e}") from e
                result.branch = branch
            else:
                commit_sha = artifacts.commit_files(repo, files, f"{spec.name}: {self.task[:40]}")
                if commit_sha is None:
                    raise StageArchiveError("代码未发生变化，无法视为一次修复")
                result.branch = f"stage/{spec.name}"
            result.commit_sha = commit_sha
            # 代码产物内容哈希：基于主线该 commit 的树对象（与基线/时间戳无关）
            content_hashes["__tree__"] = artifacts.tree_content_hash(repo, commit_sha)
        # 报告文件复制进 attempt 目录留证，并按内容计哈希
        for rf in result.report_files:
            rf = Path(rf)
            if rf.is_file():
                dest = attempt_dir / rf.name
                if rf.resolve() != dest.resolve():
                    shutil.copy2(rf, dest)
                content_hashes[rf.name] = sha256_file(dest)
        # 原始输出留证
        (attempt_dir / "stdout.txt").write_text(result.stdout or result.text, encoding="utf-8")
        (attempt_dir / "final_text.txt").write_text(result.text, encoding="utf-8")
        if spec.kind == "review":
            # 仅接收本次调用产生的结论，并固定归档到本 attempt。
            sources = [result.verdict_file, attempt_dir / "review_verdict.json",
                       self.state.run_dir / f"{spec.name}_review_verdict.json",
                       self.state.run_dir / "review_verdict.json"]
            for source in sources:
                if source is not None and Path(source).is_file():
                    dest = attempt_dir / "review_verdict.json"
                    if Path(source).resolve() != dest.resolve():
                        shutil.copy2(source, dest)
                    break
        from .evidence import seal
        return seal(attempt_dir, self.config.task_workdir, result.commit_sha)

    # ---- 审批 ----
    def approve(self, stage_name: str) -> bool:
        with self._ownership() as owned:
            if not owned:
                return False
            self.prepare()
            return self._approve_owned(stage_name)

    def _approve_owned(self, stage_name: str) -> bool:
        """恢复审批：校验当前产物哈希与申请时一致（评审 M6）。

        code/test 阶段的渲染输入可能包含实时读取的工作区内容（context_files
        原文、返工文件清单、测试工作区哈希），执行本身就会改写它们；若在审批
        时重算 inputs_hash 必然不相等，会导致审批永久被拒（P2-2）。这两类
        阶段只比对申请时冻结的哈希与 attempt 归档记录 + evidence 校验。
        """
        pending = self.state.pending_approvals.get(stage_name)
        if not pending:
            return False
        st = self.state.stage(stage_name)
        from .evidence import validate
        directory = self.state.current_attempt_dir(stage_name)
        spec = self.config.stage(stage_name)
        record = {"n": st.current_attempt, "inputs_hash": st.inputs_hash,
                  "outputs_hash": st.outputs_hash, "commit_sha": st.commit_sha}
        live_inputs_ok = spec.kind in ("code", "test")
        if not live_inputs_ok:
            live_inputs_ok = (self._inputs_hash(
                spec, self.config.stage_index(stage_name)) == st.inputs_hash)
        if (directory is None or st.outputs_hash != pending["outputs_hash"]
                or pending.get("n", st.current_attempt) != st.current_attempt
                or pending.get("inputs_hash", st.inputs_hash) != st.inputs_hash
                or not live_inputs_ok
                or not validate(directory, record)):
            self.state.log_event("approval_rejected", stage_name, reason="产物已变更")
            self.state.save()
            return False
        del self.state.pending_approvals[stage_name]
        atomic_write_json(directory / "approval.json", record)
        st.status = StageStatus.DONE
        self.state.log_event("approved", stage_name)
        self.state.save()
        return True

    # ---- 主流程 ----
    def run(self) -> dict[str, str]:
        with self._ownership() as owned:
            if not owned:
                return {self.config.stages[0].name: StageStatus.RUNNING.value}
            result = self._run_owned()
            from .metrics import write_metrics
            write_metrics(self, result)
            if self.config.autotune.enabled and self.config.source_path and not any(
                    status in ('running', 'awaiting_approval') for status in result.values()):
                from .tuning import policy_tune
                try:
                    policy_tune(self.config.source_path, self.config.source_hash)
                except (ValueError, OSError, RuntimeError) as error:
                    self.state.log_event('autotune_failed', '', reason=str(error))
                    self.state.save()
            return result

    def _run_owned(self) -> dict[str, str]:
        self.prepare()
        if self.control:
            self.control.checkpoint(self)
        results: dict[str, str] = {}
        i = 0
        # 反馈环护栏：每轮返工要重跑「目标阶段→审查阶段」之间的全部阶段，
        # 故上限 = 阶段数 × (最大轮次+1)，而非简单相加（否则提前误中止）。
        max_iters = len(self.config.stages) * (1 + sum(
            s.max_rounds for s in self.config.stages
            if s.kind in ("review", "test") and s.feedback_to)) + 1
        iters = 0
        while i < len(self.config.stages):
            if self.control:
                self.control.checkpoint(self)
            iters += 1
            if iters > max_iters:
                # G4：护栏触发必须判失败，不能静默 break 留下"看似跑完"的部分结果
                cur = self.config.stages[i]
                stg = self.state.stage(cur.name)
                stg.status = StageStatus.FAILED
                stg.error = (f"反馈环迭代超护栏上限（{max_iters}），已中止；"
                             f"请检查 max_rounds 或人工介入")
                results[cur.name] = StageStatus.FAILED.value
                self.state.log_event("feedback_loop_guard", cur.name,
                                     max_iters=max_iters)
                self.state.save()
                break
            spec = self.config.stages[i]
            status = self._run_stage(spec, i)
            results[spec.name] = status.value
            if self.control:
                self.control.checkpoint(self)
            # L2：审查不通过 => 跳回反馈目标阶段重跑
            if status == StageStatus.REVIEW_FEEDBACK:
                if not spec.feedback_to:
                    # 防御：无反馈目标却要求返工 => 判失败，不崩也不空转
                    stg = self.state.stage(spec.name)
                    stg.status = StageStatus.FAILED
                    stg.error = "要求返工但未配置 feedback_to"
                    results[spec.name] = StageStatus.FAILED.value
                    self.state.log_event("rejected", spec.name,
                                         reason="missing_feedback_to")
                    self.state.save()
                    break
                i = self.config.stage_index(spec.feedback_to)
                continue
            # 锁占用 => 中止整轮（评审 B2），不让下游在无上游产物时照跑
            if status in (StageStatus.FAILED, StageStatus.AWAITING_APPROVAL,
                          StageStatus.RUNNING):
                break
            i += 1
        return results

    def rerun_from(self, stage_name: str) -> dict[str, str]:
        with self._ownership() as owned:
            if not owned:
                return {stage_name: StageStatus.RUNNING.value}
            return self._rerun_owned(stage_name)

    def _rerun_owned(self, stage_name: str) -> dict[str, str]:
        """从指定阶段重跑（评审 B2：重跑覆盖，旧产物已在 attempts 归档）。

        N1：必须同时清未决审批 + 递增失效计数。只清 pending 不够——
        adopt_successful 会拿旧 success.json（inputs_hash 未变）重新采纳，
        导致"重跑"变成静默跳过（对不可解析的审查等于默认放行）。
        """
        self.prepare()
        self._reset_from(stage_name)
        self.state.save()
        return self.run()

    def _reset_from(self, stage_name: str) -> None:
        """仅修改内存，便于将指令收据和失效计数原子提交。"""
        idx = self.config.stage_index(stage_name)
        for s in self.config.stages[idx:]:
            recovery=self.state.control.get('recoveries',{}).get(s.name)
            if recovery:recovery['status']='consumed'
            st = self.state.stage(s.name)
            st.status = StageStatus.PENDING
            st.current_attempt = None
            st.inputs_hash = None
            st.error = None
            # 清未决审批，让该阶段可被重新执行（含 review_unparsed 的 awaiting）
            self.state.pending_approvals.pop(s.name, None)
            # 上游重跑后旧审查反馈已过期；保留会让 coding 继续走返工分支、
            # 看不到新的设计/需求产物（P3）。
            self.state.feedback.pop(s.name, None)
            self.state.control.get('consumed_feedback', {}).pop(s.name, None)
            self.state.control.get('write_violations', {}).pop(s.name, None)
            # 重置审查轮次：rerun 是新一轮完整返工，不该继承历史轮次计数
            # （否则跨多次 rerun 累积，会刚返工一轮就误判"轮次耗尽"）
            self.state.review_rounds.pop(s.name, None)
            # 递增失效计数 => inputs_hash 变化 => 旧 attempt 不被采纳，真正实跑
            self.state.invalidate_nonce[s.name] = \
                self.state.invalidate_nonce.get(s.name, 0) + 1
            self.state.log_event("rerun_reset", s.name)
