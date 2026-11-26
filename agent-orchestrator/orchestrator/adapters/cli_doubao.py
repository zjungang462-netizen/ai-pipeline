"""豆包 CLI 2.37.2 的已验证 Linux 容器协议。

宿主上的原生 CLI 可以改写工作区外的文件。这里只挂载任务工作区，
外部样本在只读挂载上的 Edit 会得到 Permission denied，宿主机字节不变。
成功终态没有 token 用量，不能把退出码 0 当成已知用量。
"""
from __future__ import annotations

import contextlib
import json
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

from ..contracts import ErrorKind, PreflightReport, Result, RunContext
from ..safety import WriteRootViolation, build_subprocess_env, resolve_exe
from ..state import sha256_file
from .cli_base import CLIAdapter, run_cli_process
from .cli_codex import strip_ansi
from .cli_result_stream import ResultStream
from .docker_base import (
    DOCKER_HOST as HOST,
    base_run_args,
    bind_mount as _mount,
    cleanup_container,
    proxy_env_args,
    validate_image_ref,
    validate_proxy_url,
)
from .verified_runtimes import get_verified_runtime
from .workspace_sync import (
    apply_raw_updates,
    collect_source_files,
    collect_staging_updates,
    make_staging_removable,
    materialize_staging,
)

_RUNTIME = get_verified_runtime('cli_doubao')
VERSION = _RUNTIME.version
BINARY_SHA256 = _RUNTIME.binary_sha256
IMAGE_ID = _RUNTIME.image
PROFILE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')


