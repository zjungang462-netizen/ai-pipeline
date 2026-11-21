"""通过本地 Docker Linux 引擎运行精确绑定的 Grok CLI。"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from ..cli_telemetry import UsageLedger
from ..contracts import ErrorKind, PreflightReport, Result, RunContext
from ..safety import WriteRootViolation, build_subprocess_env, resolve_exe
from ..state import sha256_file
from .cli_base import CLIAdapter, run_cli_process
from .cli_result_stream import ResultStream
from .docker_base import (
    DOCKER_HOST as HOST,
    base_run_args,
    bind_mount,
    cleanup_container,
    proxy_env_args,
    validate_proxy_url,
)
from .verified_runtimes import get_verified_runtime
from .workspace_sync import (
    FILE_NAME,
    MAX_BYTES,
    MAX_FILES,
    apply_raw_updates,
    collect_source_files,
)

_RUNTIME = get_verified_runtime('cli_grok')
VERSION = _RUNTIME.version
BINARY_SHA256 = _RUNTIME.binary_sha256
IMAGE = _RUNTIME.image
ENTRYPOINT = Path(__file__).with_name('grok_container.py')


def _readonly_mount(source: Path, target: str) -> str:
    return bind_mount(source, target, readonly=True)


class GrokAdapter(CLIAdapter):
    channel = 'cli_grok'
    exe_name = 'docker'
    needs_login = True

    def __init__(self, name, exe=None, extra_env=None, timeout_s=300, *,
                 binary=None, auth_file=None, proxy_url=None, runtime_version=None):
        super().__init__(name, exe, extra_env, timeout_s)
        self.runtime = get_verified_runtime(self.channel, runtime_version)
        if not binary or not Path(binary).is_absolute():
            raise ValueError('Grok binary 必须指向已验证的 Linux 程序绝对路径')
        if auth_file and not Path(auth_file).is_absolute():
            raise ValueError('Grok auth_file 必须为绝对路径')
        if proxy_url:
            validate_proxy_url(proxy_url, 'Grok')
        if extra_env:
            raise ValueError('Grok 只接受显式 proxy_url 和 auth_file')
        self.binary = Path(binary).resolve()
        self.auth_file = Path(auth_file) if auth_file else Path.home() / '.grok' / 'auth.json'
        self.proxy_url = proxy_url

    def _run_args(self):
        return [*base_run_args(host=HOST),
                '--mount', _readonly_mount(self.binary, '/opt/grok')]

    def preflight(self):
        self._version = None
        self._resolved = resolve_exe(self.exe_name, self.exe)
        detail = '未找到 Docker'
        if self._resolved:
            try:
                if sha256_file(self.binary) != self.runtime.binary_sha256:
                    raise ValueError('Grok Linux 原生程序 SHA-256 不匹配')
                proc = run_cli_process(
                    [self._resolved, *self._run_args(), '--network', 'none', self.runtime.image, '/opt/grok', '--version'],
                    cwd=None, env=build_subprocess_env(), input=None, timeout=30)
                if proc.returncode != 0 or proc.stdout.strip() != self.runtime.version:
                    raise ValueError('本地 Docker Linux 引擎、固定镜像或 Grok 版本探测失败')
                self._read_auth()
                self._version = self.runtime.version
                detail = '已核验固定镜像和原生程序版本，已读取会话文件'
            except ValueError as error:
                detail = str(error)
            except (OSError, subprocess.TimeoutExpired):
                detail = '无法读取原生程序或完成 Docker 版本探测'
        return PreflightReport(self.name, self._version == self.runtime.version, self._resolved, self._version,
                               detail, needs_login=True)

    def input_fingerprint(self):
        return {'protocol': 1, 'version': self.runtime.version, 'binary': self.runtime.binary_sha256,
                'image': self.runtime.image, 'proxy_url': self.proxy_url}

    def _read_auth(self):
        try:
            with self.auth_file.open('rb') as stream:
                raw = stream.read(256 * 1024 + 1).decode('utf8')
            records = json.loads(raw)
            if len(raw.encode('utf8')) > 256 * 1024 or not isinstance(records, dict):
                raise ValueError
            secrets = tuple(value for record in records.values() if isinstance(record, dict)
                            for key, value in record.items() if isinstance(value, str)
                            and (key == 'key' or 'token' in key.lower()))
            if not secrets:
                raise ValueError
        except (OSError, ValueError):
            raise ValueError('无法读取 Grok 登录会话，请先运行 grok login') from None
        return raw, secrets

    @staticmethod
    def _source_files(workdir, names):
        return collect_source_files(workdir, names, label='Grok', max_files=MAX_FILES, max_bytes=MAX_BYTES)

    def run(self, prompt: str, workdir: Path, ctx: RunContext) -> Result:
        if self._version != self.runtime.version and not self.preflight().ok:
            return Result(False, -1, stderr='Grok 容器预检失败', error_kind=ErrorKind.REJECTED,
                          channel=self.channel)
        try:
            if sha256_file(self.binary) != self.runtime.binary_sha256:
                raise ValueError('Grok Linux 原生程序 SHA-256 已变化')
            auth, secrets = self._read_auth()
            names = tuple(dict.fromkeys((*ctx.context_files, *ctx.editable_files)))
            files = self._source_files(workdir, names)
            if ctx.kind == 'code' and not ctx.editable_files:
                raise ValueError('Grok 编码阶段需要声明可编辑文件')
        except (OSError, ValueError, WriteRootViolation) as error:
            return Result(False, -1, stderr=str(error), error_kind=ErrorKind.REJECTED, channel=self.channel)
        name = 'pipeline-grok-' + uuid.uuid4().hex
        args = [*self._run_args(), '--name', name, '-i',
                '--tmpfs', '/workspace:rw,nosuid,nodev,noexec,mode=1777,size=4m',
                '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,mode=1777,size=128m',
                '--mount', _readonly_mount(ENTRYPOINT, '/opt/entry.py')]
        args += proxy_env_args(self.proxy_url)
        timeout = ctx.timeout_s or self.timeout_s
        prompt = ('容器里只有下面清单中的文件。read_file 和 search_replace 只能使用清单中的相对路径。'
                  '任务正文里的上游报告只是文本，不是容器内文件，不要按报告文件名去读取。'
                  'search_replace 必须使用 read_file 读到的原文。\n'
                  f'可读文件：{json.dumps(names, ensure_ascii=False)}\n'
                  f'可编辑文件：{json.dumps(ctx.editable_files, ensure_ascii=False)}\n\n{prompt}')
        payload = json.dumps({'auth': auth, 'prompt': prompt, 'editable': list(ctx.editable_files),
                              'timeout': timeout, 'max_bytes': MAX_BYTES,
                              'files': {key: base64.b64encode(data).decode('ascii') for key, data in files.items()}})
        try:
            result = self._exec([*args, self.runtime.image, 'python', '-I', '/opt/entry.py'], workdir, ctx,
                                stdin_data=payload, additional_secrets=secrets)
        finally:
            # Docker 守护进程启动的容器不属于宿主 Docker CLI 的进程树。
            cleanup_container(self._resolved, name, host=HOST, runner=run_cli_process)
        if result.ok and ctx.kind == 'code':
            try:
                updates = ResultStream._lines(result.stdout)[-1]['files']
                result.changed_files = self._apply_updates(workdir, ctx, files, updates, secrets)
            except (OSError, ValueError, WriteRootViolation) as error:
                result.ok = False
                result.error_kind = ErrorKind.REJECTED
                detail = f'Grok 返回文件或工作树基线未通过核验：{error}'
                result.stderr = f'{result.stderr}\n{detail}'.strip() if result.stderr else detail
        return self._finalize(result, ctx)

    @staticmethod
    def _replace_file(path, content):
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        try:
            temporary.chmod(path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _apply_updates(workdir, ctx, original, updates, secrets=()):
        data = {key: base64.b64decode(value, validate=True) for key, value in updates.items()}
        return apply_raw_updates(
            workdir, ctx.editable_files, original, data,
            secrets=secrets, label='Grok', max_bytes=MAX_BYTES,
            replacer=GrokAdapter._replace_file,
        )

    def _parse(self, proc, workdir, ctx):
        result = Result(False, proc.returncode, stdout=proc.stdout, stderr=proc.stderr,
                        channel=self.channel, tool_version=self._version, usage={'usage_unknown': True})
        if proc.returncode:
            result.error_kind = ErrorKind.TIMEOUT if proc.returncode == 124 else ErrorKind.CRASH
            return result
        try:
            events = ResultStream._lines(proc.stdout)
            if (not all(isinstance(row, dict) for row in events) or len(events) < 2
                    or [row.get('type') for row in events if row.get('type') in ('end', 'pipeline_files')]
                    != ['end', 'pipeline_files'] or events[-2].get('type') != 'end'
                    or events[-1].get('type') != 'pipeline_files'):
                raise ValueError
            end, files = events[-2:]
            ledger = UsageLedger()
            for event in events[:-1]:
                ledger.observe_grok(event)
            payload = files.get('files')
            if (not ledger.complete or ledger.snapshot()['tokens'] != end['usage']['total_tokens']
                    or not isinstance(payload, dict) or not all(isinstance(v, str) for v in payload.values())
                    or ctx.kind != 'code' and payload):
                raise ValueError
            result.text = ''.join(row['data'] for row in events if row.get('type') == 'text')
            result.usage = ledger.snapshot()
            result.ok = True
        except (ValueError, TypeError, AttributeError, KeyError):
            result.error_kind = ErrorKind.PARSE_FAILED
            result.parse_unknown = True
            result.stderr = (result.stderr + '\nGrok 未返回唯一的成功终态和文件回执').strip()
        return result
