"""远程 Worker 协议：心跳、HMAC 签名鉴权、任务派发与基线核验回写测试。"""
from __future__ import annotations

import base64
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from orchestrator.remote_worker import (
    RemoteJobReceipt,
    RemoteJobSpec,
    RemoteWorkerClient,
    RemoteWorkerError,
    RemoteWorkerServer,
)


class RemoteWorkerTests(unittest.TestCase):
    def test_close_after_thread_start_failure_does_not_wait_for_serve_forever(self):
        server = RemoteWorkerServer('worker-start-failure', 'secret', lambda spec: None)
        self.addCleanup(server.httpd.server_close)
        with mock.patch.object(server._thread, 'start', side_effect=RuntimeError('thread start failed')), \
                mock.patch.object(server.httpd, 'shutdown') as shutdown, \
                mock.patch.object(server.httpd, 'server_close') as server_close, \
                mock.patch.object(server._thread, 'join') as join:
            with self.assertRaisesRegex(RuntimeError, 'thread start failed'):
                server.start()
            server.close()
        shutdown.assert_not_called()
        server_close.assert_called_once_with()
        join.assert_not_called()

    def test_health_dispatch_signature_verification_and_declared_writeback(self):
        secret = 'worker-shared-secret-20261121'
        os.environ['TEST_REMOTE_WORKER_SECRET'] = secret
        self.addCleanup(lambda: os.environ.pop('TEST_REMOTE_WORKER_SECRET', None))

        def executor(spec: RemoteJobSpec) -> RemoteJobReceipt:
            raw = base64.b64decode(spec.files_b64['app.py'])
            updated = raw + b'\n# fixed remotely\n'
            return RemoteJobReceipt(
                job_id=spec.job_id,
                worker_id='worker-linux-1',
                status='done',
                exit_code=0,
                text='remote ok',
                usage={'tokens': 42},
                updates_b64={'app.py': base64.b64encode(updated).decode('ascii')},
            )

        server = RemoteWorkerServer('worker-linux-1', secret, executor)
        server.start()
        self.addCleanup(server.close)

        client = RemoteWorkerClient(f'http://127.0.0.1:{server.port}', 'TEST_REMOTE_WORKER_SECRET')
        health = client.health()
        self.assertTrue(health['ok'])
        self.assertEqual(health['worker_id'], 'worker-linux-1')

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / 'app.py').write_bytes(b'x = 1')
            spec = RemoteJobSpec(
                job_id='job-1', run_id='run-1', stage='coding', kind='code',
                prompt='fix x', editable_files=('app.py',),
            )
            receipt, changed = client.dispatch(spec, workdir=ws, context_files=('app.py',))
            self.assertEqual(receipt.status, 'done')
            self.assertEqual(changed, ['app.py'])
            self.assertEqual((ws / 'app.py').read_bytes(), b'x = 1\n# fixed remotely\n')

        # 错误密钥必须被服务端 403 拒绝
        os.environ['TEST_WRONG_SECRET'] = 'wrong-secret-00000000'
        self.addCleanup(lambda: os.environ.pop('TEST_WRONG_SECRET', None))
        bad_client = RemoteWorkerClient(f'http://127.0.0.1:{server.port}', 'TEST_WRONG_SECRET')
        with self.assertRaises(RemoteWorkerError):
            bad_client.dispatch(RemoteJobSpec('job-2', 'run-1', 'review', 'report', 'hi'))

    def test_undeclared_remote_file_update_is_rejected_without_touching_workspace(self):
        secret = 'worker-shared-secret-undeclared'
        os.environ['TEST_REMOTE_WORKER_SECRET2'] = secret
        self.addCleanup(lambda: os.environ.pop('TEST_REMOTE_WORKER_SECRET2', None))

        def malicious_executor(spec: RemoteJobSpec) -> RemoteJobReceipt:
            return RemoteJobReceipt(
                job_id=spec.job_id, worker_id='w2', status='done', exit_code=0,
                updates_b64={'secret_test.py': base64.b64encode(b'tampered').decode('ascii')},
            )

        server = RemoteWorkerServer('w2', secret, malicious_executor)
        server.start()
        self.addCleanup(server.close)
        client = RemoteWorkerClient(f'http://127.0.0.1:{server.port}', 'TEST_REMOTE_WORKER_SECRET2')

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / 'app.py').write_bytes(b'original')
            (ws / 'secret_test.py').write_bytes(b'protected')
            spec = RemoteJobSpec('job-3', 'run-1', 'coding', 'code', 'fix', editable_files=('app.py',))
            with self.assertRaises(ValueError):
                client.dispatch(spec, workdir=ws, context_files=('app.py',))
            self.assertEqual((ws / 'secret_test.py').read_bytes(), b'protected')

    def test_retry_replays_receipt_and_rejects_conflicting_payload_or_stale_timestamp(self):
        import json
        import urllib.error
        import urllib.request
        from orchestrator.remote_worker import compute_signature

        secret = 'worker-replay-secret-20261121'
        os.environ['TEST_REMOTE_REPLAY_SECRET'] = secret
        self.addCleanup(lambda: os.environ.pop('TEST_REMOTE_REPLAY_SECRET', None))

        executor = mock.Mock(side_effect=lambda spec: RemoteJobReceipt(
            spec.job_id, 'w-replay', 'done', 0, text='ok'))
        server = RemoteWorkerServer('w-replay', secret, executor)
        server.start()
        self.addCleanup(server.close)
        client = RemoteWorkerClient(f'http://127.0.0.1:{server.port}', 'TEST_REMOTE_REPLAY_SECRET')

        spec = RemoteJobSpec('job-once', 'run-1', 'review', 'report', 'hi')
        receipt, _ = client.dispatch(spec)
        self.assertEqual(receipt.status, 'done')

        replay, _ = client.dispatch(spec)
        self.assertEqual(replay, receipt)
        executor.assert_called_once()
        self.assertEqual(executor.call_args.args[0].job_id, 'job-once')

        with self.assertRaisesRegex(RemoteWorkerError, '409'):
            client.dispatch(RemoteJobSpec('job-once', 'run-1', 'review', 'report', 'changed'))

        # 过期时间戳必须被 403 拒绝
        body = json.dumps({'job_id': 'job-old', 'run_id': 'r', 'stage': 's', 'kind': 'report', 'prompt': 'p'}).encode()
        old_ts = '1000000000'
        req = urllib.request.Request(
            f'http://127.0.0.1:{server.port}/jobs', data=body, method='POST',
            headers={'Content-Type': 'application/json', 'X-Worker-Timestamp': old_ts,
                     'X-Worker-Signature-256': compute_signature(body, secret, timestamp=old_ts)},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 403)

        # 客户端也必须拒绝过期/重放的回执时间戳
        class _StaleResp:
            headers = {
                'X-Worker-Timestamp': '1000000000',
                'X-Worker-Signature-256': compute_signature(b'{"job_id":"job-stale"}', secret, timestamp='1000000000'),
            }

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, n=-1):
                return b'{"job_id":"job-stale"}'

        with mock.patch('orchestrator.remote_worker.open_same_origin', return_value=_StaleResp()):
            with self.assertRaisesRegex(RemoteWorkerError, '时间戳'):
                client.dispatch(RemoteJobSpec('job-stale', 'run-1', 'review', 'report', 'hi'))

    def test_concurrent_retry_waits_for_original_receipt_without_reexecution(self):
        secret = 'worker-concurrent-secret'
        os.environ['TEST_REMOTE_CONCURRENT_SECRET'] = secret
        self.addCleanup(lambda: os.environ.pop('TEST_REMOTE_CONCURRENT_SECRET', None))
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def execute(spec):
            started.set()
            if not release.wait(5):
                raise RuntimeError('test release timeout')
            return RemoteJobReceipt(spec.job_id, 'w-concurrent', 'done', 0, text='once')

        executor = mock.Mock(side_effect=execute)
        server = RemoteWorkerServer('w-concurrent', secret, executor)
        server.start()
        self.addCleanup(server.close)
        client = RemoteWorkerClient(f'http://127.0.0.1:{server.port}',
                                    'TEST_REMOTE_CONCURRENT_SECRET', timeout_s=5)
        spec = RemoteJobSpec('job-concurrent', 'run-1', 'review', 'report', 'hi', timeout_s=10)
        receipts, errors = [], []

        def dispatch():
            try:
                receipts.append(client.dispatch(spec)[0])
            except Exception as error:
                errors.append(error)

        first = threading.Thread(target=dispatch)
        second = threading.Thread(target=dispatch)
        first.start()
        self.assertTrue(started.wait(2))
        second.start()
        release.set()
        first.join(5)
        second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(receipts), 2)
        self.assertEqual(receipts[0], receipts[1])
        executor.assert_called_once()

    def test_executor_failure_is_cached_and_replayed_instead_of_rerun(self):
        secret = 'worker-failure-secret'
        os.environ['TEST_REMOTE_FAILURE_SECRET'] = secret
        self.addCleanup(lambda: os.environ.pop('TEST_REMOTE_FAILURE_SECRET', None))
        executor = mock.Mock(side_effect=[ValueError('invalid job'), RuntimeError('worker crashed')])
        server = RemoteWorkerServer('w-failure', secret, executor)
        server.start()
        self.addCleanup(server.close)
        client = RemoteWorkerClient(f'http://127.0.0.1:{server.port}',
                                    'TEST_REMOTE_FAILURE_SECRET')
        for job_id, error, status in (
            ('job-rejected', 'invalid job', 'rejected'),
            ('job-failed', 'worker crashed', 'failed'),
        ):
            spec = RemoteJobSpec(job_id, 'run-1', 'review', 'report', 'hi')
            receipt, _ = client.dispatch(spec)
            replay, _ = client.dispatch(spec)
            self.assertEqual(receipt.status, status)
            self.assertIn(error, receipt.stderr)
            self.assertEqual(replay, receipt)
        self.assertEqual(executor.call_count, 2)
