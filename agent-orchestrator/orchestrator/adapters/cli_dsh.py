"""DeepSeek Harness 0.2.2-rc.3 headless JSON 协议；测试结论仍以实际命令为准。"""
from __future__ import annotations

from pathlib import Path
import json
from urllib.parse import urlsplit

from ..contracts import ErrorKind, Result, RunContext
from .cli_base import CLIAdapter
from .cli_codex import strip_ansi
from ..cli_telemetry import UsageLedger

MAX_TEXT = 200_000  # 超长输出截断保护


class DshAdapter(CLIAdapter):
    channel = "cli_dsh"
    exe_name = "dsh"
    version_args = ["--version"]
    credential_env = []

    def __init__(self,name,exe=None,extra_env=None,timeout_s=300,permission_mode='read-only',proxy_url=None):
        super().__init__(name,exe,extra_env,timeout_s)
        if permission_mode not in ('read-only','workspace-write'):
            raise ValueError('dsh 只允许 read-only 或 workspace-write')
        if proxy_url:
            p=urlsplit(proxy_url)
            if p.scheme not in ('http','https') or not p.hostname or p.username or p.password:
                raise ValueError('dsh proxy_url 必须为不含凭据的 HTTP(S) 地址')
        self.permission_mode=permission_mode
        self.proxy_url=proxy_url

    def input_fingerprint(self):
        return {'protocol':2,'version':'0.2.2-rc.3','permission_mode':self.permission_mode,'proxy_url':self.proxy_url}

    def run(self, prompt: str, workdir: Path, ctx: RunContext) -> Result:
        args=['--profile','headless','--json','--permission-mode',self.permission_mode,'--approval-policy','never','--','-']
        env={}
        if self.proxy_url:
            env.update(HTTP_PROXY=self.proxy_url,HTTPS_PROXY=self.proxy_url,ALL_PROXY=self.proxy_url,NO_PROXY='localhost,127.0.0.1,::1')
        return self._finalize(self._exec(args,workdir,ctx,stdin_data=prompt,env_overrides=env),ctx)

    def _parse(self, proc, workdir: Path, ctx: RunContext) -> Result:
        stdout = strip_ansi(proc.stdout or "")
        stderr = strip_ansi(proc.stderr or "")
        events=[];ledger=UsageLedger()
        for line in stdout.splitlines():
            try:event=json.loads(line)
            except ValueError:continue
            if isinstance(event,dict):events.append(event);ledger.observe(event)
        measured=ledger.snapshot()
        usage={**measured,'usage_unknown':not measured['usage_complete'],'accounting':'reported_total_includes_cache'}
        finals=[e for e in events if e.get('type')=='final']
        final=finals[-1] if finals else {}
        if proc.returncode != 0:
            detail=stderr+' '+json.dumps(final.get('reason',{}))
            kind = ErrorKind.AUTH if any(s in detail.lower() for s in ('auth','login','missing_credential')) \
                else ErrorKind.CRASH
            return Result(ok=False, exit_code=proc.returncode, channel=self.channel,
                          stdout=stdout, stderr=stderr, text=stdout[:MAX_TEXT],
                          tool_version=self._version, error_kind=kind,usage=usage)
        if not stdout.strip():
            # 退出 0 但无输出 => 空跑，引擎拒绝判完成
            return Result(ok=True, exit_code=0, channel=self.channel,
                          stdout=stdout, stderr=stderr, text="",
                          tool_version=self._version,usage=usage)
        if self._version != '0.2.2-rc.3':
            result=self._unknown_version(proc,'未经验证的 dsh 版本')
            # 兼容旧纯文本执行器的诊断归档：进程正常退出可生成报告，
            # parse_unknown 仍禁止引擎把它当成有效阶段或审批证据。
            if not events:
                result.ok=True
                result.text=stdout.strip()[:MAX_TEXT]
            result.usage=usage
            return result
        reason=final.get('reason')
        text=final.get('text')
        if (len(finals)!=1 or not isinstance(reason,dict) or reason.get('kind')!='completed'
                or not isinstance(text,str) or not text.strip()):
            return Result(ok=False,channel=self.channel,stdout=stdout,stderr=stderr or 'dsh 未返回唯一有效完成事件',
                          tool_version=self._version,error_kind=ErrorKind.PARSE_FAILED,parse_unknown=True,usage=usage)
        return Result(ok=True, exit_code=0, channel=self.channel,
                      stdout=stdout, stderr=stderr, text=text[:MAX_TEXT],
                      tool_version=self._version,usage=usage)
