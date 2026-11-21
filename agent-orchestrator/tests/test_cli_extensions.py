"""CLI 主入口扩展子命令与 Webhook 自动通知接线回归测试。"""
from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

from orchestrator.cli import main
from orchestrator.contracts import StageStatus
from orchestrator.state import RunState, sha256_file


def _capture_cli(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(argv)
    return code, buf.getvalue()


class CLIExtensionsTests(unittest.TestCase):
    def test_state_migrate_cli_dry_run_is_readonly_and_rejects_unverified_v1(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / 'state.json'
            state_file.write_text(json.dumps({'schema_version': RunState.SCHEMA_VERSION, 'run_id': 'r1'}), encoding='utf-8')
            before = sha256_file(state_file)
            code, out = _capture_cli(['state-migrate', '--state-file', str(state_file)])
            self.assertEqual(code, 0)
            receipt = json.loads(out)
            self.assertTrue(receipt['dry_run'])
            self.assertEqual(receipt['steps'], [])
            self.assertEqual(sha256_file(state_file), before)

            v1_file = Path(tmp) / 'v1.json'
            v1_file.write_text(json.dumps({'schema_version': 1, 'run_id': 'old'}), encoding='utf-8')
            code2, out2 = _capture_cli(['state-migrate', '--state-file', str(v1_file)])
            self.assertEqual(code2, 2)
            self.assertIn('缺少', out2)

    def test_deploy_cli_enforces_dry_run_approval_and_writes_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / 'ws'
            ws.mkdir()
            cfg = root / 'p.toml'
            cfg.write_text(
                'task_workdir = "ws"\nstate_dir = "state"\n'
                '[[stages]]\nname = "testing"\nkind = "test"\ntest_cmd = ["python", "-c", "pass"]\n',
                encoding='utf-8',
            )
            state = RunState(root / 'state', 'run-cli-dep')
            st = state.stage('testing')
            st.status = StageStatus.DONE
            st.outputs_hash = 'out-hash-cli-1'
            state.save()

            # 1. 默认干跑通过
            code, out = _capture_cli([
                'deploy', str(cfg), '--run-id', 'run-cli-dep', '--require-stage', 'testing',
                '--', sys.executable, '-c', 'print("hello-deploy")',
            ])
            self.assertEqual(code, 0, out)
            dry = json.loads(out)
            self.assertTrue(dry['dry_run'])
            self.assertTrue(dry['ok'])
            approved_hash = dry['gate']['outputs_hash']

            # 2. --apply 缺审批哈希 => 拒绝（退出码 2）
            code_bad, out_bad = _capture_cli([
                'deploy', str(cfg), '--run-id', 'run-cli-dep', '--require-stage', 'testing', '--apply',
                '--', sys.executable, '-c', 'print("should-not-run")',
            ])
            self.assertEqual(code_bad, 2)
            self.assertIn('部署拒绝', out_bad)

            # 3. --apply 带正确审批哈希 => 执行成功并落盘回执
            code_ok, out_ok = _capture_cli([
                'deploy', str(cfg), '--run-id', 'run-cli-dep', '--require-stage', 'testing',
                '--apply', '--approve-hash', approved_hash,
                '--', sys.executable, '-c', 'print("live-deploy-ok")',
            ])
            self.assertEqual(code_ok, 0, out_ok)
            live = json.loads(out_ok)
            self.assertTrue(live['ok'])
            self.assertIn('live-deploy-ok', live['stdout'])

    def test_cli_run_sends_webhook_notification_when_env_configured(self):
        received: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_POST(self):
                length = int(self.headers.get('Content-Length', '0'))
                body = self.rfile.read(length) if length else b''
                received.append(json.loads(body.decode('utf-8')))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'ok')

        srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close(), th.join(3)))

        os.environ['PIPELINE_WEBHOOK_URL'] = f'http://127.0.0.1:{srv.server_port}/hook'
        os.environ['PIPELINE_WEBHOOK_FORMAT'] = 'generic'
        self.addCleanup(lambda: os.environ.pop('PIPELINE_WEBHOOK_URL', None))
        self.addCleanup(lambda: os.environ.pop('PIPELINE_WEBHOOK_FORMAT', None))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'ws').mkdir()
            cfg = root / 'p.toml'
            cfg.write_text(
                'task_workdir = "ws"\nstate_dir = "state"\n'
                '[adapters.fake]\ntype = "fake"\n'
                '[[stages]]\nname = "report"\nkind = "report"\nadapter = "fake"\nprompt = "say hi"\n',
                encoding='utf-8',
            )
            code, _ = _capture_cli(['run', str(cfg), '--run-id', 'run-hook-1', '--task', 'demo'])
            self.assertEqual(code, 0)
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]['run_id'], 'run-hook-1')
            self.assertEqual(received[0]['event'], 'done')
