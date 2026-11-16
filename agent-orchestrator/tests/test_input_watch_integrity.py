"""补充复现：真实测试缓存不能忽略源码，监视器不能移动其它写入者的文件。"""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.adapters.base import Adapter
from orchestrator.config import parse_config
from orchestrator.contracts import PreflightReport, Result
from orchestrator.engine import Engine
from orchestrator.state import atomic_write_json


class InputIntegrity(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'ws').mkdir()

    def config(self, script):
        return parse_config({'task_workdir': 'ws', 'state_dir': 'state', 'stages': [
            {'name': 'testing', 'kind': 'test',
             'test_cmd': [sys.executable, '-X', 'utf8', '-c', script]}]}, base_dir=self.root)

    def run_engine(self, cfg):
        engine = Engine(cfg, {}, run_id='verify', task='测试缓存完整性')
        return engine, engine.run()['testing']

    @staticmethod
    def starts(engine):
        return sum(e['kind'] == 'test_start' for e in engine.state.events)

    def test_existing_source_rewritten_after_assertion_is_rejected(self):
        source = self.root / 'ws/module.py'
        source.write_text('VALUE = 1\n', encoding='utf8')
        cfg = self.config("from pathlib import Path; import runpy; "
                          "assert runpy.run_path('module.py')['VALUE'] == 1; "
                          "Path('module.py').write_text('VALUE = 2\\n', encoding='utf8')")
        first, status = self.run_engine(cfg)
        self.assertEqual(status, 'failed')
        self.assertIn('被测输入在测试期间发生变化', first.state.stage('testing').error)
        self.assertNotIn('module.py', first.state.test_artifact_hashes.get('testing', {}))
        record = json.loads((first.state.current_attempt_dir('testing') / 'test_record.json')
                            .read_text(encoding='utf8'))
        self.assertEqual(record['exit_code'], 0, '保留真实命令退出码，明确是输入完整性拒收')
        self.assertIn('test_inputs_modified', record['failure_ids'])
        source.write_text('VALUE = 999\n', encoding='utf8')
        second, status = self.run_engine(cfg)
        self.assertEqual(status, 'failed')
        self.assertEqual(self.starts(second), 2)

    def test_ordinary_source_change_runs_real_test_again(self):
        source = self.root / 'ws/module.py'
        source.write_text('VALUE = 1\n', encoding='utf8')
        cfg = self.config("import runpy; assert runpy.run_path('module.py')['VALUE'] == 1")
        first, status = self.run_engine(cfg)
        self.assertEqual(status, 'done')
        source.write_text('VALUE = 999\n', encoding='utf8')
        second, status = self.run_engine(cfg)
        self.assertEqual(status, 'failed')
        self.assertEqual(self.starts(second), 2)

    def test_changed_generated_file_cannot_reuse_old_pass_after_failure(self):
        cfg = self.config("from pathlib import Path; p=Path('generated.py'); "
                          "assert not p.exists() or p.read_text(encoding='utf8') == 'VALID'; "
                          "p.write_text('VALID', encoding='utf8')")
        first, status = self.run_engine(cfg)
        self.assertEqual(status, 'done')
        expected = first.state.test_artifact_hashes['testing']['generated.py']
        (self.root / 'ws/generated.py').write_text('INVALID', encoding='utf8')
        second, status = self.run_engine(cfg)
        self.assertEqual(status, 'failed')
        self.assertEqual(second.state.test_artifact_hashes['testing']['generated.py'], expected)
        third, status = self.run_engine(cfg)
        self.assertEqual(status, 'failed', '失败不能通过更新输出绑定重新命中旧 pass')
        self.assertEqual(self.starts(third), 2)

    def test_missing_output_is_recreated_and_next_resume_converges(self):
        cfg = self.config("from pathlib import Path; Path('junit.xml').write_text('PASS', encoding='utf8')")
        first, status = self.run_engine(cfg)
        self.assertEqual(status, 'done')
        (self.root / 'ws/junit.xml').unlink()
        second, status = self.run_engine(cfg)
        self.assertEqual(status, 'done')
        self.assertEqual(self.starts(second), 2)
        self.assertTrue((self.root / 'ws/junit.xml').exists())
        third, status = self.run_engine(cfg)
        self.assertEqual(status, 'done')
        self.assertEqual(self.starts(third), 2)

    def test_names_only_legacy_record_does_not_ignore_source(self):
        source = self.root / 'ws/module.py'
        source.write_text('VALUE = 1\n', encoding='utf8')
        cfg = self.config("import runpy; assert runpy.run_path('module.py')['VALUE'] == 1")
        first, status = self.run_engine(cfg)
        self.assertEqual(status, 'done')
        raw = json.loads(first.state.state_file.read_text(encoding='utf8'))
        raw.pop('test_artifact_hashes', None)
        raw['test_artifacts'] = {'testing': ['module.py']}
        atomic_write_json(first.state.state_file, raw)
        source.write_text('VALUE = 999\n', encoding='utf8')
        second, status = self.run_engine(cfg)
        self.assertEqual(status, 'failed')
        self.assertEqual(self.starts(second), 2)
        self.assertNotIn('module.py', second.state.test_artifact_hashes.get('testing', {}))

    def test_edit_after_test_snapshot_does_not_rebind_pass_to_unseen_source(self):
        source = self.root / 'ws/module.py'
        source.write_text('VALUE = 1\n', encoding='utf8')
        cfg = self.config("import runpy; assert runpy.run_path('module.py')['VALUE'] == 1")
        engine = Engine(cfg, {}, run_id='verify', task='测试缓存完整性')
        record = engine._record_test_artifacts
        def editor(*args, **kwargs):
            result = record(*args, **kwargs)
            source.write_text('VALUE = 999\n', encoding='utf8')
            return result
        with mock.patch.object(engine, '_record_test_artifacts', side_effect=editor):
            self.assertEqual(engine.run()['testing'], 'done')
        resumed, status = self.run_engine(cfg)
        self.assertEqual(status, 'failed', '通过结果只能绑定实际测试过的输入')
        self.assertEqual(self.starts(resumed), 2)


