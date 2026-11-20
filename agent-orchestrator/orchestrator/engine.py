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


from .budget import BudgetPolicy, BudgetExceeded
from .archive import ArchiveOperations, StageArchiveError


from .review_policy import ReviewPolicy
from .review_policy import _UPSTREAM_REPORT_LIMIT


from .guards import GuardControls
from .execution import StageExecutor


class Engine(GuardControls, StageExecutor, ReviewPolicy, BudgetPolicy, ArchiveOperations, AutonomousControls):
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
        self._explicit_resume = False
        self._prompts: dict[str, str] = {}
        self._reports: dict[str, list[Path]] = {}
        # 审查上下文覆盖不完整（diff 截断且无法注入全部变更文件）=> 记录未纳入清单，
        # pass 结论不得直接当作完成，只能交人工确认/在无人值守下失败。
        self._review_missing: dict[str, list[str]] = {}
        self._review_context_cache = {}
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
        self._review_context_cache.clear()
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

        if (st.status in (StageStatus.INTERRUPTED, StageStatus.RUNNING)
                and spec.kind == "report" and spec.name not in self.state.pending_waits):
            recovered = self._recover_interrupted_wait(spec, index)
            if recovered is not None:
                return recovered

        if spec.name in self.state.pending_waits or st.status in (
                StageStatus.WAITING_EXTERNAL, StageStatus.WAITING_OWNER):
            waiting = self._resume_deferred(spec, index)
            if waiting is not None:
                return waiting

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

    def _resume_deferred(self, spec: StageSpec, index: int) -> StageStatus | None:
        """恢复前校验等待 attempt 的回执、配置绑定和封存清单。"""
        from .evidence import validate
        from .stage_result import validate_deferred

        st = self.state.stage(spec.name)
        pending = self.state.pending_waits.get(spec.name)
        if (not isinstance(pending, dict) or spec.kind != "report" or not spec.allow_wait
                or st.status.value not in ("waiting_external", "waiting_owner")):
            st.status = StageStatus.FAILED
            st.error = "等待状态缺少有效回执，拒绝自动重跑"
            self.state.log_event("deferred_rejected", spec.name, reason="missing_or_inconsistent_wait")
            self.state.save()
            return StageStatus.FAILED

        directory = self.state.current_attempt_dir(spec.name)
        record = {"n": st.current_attempt, "inputs_hash": st.inputs_hash,
                  "outputs_hash": st.outputs_hash, "commit_sha": st.commit_sha}
        try:
            deferred = validate_deferred({key: pending.get(key) for key in
                                          ("outcome", "reason", "condition", "resume_action", "next_check_at")})
            saved = json.loads((directory / "stage_result.json").read_text(encoding="utf-8")) if directory else None
            saved = validate_deferred(saved)
            target_matches = (
                pending.get("n") == st.current_attempt
                and pending.get("inputs_hash") == st.inputs_hash
                and pending.get("outputs_hash") == st.outputs_hash
                and pending.get("config_hash") == self.config.source_hash
                and pending.get("task_hash") == sha256_text(self.state.task)
                and st.status.value == deferred["outcome"]
                and saved == deferred
                and directory is not None
                and validate(directory, record)
                and self._inputs_hash(spec, index) == st.inputs_hash
            )
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            target_matches = False
            deferred = {}
        if not target_matches:
            st.status = StageStatus.FAILED
            st.error = "等待回执、任务配置或封存产物已变化，拒绝自动重跑"
            self.state.log_event("deferred_rejected", spec.name, reason="checkpoint_mismatch")
            self.state.save()
            return StageStatus.FAILED

        target = [spec.name, st.current_attempt, st.outputs_hash]
        if not self._explicit_resume and self.state.control.get('resume_wait') != target:
            return st.status
        self.state.control.pop('resume_wait', None)
        if (deferred["outcome"] == StageStatus.WAITING_EXTERNAL.value
                and deferred["next_check_at"] is not None
                and time.time() < deferred["next_check_at"]):
            self.state.save()
            return StageStatus.WAITING_EXTERNAL

        self.state.pending_waits.pop(spec.name, None)
        self.state.control.setdefault('wait_context', {})[spec.name] = {**deferred, 'attempt': record['n']}
        st.status = StageStatus.PENDING
        st.current_attempt = None
        st.inputs_hash = None
        st.outputs_hash = None
        st.error = None
        self.state.invalidate_nonce[spec.name] = self.state.invalidate_nonce.get(spec.name, 0) + 1
        self.state.log_event("deferred_resumed", spec.name, n=record["n"], outcome=deferred["outcome"])
        self.state.save()
        return None

    def _recover_interrupted_wait(self, spec: StageSpec, index: int) -> StageStatus | None:
        """Recover a fully sealed wait after a crash; partial wait receipts fail closed."""
        from .evidence import validate
        from .stage_result import validate_deferred

        attempts = self.state.attempts_dir(spec.name)
        numbered = sorted((path for path in attempts.iterdir() if path.is_dir() and path.name.isdigit()),
                          key=lambda path: int(path.name), reverse=True) if attempts.is_dir() else []
        directory = numbered[0] if numbered else None
        if directory is None or not (directory / "stage_result.json").is_file():
            return None
        n = int(directory.name)
        try:
            deferred = validate_deferred(json.loads((directory / "stage_result.json").read_text(encoding="utf-8")))
            success = json.loads((directory / "success.json").read_text(encoding="utf-8"))
            decision = json.loads((directory / "decision.json").read_text(encoding="utf-8"))
            if not isinstance(success, dict) or not isinstance(decision, dict):
                raise ValueError("等待 attempt 元数据格式错误")
            current_hash = self._inputs_hash(spec, index)
            record = {"n": n, "inputs_hash": success.get("inputs_hash"),
                      "outputs_hash": success.get("outputs_hash"), "commit_sha": success.get("commit_sha")}
            valid = (spec.allow_wait and success.get("n") == n
                     and success.get("verdict") == deferred["outcome"]
                     and decision.get("verdict") == deferred["outcome"]
                     and decision.get("status") == deferred["outcome"]
                     and success.get("inputs_hash") == current_hash
                     and all(decision.get(key) == success.get(key)
                             for key in ("n", "inputs_hash", "outputs_hash"))
                     and validate(directory, record))
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            valid = False
            deferred = {}
            record = {}
        if not valid:
            st = self.state.stage(spec.name)
            st.status = StageStatus.FAILED
            st.error = "中断的阶段已有等待回执但 attempt 未完整封存，拒绝自动重跑；核对外部效果后显式 rerun"
            self.state.pending_waits[spec.name] = {"n": n, "outcome": "rejected"}
            self.state.log_event("deferred_rejected", spec.name, n=n, reason="interrupted_partial_receipt")
            self.state.save()
            return StageStatus.FAILED

        status = StageStatus(deferred["outcome"])
        self.state._apply_record(spec.name, n, success, status, deferred["reason"])
        self.state.pending_waits[spec.name] = {
            **deferred, "n": n, "inputs_hash": success["inputs_hash"],
            "outputs_hash": success["outputs_hash"], "config_hash": self.config.source_hash,
            "task_hash": sha256_text(self.state.task),
        }
        self.state.log_event("deferred_recovered", spec.name, n=n, outcome=deferred["outcome"])
        self.state.save()
        return status


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
        self.state._set_attempt_record(stage_name, st.current_attempt, directory, success=True,
                                       outputs_hash=st.outputs_hash, commit_sha=st.commit_sha)
        self.state.log_event("approved", stage_name)
        self.state.save()
        return True

    # ---- 主流程 ----
    def run(self, *, resume_wait: bool = False) -> dict[str, str]:
        with self._ownership() as owned:
            if not owned:
                return {self.config.stages[0].name: StageStatus.RUNNING.value}
            self._explicit_resume = resume_wait and self.control is None
            try:
                result = self._run_owned()
            finally:
                self._explicit_resume = False
            from .metrics import write_metrics
            write_metrics(self, result)
            if self.config.autotune.enabled and self.config.source_path and not any(
                    status in ('running', 'awaiting_approval', 'waiting_external', 'waiting_owner')
                    for status in result.values()):
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
                          StageStatus.WAITING_EXTERNAL, StageStatus.WAITING_OWNER,
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
            self.state.pending_waits.pop(s.name, None)
            self.state.control.get('wait_context', {}).pop(s.name, None)
            if (self.state.control.get('resume_wait') or [None])[0] == s.name:
                self.state.control.pop('resume_wait', None)
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
