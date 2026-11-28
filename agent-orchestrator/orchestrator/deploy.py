"""受控部署执行器：强制前置阶段通过、审批哈希绑定、干跑预览与审计留证。"""
from __future__ import annotations

import codecs
from dataclasses import dataclass
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .artifacts import GitError, _git
from .cli_telemetry import StreamRedactor
from .contracts import StageStatus
from .output_tail import TextTail
from .safety import build_subprocess_env, environment_secrets
from .state import RunState, StageLock, atomic_write_json, sha256_text


_OUTPUT_LIMIT = 16000
_PROCESS_TREE_WAIT_S = 5


class DeployRejected(RuntimeError):
    """前置门禁、审批绑定或命令参数不满足受控部署契约。"""


@dataclass(frozen=True)
class DeploySpec:
    name: str
    command: tuple[str, ...]
    dry_run_command: tuple[str, ...] = ()
    timeout_s: int = 120
    allow_env: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError('DeploySpec.name 必须为非空字符串')
        if not self.command or not all(isinstance(arg, str) and arg for arg in self.command):
            raise ValueError('DeploySpec.command 必须为非空字符串 argv 数组')
        if self.dry_run_command and not all(isinstance(arg, str) and arg for arg in self.dry_run_command):
            raise ValueError('DeploySpec.dry_run_command 必须为字符串 argv 数组')
        if type(self.timeout_s) is not int or not 1 <= self.timeout_s <= 3600:
            raise ValueError('DeploySpec.timeout_s 必须在 1..3600 秒之间')


@dataclass(frozen=True)
class DeployApproval:
    run_id: str
    outputs_hash: str
    commit_sha: str | None = None
    approved_by: str = 'owner'
    approved_at: float = 0.0


def verify_deploy_gate(
    state: RunState,
    required_stages: tuple[str, ...],
    approval: DeployApproval | None,
    *,
    dry_run: bool = False,
) -> dict:
    """校验前置阶段全部 DONE 且（正式部署时）审批精确绑定当前产物哈希与提交。"""
    if not required_stages:
        raise DeployRejected('受控部署必须显式声明至少一个前置验收阶段')
    digests: dict[str, str] = {}
    required_names = set(required_stages)
    for stage_name in required_stages:
        st = state.stages.get(stage_name)
        if st is None or st.status != StageStatus.DONE or not st.outputs_hash:
            raise DeployRejected(f'前置阶段 {stage_name} 未完成或缺少封存 outputs_hash，拒绝部署')
        digests[stage_name] = st.outputs_hash
    stage_order = list(state.stages)
    positions = {name: index for index, name in enumerate(stage_order)}
    first_required = min(positions[name] for name in required_names)
    unverified = [
        name for name, st in state.stages.items()
        if name not in required_names and positions[name] >= first_required
        and st.status == StageStatus.DONE and st.commit_sha
    ]
    if unverified:
        raise DeployRejected(
            f'前置验收之后存在未纳入验收的已完成代码阶段: {", ".join(unverified)}，拒绝部署')
    commits = [
        st.commit_sha for name, st in state.stages.items()
        if st.status == StageStatus.DONE and st.commit_sha
        and (name in required_names or positions[name] < first_required)
    ]
    combined_outputs_hash = sha256_text(repr(sorted(digests.items())))
    head_commit = commits[-1] if commits else None
    if not dry_run:
        if approval is None:
            raise DeployRejected('正式部署需要显式 DeployApproval，拒绝自动执行')
        if approval.run_id != state.run_id:
            raise DeployRejected('审批 run_id 与当前运行不一致')
        if approval.outputs_hash != combined_outputs_hash:
            raise DeployRejected('审批 outputs_hash 与当前前置阶段封存产物不一致')
        if head_commit is not None and approval.commit_sha != head_commit:
            raise DeployRejected('审批 commit_sha 与已验证代码提交不一致')
    return {
        'run_id': state.run_id,
        'required_stages': list(required_stages),
        'outputs_hash': combined_outputs_hash,
        'commit_sha': head_commit,
    }


