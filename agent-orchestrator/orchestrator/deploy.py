"""受控部署执行器：强制前置阶段通过、审批哈希绑定、干跑预览与审计留证。"""
from __future__ import annotations

from dataclasses import dataclass
import subprocess
import time
import uuid
from pathlib import Path

from .cli_telemetry import StreamRedactor
from .contracts import StageStatus
from .safety import build_subprocess_env
from .state import RunState, atomic_write_json, sha256_text


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
    commits: list[str] = []
    for stage_name in required_stages:
        st = state.stages.get(stage_name)
        if st is None or st.status != StageStatus.DONE or not st.outputs_hash:
            raise DeployRejected(f'前置阶段 {stage_name} 未完成或缺少封存 outputs_hash，拒绝部署')
        digests[stage_name] = st.outputs_hash
        if st.commit_sha:
            commits.append(st.commit_sha)
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
    if dry_run and not spec.dry_run_command:
        receipt = {
            'deploy_id': 'deploy-' + uuid.uuid4().hex[:12],
            'name': spec.name,
            'dry_run': True,
            'executed': False,
            'ok': True,
            'exit_code': 0,
            'command': argv,
            'gate': gate,
            'duration_s': 0.0,
            'stdout': 'dry-run preview only (no command executed)',
            'stderr': '',
        }
        _write_deploy_receipt(state.run_dir, receipt)
        return receipt

    env = build_subprocess_env(extra_allow=list(spec.allow_env))
    secrets = [
        value for key, value in env.items()
        if isinstance(value, str) and len(value) >= 8
        and any(label in key.upper() for label in ('API_KEY', 'AUTH_TOKEN', 'ACCESS_TOKEN', 'SECRET', 'PASSWORD'))
    ]
    redactor = StreamRedactor(secrets)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(Path(workdir).resolve()),
            env=env,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=spec.timeout_s,
        )
        ok = proc.returncode == 0
        exit_code = proc.returncode
        stdout = redactor.feed(proc.stdout or '', final=True)[:16000]
        stderr = redactor.feed(proc.stderr or '', final=True)[:16000]
    except subprocess.TimeoutExpired as error:
        ok = False
        exit_code = -1
        stdout = ''
        stderr = f'部署命令超时（{spec.timeout_s}s）: {error}'
    except OSError as error:
        ok = False
        exit_code = -1
        stdout = ''
        stderr = f'无法启动部署命令: {error}'

    receipt = {
        'deploy_id': 'deploy-' + uuid.uuid4().hex[:12],
        'name': spec.name,
        'dry_run': dry_run,
        'executed': True,
        'ok': ok,
        'exit_code': exit_code,
        'command': argv,
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
