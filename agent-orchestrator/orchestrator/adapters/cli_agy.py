"""反重力 CLI 适配器（命令 agy）。

1.5.9 真机协议：--print= --input-format stream-json --output-format stream-json。
复用本机登录；可显式指定进程内代理。开发使用 accept-edits，不默认绕过权限。
只接受唯一的最终 SUCCESS 结果，保留原始用量与保守预算口径。
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from ..contracts import ErrorKind, Result, RunContext
from ..safety import build_subprocess_env
from .cli_base import CLIAdapter, run_cli_process
from .cli_codex import strip_ansi
from .cli_result_stream import jsonl_objects

SUPPORTED_VERSIONS = frozenset(('1.5.9', '1.5.12', '1.5.13', '1.5.14'))
_PATCH_SERIES_RE = re.compile(r'^1\.5\.(\d+)$')
_REQUIRED_HELP_FLAGS = ('--print', '--input-format', '--output-format', '--mode', '--sandbox', '--add-dir')


class AgyAdapter(CLIAdapter):
    channel = "cli_agy"
    exe_name = "agy"
    version_args = ["--version"]
    needs_login = True
    credential_env = []

    def __init__(self, name, exe=None, extra_env=None, timeout_s=300, model=None,
                 proxy_url=None, allow_patch_probe=False):
        super().__init__(name, exe, extra_env, timeout_s)
        self.model = model
        self.proxy_url = proxy_url
        self.allow_patch_probe = bool(allow_patch_probe)
        self._probed_versions: set[str] = set()
        self._detected_version: str = ''
        if proxy_url:
            parsed = urlsplit(proxy_url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('反重力 proxy_url 必须是不含凭据的 HTTP(S) 代理地址')

    def input_fingerprint(self):
        return {'protocol': 5, 'model': self.model, 'proxy_url': self.proxy_url, 'sandbox': True}

    def _probe_patch_protocol(self, version: str) -> bool:
        if version in self._probed_versions:
            return True
        if not self.allow_patch_probe or not self._resolved or not _PATCH_SERIES_RE.fullmatch(version):
            return False
        try:
            proc = run_cli_process([self._resolved, '--help'], cwd=None,
                                   env=build_subprocess_env(), input=None, timeout=15)
        except Exception:
            return False
        text = (proc.stdout or '') + '\n' + (proc.stderr or '')
        if proc.returncode == 0 and all(flag in text for flag in _REQUIRED_HELP_FLAGS):
            self._probed_versions.add(version)
            return True
        return False

    def _version_known(self) -> bool:
        v = (self._version or "").lower().strip()
        if v:
            self._detected_version = v
        if v in SUPPORTED_VERSIONS:
            return True
        return self._probe_patch_protocol(v)

    def preflight(self):
        return self.verified_preflight()

    def run(self, prompt: str, workdir: Path, ctx: RunContext) -> Result:
        if not self._ensure_resolved() or not self._version_known():
            ver = self._version or self._detected_version
            detected = f'（检测到 {ver}）' if ver else ''
            return Result(ok=False, exit_code=-1, parse_unknown=True, error_kind=ErrorKind.PARSE_FAILED,
                          channel=self.channel, tool_version=self._version,
                          stderr=f'未确认已验证反重力版本{detected}，没有发起模型请求')
        modern = self._version_known()
        if modern:
            # 1.5.9 的 --print 需要附着值；NDJSON 把完整提示词留在 stdin。
            args = ['--print=', '--input-format', 'stream-json', '--output-format', 'stream-json',
                    '--mode', 'accept-edits' if ctx.kind == 'code' else 'plan',
                    '--sandbox', '--print-timeout', str(max(1, int(ctx.timeout_s or self.timeout_s))) + 's',
                    '--add-dir', str(workdir)]
            if self.model:
                args += ['--model', self.model]
            body = json.dumps({'event': 'user', 'message': {'content': prompt}}, ensure_ascii=False) + '\n'
        else:
            args, body = ["-p", "-", "--output-format", "json"], prompt
        if ctx.allow_yolo:
            args.append("--dangerously-skip-permissions")
        environment = {}
        if self.proxy_url:
            environment.update(HTTP_PROXY=self.proxy_url, HTTPS_PROXY=self.proxy_url,
                               ALL_PROXY=self.proxy_url, NO_PROXY='localhost,127.0.0.1,::1')
        if modern:
            result = self._exec(args, workdir, ctx, stdin_data=body, env_overrides=environment)
        else:
            result = self._exec_prompt(args, body, workdir, ctx)
        return self._finalize(result, ctx)

    def _parse(self, proc, workdir: Path, ctx: RunContext) -> Result:
        stdout = strip_ansi(proc.stdout or "")
        stderr = strip_ansi(proc.stderr or "")
        events = list(jsonl_objects(stdout))
        results = [e['result'] for e in events if e.get('event') == 'result' and isinstance(e.get('result'), dict)]
        final = results[-1] if results else {}
        reported = final.get('usage', {})
        usage = {'usage_unknown': True}
        if isinstance(reported, dict) and type(reported.get('total_tokens')) is int and reported['total_tokens'] >= 0:
            usage = {k: v for k, v in reported.items() if type(v) is int and v >= 0}
            # CLI 的 total_tokens 与 cache_read_tokens 分列；预算保守覆盖两者，保留原始口径。
            usage['cli_reported_tokens'] = reported['total_tokens']
            usage['tokens'] = reported['total_tokens'] + usage.get('cache_read_tokens', 0)
            usage['accounting'] = 'conservative_total_plus_cache_read'
            if proc.returncode != 0 or final.get('status') != 'SUCCESS':
                usage['usage_unknown'] = True
        init = next((e['init'] for e in events if e.get('event') == 'init' and isinstance(e.get('init'), dict)), {})
        if isinstance(init.get('model'), str):
            usage['model'] = init['model']
        if proc.returncode != 0:
            err_lower = stderr.lower()
            kind = ErrorKind.AUTH if any(
                k in err_lower for k in ("auth", "login", "log in", "sign in", "sign-in", "unauthenticated")
            ) else ErrorKind.CRASH
            return Result(ok=False, exit_code=proc.returncode, channel=self.channel,
                          stdout=stdout, stderr=stderr, text=stdout,
                          tool_version=self._version, error_kind=kind, usage=usage)
        if not self._version_known():
            result = self._unknown_version(proc, "未知 agy 版本")
            result.usage = usage
            return result
        if self._version_known():
            if (len(results) != 1 or final.get('status') != 'SUCCESS'
                    or not events or events[-1].get('event') != 'result'
                    or any(e.get('event') == 'error' for e in events)):
                return Result(ok=False, channel=self.channel, stdout=stdout, stderr=stderr or '反重力未返回唯一成功结果',
                              tool_version=self._version, usage=usage, error_kind=ErrorKind.PARSE_FAILED, parse_unknown=True)
            text = final.get('response')
        else:
            text = self._extract(stdout)
        if not isinstance(text, str) or not text.strip():
            result = self._unknown_version(proc, "agy JSON 解析失败")
            result.usage = usage
            return result
        return Result(ok=True, exit_code=0, channel=self.channel,
                      stdout=stdout, stderr=stderr, text=text,
                      tool_version=self._version, usage=usage)

    @staticmethod
    def _extract(stdout: str) -> str | None:
        for chunk in [stdout] + stdout.splitlines():
            chunk = chunk.strip()
            if not chunk.startswith("{"):
                continue
            try:
                data = json.loads(chunk)
            except ValueError:
                continue
            for key in ("result", "text", "response", "content", "message"):
                v = data.get(key)
                if isinstance(v, str) and v:
                    return v
        return None


from .spec import COMMON_CLI, AdapterSpec, Capabilities, OptionSpec, model_option, proxy_option  # noqa: E402

SPEC = AdapterSpec(
    type="cli_agy",
    factory=AgyAdapter,
    options=COMMON_CLI + (
        model_option(),
        proxy_option(),
        OptionSpec("allow_patch_probe", bool, False, "允许探测同系列未登记补丁版本"),
    ),
    capabilities=Capabilities(model_select=True, file_tools=True),
    verified_versions=tuple(sorted(SUPPORTED_VERSIONS, key=lambda v: tuple(int(x) for x in v.split(".")))),
)
