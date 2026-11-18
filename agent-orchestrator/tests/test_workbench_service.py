"""守护进程必须隔离网页恢复与任务恢复。"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.collaboration.storage import Store
from orchestrator.dashboard.server import create_server
from orchestrator.workbench_service import health, recovery_blockers


class WorkbenchService(unittest.TestCase):
    def test_readonly_guard_blocks_pending_commands_and_managed_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / 'db.sqlite3')
            project = store.register_project(root / 'config.toml')
            # register_project 返回项目字典。
            project_id = project['id'] if isinstance(project, dict) else project
            key = store.register_run(project_id, 'r', root / 'config.toml', root / 'state', managed=True)
            self.assertIn(key, recovery_blockers(store.path))
            run = root / 'state/runs/r'
            run.mkdir(parents=True)
            (run / 'job.json').write_text(json.dumps({'status': 'failed'}), encoding='utf8')
            self.assertEqual(recovery_blockers(store.path), [])
            with store.connect() as db:
                db.execute("INSERT INTO commands(id,run_id,kind,payload,idempotency,created) VALUES(?,?,?,?,?,?)",
                           ('c', key, 'resume', '{}', 'test-command', 1))
            self.assertIn('commands', recovery_blockers(store.path))

    def test_no_recovery_server_never_launches_existing_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'db.sqlite3'
            config = root / 'config.toml'
            config.write_text('task_workdir="ws"\nstate_dir="state"\n[[stages]]\nname="verify"\nkind="test"\ntest_cmd=["python","-c","assert True"]\n', encoding='utf8')
            store = Store(path)
            project = store.register_project(config)
            project_id = project['id'] if isinstance(project, dict) else project
            store.register_run(project_id, 'r', config, root / 'state', managed=True)
            with mock.patch('orchestrator.collaboration.service.Service.launch', side_effect=AssertionError('no launch')):
                server = create_server(path, [config], 0, recover=False)
                try:
                    self.assertFalse(server.application.service.recover)
                    server.application.service.sync(recover=server.application.service.recover)
                finally:
                    server.server_close()

    def test_real_service_restarts_only_web_child_and_honors_stop_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            service = root / 'service'
            log = (root / 'supervisor.log').open('w', encoding='utf8')
            proc = subprocess.Popen([sys.executable, '-m', 'orchestrator.workbench_service', 'run',
                '--database', str(root / 'db.sqlite3'), '--directory', str(service),
                '--port', str(port), '--interval', '.1', '--max-restarts', '1'], stdout=log, stderr=log)
            def wait_for(predicate):
                end = time.monotonic() + 20
                while time.monotonic() < end:
                    try:
                        data = json.loads((service / 'service.json').read_text(encoding='utf8'))
                        if predicate(data):
                            return data
                    except (OSError, ValueError):
                        pass
                    if proc.poll() is not None:
                        self.fail((service / 'service.json').read_text(encoding='utf8'))
                    time.sleep(.05)
                self.fail('service did not reach expected state')
            try:
                first = wait_for(lambda data: data['status'] == 'healthy')
                self.assertTrue(health(port, first['pid']))
                self.assertFalse(health(port, os.getpid()))
                if os.name == 'nt':
                    import ctypes
                    handle = ctypes.windll.kernel32.OpenProcess(1, False, first['pid'])
                    self.assertTrue(handle)
                    try:
                        self.assertTrue(ctypes.windll.kernel32.TerminateProcess(handle, 71))
                    finally:
                        ctypes.windll.kernel32.CloseHandle(handle)
                else:
                    import signal
                    os.kill(first['pid'], signal.SIGKILL)
                second = wait_for(lambda data: data['status'] == 'healthy' and data['restarts_used'] == 1)
                self.assertNotEqual(first['pid'], second['pid'])
                self.assertTrue((service / '工作台入口.md').is_file())
                (service / 'stop').touch()
                self.assertEqual(proc.wait(timeout=15), 0)
                self.assertFalse(health(port, second['pid']))
                self.assertIn('child_exited', (service / 'events.jsonl').read_text(encoding='utf8'))
            finally:
                if proc.poll() is None:
                    (service / 'stop').touch()
                    try:
                        proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        proc.terminate()
                        proc.wait(timeout=10)
                log.close()


if __name__ == '__main__':
    unittest.main()