class DoubaoAdapter(CLIAdapter):
    channel = 'cli_doubao'
    exe_name = 'docker'
    needs_login = True

    def __init__(self, name, exe=None, extra_env=None, timeout_s=300, *,
                 binary=None, image=None, machine_id=None, profile=None, proxy_url=None, runtime_version=None):
        super().__init__(name, exe, extra_env, timeout_s)
        self.runtime = get_verified_runtime(self.channel, runtime_version)
        if binary or image or machine_id or profile or proxy_url:
            if not binary or not Path(binary).is_absolute():
                raise ValueError('豆包 binary 必须指向已验证 Linux 程序的绝对路径')
            validate_image_ref(image, '豆包')
            if not machine_id or not Path(machine_id).is_absolute():
                raise ValueError('豆包 machine_id 必须是绝对路径')
            if not isinstance(profile, str) or not PROFILE_NAME.fullmatch(profile):
                raise ValueError('豆包 profile 必须是 Docker 卷名')
        if proxy_url:
            validate_proxy_url(proxy_url, '豆包')
        if extra_env:
            raise ValueError('豆包不接受额外环境变量')
        self.binary = Path(binary).resolve() if binary else None
        self.image = image
        self.machine_id = Path(machine_id).resolve() if machine_id else None
        self.profile = profile
        self.proxy_url = proxy_url

    def _package_mount(self):
        return _mount(self.binary.parent, '/opt/doubao', readonly=True)

    def _container_binary(self):
        return f'/opt/doubao/{self.binary.name}'

    def _common_run(self):
        return [*base_run_args(host=HOST), '--tmpfs', '/tmp:rw,nosuid,nodev,mode=1777,size=128m',
                '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'PYTHONPYCACHEPREFIX=/tmp/pyc']

    def _proxy_env(self):
        return proxy_env_args(self.proxy_url, include_no_proxy=True)

    def _profile_args(self):
        return ['--env', 'HOME=/profile', '--env', 'XDG_RUNTIME_DIR=/tmp',
                '--mount', f'type=volume,source={self.profile},target=/profile',
                '--mount', _mount(self.machine_id, '/etc/machine-id', readonly=True),
                '--mount', self._package_mount()]

    def preflight(self):
        self._version = None
        self._resolved = None
        if self.binary is None:
            return PreflightReport(self.name, False, None, None,
                                   '缺少已验证的豆包 Linux 程序、探针镜像、设备身份和会话卷',
                                   needs_login=True)
        self._resolved = resolve_exe(self.exe_name, self.exe)
        detail = '未找到 Docker'
        if self._resolved:
            try:
                if sha256_file(self.binary) != self.runtime.binary_sha256:
                    raise ValueError('豆包 Linux 程序 SHA-256 不匹配')
                image = run_cli_process(
                    [self._resolved, '--host', HOST, 'image', 'inspect', self.image, '--format', '{{.Id}}'],
                    cwd=None, env=build_subprocess_env(), input=None, timeout=30)
                if image.returncode != 0 or image.stdout.strip() != self.runtime.image:
                    raise ValueError('豆包探针镜像与已验证映像不一致')
                version = run_cli_process(
                    [self._resolved, *self._common_run(), '--network', 'none',
                     '--env', 'HOME=/tmp', '--env', 'XDG_RUNTIME_DIR=/tmp',
                     '--mount', self._package_mount(), self.runtime.image, self._container_binary(), '--version'],
                    cwd=None, env=build_subprocess_env(), input=None, timeout=60)
                if version.returncode != 0 or version.stdout.strip() != self.runtime.version:
                    raise ValueError('豆包版本探测失败')
                auth = run_cli_process(
                    [self._resolved, *self._common_run(), *self._proxy_env(), *self._profile_args(),
                     self.runtime.image, self._container_binary(), 'auth', 'status'],
                    cwd=None, env=build_subprocess_env(), input=None, timeout=60)
                if auth.returncode != 0 or not auth.stdout.lstrip().startswith('登录成功'):
                    raise ValueError('豆包容器会话未登录')
                self._version = self.runtime.version
                detail = '已核验探针镜像、程序版本和容器会话'
            except ValueError as error:
                detail = str(error)
            except (OSError, subprocess.TimeoutExpired):
                detail = '无法完成豆包容器版本或会话探测'
        return PreflightReport(self.name, self._version == self.runtime.version, self._resolved, self._version,
                               detail, needs_login=True)

    def input_fingerprint(self):
        identity = None
        if self.machine_id and self.machine_id.is_file():
            identity = sha256_file(self.machine_id)
        return {'protocol': 1, 'version': self.runtime.version, 'binary': self.runtime.binary_sha256,
                'image': self.runtime.image, 'profile': self.profile,
                'machine_id_sha256': identity, 'proxy_url': self.proxy_url}

    def run(self, prompt: str, workdir: Path, ctx: RunContext) -> Result:
        if self.binary is None:
            return Result(False, -1, stderr='缺少独立豆包 CLI 的真实协议样本，没有发起模型请求',
                          error_kind=ErrorKind.PARSE_FAILED, parse_unknown=True, channel=self.channel)
        if self._version != self.runtime.version and not self.preflight().ok:
            return Result(False, -1, stderr='豆包容器预检失败，没有发起模型请求',
                          error_kind=ErrorKind.REJECTED, channel=self.channel)
        try:
            if sha256_file(self.binary) != self.runtime.binary_sha256:
                return Result(False, -1, stderr='豆包 Linux 程序 SHA-256 已变化',
                              error_kind=ErrorKind.REJECTED, channel=self.channel)
        except OSError as error:
            return Result(False, -1, stderr=f'无法读取豆包 Linux 程序：{error}',
                          error_kind=ErrorKind.REJECTED, channel=self.channel)
        name = 'pipeline-doubao-' + uuid.uuid4().hex
        writable = ctx.kind == 'code'
        isolated = writable and bool(ctx.editable_files)
        files = {}
        names = ()
        if isolated:
            try:
                names = tuple(dict.fromkeys((*ctx.context_files, *ctx.editable_files)))
                files = collect_source_files(workdir, names, label='豆包')
            except (OSError, ValueError, WriteRootViolation) as error:
                return Result(False, -1, stderr=str(error), error_kind=ErrorKind.REJECTED, channel=self.channel)
        with contextlib.ExitStack() as stack:
            if isolated:
                stage_parent = None
                if ctx.attempt_dir:
                    stage_parent = Path(ctx.attempt_dir)
                    stage_parent.mkdir(parents=True, exist_ok=True)
                staging_dir = Path(stack.enter_context(
                    tempfile.TemporaryDirectory(prefix='pipeline-doubao-ws-', dir=stage_parent)))
                stack.callback(make_staging_removable, staging_dir)
                materialize_staging(staging_dir, files, ctx.editable_files)
                mount_source = staging_dir.resolve()
                stdin_prompt = (
                    '容器工作区里只有下面清单中的声明文件。只能修改可编辑文件列表中的相对路径。\n'
                    f'可读文件：{json.dumps(names, ensure_ascii=False)}\n'
                    f'可编辑文件：{json.dumps(ctx.editable_files, ensure_ascii=False)}\n\n{prompt}'
                )
            else:
                mount_source = Path(workdir).resolve()
                stdin_prompt = prompt
            args = [*self._common_run(), *self._proxy_env(), *self._profile_args(),
                    '--name', name, '-i',
                    '--mount', _mount(mount_source, '/workspace', readonly=not writable),
                    self.runtime.image, self._container_binary(), '-C', '/workspace',
                    '--permission-mode', 'ask_risky', '--output-format', 'stream-json', '-p', '-']
            try:
                result = self._exec(args, workdir, ctx, stdin_data=stdin_prompt)
            finally:
                cleanup_container(self._resolved, name, host=HOST, runner=run_cli_process)
            if isolated and result.ok:
                try:
                    updates = collect_staging_updates(staging_dir, ctx.editable_files, files)
                    result.changed_files = apply_raw_updates(
                        workdir, ctx.editable_files, files, updates, label='豆包')
                except (OSError, ValueError, WriteRootViolation) as error:
                    result.ok = False
                    result.error_kind = ErrorKind.REJECTED
                    detail = f'豆包返回文件或工作树基线未通过核验：{error}'
                    result.stderr = f'{result.stderr}\n{detail}'.strip() if result.stderr else detail
        return self._finalize(result, ctx)

    def _parse(self, proc, workdir: Path, ctx: RunContext) -> Result:
        stdout = strip_ansi(proc.stdout or '')
        stderr = strip_ansi(proc.stderr or '')
        usage = {'usage_unknown': True, 'usage_scope': 'doubao_terminal_has_no_token_usage'}
        combined = f'{stdout}\n{stderr}'
        if proc.returncode != 0:
            err_lower = stderr.lower()
            is_auth = (
                proc.returncode in (3, 4)
                or 'login_required' in combined
                or '请先登录' in stderr
                or '未登录' in stderr
                or 'login' in err_lower
                or 'authentication' in err_lower
            )
            kind = ErrorKind.AUTH if is_auth else ErrorKind.CRASH
            return Result(False, proc.returncode, stdout=stdout, stderr=stderr, channel=self.channel,
                          tool_version=self._version, error_kind=kind, usage=usage)
        if self._version != self.runtime.version:
            result = self._unknown_version(proc, '未验证的豆包版本')
            result.usage = usage
            return result
        try:
            events = ResultStream._lines(stdout)
            results = [event for event in events if event.get('type') == 'result']
            if len(results) != 1 or events[-1] is not results[0]:
                raise ValueError
            final = results[0]
            body = final.get('result')
            text = body.get('content') if isinstance(body, dict) else None
            # 没有越权尝试时，真实终态会省略 permission_denials。
            denials = final.get('permission_denials', [])
            if (final.get('subtype') != 'success' or final.get('is_error') is not False
                    or final.get('tool_calls_truncated') is not False or not isinstance(denials, list)
                    or not isinstance(text, str) or not text.strip()):
                raise ValueError
            if denials:
                stderr = (stderr + '\n豆包权限拒绝 ' + str(len(denials)) + ' 项').strip()
            return Result(True, proc.returncode, stdout=stdout, stderr=stderr, text=text,
                          channel=self.channel, tool_version=self._version, usage=usage)
        except (ValueError, TypeError, AttributeError, KeyError, json.JSONDecodeError):
            result = self._unknown_version(proc, '豆包未返回唯一的成功终态')
            result.usage = usage
            return result


from .spec import COMMON_CLI, AdapterSpec, Capabilities, OptionSpec, proxy_option  # noqa: E402
from .verified_runtimes import VERIFIED_RUNTIME_CATALOG as _CATALOG  # noqa: E402

SPEC = AdapterSpec(
    type="cli_doubao",
    factory=DoubaoAdapter,
    options=COMMON_CLI + (
        OptionSpec("binary", str, None, "已验证 Linux 程序的绝对路径", blank_none=True),
        OptionSpec("image", str, None, "容器镜像", blank_none=True),
        OptionSpec("machine_id", str, None, "machine-id 文件绝对路径", blank_none=True),
        OptionSpec("profile", str, None, "Docker 卷名", blank_none=True),
        proxy_option(),
        OptionSpec("runtime_version", str, None, "已验证运行时版本", blank_none=True),
    ),
    capabilities=Capabilities(file_tools=True),
    verified_versions=tuple(r.short_version or r.version for r in _CATALOG["cli_doubao"].values()),
)
