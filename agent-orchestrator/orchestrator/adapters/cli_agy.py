"""Google Antigravity CLI 适配器（命令 agy）。

命令模板：agy -p "<prompt>" --output-format json [--dangerously-skip-permissions]
- 需先交互式登录一次（needs_login=True）；API-key 认证官方未核实。
- --output-format json 结构化输出；未知版本 => parse_unknown。
"""
from __future__ import annotations

import json
from pathlib import Path

from ..contracts import ErrorKind, Result, RunContext
from .cli_base import CLIAdapter
from .cli_codex import strip_ansi

KNOWN_VERSION_PREFIXES = ("agy ", "antigravity ")


class AgyAdapter(CLIAdapter):
    channel = "cli_agy"
    exe_name = "agy"
    version_args = ["--version"]
    needs_login = True
    credential_env = []

    def _version_known(self) -> bool:
        v = (self._version or "").lower().strip()
        return any(v.startswith(p) for p in KNOWN_VERSION_PREFIXES)

    def run(self, prompt: str, workdir: Path, ctx: RunContext) -> Result:
        args = ["-p", "-", "--output-format", "json"]
        if ctx.allow_yolo:
            args.append("--dangerously-skip-permissions")
        return self._finalize(self._exec_prompt(args, prompt, workdir, ctx), ctx)

    def _parse(self, proc, workdir: Path, ctx: RunContext) -> Result:
        stdout = strip_ansi(proc.stdout or "")
        stderr = strip_ansi(proc.stderr or "")
        if proc.returncode != 0:
            kind = ErrorKind.AUTH if ("auth" in stderr.lower() or "login" in stderr.lower()
                                      or "sign" in stderr.lower()) else ErrorKind.CRASH
            return Result(ok=False, exit_code=proc.returncode, channel=self.channel,
                          stdout=stdout, stderr=stderr, text=stdout,
                          tool_version=self._version, error_kind=kind)
        if not self._version_known():
            return self._unknown_version(proc, "未知 agy 版本")
        text = self._extract(stdout)
        if text is None:
            return self._unknown_version(proc, "agy JSON 解析失败")
        return Result(ok=True, exit_code=0, channel=self.channel,
                      stdout=stdout, stderr=stderr, text=text,
                      tool_version=self._version)

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
