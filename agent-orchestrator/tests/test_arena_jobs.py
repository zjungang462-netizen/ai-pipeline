"""工作台竞技场后台任务与取消回归。"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from orchestrator import artifacts
from orchestrator.adapters.cli_base import run_cli_process
from orchestrator.cli_telemetry import CLITrace
from orchestrator.collaboration import arena_jobs
from orchestrator.collaboration.storage import Conflict
from orchestrator.config import load_config
from orchestrator.contracts import Result, RunContext
from orchestrator.dashboard.server import create_server
from orchestrator.testrun import run_test_cmd


class ArenaJobsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config_path = self.root / 'config.toml'
        self.config_path.write_text('''task_workdir = "repo"
state_dir = "state"
[[stages]]
name = "check"
kind = "test"
test_cmd = ["python", "-c", "pass"]
''', encoding='utf-8')
        self.cfg = load_config(self.config_path)
        artifacts.ensure_repo(self.cfg.task_workdir)
        self.body = {'task': '修改样例', 'allowed_files': ['sample.py'], 'candidates': [
            {'label': 'A', 'adapter_type': 'cli_agy'}, {'label': 'B', 'adapter_type': 'cli_agy'},
        ]}

    def _workbench(self):
        server = create_server(self.root / 'workbench.sqlite3', [self.config_path], 0, recover=False)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=15)
        self.addCleanup(conn.close)

        def request(method, path, body=None, extra_headers=None):
            headers = dict(auth)
            headers.update(extra_headers or {})
            if body is not None:
                headers['Content-Type'] = 'application/json'
            conn.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = conn.getresponse()
            raw = response.read().decode('utf-8')
            if response.getheader('Content-Type').startswith('application/json'):
                raw = json.loads(raw)
            return response.status, raw, response

        auth = {'Origin': server.application.origin, 'Idempotency-Key': 'test-idempotency-key'}
        status, data, response = request('POST', '/api/session', {'token': server.application.bootstrap})
        self.assertEqual(status, 200)
        auth['Cookie'] = response.getheader('Set-Cookie').split(';')[0]
        auth['X-Workbench-CSRF'] = data['csrf']
        status, projects, _ = request('GET', '/api/projects')
        self.assertEqual(status, 200)
        project = projects['projects'][0]['id']
        return request, project, server.shutdown

    def test_worker_launch_passes_declared_adapter_environment(self):
        with self.config_path.open('a', encoding='utf-8') as config:
            config.write('\n[adapters.qwen]\ntype="cli_qwen"\n'
                         'extra_env=["QWEN_API_KEY","OPENAI_BASE_URL"]\n'
                         '[adapters.api]\ntype="openai_compat"\n'
                         'base_url="https://example.invalid/v1"\nmodel="m"\n'
                         'api_key_env="API_KEY_ENV"\n')
        cfg = load_config(self.config_path)
        path = self.root / 'worker'
        path.mkdir()
        env = {'QWEN_API_KEY': 'qwen-private-key', 'OPENAI_BASE_URL': 'https://example.invalid/v1',
               'API_KEY_ENV': 'api-private-key', 'UNDECLARED_API_KEY': 'must-not-leak'}
        with patch.dict(os.environ, env), patch.object(arena_jobs.subprocess, 'Popen') as popen:
            arena_jobs._launch(path, cfg)
        child_env = popen.call_args.kwargs['env']
        self.assertEqual(child_env['QWEN_API_KEY'], env['QWEN_API_KEY'])
        self.assertEqual(child_env['OPENAI_BASE_URL'], env['OPENAI_BASE_URL'])
        self.assertEqual(child_env['API_KEY_ENV'], env['API_KEY_ENV'])
        self.assertNotIn('UNDECLARED_API_KEY', child_env)

    def test_fixed_test_project_rejects_arena_launch(self):
        request, project, _ = self._workbench()
        with patch.object(arena_jobs, '_launch') as launch:
            status, _, _ = request('POST', f'/api/projects/{project}/arena', self.body)
        self.assertEqual(status, 409)
        launch.assert_not_called()

    def test_http_background_job_reopens_and_replays_after_cursor(self):
        # 竞技场仅对开发项目开放。启动器和候选执行器均由测试替身接管。
        with self.config_path.open('a', encoding='utf-8') as config:
            config.write('\n[adapters.coder]\ntype="cli_agy"\n[[stages]]\n'
                         'name="coding"\nkind="code"\nadapter="coder"\nprompt="test"\n')
        request, project, shutdown = self._workbench()
        route = f'/api/projects/{project}/arena'
        with patch.object(arena_jobs, '_launch') as launch:
            status, queued, _ = request('POST', route, self.body)
            self.assertEqual(status, 202, queued)
            self.assertEqual(queued['status'], 'queued')
            status, duplicate, _ = request('POST', route, self.body)
            self.assertEqual((status, duplicate['arena_id']), (202, queued['arena_id']))
            self.assertEqual(request('POST', route, dict(self.body, task='不同请求'))[0], 409)
            self.assertEqual(request('POST', route, self.body, {'Idempotency-Key': 'another-idem-key'})[0], 409)
            launch.assert_called_once()
            job_path = launch.call_args.args[0]
        shutdown()

        class Candidate:
            def __init__(self, label):
                self.label = label

            def run(self, prompt, workdir, ctx):
                if self.label.endswith('B'):
                    return Result(ok=False, stderr='B 未完成')
                (workdir / 'sample.py').write_text('x = 1\n', encoding='utf-8')
                return Result(ok=True)

        with patch('orchestrator.arena.build_adapter', side_effect=lambda cfg, **kw: Candidate(cfg.name)):
            arena_jobs.run_worker(job_path)
        request2, project2, _ = self._workbench()
        self.assertEqual(project2, project)
        self.assertEqual(request2('GET', route)[1]['jobs'][0]['arena_id'], queued['arena_id'])
        path = f"{route}/{queued['arena_id']}"
        status, done, _ = request2('GET', path)
        self.assertEqual((status, done['status']), (200, 'done'))
        self.assertEqual({c['label']: c['status'] for c in done['candidates']}, {'A': 'done', 'B': 'failed'})
        candidates = {c['label']: c for c in done['result']['candidates']}
        self.assertEqual(candidates['A']['changed_files'], ['sample.py'])
        self.assertIn('B 未完成', candidates['B']['error'])
        status, stream, _ = request2('GET', path + '/events?after=0', extra_headers={'Last-Event-ID': '2'})
        self.assertEqual(status, 200)
        ids = [int(line.removeprefix('id: ')) for line in stream.splitlines() if line.startswith('id: ')]
        self.assertEqual(ids, list(range(3, done['cursor'] + 1)))
        secret = 'arena-sse-private-token-123456'
        event = {'cursor': done['cursor'] + 1, 'status': 'running', 'label': secret, 'time': time.time()}
        with patch.dict(os.environ, {'GITHUB_TOKEN': secret}), \
                patch.object(arena_jobs, 'read_events', return_value=([event], True)):
            status, redacted, _ = request2('GET', path + '/events?after=0')
        self.assertEqual(status, 200)
        self.assertIn('[REDACTED]', redacted)
        self.assertNotIn(secret, redacted)
        self.assertEqual(request2('GET', route.replace(project, 'missing') + '/' + queued['arena_id'])[0], 404)

    def test_cancel_running_job_cleans_up_worktrees(self):
        with patch.object(arena_jobs, '_launch') as launch:
            job = arena_jobs.start_job(self.cfg, self.config_path, 'project', 'idempotency-1', self.body)
        path = launch.call_args.args[0]
        entered = threading.Event()
        workers = []

        class Candidate:
            def run(self, prompt, workdir, ctx):
                workers.append(workdir)
                entered.set()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not ctx.cancel_requested():
                    time.sleep(.02)
                return Result(ok=False)

        with patch('orchestrator.arena.build_adapter', return_value=Candidate()):
            thread = threading.Thread(target=arena_jobs.run_worker, args=(path,))
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                self.assertTrue(arena_jobs.cancel_job(self.cfg, 'project', job['arena_id'])['cancel_requested'])
            finally:
                thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertTrue(workers)
        self.assertTrue(all(not path.exists() for path in workers))
        self.assertEqual(arena_jobs.get_job(self.cfg, 'project', job['arena_id'])['status'], 'cancelled')
        self.assertFalse(arena_jobs.has_active_job(self.cfg, 'project'))

    def test_cancel_signal_stops_cli_and_running_test(self):
        import subprocess

        root = self.cfg.task_workdir
        ctx = RunContext('test', 'id', 'code', root, self.root, cancel_requested=lambda: True)
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            run_cli_process([sys.executable, '-c', 'import time; time.sleep(20)'], cwd=root, env=None,
                            input='', trace=CLITrace(ctx), timeout=5)
        self.assertEqual(caught.exception.stop_reason, 'cancelled')
        test = run_test_cmd([sys.executable, '-c', 'import time; time.sleep(20)'], root,
                            timeout_s=5, cancel_requested=lambda: True)
        self.assertEqual(test['error'], '测试已取消')
        self.assertFalse(test['ok'])

    def test_spawn_failure_is_durable_and_same_id_does_not_retry(self):
        with patch.object(arena_jobs, '_launch', side_effect=OSError('spawn fixture')) as launch:
            job = arena_jobs.start_job(self.cfg, self.config_path, 'p', 'idempotency-1', self.body)
            duplicate = arena_jobs.start_job(self.cfg, self.config_path, 'p', 'idempotency-1', self.body)
            with self.assertRaises(Conflict):
                arena_jobs.start_job(self.cfg, self.config_path, 'p', 'idempotency-1', dict(self.body, task='other'))
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job, duplicate)
        launch.assert_called_once()

    def test_independent_worker_checks_config_before_starting(self):
        launch_worker = arena_jobs._launch
        with patch.object(arena_jobs, '_launch') as launch:
            job = arena_jobs.start_job(self.cfg, self.config_path, 'p', 'idempotency-1', self.body)
        path = launch.call_args.args[0]
        with self.config_path.open('a', encoding='utf-8') as stream:
            stream.write('\n# changed while queued\n')
        processes = []
        real_popen = arena_jobs.subprocess.Popen
        def capture_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process
        with patch.object(arena_jobs.subprocess, 'Popen', side_effect=capture_popen):
            launch_worker(path, self.cfg)
        def wait_worker():
            for process in processes:
                deadline = time.monotonic() + 20
                while process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                if process.poll() is None:
                    from orchestrator.process_tree import terminate
                    terminate(process)
                    process.wait(timeout=10)
                    raise AssertionError('arena worker 在终态后 20s 仍未退出')
        self.addCleanup(wait_worker)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = arena_jobs.get_job(self.cfg, 'p', job['arena_id'])
            if result['status'] in arena_jobs.TERMINAL:
                break
            time.sleep(.05)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('项目配置已变化', result['error'])

    def test_worker_exit_marks_interrupted_and_keeps_candidate_evidence(self):
        with patch.object(arena_jobs, '_launch') as launch:
            job = arena_jobs.start_job(self.cfg, self.config_path, 'p', 'idempotency-1', self.body)
        path = launch.call_args.args[0]

        def simulate_exit(*args, on_progress, **kwargs):
            on_progress('A', 'running')
            on_progress('A', 'done', candidate={'label': 'A', 'changed_files': ['sample.py']})
            on_progress('B', 'running')
            raise SystemExit(12)

        with patch.object(arena_jobs, 'run_arena', side_effect=simulate_exit), self.assertRaises(SystemExit):
            arena_jobs.run_worker(path)
        reopened = arena_jobs.get_job(self.cfg, 'p', job['arena_id'])
        self.assertEqual(reopened['status'], 'interrupted')
        self.assertEqual(reopened['result']['candidates'][0]['changed_files'], ['sample.py'])
        self.assertFalse(arena_jobs.has_active_job(self.cfg, 'p'))
