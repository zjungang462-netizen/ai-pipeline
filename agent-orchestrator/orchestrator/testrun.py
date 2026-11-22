"""测试阶段：真实执行命令，不调用模型。

配置 `test_cmd`（数组，避免 shell 注入），引擎在该阶段 worktree 里执行：
- 记录退出码、耗时、截断后的输出，落盘 test_record.json + test_output.txt。
- 非零退出码即失败，直接作为审查反馈，不必再问模型。
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import re
import time
from pathlib import Path
from typing import Any

from .safety import build_subprocess_env

MAX_OUTPUT = 20000        # 落盘输出上限（字符）


def run_test_cmd(cmd: list[str], workdir: Path, timeout_s: int = 600,
                 extra_env: list[str] | None = None) -> dict[str, Any]:
    """执行测试命令，返回确定性记录。不经过 shell，避免注入。"""
    if not cmd:
        return {"ok": False, "exit_code": -1, "cmd": [], "duration_s": 0.0,
                "output": "", "error": "未配置 test_cmd"}
    # 纯测试流水线可能没有代码阶段，工作区尚未创建 => 先确保存在，
    # 否则 subprocess 会因 cwd 无效直接失败。
    workdir = Path(workdir)
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "exit_code": -1, "cmd": list(cmd), "duration_s": 0.0,
                "output": "", "error": f"工作目录不可用: {e}"}
    t0 = time.monotonic()
    env = build_subprocess_env(extra_allow=extra_env)
    argv = list(cmd)
    if argv and not Path(argv[0]).is_file():
        resolved = shutil.which(argv[0], path=env.get("PATH"))
        if resolved:
            argv[0] = resolved
    try:
        proc = subprocess.Popen(
            argv, cwd=str(workdir),
            env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        tails = [bytearray(), bytearray()]
        counts = [0, 0]
        def drain(pipe, index):
            try:
                while chunk := pipe.read(65536):
                    counts[index] += len(chunk)
                    tails[index].extend(chunk)
                    del tails[index][:-MAX_OUTPUT * 4]
            finally:
                pipe.close()
        threads = [threading.Thread(target=drain, args=(pipe, i), daemon=True)
                   for i, pipe in enumerate((proc.stdout, proc.stderr))]
        for thread in threads:
            thread.start()
        err = None
        try:
            proc.wait(timeout=timeout_s)
            for thread in threads:
                thread.join(max(0, timeout_s - (time.monotonic() - t0)))
            if any(thread.is_alive() for thread in threads):
                raise subprocess.TimeoutExpired(cmd, timeout_s)
        except subprocess.TimeoutExpired:
            # 先处理子进程树，避免后代持有管道导致 communicate 永不返回。
            if os.name == "nt":
                try:
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env=build_subprocess_env(), timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            for thread in threads:
                thread.join(1)
            err = f"测试超时（{timeout_s}s）"
        stdout, stderr = (bytes(t).decode("utf-8", errors="replace")[-MAX_OUTPUT:] for t in tails)
        out = stdout + stderr
        code = -1 if err else proc.returncode
    except subprocess.TimeoutExpired as e:
        def _d(b: object) -> str:
            if isinstance(b, bytes):
                return b.decode("utf-8", errors="replace")
            return b or ""
        out = _d(e.stdout) + _d(e.stderr)
        code = -1
        err = f"测试超时（{timeout_s}s）"
    except OSError as e:
        out = ""
        code = -1
        err = f"无法执行测试命令: {e}"
    duration = time.monotonic() - t0
    truncated = len(out) > MAX_OUTPUT or any(c > MAX_OUTPUT for c in locals().get("counts", []))
    failure_ids = re.findall(r'(?m)^(?:FAIL|ERROR):\s*(.+)$', out)
    failure_ids += re.findall(r'(?m)^FAILED\s+([^\n]+)', out)
    if re.search(r'Ran 0 tests\b', out):
        code, err = -1, '测试未收集到任何用例'
    return {
        "ok": code == 0,
        "exit_code": code,
        "cmd": list(cmd),
        "duration_s": duration,
        "output": out[-MAX_OUTPUT:] if truncated else out,
        "truncated": truncated,
        "error": err,
        "failure_ids": sorted(set(failure_ids))[:30],
    }


def write_record(record: dict[str, Any], attempt_dir: Path) -> tuple[Path, Path]:
    """落盘执行记录与完整输出节选，返回 (record.json, output.txt)。"""
    attempt_dir = Path(attempt_dir)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    rec_path = attempt_dir / "test_record.json"
    out_path = attempt_dir / "test_output.txt"
    rec_path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    out_path.write_text(record.get("output") or "", encoding="utf-8")
    return rec_path, out_path
