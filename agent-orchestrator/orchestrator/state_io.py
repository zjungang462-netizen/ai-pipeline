"""状态持久化基础原语：SHA-256 哈希、原子 JSON 写入与跨进程租约锁。"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


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
    try:
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
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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
