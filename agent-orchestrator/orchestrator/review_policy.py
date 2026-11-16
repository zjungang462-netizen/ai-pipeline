"""提示词上下文、输入指纹、审查覆盖与反馈策略。"""
from __future__ import annotations

from . import artifacts
from .contracts import ErrorKind, Result, RunContext, StageSpec, StageStatus
from .safety import WriteRootGuard, WriteRootViolation, relative_target
from .state import RunState, StageLock, atomic_write_json, compute_inputs_hash, sha256_text
from dataclasses import asdict
from pathlib import Path
import json
import os
import re
import time


_UPSTREAM_REPORT_LIMIT = 20000


class ReviewPolicy:
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

    def _inputs_hash(self, spec: StageSpec, index: int, prompt_override: str | None = None,
                     workspace_hash_override: str | None = None) -> str:
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
                config_keys["__workspace__"] = (workspace_hash_override
                    if workspace_hash_override is not None else artifacts.workspace_hash(
                        self.config.task_workdir, exclude=self.config.state_dir,
                        ignore_hashes=self.state.test_artifact_hashes.get(spec.name, {})))
                # 旧版仅按路径忽略的结果不能直接沿用。
                config_keys["__test_input_protocol__"] = 2
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

    def _exhausted_review_stands(self, spec: StageSpec, inputs_hash: str) -> bool:
        """同一输入上的审查耗尽结果保持失败，续跑不改判通过、也不再调用模型。"""
        if spec.kind != "review":
            return False
        st = self.state.stage(spec.name)
        if st.status != StageStatus.FAILED or st.inputs_hash != inputs_hash:
            return False
        return bool(st.error) and "仍未通过" in st.error

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