def _verify_deploy_workdir(workdir: Path, expected_commit: str,
                          allowed_ignored_root: Path | None = None) -> None:
    """在流水线锁内核对审批提交仍对应实际 HEAD，且工作树没有未验收改动。"""
    try:
        current_commit = _git(['rev-parse', 'HEAD'], workdir).stdout.strip()
    except (GitError, OSError) as error:
        raise DeployRejected('无法核对实际工作区 HEAD，拒绝部署') from error
    if current_commit != expected_commit:
        raise DeployRejected(
            f'审批后工作区 HEAD 已变化（当前 {current_commit}，期望 {expected_commit}），拒绝部署')
    try:
        status = _git(['status', '--porcelain'], workdir).stdout
    except (GitError, OSError) as error:
        raise DeployRejected('无法核对实际工作区状态，拒绝部署') from error
    if status:
        raise DeployRejected('审批后工作区存在未提交改动，拒绝部署')
    try:
        ignored = _git(['status', '--porcelain=v1', '-z', '--ignored'],
                       workdir, binary=True).stdout
    except (GitError, OSError) as error:
        raise DeployRejected('无法核对实际工作区 ignored 文件，拒绝部署') from error
    allowed_root = allowed_ignored_root.resolve() if allowed_ignored_root is not None else None
    if allowed_root == workdir.resolve():
        allowed_root = None
    for record in ignored.split(b'\0'):
        if not record or record[:2] != b'!!':
            continue
        rel = os.fsdecode(record[3:])
        path = (workdir / rel).resolve()
        if allowed_root is not None and path.is_relative_to(allowed_root):
            continue
        raise DeployRejected(f'审批后工作区存在未验收的 ignored 文件或目录: {rel}')


def _stop_process_tree(process: subprocess.Popen) -> None:
    """先终止整个部署进程树，再对直接子进程做有限等待。"""
    from .process_tree import terminate

    try:
        grouped = terminate(process)
    except Exception:
        grouped = False
    if not grouped:
        if os.name == 'nt':
            try:
                process.kill()
            except OSError:
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
    for attempt in range(2):
        try:
            process.wait(timeout=_PROCESS_TREE_WAIT_S)
            return
        except subprocess.TimeoutExpired:
            if attempt == 0:
                try:
                    process.kill()
                except OSError:
                    pass


def _join_readers(readers: list[threading.Thread], timeout_s: float) -> None:
    deadline = time.monotonic() + max(0.0, timeout_s)
    for reader in readers:
        if reader.ident is not None:
            reader.join(max(0.0, deadline - time.monotonic()))


def _run_deploy_command(
    argv: list[str],
    workdir: Path,
    env: dict[str, str],
    timeout_s: int,
) -> subprocess.CompletedProcess:
    """有界捕获部署输出，并在超时或异常时关闭整个进程树。"""
    from .process_tree import attach, close, creation_flags

    process = subprocess.Popen(
        argv,
        cwd=str(workdir),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name != 'nt',
        creationflags=creation_flags(),
    )
    process.__dict__['_pipeline_start_suspended'] = os.name == 'nt'
    readers: list[threading.Thread] = []
    try:
        attach(process)
        buffers = {name: TextTail(_OUTPUT_LIMIT) for name in ('stdout', 'stderr')}
        failures: list[Exception] = []

        def drain(name: str) -> None:
            pipe = getattr(process, name)
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            try:
                while chunk := pipe.read1(4096):
                    buffers[name].append(decoder.decode(chunk))
                buffers[name].append(decoder.decode(b'', final=True))
            except Exception as error:
                failures.append(error)
            finally:
                try:
                    pipe.close()
                except Exception as error:
                    failures.append(error)

        readers = [
            threading.Thread(target=drain, args=(name,), daemon=True)
            for name in ('stdout', 'stderr')
        ]
        for reader in readers:
            reader.start()

        deadline = time.monotonic() + timeout_s
        timed_out = False
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
        if not timed_out:
            # 后代可能继承输出管道；直接子进程退出后仍须在总超时内回收它们。
            _join_readers(readers, max(0.0, deadline - time.monotonic()))
            timed_out = any(reader.is_alive() for reader in readers)
        if timed_out:
            _stop_process_tree(process)
            _join_readers(readers, _PROCESS_TREE_WAIT_S)
            if any(reader.is_alive() for reader in readers):
                raise subprocess.TimeoutExpired(argv, timeout_s)
            error = subprocess.TimeoutExpired(
                argv, timeout_s,
                output=buffers['stdout'].getvalue(),
                stderr=buffers['stderr'].getvalue(),
            )
            raise error
        if failures:
            raise OSError(f'部署命令输出读取失败: {failures[0]}')
        return subprocess.CompletedProcess(
            argv, process.returncode,
            stdout=buffers['stdout'].getvalue(),
            stderr=buffers['stderr'].getvalue(),
        )
    finally:
        try:
            if process.poll() is None or any(reader.is_alive() for reader in readers):
                _stop_process_tree(process)
        finally:
            try:
                _join_readers(readers, _PROCESS_TREE_WAIT_S)
            finally:
                try:
                    close(process)
                finally:
                    for name in ('stdout', 'stderr'):
                        pipe = getattr(process, name)
                        if pipe is not None:
                            try:
                                pipe.close()
                            except OSError:
                                pass


