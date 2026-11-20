"""S1 崩溃恢复测试：三种 kill 点续跑后产物哈希集合一致（评审 S1/B4）。

kill 点：
①写产物中途（attempt 目录有半成品，无 success.json）
②临时文件已写、未改名（state.json.tmp 残留）
③attempt 完成但 current 未切换（success.json 在，current_attempt 未指）
"""
from __future__ import annotations

import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.config import parse_config
from orchestrator.contracts import StageStatus
from orchestrator.engine import Engine
from orchestrator.state import RunState, atomic_write_json
from orchestrator.adapters.fake import FakeAdapter


def make_cfg(tmp: Path):
    raw = {
        "task_workdir": "workspace", "state_dir": ".orchestrator",
        "stages": [
            {"name": "requirements", "adapter": "fake", "prompt": "需求", "kind": "report"},
            {"name": "coding", "adapter": "fake", "prompt": "编码", "kind": "code"},
            {"name": "review", "adapter": "fake", "prompt": "审查", "kind": "report"},
        ],
        "adapters": {"fake": {"type": "fake"}},
    }
    return parse_config(raw, base_dir=tmp)


def mk(cfg):
    return Engine(cfg, {"fake": FakeAdapter()}, run_id="r1", task="demo")


def hashes(eng):
    return {s.name: eng.state.stage(s.name).outputs_hash for s in eng.config.stages}


