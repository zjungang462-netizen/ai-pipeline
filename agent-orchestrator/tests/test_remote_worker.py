"""远程 Worker 协议：心跳、HMAC 签名鉴权、任务派发与基线核验回写测试。"""
from __future__ import annotations

import base64
import os
from pathlib import Path
import tempfile
import unittest

from orchestrator.remote_worker import (
    RemoteJobReceipt,
    RemoteJobSpec,
    RemoteWorkerClient,
    RemoteWorkerError,
    RemoteWorkerServer,
)


class RemoteWorkerTests(unittest.TestCase):
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
