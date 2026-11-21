"""状态持久化与完成判据（评审 B1/B4）。

- 完成判据 = (inputs_hash, outputs_hash) 双元组。
- attempt 目录隔离：成功后才把 current 指针原子切到该 attempt。
- 启动扫描：有 attempts 但无 success.json => interrupted。
- 租约锁：同阶段双进程只允许一个写。
- 状态写入：临时文件 + 原子改名（os.replace）。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .contracts import StageStatus
from .event_journal import append_events, read_events
from .state_io import (
    StageLock,
    _pid_alive,
    atomic_write_json,
    compute_inputs_hash,
    sha256_file,
    sha256_text,
)

if TYPE_CHECKING:
    from .artifacts import GitContext


@dataclass
class AttemptRecord:
    n: int
    path: str
    success: bool
    outputs_hash: str | None = None
    commit_sha: str | None = None


@dataclass
class StageState:
    name: str
    status: StageStatus = StageStatus.PENDING
    inputs_hash: str | None = None
    outputs_hash: str | None = None
    current_attempt: int | None = None
    commit_sha: str | None = None
    branch: str | None = None
    attempts: list[AttemptRecord] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "inputs_hash": self.inputs_hash,
            "outputs_hash": self.outputs_hash,
            "current_attempt": self.current_attempt,
            "commit_sha": self.commit_sha,
            "branch": self.branch,
            "attempts": [vars(a) for a in self.attempts],
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StageState":
        return cls(
            name=d["name"],
            status=StageStatus(d.get("status", "pending")),
            inputs_hash=d.get("inputs_hash"),
            outputs_hash=d.get("outputs_hash"),
            current_attempt=d.get("current_attempt"),
            commit_sha=d.get("commit_sha"),
            branch=d.get("branch"),
            attempts=[AttemptRecord(**a) for a in d.get("attempts", [])],
            error=d.get("error"),
        )


class RunState:
    """运行快照和快照引用的已提交事件。"""

    SCHEMA_VERSION = 3
    STATE_FIELDS = {
        'schema_version', 'run_id', 'task', 'stages', 'pending_approvals', 'pending_waits', 'events',
        'usage_total', 'feedback', 'review_rounds', 'invalidate_nonce', 'stage_usage',
        'autonomy', 'test_artifacts', 'test_artifact_hashes', 'revision', 'control',
    }

    def __init__(self, state_dir: Path, run_id: str, *, git: GitContext | None = None):
        if (not isinstance(run_id, str) or not re.fullmatch(r"[\w.-]+", run_id)
                or ".." in run_id or run_id == "."):
            raise ValueError("run_id 必须是不含路径分隔符的标识符")
        self.state_dir = Path(state_dir)
        self.run_id = run_id
        self.git = git
        self.run_dir = self.state_dir / "runs" / run_id
        self.state_file = self.run_dir / "state.json"
        self.stages: dict[str, StageState] = {}
        self.pending_approvals: dict[str, dict[str, Any]] = {}
        self.pending_waits: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self._events_bytes = 0
        self._saved_events = 0
        self.usage_total: dict[str, Any] = {"tokens": 0, "duration_s": 0.0}
        self.task: str = ""
        # L2 审查反馈环：阶段名 -> 反馈文本；阶段名 -> 已用轮次；阶段名 -> 失效计数
        self.feedback: dict[str, str] = {}
        self.review_rounds: dict[str, int] = {}
        self.invalidate_nonce: dict[str, int] = {}
        self.stage_usage: dict[str, dict[str, float]] = {}
        self.autonomy: dict[str, Any] = {}
        self.revision: int = 0
        self.control: dict[str, Any] = {}
        # 测试阶段产出的工作区文件（相对路径）。这些是测试输出而非被测输入：
        # 计入 inputs_hash 会让每次 resume 都因新产物判为 stale（P2-1）。
        self.test_artifacts: dict[str, list[str]] = {}
        # 仅有路径不足以信任输出：内容变化或丢失时必须重新执行测试。
        self.test_artifact_hashes: dict[str, dict[str, str]] = {}
        self._unsafe_to_save = False

    def _invalid_state(self, message: str) -> ValueError:
        self._unsafe_to_save = True
        return ValueError(message)

    # ---- 持久化 ----
    def load(self) -> bool:
        if not self.state_file.is_file():
            return False
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise self._invalid_state('state.json 无法安全读取') from error
        if not isinstance(data, dict):
            raise self._invalid_state('state.json 根节点必须是对象')
        version = data.get('schema_version')
        if type(version) is not int or version not in (2, self.SCHEMA_VERSION):
            raise self._invalid_state('state.json schema_version 不受支持')
        fields = self.STATE_FIELDS | {'events_bytes'} if version == 3 else self.STATE_FIELDS
        unknown = set(data) - fields
        if unknown:
            raise self._invalid_state('state.json 含有当前版本无法安全保留的字段')
        if data.get('run_id') != self.run_id:
            raise self._invalid_state('state.json run_id 与目标运行不匹配')

        defaults = {
            'task': '', 'stages': {}, 'pending_approvals': {}, 'pending_waits': {}, 'events': [],
            'usage_total': {'tokens': 0, 'duration_s': 0.0}, 'feedback': {},
            'review_rounds': {}, 'invalidate_nonce': {}, 'stage_usage': {},
            'autonomy': {}, 'revision': 0, 'control': {}, 'test_artifacts': {},
            'test_artifact_hashes': {},
        }
        values = {**defaults, **data}
        if not isinstance(values['task'], str):
            raise self._invalid_state('state.json task 字段格式错误')
        if type(values['revision']) is not int or values['revision'] < 0:
            raise self._invalid_state('state.json revision 字段格式错误')
        for key in ('stages', 'pending_approvals', 'pending_waits', 'usage_total', 'feedback',
                    'review_rounds', 'invalidate_nonce', 'stage_usage', 'autonomy',
                    'control', 'test_artifacts', 'test_artifact_hashes'):
            if not isinstance(values[key], dict):
                raise self._invalid_state(f'state.json {key} 字段必须是对象')
        try:
            events = read_events(self.run_dir, data)
            if version == 2 and any(not isinstance(event, dict) for event in events):
                raise ValueError('旧版运行事件必须是对象')
        except (OSError, ValueError) as error:
            raise self._invalid_state(f'无法安全读取运行事件: {error}') from error
        try:
            stages = {name: StageState.from_dict(stage)
                      for name, stage in values['stages'].items()}
        except (KeyError, TypeError, ValueError) as error:
            raise self._invalid_state('state.json stages 内容格式错误') from error

        # 完整解析并校验后再替换实例字段，避免损坏/未来版本部分污染后续保存。
        self.task = values['task']
        self.stages = stages
        self.pending_approvals = values['pending_approvals']
        self.pending_waits = values['pending_waits']
        self.events = events
        self._events_bytes = data['events_bytes'] if version == 3 else 0
        self._saved_events = len(events) if version == 3 else 0
        self.usage_total = values['usage_total']
        self.feedback = values['feedback']
        self.review_rounds = values['review_rounds']
        self.invalidate_nonce = values['invalidate_nonce']
        self.stage_usage = values['stage_usage']
        self.autonomy = values['autonomy']
        self.revision = values['revision']
        self.control = values['control']
        self.test_artifacts = values['test_artifacts']
        self.test_artifact_hashes = values['test_artifact_hashes']
        return True

    def save(self) -> None:
        if self._unsafe_to_save:
            raise ValueError('state.json 加载校验失败；拒绝覆盖原始状态')
        events_bytes = append_events(self.run_dir, self._events_bytes, self.events[self._saved_events:])
        revision = self.revision + 1
        atomic_write_json(self.state_file, {
            "schema_version": self.SCHEMA_VERSION,
            "run_id": self.run_id,
            "task": self.task,
            "stages": {k: v.to_dict() for k, v in self.stages.items()},
            "pending_approvals": self.pending_approvals,
            **({"pending_waits": self.pending_waits} if self.pending_waits else {}),
            "events_bytes": events_bytes,
            "usage_total": self.usage_total,
            "feedback": self.feedback,
            "review_rounds": self.review_rounds,
            "invalidate_nonce": self.invalidate_nonce,
            "stage_usage": self.stage_usage,
            "autonomy": self.autonomy,
            "test_artifacts": self.test_artifacts,
            "test_artifact_hashes": self.test_artifact_hashes,
            'revision': revision,
            'control': self.control,
        })
        self.revision = revision
        self._events_bytes = events_bytes
        self._saved_events = len(self.events)

    def log_event(self, kind: str, stage: str, **kw: Any) -> None:
        self.events.append({'event_id': uuid.uuid4().hex, 'engine_seq': len(self.events) + 1,
                            "kind": kind, "stage": stage, "ts": time.time(), **kw})

    # ---- 阶段状态 ----
    def stage(self, name: str) -> StageState:
        if name not in self.stages:
            self.stages[name] = StageState(name=name)
        return self.stages[name]

    def mark_stale_downstream(self, ordered_names: list[str], from_index: int) -> None:
        """inputs_hash 变化 => 该阶段及所有下游标 stale（评审 B1）。"""
        for name in ordered_names[from_index:]:
            st = self.stage(name)
            if st.status == StageStatus.DONE:
                st.status = StageStatus.STALE
                self.log_event("stale", name)

    # ---- attempts 目录 ----
    def stage_dir(self, name: str) -> Path:
        return self.run_dir / "stages" / name

    def attempts_dir(self, name: str) -> Path:
        return self.stage_dir(name) / "attempts"

    def new_attempt_dir(self, name: str, *, start_event: str | None = None,
                        **event_fields: Any) -> tuple[int, Path]:
        adir = self.attempts_dir(name)
        adir.mkdir(parents=True, exist_ok=True)
        existing = [int(p.name) for p in adir.iterdir() if p.is_dir() and p.name.isdigit()]
        n = (max(existing) + 1) if existing else 1
        path = adir / str(n)
        path.mkdir(parents=True, exist_ok=True)
        # 调用前即持久化：失败、超时或进程崩溃也必须留下真实尝试次数。
        self._set_attempt_record(name, n, path, success=False)
        if start_event is not None:
            self.stage(name).status = StageStatus.RUNNING
            self.log_event(start_event, name, n=n, **event_fields)
        self.save()
        return n, path

    def _set_attempt_record(self, name: str, n: int, path: Path, *, success: bool,
                            outputs_hash=None, commit_sha=None) -> None:
        stage = self.stage(name)
        record = next((a for a in stage.attempts if a.n == n), None)
        if record is None:
            stage.attempts.append(AttemptRecord(n=n, path=str(path), success=success,
                                               outputs_hash=outputs_hash, commit_sha=commit_sha))
        else:
            record.path, record.success = str(path), success
            record.outputs_hash, record.commit_sha = outputs_hash, commit_sha

    def current_attempt_dir(self, name: str) -> Path | None:
        st = self.stage(name)
        if st.current_attempt is None:
            return None
        return self.attempts_dir(name) / str(st.current_attempt)

    def failure_reason(self, name: str) -> str | None:
        """仅从与当前失败 attempt 绑定的结论读取原因码。"""
        stage = self.stage(name)
        directory = self.current_attempt_dir(name)
        if (stage.status != StageStatus.FAILED or directory is None
                or not stage.inputs_hash or not stage.outputs_hash):
            return None
        try:
            decision = json.loads((directory / "decision.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(decision, dict):
            return None
        if (decision.get("status"), decision.get("verdict"), decision.get("n"),
                decision.get("inputs_hash"), decision.get("outputs_hash")) != (
                StageStatus.FAILED.value, "fail", stage.current_attempt,
                stage.inputs_hash, stage.outputs_hash):
            return None
        reason = decision.get("reason_code")
        return reason if isinstance(reason, str) else None

    def scan_interrupted(self) -> list[str]:
        """启动扫描：有 attempts 目录但无 success.json => interrupted（评审 B4）。"""
        interrupted: list[str] = []
        for name, st in self.stages.items():
            adir = self.attempts_dir(name)
            if not adir.is_dir():
                continue
            for p in adir.iterdir():
                if p.is_dir() and p.name.isdigit() and not any(a.n == int(p.name) for a in st.attempts):
                    # 恢复目录创建后、状态保存前崩溃的孤立 attempt；没有证据不猜成功。
                    self._set_attempt_record(name, int(p.name), p, success=False)
                    self.save()
                if p.is_dir() and p.name.isdigit() and not (p / "success.json").is_file():
                    # 该 attempt 未完成；若它正是 current 则阶段不可信
                    if st.current_attempt == int(p.name):
                        st.status = StageStatus.INTERRUPTED
                        st.current_attempt = None
                        interrupted.append(name)
        return interrupted

    def _best_success(self, name: str, inputs_hash: str) -> tuple[int, dict[str, Any]] | None:
        """指纹匹配的最新 success.json。没有则 None。"""
        adir = self.attempts_dir(name)
        if not adir.is_dir():
            return None
        best: tuple[int, dict[str, Any]] | None = None
        for p in sorted(adir.iterdir()):
            sj = p / "success.json"
            if not (p.is_dir() and p.name.isdigit() and sj.is_file()):
                continue
            try:
                data = json.loads(sj.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            if data.get("inputs_hash") != inputs_hash:
                continue
            n = int(p.name)
            if best is None or n > best[0]:
                best = (n, data)
        if best is not None:
            from .evidence import validate, approved
            n, data = best
            directory = adir / str(n)
            if not validate(directory, data, git=self.git):
                return None
            try:
                decision = json.loads((directory / "decision.json").read_text(encoding="utf-8"))
                if not isinstance(decision, dict):
                    return None
                if any(decision.get(k) != data.get(k)
                       for k in ("n", "inputs_hash", "outputs_hash")):
                    return None
            except (OSError, ValueError, TypeError):
                return None
            data = {**data, **decision}
            if data.get('status') not in ('waiting_external', 'waiting_owner') and approved(directory, data):
                data["verdict"] = "approved"
            best = n, data
        return best

    def _apply_record(self, name: str, n: int, data: dict[str, Any],
                      status: StageStatus, error: str | None) -> None:
        st = self.stage(name)
        adir = self.attempts_dir(name)
        st.current_attempt = n
        st.inputs_hash = data.get("inputs_hash")
        st.outputs_hash = data.get("outputs_hash")
        st.commit_sha = data.get("commit_sha")
        st.branch = data.get("branch")
        st.status = status
        st.error = error
        self._set_attempt_record(name, n, adir / str(n), success=status == StageStatus.DONE,
                                 outputs_hash=data.get("outputs_hash"), commit_sha=data.get("commit_sha"))

    def terminal_replay(self, name: str, inputs_hash: str) -> str | None:
        """最新匹配记录若是失败或不可解析结论，恢复该状态，不当作通过。

        返回 'failed' | 'unparsed' | None。
        """
        best = self._best_success(name, inputs_hash)
        if best is None:
            return None
        n, data = best
        verdict = data.get("verdict")
        if verdict == "fail":
            self._apply_record(name, n, data, StageStatus.FAILED,
                               data.get("error") or "审查未通过")
            self.save()
            return "failed"
        if verdict == "unparsed":
            self._apply_record(name, n, data, StageStatus.AWAITING_APPROVAL, None)
            self.pending_approvals[name] = {
                "stage": name, "outputs_hash": data.get("outputs_hash"),
                "requested_at": time.time(), "requested_by": "review_unparsed",
            }
            self.save()
            return "unparsed"
        return None

    def adopt_successful(self, name: str, inputs_hash: str) -> int | None:
        """采纳已成功的 attempt（评审 M1）：success.json 的 inputs_hash 匹配则
        直接切 current，不重跑。返回被采纳的 attempt 号，无则 None。

        verdict 为 fail / unparsed 的记录不能采纳成通过。
        """
        best = self._best_success(name, inputs_hash)
        if best is None:
            return None
        n, data = best
        if data.get("verdict") in ("fail", "unparsed", "waiting_external", "waiting_owner"):
            return None
        self._apply_record(name, n, data, StageStatus.DONE, None)
        self.log_event("adopted", name, n=n)
        self.save()
        return n

    # ---- 完成判据 ----
    def commit_success(self, name: str, n: int, attempt_dir: Path,
                       inputs_hash: str, outputs_hash: str,
                       commit_sha: str | None, branch: str | None,
                       verdict: str | None = None,
                       status: StageStatus = StageStatus.DONE,
                       error: str | None = None, *, reason_code: str | None = None) -> None:
        """写产物清单与独立结论，再原子切 current 指针。

        审查结论一并写入：pass 才是完成，fail 续跑不得被当成通过。
        """
        from .evidence import seal
        outputs_hash = seal(attempt_dir)
        payload: dict[str, Any] = {
            "schema_version": 2,
            "n": n, "inputs_hash": inputs_hash, "outputs_hash": outputs_hash,
            "commit_sha": commit_sha, "branch": branch, "ts": time.time(),
        }
        if verdict:
            payload["verdict"] = verdict
        if error:
            payload["error"] = error
        coverage_path = attempt_dir/'review-coverage.json'
        if coverage_path.is_file():
            coverage = json.loads(coverage_path.read_text(encoding='utf8'))
            payload['review_coverage'] = {'complete': coverage['complete'],
                                          'missing': coverage['missing'],
                                          'incomplete_policy': coverage['policy']['incomplete']}
        atomic_write_json(attempt_dir / "success.json", payload)
        atomic_write_json(attempt_dir / "decision.json", {
            **payload, "verdict": verdict or "pass", "status": status.value,
            **({"reason_code": reason_code} if reason_code is not None else {}),
        })
        st = self.stage(name)
        self._set_attempt_record(name, n, attempt_dir, success=status == StageStatus.DONE,
                                 outputs_hash=outputs_hash, commit_sha=commit_sha)
        st.current_attempt = n
        st.inputs_hash = inputs_hash
        st.outputs_hash = outputs_hash
        st.commit_sha = commit_sha
        st.branch = branch
        st.status = status
        st.error = error
        self.save()

    def is_complete(self, name: str, inputs_hash: str) -> bool:
        """完成判据：current 指向的 attempt 有 success.json 且双哈希匹配。"""
        st = self.stage(name)
        if st.status != StageStatus.DONE or st.current_attempt is None:
            return False
        cdir = self.current_attempt_dir(name)
        if cdir is None or not (cdir / "success.json").is_file():
            return False
        if st.inputs_hash != inputs_hash or st.outputs_hash is None:
            return False
        best = self._best_success(name, inputs_hash)
        return (best is not None and best[0] == st.current_attempt
                and best[1].get("outputs_hash") == st.outputs_hash
                and best[1].get("commit_sha") == st.commit_sha
                and best[1].get("verdict") not in ("fail", "unparsed"))
