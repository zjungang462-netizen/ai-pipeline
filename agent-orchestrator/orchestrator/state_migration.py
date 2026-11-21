"""契约化状态 schema 迁移注册表、干跑校验与原字节备份门禁。

注意：本模块不臆造未经真实历史样本验证的 v1->v2 迁移；只有显式登记了
确定性契约的相邻版本步进才允许执行。
"""
from __future__ import annotations

from collections.abc import Callable
import copy
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
from typing import Any

from .state import RunState, StageLock, atomic_write_json, sha256_file, sha256_text


class MigrationError(ValueError):
    """迁移契约缺失、校验失败或锁冲突时抛出的可审计异常。"""


@dataclass(frozen=True)
class MigrationStep:
    from_version: int
    to_version: int
    name: str
    transform: Callable[[dict[str, Any]], dict[str, Any]]
    description: str = ''

    def __post_init__(self) -> None:
        if type(self.from_version) is not int or type(self.to_version) is not int:
            raise MigrationError('schema 版本号必须为整数')
        if self.from_version < 1 or self.to_version != self.from_version + 1:
            raise MigrationError('迁移步进必须满足 to_version == from_version + 1 且版本号 >= 1')
        if not isinstance(self.name, str) or not self.name.strip():
            raise MigrationError('迁移步骤必须有非空名称')
        if not callable(self.transform):
            raise MigrationError('迁移步骤 transform 必须可调用')


class SchemaMigrationRegistry:
    """只允许确定性、单步递增的已登记 schema 迁移链。"""

    def __init__(self, steps: tuple[MigrationStep, ...] = ()):
        self._steps: dict[int, MigrationStep] = {}
        for step in steps:
            self.register(step)

    def register(self, step: MigrationStep) -> None:
        if step.from_version in self._steps:
            raise MigrationError(f'重复登记来自 schema v{step.from_version} 的迁移步骤')
        self._steps[step.from_version] = step

    def registered_transitions(self) -> tuple[tuple[int, int, str], ...]:
        return tuple(
            (step.from_version, step.to_version, step.name)
            for _, step in sorted(self._steps.items())
        )

    def plan(self, current_version: int, target_version: int) -> list[MigrationStep]:
        if type(current_version) is not int or type(target_version) is not int:
            raise MigrationError('当前版本与目标版本必须为整数')
        if current_version < 1 or target_version < 1:
            raise MigrationError('版本号必须为正整数')
        if target_version < current_version:
            raise MigrationError(f'禁止 schema 降级迁移: v{current_version} -> v{target_version}')
        steps: list[MigrationStep] = []
        cursor = current_version
        while cursor < target_version:
            step = self._steps.get(cursor)
            if step is None:
                raise MigrationError(f'缺少从 schema v{cursor} 到 v{cursor + 1} 的已验证迁移契约')
            steps.append(step)
            cursor = step.to_version
        return steps

    def migrate_dict(
        self,
        data: dict[str, Any],
        target_version: int | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        if not isinstance(data, dict):
            raise MigrationError('状态根对象必须是 JSON 对象')
        current = data.get('schema_version')
        if type(current) is not int or current < 1:
            raise MigrationError('缺少合法整数 schema_version，拒绝猜测迁移')
        target = RunState.SCHEMA_VERSION if target_version is None else target_version
        steps = self.plan(current, target)
        working = copy.deepcopy(data)
        executed: list[str] = []
        for step in steps:
            updated = step.transform(copy.deepcopy(working))
            if not isinstance(updated, dict):
                raise MigrationError(f'迁移步骤 {step.name} 未返回 dict')
            if updated.get('schema_version') != step.to_version:
                raise MigrationError(
                    f'迁移步骤 {step.name} 必须将 schema_version 更新为 {step.to_version}'
                )
            working = updated
            executed.append(step.name)
        return working, executed

    def migrate_file(
        self,
        state_path: Path,
        target_version: int | None = None,
        *,
        apply: bool = False,
        backup_dir: Path | None = None,
    ) -> dict[str, Any]:
        """默认干跑（apply=False）；apply=True 时强制加锁、校验原字节备份再原子替换。"""
        path = Path(state_path).resolve()
        if not path.is_file() or path.is_symlink():
            raise MigrationError('目标 state.json 不是普通文件')
        before_sha = sha256_file(path)
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise MigrationError(f'无法解析 state.json: {error}') from None
        if not isinstance(raw, dict):
            raise MigrationError('state.json 根节点必须为 JSON 对象')
        from_version = raw.get('schema_version')
        migrated, executed = self.migrate_dict(raw, target_version=target_version)
        to_version = migrated['schema_version']
        preview_bytes = json.dumps(migrated, ensure_ascii=False, indent=2)
        preview_sha = sha256_text(preview_bytes)

        receipt: dict[str, Any] = {
            'path': str(path),
            'from_version': from_version,
            'to_version': to_version,
            'steps': executed,
            'dry_run': not apply,
            'applied': False,
            'before_sha256': before_sha,
            'after_sha256': before_sha if not apply or not executed else preview_sha,
            'backup_path': None,
        }
        if not apply or not executed:
            return receipt

        lock = StageLock(path.parent / '.run.lock')
        if not lock.acquire():
            raise MigrationError('运行目录存在活跃 .run.lock，拒绝并发迁移')
        try:
            if sha256_file(path) != before_sha:
                raise MigrationError('迁移前 state.json 字节已变化，中止写入')
            backup_root = Path(backup_dir).resolve() if backup_dir else path.parent
            backup_root.mkdir(parents=True, exist_ok=True)
            backup_file = backup_root / f'{path.name}.v{from_version}.bak'
            if backup_file.exists():
                raise MigrationError(f'备份文件已存在，拒绝覆盖：{backup_file}')
            shutil.copyfile(path, backup_file)
            if sha256_file(backup_file) != before_sha:
                backup_file.unlink(missing_ok=True)
                raise MigrationError('原字节备份哈希核验失败，中止迁移')
            atomic_write_json(path, migrated)
            receipt['applied'] = True
            receipt['after_sha256'] = sha256_file(path)
            receipt['backup_path'] = str(backup_file)
            return receipt
        finally:
            lock.release()


# 默认注册表保持严格只读：不臆造未经真实样本验证的 v1->v2 迁移。
DEFAULT_MIGRATION_REGISTRY = SchemaMigrationRegistry()
