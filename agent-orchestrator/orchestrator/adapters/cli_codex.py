"""Codex CLI 适配器（评审 M2：版本绑定解析）。

命令模板：codex exec "<prompt>" -C <dir> --sandbox workspace-write
          --skip-git-repo-check --json -o <out>
- --json 输出 JSONL 事件流；解析最终 agent 消息。
- 已知版本走 JSONL 解析；未知版本 => parse_unknown（禁止判完成）。
- 输出含中文/ANSI/超长时不崩溃。
"""
from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

from ..contracts import ErrorKind, Result, RunContext
from .cli_base import CLIAdapter
from .cli_result_stream import jsonl_objects

# 已验证可解析的 codex 版本前缀（fixture 绑定这些版本）
KNOWN_VERSION_PREFIXES = ("codex-cli 0.167.", "codex-cli 0.169.", "codex 0.167.", "codex 0.169.")

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def parse_codex_jsonl(stdout: str) -> tuple[str, bool]:
    """解析 codex --json 的 JSONL 事件流，返回 (最终文本, 是否解析成功)。

    兼容多种事件形态：item.completed / agent_message / 含 text 的消息。
    解析不到任何有效消息 => (原始文本, False)。
    """
    return _codex_message(jsonl_objects(stdout), stdout)


def _codex_message(events, stdout):
    texts: list[str] = []
    for ev in events:
        # 形态 1: {"type":"item.completed","item":{"type":"agent_message","text":...}}
        item = ev.get("item") or {}
        if ev.get('type') == 'item.completed' and isinstance(item, dict) and item.get('type') in (None, 'agent_message'):
            t = item.get("text") or item.get("content")
            if isinstance(t, str) and t and item.get('phase') != 'commentary':
                texts.append(t)
                continue
        # 形态 2: {"type":"agent_message","text":...} / {"message":...}
        if ev.get('type') not in ('agent_message', 'item.completed'):
            continue
        for key in ("text", "message", "content"):
            v = ev.get(key)
            if isinstance(v, str) and v:
                texts.append(v)
                break
    if texts:
        return texts[-1], True
    return stdout, False


