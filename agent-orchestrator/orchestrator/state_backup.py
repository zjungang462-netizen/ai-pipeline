"""停写后的完整状态备份、逐文件校验及新目录恢复；不改写历史证据。"""
from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import time

from .maintenance import boundary
from .state import RunState, StageLock, atomic_write_json, sha256_file


def inspect_state(state_dir):
    root = Path(state_dir).absolute()
    if not root.is_dir():
        raise ValueError('状态目录不存在')
    boundary(root / 'runs', root)
    rows = []
    for run in sorted((root / 'runs').iterdir()) if (root / 'runs').is_dir() else []:
        boundary(run, root)
        if not run.is_dir():
            continue
        row = dict(run_id=run.name, compatible=False, schema_version=None)
        try:
            boundary(run / 'state.json', root)
            raw = json.loads((run / 'state.json').read_text(encoding='utf8'))
            if isinstance(raw, dict):
                row['schema_version'] = raw.get('schema_version')
            state = RunState(root, run.name)
            if not state.load():
                raise ValueError('缺少 state.json')
            row['compatible'] = True
        except (OSError, ValueError, TypeError) as error:
            row['reason'] = type(error).__name__
        rows.append(row)
    return dict(schema_version=RunState.SCHEMA_VERSION, runs=rows,
                compatible=all(row['compatible'] for row in rows),
                migration='schema_2_and_3_read_only_validation',
                scope='state_only; task_workdir_and_git_worktrees_require_separate_backup')


def _inventory(root, excluded=()):
    files, directories = {}, []
    pending = [root]
    while pending:
        directory = pending.pop()
        boundary(directory / '.boundary', root)
        for path in sorted(directory.iterdir()):
            name = path.relative_to(root).as_posix()
            if name in excluded:
                continue
            boundary(path, root)
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                directories.append(name)
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                files[name] = dict(size=info.st_size, sha256=sha256_file(path))
            else:
                raise ValueError('状态目录含特殊文件')
    return dict(files=files, directories=sorted(directories))


def _new_target(path, source):
    path = Path(path).absolute()
    boundary(path, path.parent)
    if path.exists() or path.is_symlink():
        raise ValueError('目标必须是不存在的新目录')
    if path.resolve().is_relative_to(source.resolve()) or source.resolve().is_relative_to(path.resolve()):
        raise ValueError('备份与状态目录不得互相包含')
    if not path.parent.is_dir():
        raise ValueError('目标父目录必须已存在')
    return path


def _lock_paths(root):
    paths = [root / '.backup.lock']
    if (root / 'tuning').is_dir():
        paths.append(root / 'tuning/.policy.lock')
        paths.append(root / 'tuning/.lock')
    if (root / 'arenas').is_dir():
        paths.append(root / 'arenas/.apply.lock')
    for topic in sorted((root / 'discussions').iterdir()) if (root / 'discussions').is_dir() else []:
        boundary(topic, root)
        if topic.is_dir():
            paths.append(topic / '.discussion.lock')
    for run in sorted((root / 'runs').iterdir()) if (root / 'runs').is_dir() else []:
        boundary(run, root)
        if run.is_dir():
            paths += [run / name for name in ('.supervisor.lock', '.run.lock', '.control.lock', '.discussion.lock')]
    for path in root.rglob('*.lock'):
        boundary(path, root)
        if path.is_file():
            paths.append(path)
    return list(dict.fromkeys(paths))


def backup_state(state_dir, output):
    root = Path(state_dir).absolute()
    if not root.is_dir():
        raise ValueError('状态目录不存在')
    output = _new_target(output, root)
    # 与运行、审批、会诊和调优共用已有锁；调用方仍须停止创建新任务。
    with ExitStack() as stack:
        paths = _lock_paths(root)
        for path in paths:
            boundary(path, root)
            try:
                stack.enter_context(StageLock(path))
            except RuntimeError as error:
                raise ValueError('存在运行、控制、会诊或调优写入锁，请停写后备份') from error
        excluded = {path.relative_to(root).as_posix() for path in paths}
        before = _inventory(root, excluded)
        compatibility = inspect_state(root)
        with tempfile.TemporaryDirectory(prefix='.state-backup-', dir=output.parent) as temp:
            staging = Path(temp) / 'backup'
            payload = staging / 'payload'
            payload.mkdir(parents=True)
            for name in before['directories']:
                (payload / name).mkdir(parents=True, exist_ok=True)
            for name in before['files']:
                target = payload / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / name, target)
            if _inventory(payload) != before or _inventory(root, excluded) != before:
                raise ValueError('备份期间状态发生变化，未发布备份')
            manifest = dict(schema_version=1, created_at=time.time(), source_root=str(root),
                            inventory=before, compatibility=compatibility,
                            excluded_coordination_locks=sorted(excluded))
            atomic_write_json(staging / 'manifest.json', manifest)
            if output.exists():
                raise ValueError('目标已出现，拒绝覆盖')
            staging.rename(output)
    return dict(output=str(output), files=len(before['files']),
                bytes=sum(row['size'] for row in before['files'].values()),
                compatible=compatibility['compatible'])


def _validated_backup(backup):
    backup = Path(backup).absolute()
    boundary(backup / 'manifest.json', backup)
    manifest = json.loads((backup / 'manifest.json').read_text(encoding='utf8'))
    if not isinstance(manifest, dict) or type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1:
        raise ValueError('备份 manifest 版本无效')
    if not isinstance(manifest.get('source_root'), str) or not manifest['source_root']:
        raise ValueError('备份缺少原始状态目录')
    inventory = manifest.get('inventory')
    if not isinstance(inventory, dict) or not isinstance(inventory.get('files'), dict) or not isinstance(inventory.get('directories'), list):
        raise ValueError('备份清单格式错误')
    for name in [*inventory['files'], *inventory['directories']]:
        if (not isinstance(name, str) or not name or '\\' in name or ':' in name
                or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts
                or str(PurePosixPath(name)) != name):
            raise ValueError('备份清单含非法路径')
    payload = backup / 'payload'
    boundary(payload, backup)
    if _inventory(payload) != inventory:
        raise ValueError('备份文件集合、大小或 SHA-256 不匹配')
    return backup, manifest


def restore_state(backup, output):
    backup, manifest = _validated_backup(backup)
    output = _new_target(output, backup)
    with tempfile.TemporaryDirectory(prefix='.state-restore-', dir=output.parent) as temp:
        staging = Path(temp) / 'state'
        shutil.copytree(backup / 'payload', staging)
        if _inventory(staging) != manifest['inventory']:
            raise ValueError('恢复副本校验失败')
        if output.exists():
            raise ValueError('目标已出现，拒绝覆盖')
        staging.rename(output)
    return dict(output=str(output), files=len(manifest['inventory']['files']),
                source_root=manifest['source_root'], automatic_resume=False,
                paths_rewritten=False, scope='restore_bytes_only')