class TestCrashRecovery(unittest.TestCase):
    def _baseline(self, tmp):
        cfg = make_cfg(tmp)
        eng = mk(cfg)
        eng.run()
        return cfg, hashes(eng)

    def test_kill_mid_artifact(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg, base = self._baseline(tmp)
            # 模拟 coding 阶段写产物中途被 kill：新建一个无 success.json 的 attempt
            rs = RunState(cfg.state_dir, "r1")
            rs.load()
            n, adir = rs.new_attempt_dir("coding")
            (adir / "half.txt").write_text("半成品", encoding="utf-8")
            rs.stage("coding").current_attempt = n
            rs.stage("coding").status = StageStatus.RUNNING
            rs.save()
            # 续跑
            eng = mk(cfg)
            res = eng.run()
            self.assertEqual(res["coding"], StageStatus.DONE.value)
            self.assertEqual(hashes(eng), base)

    def test_kill_tmp_state_file(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg, base = self._baseline(tmp)
            # 模拟 state.json 临时文件已写未改名残留
            state_file = cfg.state_dir / "runs" / "r1" / "state.json"
            tmp_residue = state_file.with_name("state.json.tmp.deadbeef")
            tmp_residue.write_text('{"corrupt": true', encoding="utf-8")
            self.assertTrue(tmp_residue.exists())
            # 续跑：权威 state.json 完好，残留 tmp 不影响
            eng = mk(cfg)
            res = eng.run()
            for name in res:
                self.assertEqual(res[name], StageStatus.DONE.value)
            self.assertEqual(hashes(eng), base)

    def test_kill_success_not_committed(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg, base = self._baseline(tmp)
            rs = RunState(cfg.state_dir, "r1")
            rs.load()
            # 模拟 coding 的 current 指针丢失（success.json 在但 current 未指）
            st = rs.stage("coding")
            old_current = st.current_attempt
            st.current_attempt = None
            st.status = StageStatus.INTERRUPTED
            rs.save()
            eng = mk(cfg)
            res = eng.run()
            self.assertEqual(res["coding"], StageStatus.DONE.value)
            self.assertEqual(hashes(eng), base)

    def test_state_load_rejects_future_schema_without_mutating_or_rewriting(self):
        with tempfile.TemporaryDirectory() as d:
            state=RunState(Path(d),'expected')
            state.task='unchanged in-memory task'
            state.state_file.parent.mkdir(parents=True)
            raw=b'{"schema_version":4,"run_id":"expected","task":"future"}'
            state.state_file.write_bytes(raw)

            with self.assertRaisesRegex(ValueError,'schema_version'):
                state.load()

            self.assertEqual(state.task,'unchanged in-memory task')
            self.assertEqual(state.state_file.read_bytes(),raw)
            with self.assertRaisesRegex(ValueError,'拒绝覆盖'):
                state.save()
            self.assertEqual(state.state_file.read_bytes(),raw)

    def test_state_load_rejects_mismatched_run_id_without_mutating_or_rewriting(self):
        with tempfile.TemporaryDirectory() as d:
            state=RunState(Path(d),'expected')
            state.task='unchanged in-memory task'
            state.state_file.parent.mkdir(parents=True)
            raw=b'{"schema_version":2,"run_id":"other","task":"wrong run"}'
            state.state_file.write_bytes(raw)

            with self.assertRaisesRegex(ValueError,'run_id'):
                state.load()

            self.assertEqual(state.task,'unchanged in-memory task')
            self.assertEqual(state.state_file.read_bytes(),raw)

    def test_state_load_accepts_and_round_trips_current_schema(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            state=RunState(root,'expected')
            state.task='preserved task'
            state.stage('coding').status=StageStatus.INTERRUPTED
            state.save()

            loaded=RunState(root,'expected')
            self.assertTrue(loaded.load())
            self.assertEqual(loaded.task,'preserved task')
            self.assertEqual(loaded.stage('coding').status,StageStatus.INTERRUPTED)
            loaded.save()
            saved=json.loads(state.state_file.read_text(encoding='utf8'))
            self.assertEqual(saved['schema_version'],RunState.SCHEMA_VERSION)
            self.assertEqual(saved['run_id'],'expected')

    def test_legacy_events_migrate_once_on_save(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state = RunState(root, 'legacy')
            old_events = [{'kind': 'model_call_started', 'stage': 'review', 'ts': 1}]
            atomic_write_json(state.state_file, {'schema_version': 2, 'run_id': 'legacy',
                                                'revision': 8, 'events': old_events})
            self.assertTrue(state.load())
            path = state.run_dir / 'events.jsonl'
            self.assertFalse(path.exists())
            state.log_event('attempt_done', 'review')
            state.save()
            prefix = path.read_bytes()
            state.save()
            self.assertEqual(path.read_bytes(), prefix)
            state.log_event('approved', 'review')
            state.save()
            self.assertTrue(path.read_bytes().startswith(prefix))
            snapshot = json.loads(state.state_file.read_bytes())
            self.assertNotIn('events', snapshot)
            self.assertEqual(snapshot['events_bytes'], path.stat().st_size)
            loaded = RunState(root, 'legacy')
            self.assertTrue(loaded.load())
            self.assertEqual(loaded.events, state.events)
            self.assertEqual(loaded.events[:1], old_events)
            self.assertEqual(loaded.events[-1]['engine_seq'], 3)

    def test_crash_after_event_sync_exposes_only_committed_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state = RunState(root, 'crash')
            state.log_event('attempt_start', 'review')
            state.save()
            before = state.state_file.read_bytes()
            state.log_event('model_call_started', 'review')
            state.usage_total['tokens'] = 100
            with patch('orchestrator.state.atomic_write_json', side_effect=OSError('replace failed')):
                with self.assertRaises(OSError):
                    state.save()
            self.assertEqual(state.state_file.read_bytes(), before)
            journal = state.run_dir / 'events.jsonl'
            with journal.open('ab') as stream:
                stream.write(b'{"partial":')
            restarted = RunState(root, 'crash')
            self.assertTrue(restarted.load())
            self.assertEqual(restarted.usage_total['tokens'], 0)
            self.assertEqual([event['kind'] for event in restarted.events], ['attempt_start'])
            restarted.log_event('interrupted', 'review')
            restarted.save()
            self.assertTrue(state.load())
            self.assertEqual([event['kind'] for event in state.events], ['attempt_start', 'interrupted'])
            self.assertEqual(len(journal.read_bytes().splitlines()), 2)

    def test_short_committed_event_log_rejects_unsafe_resume(self):
        with tempfile.TemporaryDirectory() as d:
            state = RunState(Path(d), 'short')
            state.log_event('model_call_started', 'review')
            state.save()
            path = state.run_dir / 'events.jsonl'
            path.write_bytes(path.read_bytes()[:-1])
            with self.assertRaisesRegex(ValueError, '事件日志短于'):
                state.load()
            with self.assertRaisesRegex(ValueError, '拒绝覆盖'):
                state.save()

    def test_state_load_rejects_unversioned_state_without_mutating_or_rewriting(self):
        with tempfile.TemporaryDirectory() as d:
            state=RunState(Path(d),'expected')
            state.task='unchanged in-memory task'
            state.state_file.parent.mkdir(parents=True)
            raw=b'{"run_id":"expected","task":"unversioned"}'
            state.state_file.write_bytes(raw)

            with self.assertRaisesRegex(ValueError,'schema_version'):
                state.load()

            self.assertEqual(state.task,'unchanged in-memory task')
            self.assertEqual(state.state_file.read_bytes(),raw)
            with self.assertRaisesRegex(ValueError,'拒绝覆盖'):
                state.save()
            self.assertEqual(state.state_file.read_bytes(),raw)

    def test_state_load_rejects_malformed_field_before_mutating_instance(self):
        with tempfile.TemporaryDirectory() as d:
            state=RunState(Path(d),'expected')
            state.task='unchanged in-memory task'
            state.state_file.parent.mkdir(parents=True)
            raw=b'{"schema_version":2,"run_id":"expected","task":"bad","stages":[]}'
            state.state_file.write_bytes(raw)

            with self.assertRaisesRegex(ValueError,'stages'):
                state.load()

            self.assertEqual(state.task,'unchanged in-memory task')
            self.assertEqual(state.state_file.read_bytes(),raw)


if __name__ == "__main__":
    unittest.main()
