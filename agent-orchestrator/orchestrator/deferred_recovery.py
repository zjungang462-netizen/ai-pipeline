"""延迟等待状态（waiting_external / waiting_owner）的显式恢复与中断封存核验。"""
from __future__ import annotations

import json
import time
from collections.abc import Callable

from .artifacts import GitContext
from .config import Config
from .contracts import StageSpec, StageStatus
from .evidence import validate
from .stage_result import validate_deferred
from .state import RunState, sha256_text


class DeferredWaitRecovery:
    def __init__(
        self,
        config: Config,
        state: RunState,
        git: GitContext,
        inputs_hash: Callable[[StageSpec, int], str],
    ):
        self.config = config
        self.state = state
        self.git = git
        self.inputs_hash = inputs_hash

    def resume_deferred(self, spec: StageSpec, index: int, *, explicit_resume: bool) -> StageStatus | None:
        """恢复前校验等待 attempt 的回执、配置绑定和封存清单。"""
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
                and validate(directory, record, git=self.git)
                and self.inputs_hash(spec, index) == st.inputs_hash
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
        if not explicit_resume and self.state.control.get('resume_wait') != target:
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

    def recover_interrupted_wait(self, spec: StageSpec, index: int) -> StageStatus | None:
        """Recover a fully sealed wait after a crash; partial wait receipts fail closed."""
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
            current_hash = self.inputs_hash(spec, index)
            record = {"n": n, "inputs_hash": success.get("inputs_hash"),
                      "outputs_hash": success.get("outputs_hash"), "commit_sha": success.get("commit_sha")}
            valid = (spec.allow_wait and success.get("n") == n
                     and success.get("verdict") == deferred["outcome"]
                     and decision.get("verdict") == deferred["outcome"]
                     and decision.get("status") == deferred["outcome"]
                     and success.get("inputs_hash") == current_hash
                     and all(decision.get(key) == success.get(key)
                             for key in ("n", "inputs_hash", "outputs_hash"))
                     and validate(directory, record, git=self.git))
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
