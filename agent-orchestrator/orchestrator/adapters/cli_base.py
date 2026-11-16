"""CLI 适配器基类（评审 M1/M2/M3/M4）。

- 子进程：env 白名单（不透传 API Key）、超时、捕获 stdout/stderr。
- preflight：绝对路径解析 + 版本探测 + 落盘。
- 版本绑定解析：解析器按 tool_version 分派；未知版本 => parse_unknown，禁止判完成。
- changed_files 由子类填充；空跑不得判完成（引擎兜底）。
"""
from __future__ import annotations

import os
import codecs
import signal
import subprocess
import threading
import time
from pathlib import Path

from ..contracts import ErrorKind, PreflightReport, Result, RunContext
from ..safety import build_subprocess_env, preflight_cli, resolve_exe
from .base import Adapter
from ..cli_telemetry import CLITrace, counter, partial_usage
from ..output_tail import TextTail


def _merge_observed_usage(usage, observed):
    """完整流、尾部重放和版本解析取较大下界，不能相加或互相冲掉。"""
    merged = dict(usage)
    if counter(observed.get('known_tokens')) > counter(usage.get('tokens')):
        merged.update(observed)
    for key in ('tokens', 'known_tokens', 'cli_reported_tokens',
                'cache_read_tokens', 'completed_responses'):
        if key in usage or key in observed:
            merged[key] = max(counter(usage.get(key)), counter(observed.get(key)))
    merged['known_tokens'] = max(counter(merged.get('known_tokens')), counter(merged.get('tokens')))
    if usage.get('usage_unknown') or observed.get('usage_unknown'):
        merged['usage_unknown'] = True
    return merged


def run_cli_process(command, *, cwd, env, input, capture_output=True, text=True,
                    encoding='utf-8', errors='replace', timeout=None, trace=None):
    """超时时停止本次 CLI 的进程树，避免后台工具继续改写已经回收的 worktree。"""
    from ..process_tree import attach, close, terminate
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name != 'nt',
                               creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
    output_limit=4*1024*1024
    buffers={name:TextTail(output_limit) for name in ('stdout','stderr')}
    failures=[]
    def drain(pipe,name):
        decoder=codecs.getincrementaldecoder(encoding)(errors=errors)
        try:
            while chunk:=pipe.read1(4096):
                value=decoder.decode(chunk)
                buffers[name].append(value)
                if trace:trace.feed(name,value)
            value=decoder.decode(b'',final=True)
            if value:
                buffers[name].append(value)
                if trace:trace.feed(name,value)
        except Exception as error:
            failures.append(error)
        finally:pipe.close()
    def send():
        try:
            value=input if isinstance(input,bytes) else (input or '').encode(encoding)
            process.stdin.write(value)
            process.stdin.flush()
        except (BrokenPipeError,OSError):pass
        finally:process.stdin.close()
    readers=[threading.Thread(target=drain,args=(process.stdout,'stdout'),daemon=True),
             threading.Thread(target=drain,args=(process.stderr,'stderr'),daemon=True)]
    writer=threading.Thread(target=send,daemon=True)
    start=time.monotonic()
    stopped=''
    status='exited'
    try:
        try:
            attach(process)
        except OSError:
            process.kill()
            process.communicate()
            raise
        for thread in readers:thread.start()
        writer.start()
        while True:
            if failures:raise OSError('CLI 实时日志记录失败：'+str(failures[0]))
            if trace:
                stopped=trace.tick()
            if stopped or (timeout is not None and time.monotonic()-start>=timeout):
                stopped=stopped or 'call_timeout'
                status='stopped'
                break
            if process.poll() is not None and all(not t.is_alive() for t in readers):break
            time.sleep(.05)
        if stopped:
            if not terminate(process):
                if os.name == 'nt':
                    process.kill()
                else:
                    try:os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:pass
            process.wait(timeout=10)
        for thread in [*readers,writer]:thread.join(5)
        if any(t.is_alive() for t in readers):raise OSError('CLI 管道未关闭')
        if failures:raise OSError('CLI 实时日志记录失败：'+str(failures[0]))
        stdout,stderr=buffers['stdout'].getvalue(),buffers['stderr'].getvalue()
        if stopped:
            error=subprocess.TimeoutExpired(command,timeout,output=stdout,stderr=stderr)
            error.stop_reason=stopped
            raise error
        return subprocess.CompletedProcess(command,process.returncode,
            stdout if text else stdout.encode(encoding),stderr if text else stderr.encode(encoding))
    finally:
        if process.poll() is None:
            if not terminate(process):
                if os.name!='nt':
                    try:os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                else:process.kill()
            process.wait(timeout=10)
        close(process)
        for thread in [*readers,writer]:
            if thread.ident is not None:thread.join(2)
        if trace:trace.finish(status,stopped)


