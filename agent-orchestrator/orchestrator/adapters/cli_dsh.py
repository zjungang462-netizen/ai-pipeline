"""DeepSeek Harness 0.2.2-rc.3 headless JSON 协议；测试结论仍以实际命令为准。"""
from __future__ import annotations

from pathlib import Path
import json
from urllib.parse import urlsplit

from ..contracts import ErrorKind, Result, RunContext
from .cli_base import CLIAdapter
from .cli_codex import strip_ansi
from .cli_result_stream import jsonl_objects
from ..cli_telemetry import UsageLedger

MAX_TEXT = 200_000  # 超长输出截断保护
SUPPORTED_VERSION = '0.2.2-rc.3'


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
        return {'protocol':2,'version':SUPPORTED_VERSION,'permission_mode':self.permission_mode,'proxy_url':self.proxy_url}

    def _version_known(self) -> bool:
        return self._version == SUPPORTED_VERSION

    def preflight(self):
        return self.verified_preflight()

    def run(self, prompt: str, workdir: Path, ctx: RunContext) -> Result:
        if not self._ensure_resolved() or not self._version_known():
            return Result(ok=False, exit_code=-1, channel=self.channel, tool_version=self._version,
                          parse_unknown=True, error_kind=ErrorKind.PARSE_FAILED,
                          stderr='未确认已验证的 DSH 版本，没有发起模型请求')
        effective_mode = self.permission_mode if ctx.kind == 'code' else 'read-only'
        args=['--profile','headless','--json','--permission-mode',effective_mode,'--approval-policy','never','--','-']
        env={}
        if self.proxy_url:
            env.update(HTTP_PROXY=self.proxy_url,HTTPS_PROXY=self.proxy_url,ALL_PROXY=self.proxy_url,NO_PROXY='localhost,127.0.0.1,::1')
        return self._finalize(self._exec(args,workdir,ctx,stdin_data=prompt,env_overrides=env),ctx)

    def _parse(self, proc, workdir: Path, ctx: RunContext) -> Result:
        stdout = strip_ansi(proc.stdout or "")
        stderr = strip_ansi(proc.stderr or "")
        events = list(jsonl_objects(stdout))
        ledger = UsageLedger()
        for event in events:
            ledger.observe(event)
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
            return Result(ok=False, exit_code=0, channel=self.channel,
                          stdout=stdout, stderr=stderr or 'dsh 退出 0 但未返回协议输出', text="",
                          tool_version=self._version,error_kind=ErrorKind.PARSE_FAILED,
                          parse_unknown=True,usage=usage)
        if not self._version_known():
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


from .spec import COMMON_CLI, AdapterSpec, Capabilities, OptionSpec, proxy_option  # noqa: E402

SPEC = AdapterSpec(
    type="cli_dsh",
    factory=DshAdapter,
    options=COMMON_CLI + (
        OptionSpec("permission_mode", str, "read-only", "文件权限", choices=("read-only", "workspace-write")),
        proxy_option(),
    ),
    capabilities=Capabilities(file_tools=True),
    verified_versions=(SUPPORTED_VERSION,),
)