class Report(Adapter):
    channel = 'fake'

    def __init__(self, write):
        self.write = write
        self.calls = 0

    def preflight(self):
        return PreflightReport(adapter='fake', ok=True)

    def run(self, prompt, workdir, ctx):
        self.calls += 1
        self.write()
        report = Path(ctx.runs_dir) / 'reports/design_report.md'
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text('本地测试报告\n', encoding='utf8')
        return Result(ok=True, exit_code=0, channel=self.channel, text='report', report_files=[report])


class WatchIntegrity(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.cfg = parse_config({'task_workdir': 'ws', 'state_dir': 'state',
                                 'watch_root': '.', 'watch_enforce': True,
                                 'adapters': {'fake': {'type': 'fake'}}, 'stages': [
                                     {'name': 'design', 'kind': 'report', 'adapter': 'fake',
                                      'prompt': 'fixture', 'max_attempts': 1}]}, base_dir=self.root)

    def test_independent_writer_file_stays_in_place_and_resume_stays_failed(self):
        ready, written = threading.Event(), threading.Event()
        source = self.root / 'other-project/state.json'
        def writer():
            if ready.wait(10):
                source.parent.mkdir()
                source.write_text('{"owner":"other"}', encoding='utf8')
                written.set()
        thread = threading.Thread(target=writer)
        thread.start()
        def write():
            ready.set()
            self.assertTrue(written.wait(10))
        adapter = Report(write)
        engine = Engine(self.cfg, {'fake': adapter}, run_id='watch', task='独立写入者')
        try:
            self.assertEqual(engine.run()['design'], 'failed')
        finally:
            ready.set()
            thread.join(10)
        self.assertEqual(source.read_text(encoding='utf8'), '{"owner":"other"}')
        violation = engine.state.control['write_violations']['design']
        evidence = json.loads((engine.state.run_dir / violation['evidence']).read_text(encoding='utf8'))
        self.assertEqual(evidence['observed_count'], 1)
        self.assertEqual(evidence['changes'][0]['path'], str(source.resolve()))
        self.assertNotIn('owner', json.dumps(evidence), '只保存变化元数据，不复制他人文件正文')
        resumed = Engine(self.cfg, {'fake': adapter}, run_id='watch', task='独立写入者')
        self.assertEqual(resumed.run()['design'], 'failed')
        self.assertEqual(adapter.calls, 1)

    def test_evidence_write_error_still_fails_without_moving_source(self):
        source = self.root / 'unrelated.txt'
        adapter = Report(lambda: source.write_text('keep', encoding='utf8'))
        engine = Engine(self.cfg, {'fake': adapter}, run_id='error', task='证据失败')
        with mock.patch('orchestrator.guards.atomic_write_json', side_effect=OSError('fixture full disk')):
            self.assertEqual(engine.run()['design'], 'failed')
        self.assertEqual(source.read_text(encoding='utf8'), 'keep')
        self.assertIsNone(engine.state.control['write_violations']['design']['evidence'])
        self.assertTrue(any(e['kind'] == 'watch_evidence_error' for e in engine.state.events))


if __name__ == '__main__':
    unittest.main()