def execute_deploy(
    spec: DeploySpec,
    state: RunState,
    workdir: Path,
    required_stages: tuple[str, ...],
    *,
    approval: DeployApproval | None = None,
    dry_run: bool = True,
) -> dict:
    """按固定 argv 执行受控部署或干跑，并落盘不可变审计回执。"""
    gate = verify_deploy_gate(state, required_stages, approval, dry_run=dry_run)
    argv = list(spec.dry_run_command if (dry_run and spec.dry_run_command) else spec.command)
    env = build_subprocess_env(extra_allow=list(spec.allow_env))
    secrets = environment_secrets()
    safe_argv = [StreamRedactor(secrets).feed(arg, final=True) for arg in argv]
    if dry_run and not spec.dry_run_command:
        entry = argv[0]
        entry_path = Path(entry)
        exists = (entry_path.is_file() if (entry_path.is_absolute() or '/' in entry or '\\' in entry)
                  else bool(shutil.which(entry)))
        receipt = {
            'deploy_id': 'deploy-' + uuid.uuid4().hex[:12],
            'name': spec.name,
            'dry_run': True,
            'executed': False,
            'ok': exists,
            'exit_code': 0 if exists else -1,
            'command': safe_argv,
            'gate': gate,
            'duration_s': 0.0,
            'stdout': 'dry-run preview only (no command executed)' if exists else '',
            'stderr': '' if exists else f'部署命令入口不存在或不可执行: {safe_argv[0]}',
        }
        _write_deploy_receipt(state.run_dir, receipt)
        return receipt

    workdir = Path(workdir).resolve()
    lock = StageLock(workdir.parent / f'.{workdir.name}.pipeline.lock')
    if not lock.acquire():
        raise DeployRejected(f'工作区流水线锁被占用，拒绝部署: {lock.lock_path}')
    try:
        if gate['commit_sha']:
            _verify_deploy_workdir(workdir, gate['commit_sha'], state.state_dir)
        started = time.monotonic()
        try:
            proc = _run_deploy_command(argv, workdir, env, spec.timeout_s)
            ok = proc.returncode == 0
            exit_code = proc.returncode
            stdout = StreamRedactor(secrets).feed(proc.stdout or '', final=True)[:_OUTPUT_LIMIT]
            stderr = StreamRedactor(secrets).feed(proc.stderr or '', final=True)[:_OUTPUT_LIMIT]
        except subprocess.TimeoutExpired as error:
            ok = False
            exit_code = -1
            stdout = ''
            stderr = StreamRedactor(secrets).feed(
                f'部署命令超时（{spec.timeout_s}s）: {error}', final=True)[:_OUTPUT_LIMIT]
        except OSError as error:
            ok = False
            exit_code = -1
            stdout = ''
            stderr = StreamRedactor(secrets).feed(f'无法启动部署命令: {error}', final=True)[:_OUTPUT_LIMIT]
    finally:
        lock.release()

    receipt = {
        'deploy_id': 'deploy-' + uuid.uuid4().hex[:12],
        'name': spec.name,
        'dry_run': dry_run,
        'executed': True,
        'ok': ok,
        'exit_code': exit_code,
        'command': safe_argv,
        'gate': gate,
        'approved_by': approval.approved_by if approval and not dry_run else None,
        'duration_s': round(time.monotonic() - started, 3),
        'stdout': stdout,
        'stderr': stderr,
    }
    _write_deploy_receipt(state.run_dir, receipt)
    return receipt


def _write_deploy_receipt(run_dir: Path, receipt: dict) -> Path:
    target = Path(run_dir) / 'deployments' / receipt['deploy_id'] / 'deploy_receipt.json'
    atomic_write_json(target, receipt)
    return target
