"""豆包 CLI 适配器（字节，命令 doubao，可由 Agent 调用）。

命令模板：doubao -p "<prompt>" -C <dir> --permission-mode always_allow
- 认证：doubao login 扫码（无 API-key 方式，needs_login=True）。
- stdout 即结果（纯文本，无 JSON 选项）；做 ANSI 清理与超长截断。
"""
from __future__ import annotations

from pathlib import Path

from ..contracts import ErrorKind, Result, RunContext
from .cli_base import CLIAdapter
from .cli_codex import strip_ansi

MAX_TEXT = 200_000


class DoubaoAdapter(CLIAdapter):
    channel = "cli_doubao"
    exe_name = "doubao"
    version_args = ["--version"]
    needs_login = True
    credential_env = []

    def run(self, prompt: str, workdir: Path, ctx: RunContext) -> Result:
        # -p - 表示提示词来自 stdin，避免 Windows 命令行长度上限。
        args = ["-p", "-", "-C", str(workdir)]
        if ctx.allow_yolo:
            args += ["--permission-mode", "always_allow"]
        return self._finalize(self._exec_prompt(args, prompt, workdir, ctx), ctx)

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
