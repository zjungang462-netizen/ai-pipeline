"""阶段 attempt 执行；由 Engine 状态机调用，顺序与原实现一致。"""
from __future__ import annotations

from . import artifacts
from .archive import ArchiveOperations, StageArchiveError
from .archive import StageArchiveError
from .budget import BudgetExceeded
from .budget import BudgetPolicy, BudgetExceeded
from .contracts import ErrorKind, Result, RunContext, StageSpec, StageStatus
from .safety import WriteRootGuard, WriteRootViolation, relative_target
from .state import RunState, StageLock, atomic_write_json, compute_inputs_hash, sha256_text
from pathlib import Path
import time


class StageExecutor:
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
            # 无上游 commit 时记录新输出及其内容；既有输入被改写须拒收，
            # 不能把测试/其它进程改写的源码自动降格为可忽略的输出。
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
                    changed_inputs = self._record_test_artifacts(spec, before_workspace,
                                                                 successful=record['ok'])
                    if changed_inputs:
                        message = '被测输入在测试期间发生变化：' + ', '.join(changed_inputs[:20])
                        record['ok'] = False
                        record['error'] = message
                        record['output'] = record.get('output', '') + '\n' + message
                        record['failure_ids'] = list(record.get('failure_ids', [])) + ['test_inputs_modified']
                        self.state.log_event('test_inputs_modified', spec.name,
                                             files=changed_inputs[:20], count=len(changed_inputs))
                    elif record['ok']:
                        # 已执行的测试验证了当前输入；用新输出哈希规范化缓存，
                        # 避免产物被外部修改后验证成功还要额外重跑一轮。
                        outputs = self.state.test_artifact_hashes.get(spec.name, {})
                        frozen = sorted((rel, digest) for rel, digest in before_workspace.items()
                                        if rel not in outputs)
                        inputs_hash = self._inputs_hash(
                            spec, index, workspace_hash_override=sha256_text(repr(frozen)))
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
            guard = self._make_write_guard(spec)
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
                # 无法按目录快照归属写入者；只保存变化元数据，不移动、删除
                # 或复制无关进程的原文件，继续保守失败并持久化违规记录。
                evidence = self._record_outside_evidence(spec, before_snap, after_snap, outside)
                self.state.control.setdefault('write_violations', {})[spec.name] = {
                    'inputs_hash': inputs_hash, 'files': outside[:20],
                    'evidence': evidence, 'source_files_preserved': True, 'at': time.time()}
                self.state.log_event("watch_enforce", spec.name,
                                     files=outside[:10], evidence=evidence,
                                     source_files_preserved=True)
                self.state.save()
                result.ok = False
                result.error_kind = ErrorKind.REJECTED
                result.stderr = (result.stderr + f" [可写根外变更: {outside[:5]}]").strip()

            # 代码阶段：检测实际变更文件（评审 B4），归一化为相对路径供按清单提交
            bad_paths: list[str] = []
            if spec.kind == "code" and self._git_ready and result.ok:
                comparison_base = base_sha or artifacts.head_sha(workdir)
                observed_changes = None
                if not result.changed_files:
                    observed_changes = artifacts.changed_files_since(workdir, comparison_base)
                    result.changed_files = observed_changes
                result.changed_files = artifacts.normalize_rel(workdir, result.changed_files)
                bad_paths = artifacts.unsafe_rel_paths(result.changed_files)
                if bad_paths:
                    artifacts.remove_git_metadata_files(workdir, bad_paths)
                    result.ok = False
                    result.error_kind = ErrorKind.REJECTED
                    result.stderr = (result.stderr + f" [拒绝越界或 .git 路径: {bad_paths[:5]}]").strip()
                if not bad_paths:
                    scope_error = self._check_code_scope(spec, result, workdir, comparison_base,
                                                         observed_changes=observed_changes)
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
