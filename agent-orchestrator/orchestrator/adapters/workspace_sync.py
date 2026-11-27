"""容器适配器的声明文件提取、临时工作区物化、基线核验与原子回写。"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from ..safety import relative_target

MAX_BYTES = 512 * 1024
MAX_FILES = 64
FILE_NAME = re.compile(r'[\w+., @-]+(?:/[\w+., @-]+)*')


def collect_source_files(
    workdir: Path,
    names: tuple[str, ...],
    *,
    label: str = '容器',
    max_files: int = MAX_FILES,
    max_bytes: int = MAX_BYTES,
) -> dict[str, bytes]:
    root = Path(workdir).resolve()
    if len(names) > max_files or len({name.casefold() for name in names}) != len(names):
        raise ValueError('声明文件数量超限或名称重复')
    files: dict[str, bytes] = {}
    remaining = max_bytes
    for name in names:
        if not FILE_NAME.fullmatch(name) or any(
            part.startswith('.') or part.endswith((' ', '.')) for part in name.split('/')
        ):
            raise ValueError(f'{label} 只加载普通项目相对路径')
        path = relative_target(root, name)
        if path != root / name or not path.is_file() or path.is_symlink():
            raise ValueError(f'{label} 声明文件不是工作区内的普通文件：{name}')
        with path.open('rb') as stream:
            data = stream.read(remaining + 1)
        if len(data) > remaining:
            raise ValueError(f'{label} 声明文件总大小超限')
        remaining -= len(data)
        files[name] = data
    return files


def replace_file(path: Path, content: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    try:
        temporary.chmod(path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_raw_updates(
    workdir: Path,
    editable_files: tuple[str, ...],
    original: dict[str, bytes],
    updates: dict[str, bytes],
    *,
    secrets: tuple[str, ...] = (),
    label: str = '容器',
    max_bytes: int = MAX_BYTES,
    replacer=replace_file,
) -> list[str]:
    if set(updates) - set(editable_files):
        raise ValueError('拒绝未声明文件')
    if sum(map(len, updates.values())) > max_bytes:
        raise ValueError('返回文件超限')
    material = [
        secret.encode('utf-8')
        for secret in secrets
        if isinstance(secret, str) and len(secret) >= 8
    ]
    if any(secret in content for secret in material for content in updates.values()):
        raise ValueError('返回文件含有会话密钥')
    if collect_source_files(workdir, tuple(original), label=label, max_bytes=max_bytes) != original:
        raise ValueError('工作树已变化')
    applied: list[str] = []
    try:
        for key, content in updates.items():
            replacer(relative_target(workdir, key), content)
            applied.append(key)
    except OSError:
        for key in applied:
            replacer(relative_target(workdir, key), original[key])
        raise
    return list(updates)


def _staging_chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        if os.name != 'nt':
            raise


def materialize_staging(
    staging_dir: Path,
    files: dict[str, bytes],
    editable_files: tuple[str, ...],
) -> int | None:
    """物化宿主私有暂存区；返回需要 DAC_OVERRIDE 的宿主 UID，否则返回 None。"""
    root = Path(staging_dir).resolve()
    _staging_chmod(root, 0o700)
    editable = set(editable_files)
    for name, data in files.items():
        target = relative_target(root, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        current = target.parent
        while current != root and root in current.parents:
            _staging_chmod(current, 0o700)
            current = current.parent
        target.write_bytes(data)
        _staging_chmod(target, 0o600 if name in editable else 0o400)
    if os.name == 'nt':
        return None
    euid = os.geteuid()
    if euid == 65534:
        return None
    try:
        for current, _, names in os.walk(root):
            os.chown(current, 65534, 65534)
            for name in names:
                os.chown(Path(current) / name, 65534, 65534)
    except OSError as error:
        if euid == 0:
            raise RuntimeError('无法将容器暂存区交给 nobody 用户；已停止执行') from error
        return euid
    return None


def make_staging_removable(staging_dir: Path) -> None:
    """清理前恢复目录与文件可写权限，防止只读文件导致清理失败。"""
    root = Path(staging_dir)
    if not root.exists():
        return
    for current, dirs, files in os.walk(root):
        cur_path = Path(current)
        try:
            cur_path.chmod(0o700)
        except OSError:
            pass
        for name in files:
            try:
                (cur_path / name).chmod(0o600)
            except OSError:
                pass


def collect_staging_updates(
    staging_dir: Path,
    editable_files: tuple[str, ...],
    original: dict[str, bytes],
    *,
    max_bytes: int = MAX_BYTES,
) -> dict[str, bytes]:
    """只读取隔离目录中已修改的声明可编辑文件，拒绝符号链接和超限输出。"""
    root = Path(staging_dir).resolve()
    updates: dict[str, bytes] = {}
    remaining = max_bytes
    for name in editable_files:
        target = relative_target(root, name)
        if target != root / name or not target.is_file() or target.is_symlink():
            raise ValueError(f'容器返回的可编辑文件无效：{name}')
        with target.open('rb') as stream:
            data = stream.read(remaining + 1)
        if len(data) > remaining:
            raise ValueError('返回文件超限')
        remaining -= len(data)
        if data != original[name]:
            updates[name] = data
    return updates
