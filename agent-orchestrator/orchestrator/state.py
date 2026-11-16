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
from typing import Any

from .contracts import StageStatus


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_inputs_hash(prompt_text: str, upstream_hashes: list[str],
                        config_keys: dict[str, Any]) -> str:
    """inputs_hash = hash(prompt 模板 + 上游产物哈希 + 本阶段读取的配置键)。"""
    payload = json.dumps(
        {"prompt": prompt_text, "upstream": sorted(upstream_hashes),
         "config": config_keys},
        sort_keys=True, ensure_ascii=False,
    )
    return sha256_text(payload)


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """临时文件 + 原子改名，避免半写状态。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{uuid.uuid4().hex}")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    # Windows 下读者/防护软件可能短暂持有目标文件；只重试尚未生效的原子替换。
    for attempt in range(7):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if attempt == 6:
                raise
            time.sleep(min(.01 * (2 ** attempt), .2))


def _pid_alive(pid: object) -> bool:
    """锁里的 pid 是否还在。Windows 上不能用 os.kill(pid, 0)，那会杀掉进程。"""
    try:
        pid_i = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if pid_i <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        process_query = 0x1000
        still_active = 259
        access_denied = 5
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel.GetExitCodeProcess.restype = ctypes.c_bool
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_bool
        handle = kernel.OpenProcess(process_query, False, pid_i)
        if not handle:
            return ctypes.get_last_error() == access_denied
        code = ctypes.c_uint32()
        ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return bool(ok) and code.value == still_active
    try:
        os.kill(pid_i, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class StageLock:
    """文件租约锁（评审补测 2：同阶段双进程只允许一个写）。

    用 O_CREAT|O_EXCL 原子创建锁文件；持有者写入 pid+token。
    释放时校验 token，避免误删他人锁。持有进程已经不在时立即回收，
    不等 24 小时；内容损坏的锁给一个短宽限期（创建后被强杀时内容可能为空）。
    回收先抢占 `.reclaim` sidecar，再核对旧锁，避免两个进程同时回收把对方
    刚创建的新锁删掉（TOCTOU，P2-11）。
    """

    MALFORMED_GRACE_S = 5.0
    CLAIM_STALE_S = 30.0

    def __init__(self, lock_path: Path, stale_s: float = 24 * 3600.0):
        self.lock_path = Path(lock_path)
        self.stale_s = stale_s
        self.token = uuid.uuid4().hex
        self._held = False

    def _claim_path(self) -> Path:
        return self.lock_path.with_name(self.lock_path.name + ".reclaim")

    def _content_stale(self) -> bool:
        """锁是否可回收：持有进程已退出，或内容损坏且超过短宽限期。"""
        try:
            stat = self.lock_path.stat()
        except OSError:
            return True
        try:
            data = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return time.time() - stat.st_mtime > self.MALFORMED_GRACE_S
        if not isinstance(data, dict):
            return time.time() - stat.st_mtime > self.MALFORMED_GRACE_S
        return not _pid_alive(data.get("pid"))

    def _claim_stale(self, claim: Path) -> bool:
        try:
            stat = claim.stat()
            data = json.loads(claim.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            try:
                return time.time() - claim.stat().st_mtime > self.MALFORMED_GRACE_S
            except OSError:
                return False
        if not isinstance(data, dict):
            return time.time() - stat.st_mtime > self.MALFORMED_GRACE_S
        try:
            age = time.time() - float(data.get("ts") or 0)
        except (TypeError, ValueError):
            age = self.CLAIM_STALE_S + 1
        return (not _pid_alive(data.get("pid"))) or age > self.CLAIM_STALE_S

    def _claim(self) -> bool:
        """原子抢占回收权；成功返回 True 并持有 sidecar。"""
        claim = self._claim_path()
        try:
            fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        except OSError:
            return False
        try:
            os.write(fd, json.dumps({"pid": os.getpid(), "ts": time.time()}).encode("utf-8"))
        finally:
            os.close(fd)
        return True

    def acquire(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        claim = self._claim_path()
        # 有未过期的回收者 => 让路，避免两个人同时接管。
        if claim.exists():
            if not self._claim_stale(claim):
                return False
            try:
                claim.unlink()
            except OSError:
                return False
        # 过期/损坏锁：抢占 reclaim sidecar 后再动手，防止删掉别人的新锁。
        if self.lock_path.exists() and self._content_stale():
            if not self._claim():
                return False
            try:
                if self.lock_path.exists() and self._content_stale():
                    try:
                        self.lock_path.unlink()
                    except FileNotFoundError:
                        pass
            finally:
                try:
                    claim.unlink()
                except OSError:
                    pass
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"pid": os.getpid(), "token": self.token,
                                "ts": time.time()}))
        self._held = True
        return True

    def release(self) -> None:
        if not self._held:
            return
        try:
            data = json.loads(self.lock_path.read_text(encoding="utf-8"))
            if data.get("token") == self.token:
                self.lock_path.unlink()
        except (OSError, ValueError):
            pass
        self._held = False

    def __enter__(self) -> "StageLock":
        if not self.acquire():
            raise RuntimeError(f"阶段锁被占用: {self.lock_path}")
        return self

    def __exit__(self, *exc) -> None:
        self.release()


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
    """一次 run 的持久化状态。state.json 为唯一权威。"""

    def __init__(self, state_dir: Path, run_id: str):
        if (not isinstance(run_id, str) or not re.fullmatch(r"[\w.-]+", run_id)
                or ".." in run_id or run_id == "."):
            raise ValueError("run_id 必须是不含路径分隔符的标识符")
        self.state_dir = Path(state_dir)
        self.run_id = run_id
        self.run_dir = self.state_dir / "runs" / run_id
        self.state_file = self.run_dir / "state.json"
        self.stages: dict[str, StageState] = {}
        self.pending_approvals: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
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

    # ---- 持久化 ----
    def load(self) -> bool:
        if not self.state_file.is_file():
            return False
        data = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.task = data.get("task", "")
        self.stages = {k: StageState.from_dict(v) for k, v in data.get("stages", {}).items()}
        self.pending_approvals = data.get("pending_approvals", {})
        self.events = data.get("events", [])
        self.usage_total = data.get("usage_total", {"tokens": 0, "duration_s": 0.0})
        self.feedback = data.get("feedback", {})
        self.review_rounds = data.get("review_rounds", {})
        self.invalidate_nonce = data.get("invalidate_nonce", {})
        self.stage_usage = data.get("stage_usage", {})
        self.autonomy = data.get("autonomy", {})
        self.revision = data.get('revision', 0)
        self.control = data.get('control', {})
        self.test_artifacts = data.get('test_artifacts', {})
        self.test_artifact_hashes = data.get('test_artifact_hashes', {})
        return True

    def save(self) -> None:
        self.revision += 1
        atomic_write_json(self.state_file, {
            "schema_version": 2,
            "run_id": self.run_id,
            "task": self.task,
            "stages": {k: v.to_dict() for k, v in self.stages.items()},
            "pending_approvals": self.pending_approvals,
            "events": self.events,
            "usage_total": self.usage_total,
            "feedback": self.feedback,
            "review_rounds": self.review_rounds,
            "invalidate_nonce": self.invalidate_nonce,
            "stage_usage": self.stage_usage,
            "autonomy": self.autonomy,
            "test_artifacts": self.test_artifacts,
            "test_artifact_hashes": self.test_artifact_hashes,
            'revision': self.revision,
            'control': self.control,
        })

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

    def new_attempt_dir(self, name: str) -> tuple[int, Path]:
        adir = self.attempts_dir(name)
        adir.mkdir(parents=True, exist_ok=True)
        existing = [int(p.name) for p in adir.iterdir() if p.is_dir() and p.name.isdigit()]
        n = (max(existing) + 1) if existing else 1
        path = adir / str(n)
        path.mkdir(parents=True, exist_ok=True)
        # 调用前即持久化：失败、超时或进程崩溃也必须留下真实尝试次数。
        self._set_attempt_record(name, n, path, success=False)
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
            if not validate(directory, data):
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
            if approved(directory, data):
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
        if data.get("verdict") in ("fail", "unparsed"):
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
                       error: str | None = None) -> None:
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
