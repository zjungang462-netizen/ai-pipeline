"""远程 Worker 调度协议：HMAC 鉴权、心跳探测、任务派发与基线核验回写。"""
from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import asdict, dataclass
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .adapters.workspace_sync import apply_raw_updates, collect_source_files

_LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1'}
_TERMINAL_STATUSES = {'done', 'failed', 'timeout', 'rejected'}


class RemoteWorkerError(RuntimeError):
    """远程 Worker 鉴权失败、协议违规或通信异常。"""


@dataclass(frozen=True)
class RemoteJobSpec:
    job_id: str
    run_id: str
    stage: str
    kind: str
    prompt: str
    editable_files: tuple[str, ...] = ()
    files_b64: dict[str, str] | None = None
    timeout_s: int = 120
    inputs_hash: str = ''


@dataclass(frozen=True)
class RemoteJobReceipt:
    job_id: str
    worker_id: str
    status: str
    exit_code: int
    text: str = ''
    stderr: str = ''
    usage: dict | None = None
    updates_b64: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.status not in _TERMINAL_STATUSES:
            raise ValueError(f'非法远程回执状态: {self.status}')


def validate_worker_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.username or parsed.password or not parsed.hostname:
        raise ValueError('Worker URL 必须包含有效主机且不含凭据')
    if parsed.scheme == 'https':
        return url.strip().rstrip('/')
    if parsed.scheme == 'http' and parsed.hostname.lower() in _LOOPBACK_HOSTS:
        return url.strip().rstrip('/')
    raise ValueError('Worker URL 仅允许 https://（或本机 loopback http://）')


def compute_signature(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()
    return f'sha256={digest}'


def verify_signature(body: bytes, secret: str, header_value: str) -> bool:
    if not secret or not isinstance(header_value, str):
        return False
    expected = compute_signature(body, secret)
    return hmac.compare_digest(expected, header_value.strip())


class RemoteWorkerClient:
    """向远程受控 Worker 发送心跳与签名任务，并校验回执与文件回写。"""

    def __init__(self, endpoint: str, secret_env: str, *, timeout_s: int = 120):
        self.endpoint = validate_worker_url(endpoint)
        if not isinstance(secret_env, str) or not secret_env.strip():
            raise ValueError('secret_env 必须为环境变量名')
        self.secret_env = secret_env
        self.timeout_s = timeout_s

    def _secret(self) -> str:
        value = os.environ.get(self.secret_env, '').strip()
        if not value:
            raise RemoteWorkerError(f'环境变量 {self.secret_env} 未设置')
        return value

    def health(self, timeout_s: float = 5.0) -> dict:
        req = urllib.request.Request(f'{self.endpoint}/health', method='GET')
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            raise RemoteWorkerError(f'Worker 心跳探测失败: {error}') from None

    def dispatch(self, spec: RemoteJobSpec, *, workdir: Path | None = None,
                 context_files: tuple[str, ...] = ()) -> tuple[RemoteJobReceipt, list[str]]:
        secret = self._secret()
        original_files: dict[str, bytes] = {}
        files_b64 = dict(spec.files_b64 or {})
        if workdir is not None and (context_files or spec.editable_files):
            names = tuple(dict.fromkeys((*context_files, *spec.editable_files)))
            original_files = collect_source_files(workdir, names, label='远程 Worker')
            files_b64 = {k: base64.b64encode(v).decode('ascii') for k, v in original_files.items()}
        payload_dict = asdict(spec)
        payload_dict['files_b64'] = files_b64
        body = json.dumps(payload_dict, ensure_ascii=False).encode('utf-8')
        headers = {
            'Content-Type': 'application/json; charset=utf-8',
            'X-Worker-Signature-256': compute_signature(body, secret),
        }
        req = urllib.request.Request(f'{self.endpoint}/jobs', data=body, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=min(self.timeout_s, spec.timeout_s)) as resp:
                resp_body = resp.read()
                sig = resp.headers.get('X-Worker-Signature-256', '')
        except urllib.error.HTTPError as error:
            raise RemoteWorkerError(f'远程 Worker 返回 HTTP {error.code}') from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise RemoteWorkerError(f'远程 Worker 通信失败: {error}') from None

        if not verify_signature(resp_body, secret, sig):
            raise RemoteWorkerError('远程 Worker 回执签名校验失败')
        try:
            data = json.loads(resp_body.decode('utf-8'))
            receipt = RemoteJobReceipt(
                job_id=data['job_id'],
                worker_id=data['worker_id'],
                status=data['status'],
                exit_code=int(data['exit_code']),
                text=data.get('text', ''),
                stderr=data.get('stderr', ''),
                usage=data.get('usage') or {'usage_unknown': True},
                updates_b64=data.get('updates_b64') or {},
            )
        except (ValueError, KeyError, TypeError) as error:
            raise RemoteWorkerError(f'远程 Worker 回执格式无效: {error}') from None
        if receipt.job_id != spec.job_id:
            raise RemoteWorkerError('远程 Worker 回执 job_id 不匹配')

        changed: list[str] = []
        if receipt.status == 'done' and receipt.updates_b64 and workdir is not None:
            decoded = {k: base64.b64decode(v, validate=True) for k, v in receipt.updates_b64.items()}
            changed = apply_raw_updates(
                workdir, spec.editable_files, original_files, decoded, label='远程 Worker'
            )
        return receipt, changed


class RemoteWorkerServer:
    """轻量受控远程 Worker HTTP 服务端（仅接受 HMAC 签名任务）。"""

    def __init__(
        self,
        worker_id: str,
        secret: str,
        executor: Callable[[RemoteJobSpec], RemoteJobReceipt],
        *,
        host: str = '127.0.0.1',
        port: int = 0,
    ):
        self.worker_id = worker_id
        self._secret = secret
        self._executor = executor
        self.started_at = time.time()
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _send_json(self, code: int, payload: dict, *, sign: bool = False):
                raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                if sign:
                    self.send_header('X-Worker-Signature-256', compute_signature(raw, parent._secret))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == '/health':
                    self._send_json(200, {'ok': True, 'worker_id': parent.worker_id, 'uptime_s': round(time.time() - parent.started_at, 3)})
                    return
                self._send_json(404, {'error': 'not_found'})

            def do_POST(self):
                if self.path != '/jobs':
                    self._send_json(404, {'error': 'not_found'})
                    return
                length = int(self.headers.get('Content-Length', '0'))
                raw = self.rfile.read(length) if length else b''
                sig = self.headers.get('X-Worker-Signature-256', '')
                if not verify_signature(raw, parent._secret, sig):
                    self._send_json(403, {'error': 'invalid_signature'})
                    return
                try:
                    data = json.loads(raw.decode('utf-8'))
                    spec = RemoteJobSpec(
                        job_id=data['job_id'],
                        run_id=data['run_id'],
                        stage=data['stage'],
                        kind=data['kind'],
                        prompt=data['prompt'],
                        editable_files=tuple(data.get('editable_files') or ()),
                        files_b64=data.get('files_b64') or {},
                        timeout_s=int(data.get('timeout_s', 120)),
                        inputs_hash=data.get('inputs_hash', ''),
                    )
                    receipt = parent._executor(spec)
                except (ValueError, KeyError, TypeError) as error:
                    self._send_json(400, {'error': str(error)})
                    return
                self._send_json(200, asdict(receipt), sign=True)

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.port = self.httpd.server_port
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(3)
