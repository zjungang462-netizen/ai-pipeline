"""Claude Code已验证版本的真实协议；工具身份与兼容API后端分开记录。"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from .cli_base import CLIAdapter, run_cli_process
from .cli_codex import strip_ansi
from .cli_result_stream import RESULT_STREAMS
from ..contracts import ErrorKind,Result,PreflightReport
from ..safety import build_subprocess_env, resolve_exe

SUPPORTED_VERSIONS = RESULT_STREAMS['cli_claude'].accepted_versions

CONNECTION_KEYS=frozenset(('ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_BASE_URL',
    'ANTHROPIC_MODEL','ANTHROPIC_DEFAULT_OPUS_MODEL','ANTHROPIC_DEFAULT_SONNET_MODEL','ANTHROPIC_DEFAULT_HAIKU_MODEL'))


class ClaudeAdapter(CLIAdapter):
    channel='cli_claude'
    exe_name='claude'
    credential_env=[]

    def __init__(self,name,exe=None,extra_env=None,timeout_s=300,model=None,
                 reasoning_effort=None,inherit_connection=True):
        super().__init__(name,exe,extra_env,timeout_s)
        if reasoning_effort is not None and reasoning_effort not in ('low','medium','high','xhigh','max'):
            raise ValueError('Claude Code reasoning_effort非法')
        if type(inherit_connection) is not bool:raise ValueError('inherit_connection必须为布尔值')
        self.model,self.reasoning_effort,self.inherit_connection=model,reasoning_effort,inherit_connection

    def _version_known(self):
        return self._version in {version + ' (Claude Code)' for version in SUPPORTED_VERSIONS}

    def preflight(self):
        self._resolved = resolve_exe(self.exe_name, self.exe)
        self._version = None
        if self._resolved:
            try:
                process = run_cli_process([self._resolved, '--version'], cwd=None,
                                          env=build_subprocess_env(), input=None, timeout=30)
                version = strip_ansi(process.stdout or '').strip()
                if process.returncode == 0 and version in {v + ' (Claude Code)' for v in SUPPORTED_VERSIONS}:
                    self._version = version
            except (OSError, subprocess.TimeoutExpired):
                pass
        return PreflightReport(adapter=self.name, ok=self._version_known(), exe_path=self._resolved,
                               tool_version=self._version, detail='只检查已验证版本，不探测登录')

    def _profile(self):
        env={};settings={}
        if self.inherit_connection:
            path=Path.home()/'.claude/settings.json'
            if path.is_file():
                if path.stat().st_size>2*1024*1024:raise ValueError('Claude连接配置超过读取上限')
                settings=json.loads(path.read_text(encoding='utf-8-sig'))
                if not isinstance(settings,dict) or not isinstance(settings.get('env',{}),dict):
                    raise ValueError('Claude settings/env必须为对象')
                for key,value in settings.get('env',{}).items():
                    if key in CONNECTION_KEYS:
                        if not isinstance(value,str):raise ValueError('Claude连接环境字段必须为字符串')
                        env[key]=value
        # 无自动透传：只有显式extra_env声明的连接字段可以覆盖用户连接快照。
        for key in CONNECTION_KEYS:
            if key in self.extra_env and key in os.environ:env[key]=os.environ[key]
        base=env.get('ANTHROPIC_BASE_URL')
        if base:
            parsed=urlsplit(base)
            if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('Claude兼容端点必须为不含凭据/查询的HTTP(S)地址')
        model=self.model or env.get('ANTHROPIC_DEFAULT_OPUS_MODEL') or env.get('ANTHROPIC_MODEL')
        saved_effort=settings.get('env',{}).get('CLAUDE_CODE_EFFORT_LEVEL')
        if 'CLAUDE_CODE_EFFORT_LEVEL' in self.extra_env:
            saved_effort=os.environ.get('CLAUDE_CODE_EFFORT_LEVEL',saved_effort)
        effort=self.reasoning_effort or saved_effort or settings.get('effortLevel')
        if effort is not None and effort not in ('low','medium','high','xhigh','max'):
            raise ValueError('Claude思考配置非法')
        return env,model,effort

    def input_fingerprint(self):
        env,model,effort=self._profile()
        return {'protocol':2,'supported_cli_versions':sorted(SUPPORTED_VERSIONS),'backend_endpoint':env.get('ANTHROPIC_BASE_URL','https://api.anthropic.com'),
                'model':model,'reasoning_effort':effort,'inherit_connection':self.inherit_connection,
                'model_aliases':{k:v for k,v in env.items() if k=='ANTHROPIC_MODEL' or k.startswith('ANTHROPIC_DEFAULT_')}}

    def run(self,prompt,workdir,ctx):
        if not self._ensure_resolved() or not self._version_known():
            return Result(ok=False, exit_code=-1, parse_unknown=True, error_kind=ErrorKind.PARSE_FAILED,
                          channel=self.channel, tool_version=self._version,
                          stderr='未确认已验证的 Claude Code 版本，没有发起模型请求')
        env,model,effort=self._profile()
        env.update(DISABLE_AUTOUPDATER='1',CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',CI='1')
        tools='Read,Glob,Grep,Edit,Write' if ctx.kind=='code' else 'Read,Glob,Grep'
        args=['--bare','--safe-mode','--restricted','--setting-sources','','--strict-mcp-config',
              '--no-session-persistence','--disable-slash-commands','--no-chrome','--print',
              '--verbose','--output-format','stream-json','--tools',tools,
              '--permission-mode','acceptEdits' if ctx.kind=='code' else 'dontAsk','--permission-prompts','none']
        if model:args+=['--model',model]
        if effort:args+=['--effort',effort]
        result=self._exec(args,workdir,ctx,stdin_data=prompt,env_overrides=env)
        result.usage['backend_endpoint']=env.get('ANTHROPIC_BASE_URL','https://api.anthropic.com')
        return self._finalize(result,ctx)

    def _parse(self,proc,workdir,ctx):
        stdout,stderr=strip_ansi(proc.stdout or ''),strip_ansi(proc.stderr or '')
        if not self._version_known():
            result=self._unknown_version(proc,'未验证的Claude Code版本');result.usage={'usage_unknown':True};return result
        try:
            init, final = RESULT_STREAMS[self.channel].parse(stdout, self._version.split(' ', 1)[0])
            if not isinstance(final.get('permission_denials'),list):raise ValueError('权限拒绝字段非法')
            raw=final.get('usage')
            if not isinstance(raw,dict):raise ValueError('usage必须为对象')
            keys=('input_tokens','output_tokens','cache_read_input_tokens','cache_creation_input_tokens')
            complete=all(key in raw for key in keys)
            for key in keys:
                if key in raw and (type(raw[key]) is not int or raw[key]<0):raise ValueError('token计数非法')
            total=sum(raw.get(key,0) for key in keys)
            usage={'tokens':total,'known_tokens':total,'cli_reported_tokens':total,
                   'cache_read_tokens':raw.get('cache_read_input_tokens',0),
                   'cache_creation_tokens':raw.get('cache_creation_input_tokens',0),
                   'usage_unknown':not complete,'usage_scope':'claude_reported_anthropic_compatible_counters',
                   'tool':'Claude Code','backend_model':init.get('model'),'billing_cost_status':'not_verified_for_compatible_backend'}
            text=final.get('result')
            ok=(proc.returncode==0 and final.get('subtype')=='success' and final.get('is_error') is False
                and final.get('stop_reason')=='end_turn' and final.get('terminal_reason')=='completed'
                and not final.get('permission_denials') and final.get('safety_stops')==0
                and isinstance(text,str) and bool(text.strip()))
            return Result(ok=ok,exit_code=proc.returncode,stdout=stdout,stderr=stderr,text=text if isinstance(text,str) else '',
                channel=self.channel,tool_version=self._version,error_kind=ErrorKind.NONE if ok else ErrorKind.REJECTED,usage=usage)
        except (ValueError,TypeError,KeyError,StopIteration):
            if proc.returncode!=0 and any(s in stderr.lower() for s in ('authentication','api key','not logged','401','403')):
                return Result(ok=False,exit_code=proc.returncode,stdout=stdout,stderr=stderr,
                              channel=self.channel,tool_version=self._version,error_kind=ErrorKind.AUTH,usage={'usage_unknown':True})
            result=self._unknown_version(proc,'Claude Code终态协议不匹配');result.usage={'usage_unknown':True};return result


from .spec import COMMON_CLI, AdapterSpec, Capabilities, OptionSpec, effort_option, model_option  # noqa: E402

_CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")

SPEC = AdapterSpec(
    type="cli_claude",
    factory=ClaudeAdapter,
    options=COMMON_CLI + (
        model_option(),
        effort_option(_CLAUDE_EFFORTS),
        OptionSpec("inherit_connection", bool, True, "继承 ~/.claude/settings.json 的连接与模型字段"),
    ),
    capabilities=Capabilities(model_select=True, reasoning_effort=_CLAUDE_EFFORTS, file_tools=True),
    verified_versions=tuple(sorted(SUPPORTED_VERSIONS)),
)
