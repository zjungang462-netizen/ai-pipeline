"""真实状态与归档证据的备份、拒绝覆盖和恢复完整性。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from orchestrator.cli import main
from orchestrator.state import RunState, StageLock, atomic_write_json
from orchestrator.state_backup import backup_state, inspect_state, restore_state


class StateBackup(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'state'
        self.state = RunState(self.source, 'existing')
        self.state.task = '保留历史任务'
        self.state.usage_total = {'tokens': 123, 'duration_s': 4.5}
        self.state.save()
        self.evidence = self.state.run_dir / 'stages/verify/attempts/1/approval.json'
        atomic_write_json(self.evidence, {'outputs_hash': 'frozen', 'approved': True})
        (self.source / 'empty').mkdir()
        self.output = self.root / 'backup'

    def test_restore_preserves_bytes_and_empty_directories(self):
        self.state.log_event('approved', 'verify')
        self.state.save()
        before = self.state.state_file.read_bytes()
        journal = self.state.run_dir / 'events.jsonl'
        log_before = journal.read_bytes()
        evidence = self.evidence.read_bytes()
        report = backup_state(self.source, self.output)
        self.assertTrue(report['compatible'])
        target = self.root / 'restored'
        restored = restore_state(self.output, target)
        self.assertFalse(restored['automatic_resume'])
        self.assertFalse(restored['paths_rewritten'])
        self.assertEqual((target / 'runs/existing/state.json').read_bytes(), before)
        self.assertEqual((target / journal.relative_to(self.source)).read_bytes(), log_before)
        loaded = RunState(target, 'existing')
        self.assertTrue(loaded.load())
        self.assertEqual(loaded.events, self.state.events)
        self.assertEqual((target / self.evidence.relative_to(self.source)).read_bytes(), evidence)
        self.assertTrue((target / 'empty').is_dir())
        self.assertFalse(list(target.rglob('*.lock')))
        self.assertEqual(self.state.state_file.read_bytes(), before)

    def test_check_rejects_unknown_schema_without_modifying_it(self):
        raw = json.loads(self.state.state_file.read_text(encoding='utf8'))
        raw['schema_version'] = 99
        atomic_write_json(self.state.state_file, raw)
        before = self.state.state_file.read_bytes()
        self.assertFalse(inspect_state(self.source)['compatible'])
        self.assertFalse(backup_state(self.source, self.output)['compatible'])
        restore_state(self.output, self.root / 'forensic')
        self.assertEqual((self.root / 'forensic/runs/existing/state.json').read_bytes(), before)
        self.assertEqual(self.state.state_file.read_bytes(), before)

    def test_check_accepts_old_schema_two_missing_optional_fields(self):
        atomic_write_json(self.state.state_file, {'schema_version': 2, 'run_id': 'existing'})
        before = self.state.state_file.read_bytes()
        self.assertTrue(inspect_state(self.source)['compatible'])
        self.assertEqual(self.state.state_file.read_bytes(), before)

    def test_existing_and_nested_outputs_are_refused(self):
        with self.assertRaises(ValueError):
            backup_state(self.source, self.source / 'backup')
        self.output.mkdir()
        (self.output / 'keep').write_bytes(b'preserve')
        with self.assertRaises(ValueError):
            backup_state(self.source, self.output)
        self.assertEqual((self.output / 'keep').read_bytes(), b'preserve')

    def test_active_writer_blocks_backup_and_releases_its_own_locks(self):
        with StageLock(self.state.run_dir / '.run.lock'):
            with self.assertRaisesRegex(ValueError, '停写'):
                backup_state(self.source, self.output)
        self.assertFalse(self.output.exists())
        self.assertFalse((self.source / '.backup.lock').exists())
        self.assertFalse((self.state.run_dir / '.supervisor.lock').exists())

    def test_payload_tampering_is_rejected_before_restore(self):
        backup_state(self.source, self.output)
        (self.output / 'payload/runs/existing/state.json').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            restore_state(self.output, self.root / 'restored')
        self.assertFalse((self.root / 'restored').exists())

    def test_extra_payload_file_is_rejected(self):
        backup_state(self.source, self.output)
        (self.output / 'payload/unlisted.txt').write_bytes(b'unknown')
        with self.assertRaises(ValueError):
            restore_state(self.output, self.root / 'restored')

    def test_manifest_parent_path_is_rejected(self):
        backup_state(self.source, self.output)
        path = self.output / 'manifest.json'
        manifest = json.loads(path.read_text(encoding='utf8'))
        manifest['inventory']['files']['../outside'] = {'size': 0, 'sha256': ''}
        atomic_write_json(path, manifest)
        with self.assertRaisesRegex(ValueError, '非法路径'):
            restore_state(self.output, self.root / 'restored')

    def test_changed_source_during_copy_does_not_publish_backup(self):
        import shutil
        original = shutil.copy2

        def changing(source, target):
            result = original(source, target)
            if Path(source) == self.state.state_file:
                self.state.state_file.write_bytes(b'changed during backup')
            return result

        with mock.patch('orchestrator.state_backup.shutil.copy2', side_effect=changing):
            with self.assertRaisesRegex(ValueError, '发生变化'):
                backup_state(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_restore_cannot_overwrite_live_state(self):
        backup_state(self.source, self.output)
        before = self.state.state_file.read_bytes()
        with self.assertRaises(ValueError):
            restore_state(self.output, self.source)
        self.assertEqual(self.state.state_file.read_bytes(), before)

    def test_linked_source_is_rejected(self):
        with mock.patch('orchestrator.maintenance.linked', return_value=True):
            with self.assertRaises(ValueError):
                backup_state(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_cli_backup_check_and_restore(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['state-check', '--state-dir', str(self.source)]), 0)
            self.assertEqual(main(['state-backup', '--state-dir', str(self.source), '--output', str(self.output)]), 0)
            self.assertEqual(main(['state-restore', '--backup', str(self.output), '--output', str(self.root / 'restored')]), 0)


if __name__ == '__main__':
    unittest.main()
