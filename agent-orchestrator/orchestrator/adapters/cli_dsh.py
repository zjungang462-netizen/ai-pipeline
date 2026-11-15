"""DeepSeek Harness CLI 适配器（命令 dsh）。

命令模板：dsh --profile headless "<task>"
- headless：单次执行，打印最终答案后退出（纯文本）。
- 另有 --profile sdk(JSON-RPC stdio)/acp 供编排，留作 v2。
- 纯文本输出，做 ANSI 清理与超长截断保护。
"""
from __future__ import annotations

from pathlib import Path

from ..contracts import ErrorKind, Result, RunContext
from .cli_base import CLIAdapter
from .cli_codex import strip_ansi

MAX_TEXT = 200_000  # 超长输出截断保护


class DshAdapter(CLIAdapter):
    channel = "cli_dsh"
    exe_name = "dsh"
    version_args = ["--version"]
    credential_env = []

    def run(self, prompt: str, workdir: Path, ctx: RunContext) -> Result:
        # 任务文本走 stdin。`-` 是占位，避免把整段提示词放进命令行。
        args = ["--profile", "headless", "-"]
        return self._finalize(self._exec_prompt(args, prompt, workdir, ctx), ctx)

    def _parse(self, proc, workdir: Path, ctx: RunContext) -> Result:
        stdout = strip_ansi(proc.stdout or "")
        stderr = strip_ansi(proc.stderr or "")
        if proc.returncode != 0:
            kind = ErrorKind.AUTH if ("auth" in stderr.lower() or "login" in stderr.lower()) \
                else ErrorKind.CRASH
            return Result(ok=False, exit_code=proc.returncode, channel=self.channel,
                          stdout=stdout, stderr=stderr, text=stdout[:MAX_TEXT],
                          tool_version=self._version, error_kind=kind)
        text = stdout.strip()
        if not text:
            # 退出 0 但无输出 => 空跑，引擎拒绝判完成
            return Result(ok=True, exit_code=0, channel=self.channel,
                          stdout=stdout, stderr=stderr, text="",
                          tool_version=self._version)
        return Result(ok=True, exit_code=0, channel=self.channel,
                      stdout=stdout, stderr=stderr, text=text[:MAX_TEXT],
                      tool_version=self._version)