class CLIAdapter(Adapter):
    exe_name: str = ""
    version_args: list[str] = ["--version"]
    needs_login: bool = False
    # 该工具自身需要的凭据环境变量（须配置显式开启）；默认空 => 不透传任何 Key
    credential_env: list[str] = []

    def __init__(self, name: str, exe: str | None = None,
                 extra_env: list[str] | None = None, timeout_s: int = 300):
        self.name = name
        self.exe = exe or None
        self.extra_env = extra_env or []
        self.timeout_s = timeout_s
        self._resolved: str | None = None
        self._version: str | None = None

    # ---- preflight ----
    def preflight(self) -> PreflightReport:
        rep = preflight_cli(self.name, self.exe_name, self.exe,
                            version_args=self.version_args, needs_login=self.needs_login)
        self._resolved = rep.exe_path
        self._version = rep.tool_version
        return rep

    def _ensure_resolved(self) -> str | None:
        if self._resolved is None or self._version is None:
            self.preflight()
        return self._resolved

    # ---- 子进程 ----
    def _exec(self, args: list[str], workdir: Path, ctx: RunContext,
              stdin_data: str | None = None, env_overrides: dict[str, str] | None = None) -> Result:
        exe = self._ensure_resolved()
        if not exe:
            return Result(ok=False, exit_code=-1, error_kind=ErrorKind.AUTH,
                          channel=self.channel,
                          stderr=f"未找到 {self.exe_name}（配置 exe 填绝对路径）")
        allow = list(self.credential_env) + list(self.extra_env)
        env = build_subprocess_env(extra_allow=allow)
        env.update(env_overrides or {})
        secrets=[v for k,v in env.items() if any(label in k.upper() for label in ('API_KEY','AUTH_TOKEN','ACCESS_TOKEN','CODEX_KEY'))]
        trace=CLITrace(ctx,secrets,channel=self.channel)
        def scrub(value):
            value = _dec(value)
            for key, secret in env.items():
                if len(secret) >= 8 and any(label in key.upper() for label in ('API_KEY', 'AUTH_TOKEN', 'ACCESS_TOKEN', 'CODEX_KEY')):
                    value = value.replace(secret, '[REDACTED]')
            return value
        try:
            proc = run_cli_process(
                [exe, *args], cwd=str(workdir), env=env,
                input=stdin_data,
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=ctx.timeout_s or self.timeout_s,
                trace=trace,
            )
        except subprocess.TimeoutExpired as e:
            usage=_merge_observed_usage(partial_usage(scrub(e.stdout)),trace.ledger.snapshot())
            usage['usage_unknown']=True
            reason=getattr(e,'stop_reason','call_timeout')
            kind=ErrorKind.AUTH if reason=='authentication_required' else ErrorKind.REJECTED if reason in ('budget_limit','output_limit') else ErrorKind.TOOL_LIMIT if reason in ('tool_error_limit','repeated_read_limit') else ErrorKind.TIMEOUT
            return Result(ok=False, exit_code=-1, error_kind=kind,
                          channel=self.channel, tool_version=self._version,
                          stdout=scrub(e.stdout), stderr=(scrub(e.stderr)+'\nCLI 停止：'+reason).strip(),
                          usage=usage)
        except OSError as e:
            usage={**trace.ledger.snapshot(),'usage_unknown':True}
            return Result(ok=False, exit_code=-1, error_kind=ErrorKind.CRASH,
                          channel=self.channel, stderr=scrub(str(e)), usage=usage)
        finally:
            trace.finish('exited' if not trace.reason else 'stopped')
        proc.stdout, proc.stderr = scrub(proc.stdout), scrub(proc.stderr)
        result=self._parse(proc, workdir, ctx)
        # 最后一个无换行事件在 finish 才解析，护栏结论仍须覆盖退出码 0。
        if trace.reason:
            result.ok=False
            result.error_kind=(ErrorKind.TOOL_LIMIT if trace.reason in ('tool_error_limit','repeated_read_limit')
                               else ErrorKind.AUTH if trace.reason=='authentication_required'
                               else ErrorKind.REJECTED if trace.reason in ('budget_limit','output_limit')
                               else ErrorKind.TIMEOUT)
            result.stderr=(result.stderr+'\nCLI 停止：'+trace.reason).strip()
        if 'tokens' not in result.usage or (not result.ok and not trace.ledger.complete):
            result.usage=_merge_observed_usage(result.usage,partial_usage(proc.stdout))
        result.usage=_merge_observed_usage(result.usage,trace.ledger.snapshot())
        return result

    def _exec_prompt(self, args: list[str], prompt: str, workdir: Path,
                     ctx: RunContext) -> Result:
        """提示词只走 stdin。argv 以 -- 结束，避免超长命令行，也避免提示词被当成选项。"""
        if not args or args[-1] != "--":
            args = [*args, "--"]
        return self._exec(args, workdir, ctx, stdin_data=prompt)

    def _parse(self, proc: subprocess.CompletedProcess, workdir: Path,
               ctx: RunContext) -> Result:
        """子类实现版本绑定解析。基类提供通用兜底。"""
        ok = proc.returncode == 0
        return Result(
            ok=ok, exit_code=proc.returncode, channel=self.channel,
            stdout=proc.stdout or "", stderr=proc.stderr or "",
            text=(proc.stdout or "").strip(), tool_version=self._version,
            error_kind=ErrorKind.NONE if ok else ErrorKind.CRASH,
        )

    def _unknown_version(self, proc, reason: str) -> Result:
        """未知版本/解析失败 => parse_unknown，禁止判完成（评审 M2）。"""
        return Result(
            ok=False, exit_code=proc.returncode, channel=self.channel,
            stdout=proc.stdout or "", stderr=proc.stderr or "",
            text=(proc.stdout or "").strip(), tool_version=self._version,
            error_kind=ErrorKind.PARSE_FAILED, parse_unknown=True,
        )

    def _finalize(self, result: Result, ctx: RunContext) -> Result:
        """G5：按当前阶段类型补产物。report/review 阶段把输出写成报告文件，
        使 CLI 适配器也能满足 report/review 的完成判据（须有 report_files）。
        code 阶段不在此处理（由引擎 git 检测变更）。"""
        kind = getattr(ctx, "kind", "") or ""
        if result.ok and kind in ("report", "review") and not result.report_files:
            text = result.text or result.stdout or ""
            if text.strip():
                report_dir = ctx.attempt_dir or Path(ctx.runs_dir) / "reports"
                report_dir.mkdir(parents=True, exist_ok=True)
                report = report_dir / f"{ctx.stage}_report.md"
                report.write_text(text, encoding="utf-8")
                result.report_files = [report]
        if result.ok and kind == "review":
            from .openai_compat import OpenAICompatAdapter
            result.verdict_file = OpenAICompatAdapter._write_verdict_file(
                result.text, ctx.attempt_dir or Path(ctx.runs_dir))
        return result


def _dec(b) -> str:
    if b is None:
        return ""
    if isinstance(b, bytes):
        return b.decode("utf-8", errors="replace")
    return str(b)
