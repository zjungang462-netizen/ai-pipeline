"""Qoder 1.1.89/协议1.5.0：stdin、流式终态、受限文件工具、未知token与credits分列。"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

from ..contracts import ErrorKind, PreflightReport, Result, RunContext
from ..safety import build_subprocess_env, resolve_exe
from .cli_base import CLIAdapter, run_cli_process
from .cli_codex import strip_ansi
from .cli_result_stream import RESULT_STREAMS

SUPPORTED_VERSION, = RESULT_STREAMS['cli_qoder'].accepted_versions


class QoderAdapter(CLIAdapter):
    channel = "cli_qoder"
    exe_name = "qoder"
    version_args = ["--version"]
    credential_env = []  # QODER_PERSONAL_ACCESS_TOKEN 须配置显式开启

    def __init__(self, name, exe=None, extra_env=None, timeout_s=300, model=None,
                 reasoning_effort=None, thinking=None):
        super().__init__(name, exe, extra_env, timeout_s)
        if reasoning_effort is not None and reasoning_effort not in ('low','medium','high','xhigh','max'):
            raise ValueError('Qoder reasoning_effort 非法')
        if thinking is not None and thinking not in ('auto','adaptive','disabled'):
            raise ValueError('Qoder 无固定思考预算模式只支持 auto/adaptive/disabled')
        self.model, self.reasoning_effort, self.thinking = model, reasoning_effort, thinking

    def input_fingerprint(self):
        return {'protocol':2,'model':self.model,'reasoning_effort':self.reasoning_effort,
                'thinking':self.thinking,'cli_protocol':'1.1.89/1.5.0'}

    def preflight(self) -> PreflightReport:
        self._resolved = resolve_exe(self.exe_name, self.exe)
        self._version = None
        detail = '未找到 Qoder CLI（配置 exe 填实际入口的绝对路径）'
        if self._resolved:
            try:
                proc = run_cli_process([self._resolved, *self.version_args], cwd=None,
                                       env=build_subprocess_env(), input=None, timeout=30)
                if proc.returncode == 0 and strip_ansi(proc.stdout).strip() == SUPPORTED_VERSION:
                    self._version = SUPPORTED_VERSION
                    detail = '已确认 Qoder CLI 版本，未检测登录状态'
                elif proc.returncode != 0:
                    detail = f'Qoder CLI 版本探测失败（退出码 {proc.returncode}）'
                else:
                    detail = f'未确认受支持的 Qoder CLI 版本 {SUPPORTED_VERSION}'
            except subprocess.TimeoutExpired:
                detail = 'Qoder CLI 版本探测超时（30 秒）'
            except OSError:
                detail = '无法启动 Qoder CLI 版本探测'
        return PreflightReport(adapter=self.name, ok=self._version is not None,
                               exe_path=self._resolved, tool_version=self._version, detail=detail)

    def run(self, prompt: str, workdir: Path, ctx: RunContext) -> Result:
        exe = self._ensure_resolved()
        if not exe or self._version != SUPPORTED_VERSION:
            return Result(ok=False, exit_code=-1, channel=self.channel, parse_unknown=True,
                          tool_version=self._version, error_kind=ErrorKind.PARSE_FAILED,
                          stderr=f'未确认 Qoder CLI {SUPPORTED_VERSION}，没有发起模型请求')
        # stdout 有界流式留证；纯报告/审查只读，编码不启用 shell、MCP 或子代理。
        tools = 'Read,Glob,Grep,Edit,Write' if ctx.kind == 'code' else 'Read,Glob,Grep'
        args = ['--print','--output-format','stream-json','--tools',tools,
                '--permission-mode','accept_edits' if ctx.kind == 'code' else 'dont_ask',
                '--strict-mcp-config','--setting-sources','','--max-model-request-retries','0',
                '--no-session-persistence']
        if self.model:args += ['--model',self.model]
        if self.reasoning_effort:args += ['--reasoning-effort',self.reasoning_effort]
        if self.thinking:args += ['--thinking',self.thinking]
        args += ['-']
        return self._finalize(self._exec(args,workdir,ctx,stdin_data=prompt,
                                         env_overrides={'CI':'1','BROWSER':'www-browser'}),ctx)

    def _parse(self, proc, workdir: Path, ctx: RunContext) -> Result:
        stdout = strip_ansi(proc.stdout or "")
        stderr = strip_ansi(proc.stderr or "")
        if proc.returncode != 0:
            kind = ErrorKind.AUTH if ("auth" in stderr.lower() or "token" in stderr.lower()
                                      or "login" in stderr.lower()) else ErrorKind.CRASH
            if self._version == SUPPORTED_VERSION and stdout.strip():
                result=self._parse_1189(proc,stdout,stderr)
                result.ok=False;result.error_kind=kind
                return result
            return Result(ok=False, exit_code=proc.returncode, channel=self.channel,
                          stdout=stdout, stderr=stderr, text=stdout,
                          tool_version=self._version, error_kind=kind, usage={'usage_unknown':True})
        if self._version != SUPPORTED_VERSION:
            return self._unknown_version(proc, "未知 qoder 版本")
        return self._parse_1189(proc, stdout, stderr)

    def _parse_1189(self, proc, stdout, stderr):
        try:
            _, final = RESULT_STREAMS[self.channel].parse(stdout, self._version)
            if not isinstance(final.get('permission_denials'),list):raise ValueError('permission_denials 非列表')
            usage=self._reported_usage(final)
            if (final.get('subtype')!='success' or final.get('is_error') is not False
                    or final.get('stop_reason')!='end_turn' or not isinstance(final.get('result'),str)
                    or not final['result'].strip() or final.get('permission_denials')):
                return Result(ok=False,exit_code=proc.returncode,channel=self.channel,
                    stdout=stdout,stderr=stderr+' Qoder 未正常完成或工具被拒绝',text=final.get('result',''),
                    tool_version=self._version,error_kind=ErrorKind.REJECTED,usage=usage)
            return Result(ok=True,exit_code=proc.returncode,channel=self.channel,stdout=stdout,stderr=stderr,
                          text=final['result'],tool_version=self._version,usage=usage)
        except (ValueError,TypeError,KeyError,StopIteration):
            result=self._unknown_version(proc,'Qoder 1.1.89 终态/协议未匹配')
            result.usage={'usage_unknown':True,'tokens':0,'known_tokens':0}
            return result

    @staticmethod
    def _reported_usage(final):
        raw=final.get('usage')
        if not isinstance(raw,dict):raise ValueError('usage 非对象')
        keys=('input_tokens','output_tokens','cache_read_input_tokens','cache_creation_input_tokens')
        for key in keys:
            if key in raw and (type(raw[key]) is not int or raw[key]<0):raise ValueError('非法token计数')
        known=sum(raw.get(key,0) for key in keys)
        measured={'tokens':known,'known_tokens':known,'usage_unknown':True,
                  'cli_reported_tokens':known,'usage_scope':'unverified_qoder_token_counters',
                  'cache_read_tokens':raw.get('cache_read_input_tokens',0),
                  'billing_cost_status':'not_reported','model_names':sorted(final.get('modelUsage',{}))
                  if isinstance(final.get('modelUsage'),dict) else []}
        # 真机1.1.89有实际模型结果与credits却全部token=0；永远不将其当完整账单计量。
        for key in ('total_credits',):
            value=final.get(key)
            if value is not None:
                if type(value) not in (int,float) or not math.isfinite(value) or value<0:raise ValueError('非法credits')
                measured['qoder_credits']=value
        return measured


from .spec import COMMON_CLI, AdapterSpec, Capabilities, OptionSpec, effort_option, model_option  # noqa: E402

_QODER_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_QODER_THINKING = ("auto", "adaptive", "disabled")

SPEC = AdapterSpec(
    type="cli_qoder",
    factory=QoderAdapter,
    options=COMMON_CLI + (
        model_option(),
        effort_option(_QODER_EFFORTS),
        OptionSpec("thinking", str, None, "无固定思考预算模式", choices=_QODER_THINKING,
                   blank_none=True, overridable=True),
    ),
    capabilities=Capabilities(model_select=True, reasoning_effort=_QODER_EFFORTS, thinking=_QODER_THINKING,
                              file_tools=True, usage_reporting="unknown"),
    verified_versions=(SUPPORTED_VERSION,),
)
