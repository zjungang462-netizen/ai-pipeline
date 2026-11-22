"""输入指纹、审查覆盖判定与返工反馈策略。"""
from __future__ import annotations

from . import artifacts
from .contracts import ErrorKind, Result, RunContext, StageSpec, StageStatus
from .lessons import load_approved_lessons_text
from .prompt_context import PromptContextMixin, _UPSTREAM_REPORT_LIMIT
from .safety import WriteRootGuard, WriteRootViolation, relative_target
from .state import RunState, StageLock, atomic_write_json, compute_inputs_hash, sha256_text
from dataclasses import asdict
from pathlib import Path
import json
import os
import re
import time


class ReviewPolicy(PromptContextMixin):
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
        if spec.kind == 'review':
            config_keys['__review_coverage__'] = asdict(self.config.review_coverage)
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
        if spec.allow_wait:
            config_keys['__wait_protocol__'] = 1
            continuation = self.state.control.get('wait_context', {}).get(spec.name)
            if continuation:
                config_keys['__wait_context__'] = continuation
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
                tree = (artifacts.verified_workspace_tree(
                    self.config.task_workdir, exclude=self.config.state_dir, git=self.git) if self._git_ready else None)
                self._review_workspace_hash = (
                    (tree, True) if tree else
                    (artifacts.workspace_hash(self.config.task_workdir, exclude=self.config.state_dir), False))
            workspace, is_tree = self._review_workspace_hash
            config_keys["__review_workspace__"] = workspace
            if is_tree:
                config_keys["__review_workspace_protocol__"] = 3
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
            approved_lessons = (
                load_approved_lessons_text(self.config.state_dir)
                if spec.kind in ('code', 'report') else ''
            )
            approval_notes = (
                self._upstream_approval_notes(index)
                if spec.kind in ('code', 'report') else ''
            )
            logical = compute_inputs_hash(self._read_prompt(spec) + self.task + self._feedback(spec.name)
                                          + self.state.autonomy.get('recovery_hints', {}).get(spec.name, '')
                                          + approval_notes + approved_lessons,
                                          self._upstream_hashes(index), config_keys)
            saved = self.state.autonomy.setdefault('execution_inputs', {}).get(spec.name)
            if prompt_override is None and saved and saved['logical_hash'] == logical:
                self._prompts[spec.name] = saved['prompt']
                if spec.kind == 'review':
                    self._review_missing[spec.name] = saved.get('coverage_missing', [])
                return saved['inputs_hash']
        if spec.kind == "test":
            prompt = ""
        else:
            prompt = prompt_override if prompt_override is not None else self._render_prompt(spec, index)
            self._prompts[spec.name] = prompt
        inputs_hash = compute_inputs_hash(prompt, self._upstream_hashes(index), config_keys)
        if logical:
            self.state.autonomy['execution_inputs'][spec.name] = {
                'logical_hash': logical, 'inputs_hash': inputs_hash, 'prompt': prompt,
                'coverage_missing': self._review_missing.get(spec.name, [])}
        return inputs_hash

    def _exhausted_review_stands(self, spec: StageSpec, inputs_hash: str) -> bool:
        """同一输入上的审查耗尽结果保持失败，续跑不改判通过、也不再调用模型。"""
        if spec.kind != "review":
            return False
        st = self.state.stage(spec.name)
        if st.status != StageStatus.FAILED or st.inputs_hash != inputs_hash:
            return False
        if self.state.failure_reason(spec.name) == "review_exhausted":
            return True
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
            action = self.config.review_coverage.incomplete
            if action == 'fail':
                self.state.log_event('review_incomplete', spec.name, missing=missing[:20], policy=action)
                self.state.commit_success(spec.name, n, attempt_dir, inputs_hash, outputs_hash,
                    result.commit_sha, result.branch, verdict='fail', status=StageStatus.FAILED,
                    error='审查覆盖不完整，按 review_coverage.incomplete=fail 拒绝通过')
                return StageStatus.FAILED
            if action == 'allow':
                self.state.log_event('review_incomplete_allowed', spec.name, missing=missing[:20], policy=action)
            else:
                return self._incomplete_review_approval(spec, result, attempt_dir, n, inputs_hash,
                                                        outputs_hash, rounds_used, missing)

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

        return self._failed_review_feedback(spec, result, attempt_dir, n, inputs_hash,
                                             outputs_hash, rounds_used, verdict)

    def _incomplete_review_approval(self, spec, result, attempt_dir, n, inputs_hash,
                                   outputs_hash, rounds_used, missing):
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

    def _failed_review_feedback(self, spec, result, attempt_dir, n, inputs_hash,
                                outputs_hash, rounds_used, verdict):
        from .review import format_feedback
        # 不通过
        stopped = self._feedback_limit(spec, format_feedback(verdict, rounds_used)) if spec.feedback_to else ''
        if not spec.feedback_to or rounds_used >= spec.max_rounds or stopped:
            error = f"审查 {rounds_used} 轮仍未通过（上限 {spec.max_rounds}）"
            self.state.log_event("review_exhausted", spec.name,
                                 round=rounds_used, max_rounds=spec.max_rounds)
            self.state.commit_success(
                spec.name, n, attempt_dir, inputs_hash, outputs_hash,
                result.commit_sha, result.branch, verdict="fail",
                status=StageStatus.FAILED, error=stopped or error, reason_code="review_exhausted")
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
