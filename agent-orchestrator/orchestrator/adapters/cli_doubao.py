"""保留旧豆包输出的离线解析，实时执行等待真实 CLI 协议验证。"""
from __future__ import annotations

from pathlib import Path

from ..contracts import ErrorKind, PreflightReport, Result, RunContext
from .cli_base import CLIAdapter
from .cli_codex import strip_ansi

MAX_TEXT = 200_000


class DoubaoAdapter(CLIAdapter):
    channel = "cli_doubao"
    exe_name = "doubao"
    version_args = ["--version"]
    needs_login = True
    credential_env = []

    def preflight(self) -> PreflightReport:
        return PreflightReport(adapter=self.name, ok=False,
                               detail='缺少独立豆包 CLI 的真实协议样本')

    def run(self, prompt: str, workdir: Path, ctx: RunContext) -> Result:
        return Result(ok=False, exit_code=-1, channel=self.channel, parse_unknown=True,
                      error_kind=ErrorKind.PARSE_FAILED,
                      stderr='缺少独立豆包 CLI 的真实协议样本，没有发起模型请求')

    def _parse(self, proc, workdir: Path, ctx: RunContext) -> Result:
        stdout = strip_ansi(proc.stdout or "")
        stderr = strip_ansi(proc.stderr or "")
        if proc.returncode != 0:
            kind = ErrorKind.AUTH if ("login" in stderr.lower() or "登录" in stderr
                                      or "auth" in stderr.lower()) else ErrorKind.CRASH
            return Result(ok=False, exit_code=proc.returncode, channel=self.channel,
                          stdout=stdout, stderr=stderr, text=stdout[:MAX_TEXT],
                          tool_version=self._version, error_kind=kind)
        text = stdout.strip()
        return Result(ok=True, exit_code=0, channel=self.channel,
                      stdout=stdout, stderr=stderr, text=text[:MAX_TEXT],
                      tool_version=self._version)
