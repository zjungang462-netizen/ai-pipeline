"""契约化状态 schema 迁移注册表、干跑与原字节备份回滚门禁测试。"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from orchestrator.state import RunState, StageLock, sha256_file
from orchestrator.state_migration import (
    DEFAULT_MIGRATION_REGISTRY,
    MigrationError,
    MigrationStep,
    SchemaMigrationRegistry,
)


def _v2_to_v3(data: dict) -> dict:
    data['schema_version'] = 3
    data.setdefault('migrated_flags', []).append('v2_to_v3')
    return data


def _v3_to_v4(data: dict) -> dict:
    data['schema_version'] = 4
    data.setdefault('migrated_flags', []).append('v3_to_v4')
    return data


class StateMigrationTests(unittest.TestCase):
    def test_default_registry_refuses_unverified_v1_migration_and_allows_noop(self):
        self.assertEqual(DEFAULT_MIGRATION_REGISTRY.registered_transitions(), ())
        with self.assertRaises(MigrationError):
            DEFAULT_MIGRATION_REGISTRY.migrate_dict({'schema_version': 1, 'run_id': 'old'})
        current = {'schema_version': RunState.SCHEMA_VERSION, 'run_id': 'cur'}
        out, steps = DEFAULT_MIGRATION_REGISTRY.migrate_dict(current)
        self.assertEqual(steps, [])
        self.assertEqual(out, current)
        self.assertIsNot(out, current)

    def test_dry_run_never_mutates_file_and_apply_creates_verified_backup(self):
        registry = SchemaMigrationRegistry((
            MigrationStep(2, 3, 'add_flag_v3', _v2_to_v3),
            MigrationStep(3, 4, 'add_flag_v4', _v3_to_v4),
        ))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / 'run-1'
            run_dir.mkdir()
            state_file = run_dir / 'state.json'
            state_file.write_text(json.dumps({'schema_version': 2, 'run_id': 'run-1'}), encoding='utf-8')
            original_sha = sha256_file(state_file)

            dry = registry.migrate_file(state_file, target_version=4, apply=False)
            self.assertTrue(dry['dry_run'])
            self.assertFalse(dry['applied'])
            self.assertEqual(dry['steps'], ['add_flag_v3', 'add_flag_v4'])
            self.assertEqual(sha256_file(state_file), original_sha)

            backup_dir = root / 'backups'
            applied = registry.migrate_file(state_file, target_version=4, apply=True, backup_dir=backup_dir)
            self.assertTrue(applied['applied'])
            self.assertEqual(applied['before_sha256'], original_sha)
            backup_path = Path(applied['backup_path'])
            self.assertTrue(backup_path.is_file())
            self.assertEqual(sha256_file(backup_path), original_sha)
            updated = json.loads(state_file.read_text(encoding='utf-8'))
            self.assertEqual(updated['schema_version'], 4)
            self.assertEqual(updated['migrated_flags'], ['v2_to_v3', 'v3_to_v4'])

    def test_active_lock_downgrade_or_bad_step_fails_closed_without_modifying_file(self):
        def broken_step(data: dict) -> dict:
            data['schema_version'] = 99  # 违反步进契约
            return data

        bad_registry = SchemaMigrationRegistry((MigrationStep(2, 3, 'broken', broken_step),))
        good_registry = SchemaMigrationRegistry((MigrationStep(2, 3, 'ok', _v2_to_v3),))

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / 'run-2'
            run_dir.mkdir()
            state_file = run_dir / 'state.json'
            state_file.write_text(json.dumps({'schema_version': 2, 'run_id': 'run-2'}), encoding='utf-8')
            original_sha = sha256_file(state_file)

            # 违反契约的迁移步骤必须被拒绝且不改写磁盘
            with self.assertRaises(MigrationError):
                bad_registry.migrate_file(state_file, target_version=3, apply=True)
            self.assertEqual(sha256_file(state_file), original_sha)

            # 禁止降级
            with self.assertRaises(MigrationError):
                good_registry.plan(3, 2)

            # 存在活跃 .run.lock 时拒绝并发迁移
            lock = StageLock(run_dir / '.run.lock')
            self.assertTrue(lock.acquire())
            try:
                with self.assertRaises(MigrationError):
                    good_registry.migrate_file(state_file, target_version=3, apply=True)
            finally:
                lock.release()
            self.assertEqual(sha256_file(state_file), original_sha)
