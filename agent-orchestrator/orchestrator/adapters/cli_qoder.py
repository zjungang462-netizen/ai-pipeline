"""Qoder 1.1.89/协议1.5.0：stdin、流式终态、受限文件工具、未知token与credits分列。"""
from __future__ import annotations

import json
import math
from pathlib import Path

from ..contracts import ErrorKind, Result, RunContext
from .cli_base import CLIAdapter
from .cli_codex import strip_ansi

KNOWN_VERSION_PREFIXES = ("qoder ",)


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

    def _version_known(self) -> bool:
        v = (self._version or "").lower().strip()
        return v == '1.1.89' or any(v.startswith(p) for p in KNOWN_VERSION_PREFIXES)

    def run(self, prompt: str, workdir: Path, ctx: RunContext) -> Result:
        self._ensure_resolved()
        if (self._version or '').strip() == '1.1.89':
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
        args = ["-p", "-", "--output-format", "json"]
        if ctx.allow_yolo:
            args.append("--yolo")
        else:
            args += ["--permission-mode", "accept_edits"]
        return self._finalize(self._exec_prompt(args, prompt, workdir, ctx), ctx)

    def _parse(self, proc, workdir: Path, ctx: RunContext) -> Result:
        stdout = strip_ansi(proc.stdout or "")
        stderr = strip_ansi(proc.stderr or "")
        if proc.returncode != 0:
            kind = ErrorKind.AUTH if ("auth" in stderr.lower() or "token" in stderr.lower()
                                      or "login" in stderr.lower()) else ErrorKind.CRASH
            if (self._version or '').strip() == '1.1.89' and stdout.strip():
                result=self._parse_1189(proc,stdout,stderr)
                result.ok=False;result.error_kind=kind
                return result
            return Result(ok=False, exit_code=proc.returncode, channel=self.channel,
                          stdout=stdout, stderr=stderr, text=stdout,
                          tool_version=self._version, error_kind=kind, usage={'usage_unknown':True})
        if not self._version_known():
            return self._unknown_version(proc, "未知 qoder 版本")
        if (self._version or '').strip() == '1.1.89':
            return self._parse_1189(proc, stdout, stderr)
        text = self._extract(stdout)
        if text is None:
            return self._unknown_version(proc, "qoder JSON 解析失败")
        return Result(ok=True, exit_code=0, channel=self.channel,
                      stdout=stdout, stderr=stderr, text=text,
                      tool_version=self._version)

    def _parse_1189(self, proc, stdout, stderr):
        try:
            try:
                data=json.loads(stdout)
                if not isinstance(data,dict):raise ValueError('不是对象')
                events=[data]
            except json.JSONDecodeError:
                events=[json.loads(line) for line in stdout.splitlines() if line.strip()]
                if not events or not all(isinstance(e,dict) for e in events):raise ValueError('非法事件流')
                init=next(e for e in events if e.get('type')=='system' and e.get('subtype')=='init')
                if init.get('qodercli_version')!='1.1.89' or init.get('protocol_version')!='1.5.0':
                    raise ValueError('初始化协议不匹配')
            finals=[e for e in events if e.get('type')=='result']
            if len(finals)!=1 or events[-1] is not finals[0]:raise ValueError('缺少唯一最终结果')
            final=finals[0]
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