class CodexAdapter(CLIAdapter):
    channel = "cli_codex"
    exe_name = "codex"
    version_args = ["--version"]
    # CI 场景 codex 需要 CODEX_API_KEY；默认不透传，须配置显式开启
    credential_env = []

    def __init__(self, name, exe=None, extra_env=None, timeout_s=300,
                 model=None, isolate_config=False, reasoning_effort=None):
        super().__init__(name, exe, extra_env, timeout_s)
        if type(isolate_config) is not bool:
            raise ValueError('Codex isolate_config 必须为布尔值')
        self.model = model
        self.isolate_config = isolate_config
        self.reasoning_effort = reasoning_effort

    def _profile(self):
        if not self.isolate_config:
            return {}, self.model
        home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        path = home / 'config.toml'
        data = tomllib.loads(path.read_text(encoding='utf8')) if path.is_file() else {}
        windows_sandbox = None
        if os.name == 'nt':
            windows = data.get('windows', {})
            if not isinstance(windows, dict):
                raise ValueError('Codex [windows] 必须是配置表')
            windows_sandbox = windows.get('sandbox')
            if windows_sandbox is not None and windows_sandbox not in ('unelevated', 'elevated', 'mxc'):
                raise ValueError('Codex windows.sandbox 必须为 unelevated、elevated 或 mxc')
        name = data.get('model_provider', 'openai')
        provider = data.get('model_providers', {}).get(name, {})
        # 仅复制模型连接与显式 Windows 沙箱后端；MCP、插件、通知、hooks 不进入本次 CLI。
        profile = {key: provider[key] for key in ('name', 'base_url', 'wire_api', 'env_key', 'requires_openai_auth')
                   if key in provider}
        token = provider.get('experimental_bearer_token')
        if not token and provider.get('env_key'):
            token = os.environ.get(provider['env_key'])
        return {'provider': name, 'connection': profile, 'token': token,
                'codex_home': str(home), 'windows_sandbox': windows_sandbox}, self.model or data.get('model')

    def input_fingerprint(self):
        profile, model = self._profile()
        return {'protocol': 6, 'model': model, 'isolate_config': self.isolate_config,
                'reasoning_effort': self.reasoning_effort,
                'windows_sandbox': profile.get('windows_sandbox'),
                'connection': profile.get('connection', {})}

    def _version_known(self) -> bool:
        v = (self._version or "").lower().strip()
        return v == 'codex-cli 0.170.0' or any(v.startswith(p) for p in KNOWN_VERSION_PREFIXES)

    def preflight(self):
        return self.verified_preflight()

    def run(self, prompt: str, workdir: Path, ctx: RunContext) -> Result:
        if not self._ensure_resolved() or not self._version_known():
            return Result(ok=False, exit_code=-1, parse_unknown=True, error_kind=ErrorKind.PARSE_FAILED,
                          channel=self.channel, tool_version=self._version,
                          stderr='未确认已验证 Codex 版本，没有发起模型请求')
        out_file = (ctx.attempt_dir or Path(ctx.runs_dir) / ctx.stage) / "codex_last_message.txt"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.unlink(missing_ok=True)
        profile, model = self._profile()
        environment = {}
        args = ["exec", "-C", str(workdir),
                "--sandbox", 'workspace-write' if ctx.kind == 'code' else 'read-only',
                "--skip-git-repo-check", '--ephemeral', "--json", '--color', 'never',
                '-c', 'notify=[]', '-c', 'approval_policy="never"', "-o", str(out_file)]
        if self.isolate_config:
            args.append('--ignore-user-config')
            environment['CODEX_HOME'] = profile['codex_home']
            if profile.get('windows_sandbox'):
                args += ['-c', 'windows.sandbox=' + json.dumps(profile['windows_sandbox'])]
            if profile['connection']:
                connection = dict(profile['connection'])
                if profile.get('token'):
                    connection.update(env_key='AI_PIPELINE_CODEX_KEY', requires_openai_auth=False)
                    environment['AI_PIPELINE_CODEX_KEY'] = profile['token']
                inline = '{' + ','.join(json.dumps(k) + '=' + json.dumps(v) for k, v in connection.items()) + '}'
                args += ['-c', 'model_provider="pipeline"', '-c', 'model_providers.pipeline=' + inline]
        if model:
            args += ['--model', model]
        if self.reasoning_effort:
            args += ['-c', 'model_reasoning_effort=' + json.dumps(self.reasoning_effort)]
        if ctx.allow_yolo:
            args.append("--yolo")
        # 显式的 stdin 占位符；不把提示词拼进命令行。
        res = self._exec([*args, '-'], workdir, ctx, stdin_data=prompt, env_overrides=environment)
        if model:
            res.usage['requested_model'] = model
        return self._finalize(res, ctx)

    def _parse(self, proc, workdir: Path, ctx: RunContext) -> Result:
        stdout = strip_ansi(proc.stdout or "")
        stderr = strip_ansi(proc.stderr or "")
        events = list(jsonl_objects(stdout))
        # 同一个 turn 的重放/重复完成事件只能入账一次。
        by_turn = {}
        for event in events:
            reported = event.get('usage')
            if event.get('type') != 'turn.completed' or not isinstance(reported, dict):
                continue
            if not all(type(reported.get(k)) is int and reported[k] >= 0 for k in ('input_tokens','output_tokens')):
                continue
            key = str(event.get('turn_id') or 'single')
            previous = by_turn.get(key,{})
            by_turn[key] = {k:max(previous.get(k,0), reported.get(k,0)
                                  if type(reported.get(k,0)) is int and reported.get(k,0)>=0 else 0)
                           for k in ('input_tokens','output_tokens','cached_input_tokens')}
        usages = list(by_turn.values())
        usage = {'usage_unknown': True}
        if usages and all(type(u.get(k)) is int and u[k] >= 0 for u in usages for k in ('input_tokens', 'output_tokens')):
            usage = {key: sum(u.get(key, 0) if type(u.get(key, 0)) is int and u.get(key, 0) >= 0 else 0 for u in usages)
                     for key in ('input_tokens', 'output_tokens', 'cached_input_tokens')}
            usage['tokens'] = usage['input_tokens'] + usage['output_tokens']
            if usage['tokens'] == 0:
                usage['usage_unknown'] = True
        if proc.returncode != 0:
            kind = ErrorKind.AUTH if ("auth" in stderr.lower() or "login" in stderr.lower()) \
                else ErrorKind.CRASH
            return Result(ok=False, exit_code=proc.returncode, channel=self.channel,
                          stdout=stdout, stderr=stderr, text=stdout,
                          tool_version=self._version, error_kind=kind, usage=usage)
        if any(e.get('type') in ('turn.failed', 'error') for e in events):
            return Result(ok=False, exit_code=proc.returncode, channel=self.channel,
                          stdout=stdout, stderr=stderr or 'Codex 返回失败事件',
                          tool_version=self._version, error_kind=ErrorKind.CRASH, usage=usage)
        # 版本绑定：未知版本不默认走老解析器（评审 M2）
        if not self._version_known():
            result = self._unknown_version(proc, "未知 codex 版本，需人工确认输出格式")
            result.usage = usage
            return result
        if self._version == 'codex-cli 0.170.0' and (
                not events or events[-1].get('type') != 'turn.completed'
                or sum(e.get('type') == 'turn.completed' for e in events) != 1):
            result = self._unknown_version(proc, 'Codex 0.170.0 需要唯一末尾完成事件')
            result.usage = usage
            return result
        if not any(e.get('type') == 'turn.completed' for e in events):
            result = self._unknown_version(proc, 'Codex 未提交回合完成事件')
            result.usage = usage
            return result
        text, ok = _codex_message(events, stdout)
        if not ok:
            result = self._unknown_version(proc, "JSONL 解析未得到有效消息")
            result.usage = usage
            return result
        # changed_files 由引擎 git 检测填充；此处给出文本产物
        return Result(ok=True, exit_code=0, channel=self.channel,
                      stdout=stdout, stderr=stderr, text=text,
                      tool_version=self._version, usage=usage)


from .spec import COMMON_CLI, AdapterSpec, Capabilities, OptionSpec, effort_option, model_option  # noqa: E402

_CODEX_EFFORTS = ("none", "low", "medium", "high", "xhigh")

SPEC = AdapterSpec(
    type="cli_codex",
    factory=CodexAdapter,
    options=COMMON_CLI + (
        model_option(),
        OptionSpec("isolate_config", bool, False, "使用隔离的 Codex 配置目录"),
        effort_option(_CODEX_EFFORTS),
    ),
    capabilities=Capabilities(model_select=True, reasoning_effort=_CODEX_EFFORTS, file_tools=True),
    verified_versions=tuple(sorted({p.split()[-1] + "x" for p in KNOWN_VERSION_PREFIXES})) + ("0.170.0",),
)
