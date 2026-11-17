"""状态与诊断包保留策略；默认只预览，删除前持锁重新核对目录快照。"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

from .safety import build_subprocess_env
from .state import StageLock

DEFAULT_RETENTION_DAYS = 30
DEFAULT_EVIDENCE_BYTES = 100 * 1024 * 1024
PROTECTED = {'success.json', 'decision.json', 'approval.json', 'recovery',
             'recovery-reference.json', '.git'}


def linked(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def boundary(path: Path, root: Path) -> None:
    """先检查绝对路径及每个祖先，避免 resolve() 隐藏 junction/symlink。"""
    path, root = path.absolute(), root.absolute()
    if path == root or not path.is_relative_to(root):
        raise ValueError('清理目标必须严格位于指定根目录内')
    for parent in (path, *path.parents):
        if parent.exists() and linked(parent):
            raise ValueError('清理路径含符号链接或 reparse point，保留')


def tree_info(path: Path) -> dict:
    records, names, total, newest = [], set(), 0, 0
    pending = [path]
    while pending:
        node = pending.pop()
        if linked(node):
            raise ValueError('目录含链接，保留')
        info = node.lstat()
        if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
            raise ValueError('目录含特殊文件，保留')
        rel = node.relative_to(path).as_posix()
        names.add(node.name.lower().rstrip(' .'))
        records.append((rel, info.st_mode, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
        newest = max(newest, info.st_mtime)
        if stat.S_ISDIR(info.st_mode):
            pending.extend(node.iterdir())
        else:
            total += info.st_size
        if len(records) > 100000:
            raise ValueError('目录超过清理扫描上限，保留')
    digest = hashlib.sha256(json.dumps(sorted(records)).encode()).hexdigest()
    return dict(bytes=total, newest=newest, fingerprint=digest, names=names)


def remove_checked(path: Path, root: Path, fingerprint: str) -> None:
    boundary(path, root)
    if tree_info(path)['fingerprint'] != fingerprint:
        raise ValueError('预览后目录发生变化，保留')
    # 仅对已验证位于指定根内、且整棵树没有链接的目录递归删除。
    shutil.rmtree(path)


def _read_json(path: Path) -> dict:
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('状态文件超过读取上限')
    data = json.loads(path.read_text(encoding='utf8'))
    if not isinstance(data, dict):
        raise ValueError('状态必须是 JSON 对象')
    return data


def _run_actions(run: Path, root: Path, cutoff: float, *, owned=False) -> tuple[list, list]:
    actions, kept = [], []
    boundary(run, root)
    state = _read_json(run/'state.json')
    stages = state.get('stages')
    if not isinstance(stages, dict) or not stages:
        raise ValueError('缺少有效阶段状态，保留整个运行')
    valid_statuses = {'pending', 'running', 'done', 'failed', 'stale', 'awaiting_approval',
                      'interrupted', 'review_feedback'}
    for stage in stages.values():
        if (not isinstance(stage, dict) or stage.get('status') not in valid_statuses
                or (stage.get('current_attempt') is not None and
                    (type(stage['current_attempt']) is not int or stage['current_attempt'] < 1))):
            raise ValueError('阶段状态或 current_attempt 无效，保留整个运行')
    if state.get('pending_approvals') or state.get('control', {}).get('cli_worktrees'):
        return [], [dict(path=str(run), reason='待审批或存在尚未回收的 CLI worktree，保留运行')]
    if any(not isinstance(s, dict) or s.get('status') in ('running', 'awaiting_approval') for s in stages.values()):
        return [], [dict(path=str(run), reason='阶段正在执行、待审批或状态无效，保留运行')]
    job = _read_json(run/'job.json') if (run/'job.json').exists() else {}
    if job.get('status') in ('pending', 'running', 'recovering'):
        return [], [dict(path=str(run), reason='job 尚未终止，保留运行')]
    if not owned and any(p.exists() for p in (run/'.run.lock', run/'.supervisor.lock', run/'.control.lock')):
        return [], [dict(path=str(run), reason='存在运行/控制锁，保留运行')]
    if (run/'stages').exists():
        boundary(run/'stages', root)
    for stage_dir in (run/'stages').iterdir() if (run/'stages').is_dir() else []:
        boundary(stage_dir, root)
        if not stage_dir.is_dir() or stage_dir.name not in stages:
            continue
        current = stages[stage_dir.name].get('current_attempt')
        recovery = state.get('control', {}).get('recoveries', {}).get(stage_dir.name, {})
        recovery_attempt = recovery.get('attempt') if isinstance(recovery, dict) else None
        attempts = stage_dir/'attempts'
        if not attempts.exists():
            continue
        boundary(attempts, root)
        folders = [p for p in attempts.iterdir() if p.name.isascii() and p.name.isdigit()]
        latest = max((int(p.name) for p in folders), default=0)
        for folder in folders:
            try:
                boundary(folder, root)
                info = tree_info(folder)
                reason = None
                if int(folder.name) in (current, latest, recovery_attempt):
                    reason = '当前、最新或恢复候选 attempt'
                elif info['names'] & PROTECTED:
                    reason = '含成功、审批或恢复证据'
                elif info['newest'] >= cutoff:
                    reason = '未达到保留期'
                if reason:
                    kept.append(dict(path=str(folder), reason=reason))
                else:
                    actions.append(dict(path=str(folder), run=str(run), kind='attempt',
                                        bytes=info['bytes'], fingerprint=info['fingerprint']))
            except (OSError, ValueError) as error:
                kept.append(dict(path=str(folder), reason=str(error)))
    violations = run/'violations'
    if violations.exists():
        boundary(violations, root)
        for folder in violations.iterdir():
            try:
                boundary(folder, root)
                info = tree_info(folder)
                if not folder.is_dir() or info['newest'] >= cutoff or info['names'] & PROTECTED:
                    kept.append(dict(path=str(folder), reason='隔离证据未过期、非目录或含受保护证据'))
                else:
                    actions.append(dict(path=str(folder), run=str(run), kind='violation',
                                        bytes=info['bytes'], fingerprint=info['fingerprint']))
            except (OSError, ValueError) as error:
                kept.append(dict(path=str(folder), reason=str(error)))
    return actions, kept


def evidence_plan(root: Path, *, older_than=DEFAULT_RETENTION_DAYS,
                  max_bytes=DEFAULT_EVIDENCE_BYTES, now=None, keep=()) -> dict:
    """只管理含 evidence.json 的诊断包；其它文件不删除、计入占用。"""
    now = time.time() if now is None else now
    rows, actions, issues, unmanaged = [], [], [], 0
    if not root.exists():
        return dict(actions=[], issues=[], total_bytes=0)
    boundary(root/'.retention.lock', root)
    for folder in root.iterdir():
        if folder.name == '.retention.lock':
            continue
        try:
            boundary(folder, root)
            info = tree_info(folder)
            if folder.is_dir() and (folder/'evidence.json').is_file() and not info['names'] & PROTECTED:
                rows.append((folder, info))
            else:
                unmanaged += info['bytes']
        except (OSError, ValueError) as error:
            issues.append(dict(path=str(folder), reason=str(error)))
    total = unmanaged + sum(info['bytes'] for _, info in rows)
    remaining = total
    for folder, info in sorted(rows, key=lambda row: (row[1]['newest'], str(row[0]))):
        if folder in keep:
            continue
        if info['newest'] < now - older_than*86400 or remaining > max_bytes:
            actions.append(dict(path=str(folder), kind='test_evidence', bytes=info['bytes'],
                                fingerprint=info['fingerprint']))
            remaining -= info['bytes']
    if remaining > max_bytes:
        issues.append(dict(path=str(root), reason='受保护或非诊断文件使占用仍超过上限'))
    return dict(actions=actions, issues=issues, total_bytes=total, remaining_bytes=remaining)


def prune_evidence(root: Path, *, keep=(), older_than=DEFAULT_RETENTION_DAYS,
                   max_bytes=DEFAULT_EVIDENCE_BYTES) -> dict:
    """调用方须持有该目录的 .retention.lock，避免采集中途淘汰。"""
    plan = evidence_plan(root, keep=keep, older_than=older_than, max_bytes=max_bytes)
    for row in plan['actions']:
        remove_checked(Path(row['path']), root, row['fingerprint'])
    return plan


def _git(repo, *args):
    env = build_subprocess_env()
    env.update(LC_ALL='C', LANG='C')
    return subprocess.run(['git', '-C', str(repo), *args], env=env, capture_output=True,
                          text=True, encoding='utf8', errors='replace', timeout=30, check=True)


def worktree_plan(repo: Path, root: Path, cutoff: float) -> dict | None:
    """仅在 Git 将淘汰的所有登记都指向状态根内的失效路径时允许 prune。"""
    preview = _git(repo, 'worktree', 'prune', '--dry-run', '--verbose', f'--expire=@{int(cutoff)}')
    lines = (preview.stdout+preview.stderr).strip().splitlines()
    if not lines:
        return None
    common = Path(_git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip())
    records = []
    for line in lines:
        match = re.match(r'^Removing worktrees/([^/:\\]+):', line)
        if not match:
            raise ValueError('无法识别 Git prune 预览，拒绝执行')
        metadata = common/'worktrees'/match[1]
        target = Path((metadata/'gitdir').read_text(encoding='utf8').strip()).parent.absolute()
        boundary(target, root)
        if target.exists():
            raise ValueError('失效登记指向仍存在的目录，拒绝自动 prune')
        records.append(dict(path=str(target), fingerprint=tree_info(metadata)['fingerprint']))
    return dict(kind='worktree_registration', path=str(repo.absolute()), bytes=0,
                fingerprint=hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest(),
                preview=lines)


def clean_state(state_dir, *, apply=False, older_than=DEFAULT_RETENTION_DAYS,
                test_failures=None, max_evidence_bytes=DEFAULT_EVIDENCE_BYTES, repo=None, now=None):
    if not isinstance(older_than, (int, float)) or not math.isfinite(older_than) or older_than <= 0:
        raise ValueError('older-than 必须是正数天数')
    if max_evidence_bytes < 2*1024*1024:
        raise ValueError('诊断包总量上限不得小于 2 MiB')
    root = Path(state_dir).absolute()
    if not root.is_dir():
        raise ValueError('state-dir 必须是已存在的状态目录')
    boundary(root/'runs', root)
    cutoff = (time.time() if now is None else now) - older_than*86400
    actions, kept, errors, deleted = [], [], [], []
    runs = root/'runs'
    for run in runs.iterdir() if runs.is_dir() else []:
        try:
            if not run.is_dir():
                continue
            rows, skips = _run_actions(run, root, cutoff)
            actions.extend(rows); kept.extend(skips)
            if apply and rows:
                with contextlib.ExitStack() as stack:
                    # 与 worker、supervisor、审批和控制共用所有权锁。
                    for name in ('.supervisor.lock', '.run.lock', '.control.lock'):
                        stack.enter_context(StageLock(run/name))
                    fresh, _ = _run_actions(run, root, cutoff, owned=True)
                    available = {row['path']: row for row in fresh}
                    for row in rows:
                        if available.get(row['path']) != row:
                            raise ValueError('持锁复核时状态或清理目录变化，保留')
                        remove_checked(Path(row['path']), root, row['fingerprint'])
                        deleted.append(row['path'])
        except (OSError, ValueError, RuntimeError, TypeError, AttributeError) as error:
            errors.append(dict(path=str(run), reason=str(error)))
    if test_failures:
        evidence = Path(test_failures).absolute()
        try:
            plan = evidence_plan(evidence, older_than=older_than, max_bytes=max_evidence_bytes, now=now)
            actions.extend(plan['actions']); kept.extend(plan['issues'])
            if apply and plan['actions']:
                with StageLock(evidence/'.retention.lock'):
                    fresh = evidence_plan(evidence, older_than=older_than, max_bytes=max_evidence_bytes, now=now)
                    available = {r['path']: r for r in fresh['actions']}
                    for row in plan['actions']:
                        if available.get(row['path']) != row:
                            raise ValueError('诊断包在预览后发生变化，保留')
                        remove_checked(Path(row['path']), evidence, row['fingerprint'])
                        deleted.append(row['path'])
        except (OSError, ValueError, RuntimeError) as error:
            errors.append(dict(path=str(evidence), reason=str(error)))
    if repo:
        try:
            row = worktree_plan(Path(repo), root, cutoff)
            if row:
                actions.append(row)
                if apply:
                    if worktree_plan(Path(repo), root, cutoff) != row:
                        raise ValueError('Git 登记在预览后变化，拒绝 prune')
                    _git(Path(repo), 'worktree', 'prune', '--verbose', f'--expire=@{int(cutoff)}')
                    deleted.append(row['path']+' (失效 worktree 登记)')
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            errors.append(dict(path=str(repo), reason=str(error)))
    return dict(dry_run=not apply, state_dir=str(root), older_than_days=older_than,
                actions=actions, bytes=sum(r['bytes'] for r in actions), kept=kept,
                errors=errors, deleted=deleted, ok=not errors)
