"""协作存储、进程控制和本地接口的实际文件与 HTTP 回归。"""
import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.collaboration.service import Service
from orchestrator.collaboration.storage import Store, Conflict
from orchestrator.collaboration.projection import directory
from orchestrator.dashboard.server import create_server
from orchestrator.config import load_config


def eventually(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('等待执行确认超时')


class Collaboration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'workflow.toml'
        self.write_config('assert 1 + 1 == 2')

    def write_config(self, command, approval=False):
        value = json.dumps([sys.executable.replace('\\', '/'), '-c', command])
        self.config.write_text('task_workdir="ws"\nstate_dir=".state"\n'
                               '[run_budget]\nmax_tokens=12000\nmax_duration_s=90\n'
                               '[[stages]]\nname="verify"\nkind="test"\ntimeout_s=20\n'
                               f'approval={str(approval).lower()}\ntest_cmd={value}\n', encoding='utf8')

    def service(self):
        service = Service(self.root / 'collaboration.sqlite3', [self.config])
        def cleanup():
            for process in service.active.values():
                try:
                    process.wait(timeout=10)
                except Exception:
                    from orchestrator.supervisor import _terminate
                    _terminate(process)
        self.addCleanup(cleanup)
        return service

    def create(self, service, key='create-request-001'):
        project = service.store.projects()[0]
        return service.create_run(project['id'], '验证受控运行', key)

    def wait_status(self, service, key, status):
        return eventually(lambda: (value if (value := service.snapshot(key))['lifecycle'] == status else None))

    def test_store_commands_messages_and_replay_are_idempotent(self):
        store = Store(self.root / 'db.sqlite3')
        first = store.enqueue('run', 'pause', {}, 'same-command-001')
        self.assertEqual(first['id'], store.enqueue('run', 'pause', {}, 'same-command-001')['id'])
        with self.assertRaises(Conflict):
            store.enqueue('run', 'cancel', {}, 'same-command-001')
        store.finish(first['id'], 'applied', {'checkpoint': True})
        store.finish(first['id'], 'applied', {'checkpoint': True})
        a = store.message('run', '<script>untrusted</script>', 'message-001')
        b = store.message('run', '<script>untrusted</script>', 'message-001')
        self.assertEqual(a['id'], b['id'])
        events = store.events('run')
        self.assertEqual(len(events), 3)
        self.assertEqual(store.events('run', events[0]['cursor']), events[1:])

    def test_atomic_state_replace_retries_transient_windows_read_lock(self):
        import os
        from orchestrator.state import atomic_write_json
        target = self.root / 'state.json'
        original = os.replace
        attempts = []
        def replace(source, destination):
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError('temporary sharing violation')
            return original(source, destination)
        with mock.patch('orchestrator.state.os.replace', side_effect=replace):
            atomic_write_json(target, {'committed': True})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {'committed': True})
        self.assertEqual(len(attempts), 3)

    def test_managed_run_finishes_and_projection_does_not_duplicate(self):
        service = self.service()
        run = self.create(service)
        result = self.wait_status(service, run['id'], 'done')
        self.assertEqual(result['stages']['verify']['status'], 'done')
        before = service.store.last_cursor(run['id'])
        for _ in range(3):
            service.snapshot(run['id'])
        self.assertEqual(service.store.last_cursor(run['id']), before)
        self.assertEqual(self.create(service)['id'], run['id'])

    def test_pause_at_real_test_boundary_then_resume(self):
        self.write_config('import time; time.sleep(1.2); print("completed current test")')
        service = self.service()
        run = self.create(service)
        eventually(lambda: service.snapshot(run['id'])['stages'].get('verify', {}).get('status') == 'running')
        command = service.command(run['id'], 'pause', {}, 'pause-command-001')
        paused = self.wait_status(service, run['id'], 'paused')
        self.assertEqual(paused['stages']['verify']['status'], 'done')
        self.assertEqual(service.store.command(command['id'])['status'], 'applied')
        self.assertEqual(service.command(run['id'], 'pause', {}, 'pause-command-001')['id'], command['id'])
        service.command(run['id'], 'resume', {}, 'resume-command-001')
        result = self.wait_status(service, run['id'], 'done')
        self.assertEqual(result['stages']['verify']['current_attempt'], 1)

    def test_cancel_terminates_owned_worker_and_is_terminal(self):
        self.write_config('import time; time.sleep(15)')
        service = self.service()
        run = self.create(service)
        eventually(lambda: service.snapshot(run['id'])['stages'].get('verify', {}).get('status') == 'running')
        command = service.command(run['id'], 'cancel', {}, 'cancel-command-001')
        value = self.wait_status(service, run['id'], 'cancelled')
        self.assertEqual(service.store.command(command['id'])['status'], 'applied')
        self.assertFalse(value['capabilities']['resume'])

    def test_approval_binds_attempt_and_duplicate_is_not_reapplied(self):
        self.write_config('print("verified")', approval=True)
        service = self.service()
        run = self.create(service)
        value = self.wait_status(service, run['id'], 'waiting_approval')
        stage = value['stages']['verify']
        target = {'attempt': stage['current_attempt'], 'inputs_hash': stage['inputs_hash'], 'outputs_hash': stage['outputs_hash']}
        with self.assertRaises(Conflict):
            service.command(run['id'], 'approve', {'stage': 'verify', 'target': {**target, 'attempt': 99}}, 'stale-approval-001')
        command = service.command(run['id'], 'approve', {'stage': 'verify', 'target': target}, 'approve-command-001')
        self.wait_status(service, run['id'], 'done')
        repeated = service.command(run['id'], 'approve', {'stage': 'verify', 'target': target}, 'approve-command-001')
        self.assertEqual(repeated['id'], command['id'])
        state = json.loads((directory(service.store.run(run['id'])) / 'state.json').read_text(encoding='utf8'))
        from orchestrator.event_journal import read_events
        events = read_events(directory(service.store.run(run['id'])), state)
        self.assertEqual(sum(e['kind'] == 'approved' for e in events), 1)

    def test_historical_read_does_not_start_model_or_mutate_state(self):
        from orchestrator.engine import Engine
        cfg = load_config(self.config)
        engine = Engine(cfg, {}, 'historical', '已完成的历史任务')
        engine.run()
        path = engine.state.state_file
        before = path.read_bytes()
        service = self.service()
        run = service.store.runs()[0]
        with mock.patch('orchestrator.adapters.openai_compat.urllib.request.urlopen', side_effect=AssertionError('must not call')):
            for _ in range(3):
                self.assertFalse(service.snapshot(run['id'])['managed'])
                service.store.message(run['id'], '留言不执行 @developer', 'note-only-001')
        self.assertEqual(path.read_bytes(), before)

    def test_cached_view_observes_committed_state_and_config_changes(self):
        service = self.service()
        run = self.create(service)
        original = self.wait_status(service, run['id'], 'done')
        path = directory(service.store.run(run['id'])) / 'state.json'
        state = json.loads(path.read_text(encoding='utf8'))
        state['task'] = '更新后的已提交任务快照'
        state['revision'] += 1
        from orchestrator.state import atomic_write_json
        atomic_write_json(path, state)
        updated = service.snapshot(run['id'])
        self.assertEqual(updated['task'], state['task'])
        self.assertNotEqual(updated['task'], original['task'])
        self.config.write_text(self.config.read_text(encoding='utf8').replace('max_tokens=12000', 'max_tokens=13000'), encoding='utf8')
        changed = service.snapshot(run['id'])
        self.assertEqual(changed['budget']['max_tokens'], 13000)
        self.assertEqual(changed['config_integrity'], 'changed')
        self.assertFalse(changed['capabilities']['rerun'])

    def test_run_list_reads_summaries_without_loading_artifact_bodies(self):
        service = self.service()
        run = self.create(service)
        self.wait_status(service, run['id'], 'done')
        with mock.patch('orchestrator.collaboration.service.artifact_list', side_effect=AssertionError('list must stay lightweight')):
            values = service.list_runs(20)
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]['id'], run['id'])
        self.assertEqual(values[0]['lifecycle'], 'done')
        self.assertNotIn('artifacts', values[0])

    def test_snapshot_retries_when_state_changes_between_pair_reads(self):
        from orchestrator.collaboration import projection
        from orchestrator.state import atomic_write_json
        service = self.service()
        run = self.create(service)
        self.wait_status(service, run['id'], 'done')
        entry = service.store.run(run['id'])
        path = directory(entry) / 'state.json'
        original = projection.read_json
        updated = False
        def read_and_commit(target, default=None):
            nonlocal updated
            result = original(target, default)
            if Path(target) == path and not updated:
                updated = True
                committed = {**result, 'task': '下一次原子提交的任务快照'}
                atomic_write_json(path, committed)
            return result
        with mock.patch.object(projection, 'read_json', side_effect=read_and_commit):
            state, _ = projection.project(service.store, entry, {})
        self.assertTrue(updated)
        self.assertEqual(state['task'], '下一次原子提交的任务快照')

    def test_http_requires_local_session_origin_and_csrf(self):
        server = create_server(self.root / 'http.sqlite3', [self.config], 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.application.closed.set)
        port = server.server_port
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        self.addCleanup(conn.close)
        conn.request('GET', '/api/projects')
        response = conn.getresponse();response.read();self.assertEqual(response.status, 401)
        conn.request('POST', '/api/session', json.dumps({'token': server.application.bootstrap}),
                     {'Content-Type': 'application/json', 'Origin': server.application.origin})
        response = conn.getresponse();data = json.loads(response.read());cookie = response.getheader('Set-Cookie').split(';')[0]
        self.assertEqual(response.status, 200)
        headers = {'Cookie': cookie, 'Content-Type': 'application/json', 'Origin': server.application.origin}
        conn.request('POST', '/api/runs', '{}', headers)
        response = conn.getresponse();response.read();self.assertEqual(response.status, 403)
        headers['X-Workbench-CSRF'] = data['csrf'];headers['Origin'] = 'https://untrusted.invalid'
        conn.request('POST', '/api/runs', '{}', headers)
        response = conn.getresponse();response.read();self.assertEqual(response.status, 403)
        conn.request('GET', '/api/projects', headers={'Cookie': cookie})
        response = conn.getresponse();self.assertEqual(response.status, 200);self.assertEqual(len(json.loads(response.read())['projects']), 1)

    def test_artifact_download_verifies_hash_and_rejects_paths(self):
        service = self.service();run = self.create(service)
        value = self.wait_status(service, run['id'], 'done')
        from orchestrator.collaboration.projection import read_artifact
        ref = next(a for a in value['artifacts'] if a['name'] == 'test_record.json')
        self.assertEqual(read_artifact(service.store.run(run['id']), ref['id'])['integrity'], 'valid')
        with self.assertRaises((ValueError, KeyError, RuntimeError)):
            read_artifact(service.store.run(run['id']), '../1/secret')

    def test_http_event_history_does_not_build_a_room_snapshot(self):
        from orchestrator.state import RunState
        cfg = load_config(self.config)
        state = RunState(cfg.state_dir, 'history')
        state.log_event('fixture_published', 'verify')
        state.save()
        server = create_server(self.root / 'history.sqlite3', [self.config], 0, recover=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        service = server.application.service
        entry = service.store.runs()[0]
        # 下一条事件由 HTTP 请求同步，即使完整房间暂不可用也能读取。
        state.log_event('next_fixture', 'verify')
        state.save()
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        self.addCleanup(conn.close)
        headers = {'Cookie': f'workbench_session={server.application.session}'}
        with mock.patch.object(service, 'snapshot', side_effect=AssertionError('禁止构建房间')):
            conn.request('GET', f"/api/runs/{entry['id']}/events", headers=headers)
            response = conn.getresponse()
            payload = json.loads(response.read())
        self.assertEqual(response.status, 200)
        kinds = [row['payload']['kind'] for row in payload['events'] if row['kind'] == 'engine_event']
        self.assertEqual(kinds, ['fixture_published', 'next_fixture'])

    def test_projection_recovers_after_state_committed_but_message_not_written(self):
        from orchestrator.engine import Engine
        from orchestrator.collaboration.projection import project
        cfg = load_config(self.config)
        engine = Engine(cfg, {}, 'history', 'projection proof');engine.run()
        store = Store(self.root / 'projection.sqlite3')
        project_id = store.register_project(self.config)
        key = store.register_run(project_id, 'history', self.config, cfg.state_dir)
        run = store.run(key)
        original = engine.state.state_file.read_bytes()
        with mock.patch.object(store, 'message', side_effect=OSError('simulated projector interruption')):
            with self.assertRaises(OSError):project(store, run)
        project(store, run)
        self.assertEqual(sum(e['kind'] == 'message' for e in store.events(key)), 1)
        before = store.last_cursor(key);project(store, run)
        self.assertEqual(store.last_cursor(key), before)
        self.assertEqual(engine.state.state_file.read_bytes(), original)

    def test_control_receipt_recovers_without_approving_twice(self):
        from orchestrator.engine import Engine
        from orchestrator.collaboration.control import Control
        self.write_config('print("proof")', approval=True)
        cfg = load_config(self.config);engine = Engine(cfg, {}, 'r', 'receipt proof');engine.run()
        store = Store(self.root / 'control.sqlite3')
        project_id = store.register_project(self.config)
        key = store.register_run(project_id, 'r', self.config, cfg.state_dir)
        stage = engine.state.stage('verify')
        command = store.enqueue(key, 'approve', {'stage': 'verify', 'target': {
            'attempt': stage.current_attempt, 'inputs_hash': stage.inputs_hash,
            'outputs_hash': stage.outputs_hash}}, 'receipt-recovery-001')
        control = Control(store.path, key)
        with engine._ownership():
            with mock.patch.object(control.store, 'finish', side_effect=OSError('lost acknowledgement')):
                with self.assertRaises(OSError):control.checkpoint(engine)
        resumed = Engine(cfg, {}, 'r', 'receipt proof');resumed.prepare()
        with resumed._ownership():Control(store.path, key).checkpoint(resumed)
        self.assertEqual(store.command(command['id'])['status'], 'applied')
        self.assertEqual(sum(e['kind'] == 'approved' for e in resumed.state.events), 1)

    def test_derived_task_uses_fixed_independent_workspace(self):
        from orchestrator.engine import Engine
        from orchestrator.adapters.factory import build_all
        self.config.write_text('task_workdir="ws"\nstate_dir=".state"\n'
            '[run_budget]\nmax_tokens=12000\nmax_duration_s=90\n'
            '[[stages]]\nname="coding"\nkind="code"\nadapter="fake"\nprompt="生成任务源文件"\n'
            '[adapters.fake]\ntype="fake"\n', encoding='utf8')
        cfg = load_config(self.config)
        engine = Engine(cfg, build_all(cfg.adapters, {}), 'parent', 'original task');engine.run()
        service = self.service();parent = service.store.runs()[0]
        before = (cfg.task_workdir / 'coding_output.py').read_bytes()
        old_state = engine.state.state_file.read_bytes()
        child = service.create_run(parent['project_id'], 'follow-up task', 'derive-request-001', parent['id'])
        self.wait_status(service, child['id'], 'done')
        child_cfg = load_config(service.store.run(child['id'])['config'])
        self.assertNotEqual(child_cfg.task_workdir, cfg.task_workdir)
        self.assertEqual((cfg.task_workdir / 'coding_output.py').read_bytes(), before)
        self.assertEqual(engine.state.state_file.read_bytes(), old_state)
        from orchestrator.collaboration.projection import code_diff
        diff = code_diff(service.store.run(child['id']), 'coding', 'project')
        self.assertIn('follow-up task', diff['body'])


if __name__ == '__main__':
    unittest.main()
