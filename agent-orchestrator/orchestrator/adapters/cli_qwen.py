"""Qwen Code CLI 适配器（命令 qwen，Apache-2.0 开源）。

0.26.0 使用隔离自定义配置的 stream-json 文件工具；旧数组样本继续回放。
连接变量须由 extra_env 显式列名，未知版本在模型调用前停止。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from ..contracts import ErrorKind, Result, RunContext
from .cli_base import CLIAdapter
from .cli_codex import strip_ansi
from .cli_result_stream import RESULT_STREAMS

class QwenAdapter(CLIAdapter):
    channel = "cli_qwen"
    exe_name = "qwen"
    version_args = ["--version"]
    # 需配置显式开启才透传（评审 M4）
    credential_env = []

    def __init__(self, name: str, exe: str | None = None,
                 extra_env: list[str] | None = None, timeout_s: int = 300,
                 model: str | None = None):
        super().__init__(name, exe, extra_env, timeout_s)
        self.model = model

    def _version_known(self) -> bool:
        v = (self._version or "").lower().strip()
        return v in RESULT_STREAMS[self.channel].accepted_versions or v in ('qwen-code 0.5.0', 'qwen 0.5.0')

    def preflight(self):
        return self.verified_preflight()

    def input_fingerprint(self):
        endpoint = os.environ.get('OPENAI_BASE_URL') if 'OPENAI_BASE_URL' in self.extra_env else None
        if endpoint:
            parsed = urlsplit(endpoint)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('Qwen 兼容端点必须为不含凭据/查询的 HTTP(S) 地址')
        model = self.model or (os.environ.get('OPENAI_MODEL') if 'OPENAI_MODEL' in self.extra_env else None)
        return {'protocol': 3, 'model': model, 'backend_endpoint': endpoint, 'current_protocol': '0.26.0-stream-json',
                'isolated_customizations': True, 'file_tools_only': True}

    def run(self, prompt: str, workdir: Path, ctx: RunContext) -> Result:
        if not self._ensure_resolved() or not self._version_known():
            return Result(ok=False, exit_code=-1, parse_unknown=True, error_kind=ErrorKind.PARSE_FAILED,
                          channel=self.channel, tool_version=self._version,
                          stderr='未确认已验证 Qwen 版本，没有发起模型请求')
        # 评审 M5：prompt 走 stdin，避免撞 Windows 命令行 32767 字符上限
        args = ["-p", "-", "--output-format", "json"]
        if self._version == '0.26.0':
            args = ['--bare', '--safe-mode', '--chat-recording=false', '-p', '-', '--output-format', 'stream-json',
                    '--approval-mode', 'auto-edit' if ctx.kind == 'code' else 'plan',
                    '--max-session-turns', '12', '--max-tool-calls', '24',
                    '--max-wall-time', str(max(1, int(ctx.timeout_s or self.timeout_s))) + 's']
            # safe mode 忽略 core-tools，但仍应用显式 exclude-tools；真机工具清单已验证。
            for tool in ('run_shell_command', 'get_goal', 'update_goal', 'notebook_edit', 'task', 'web_fetch', 'web_search'):
                args += ['--exclude-tools', tool]
            if ctx.kind != 'code':
                args += ['--exclude-tools', 'edit']
        if self.model:
            args += ["-m", self.model]
        if ctx.allow_yolo:
            args.append("--yolo")
        return self._finalize(self._exec(args, workdir, ctx, stdin_data=prompt), ctx)

    def _parse(self, proc, workdir: Path, ctx: RunContext) -> Result:
        stdout = strip_ansi(proc.stdout or "")
        stderr = strip_ansi(proc.stderr or "")
        if proc.returncode != 0:
            kind = ErrorKind.AUTH if ("auth" in stderr.lower() or "api key" in stderr.lower()) \
                else ErrorKind.CRASH
            return Result(ok=False, exit_code=proc.returncode, channel=self.channel,
                          stdout=stdout, stderr=stderr, text=stdout,
                          tool_version=self._version, error_kind=kind)
        if not self._version_known():
            return self._unknown_version(proc, "未知 qwen 版本")
        if (self._version or '').strip() == '0.26.0':
            return self._parse_026(stdout, stderr, proc.returncode)
        text = self._extract_json_text(stdout)
        if text is None:
            return self._unknown_version(proc, "qwen JSON 解析失败")
        return Result(ok=True, exit_code=0, channel=self.channel,
                      stdout=stdout, stderr=stderr, text=text,
                      tool_version=self._version)

    def _parse_026(self, stdout, stderr, exit_code):
        """验证当前流式输出与旧事件数组的唯一末尾终态。"""
        try:
            _, final = RESULT_STREAMS[self.channel].parse(stdout, self._version.strip())
            if final.get('subtype') != 'success' or final.get('is_error') is not False:
                raise ValueError('CLI 结果不是成功')
            if final.get('permission_denials') not in (None, []):
                raise ValueError('存在权限拒绝')
            text = final.get('result')
            if not isinstance(text, str) or not text.strip():
                raise ValueError('缺少结果正文')
            usage = final.get('usage', {})
            if not isinstance(usage, dict):
                raise ValueError('usage 必须为对象')
            counters = {}
            for key in ('total_tokens', 'input_tokens', 'output_tokens', 'cache_read_input_tokens'):
                value = usage.get(key, 0)
                if type(value) is not int or value < 0:
                    raise ValueError('用量计数非法')
                counters[key] = value
            measured = {'tokens': counters['total_tokens'], 'known_tokens': counters['total_tokens'],
                        'prompt_tokens': counters['input_tokens'], 'completion_tokens': counters['output_tokens'],
                        'cache_read_tokens': counters['cache_read_input_tokens'],
                        'usage_unknown': 'total_tokens' not in usage}
            return Result(ok=True, exit_code=exit_code, channel=self.channel, stdout=stdout,
                          stderr=stderr, text=text, tool_version=self._version, usage=measured)
        except (ValueError, TypeError, StopIteration, KeyError, IndexError):
            return Result(ok=False, exit_code=exit_code, channel=self.channel, stdout=stdout,
                          stderr=stderr+' Qwen 0.26.0 成功终态或用量未匹配', text=stdout,
                          tool_version=self._version, parse_unknown=True,
                          error_kind=ErrorKind.PARSE_FAILED, usage={'usage_unknown': True})

    @staticmethod
    def _extract_json_text(stdout: str) -> str | None:
        # 整体 JSON 或逐行 JSONL，取 result/text/response 字段
        candidates = [stdout] + stdout.splitlines()
        for chunk in candidates:
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


from .spec import COMMON_CLI, AdapterSpec, Capabilities, model_option  # noqa: E402

SPEC = AdapterSpec(
    type="cli_qwen",
    factory=QwenAdapter,
    options=COMMON_CLI + (model_option(),),
    capabilities=Capabilities(model_select=True, file_tools=True),
    verified_versions=("0.5.0",) + tuple(sorted(RESULT_STREAMS["cli_qwen"].accepted_versions)),
)
