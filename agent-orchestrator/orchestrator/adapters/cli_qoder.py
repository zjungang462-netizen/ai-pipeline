"""Qoder CLI 适配器（阿里，命令 qoder）。

命令模板：qoder -p "<prompt>" --output-format json [--permission-mode accept_edits | --yolo]
- CI 认证：QODER_PERSONAL_ACCESS_TOKEN（须配置显式开启）。
- Quest 自主模式的无头调用官方未核实，此处用 -p 非交互。
- 未知版本 => parse_unknown。
"""
from __future__ import annotations

import json
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

    def _version_known(self) -> bool:
        v = (self._version or "").lower().strip()
        return any(v.startswith(p) for p in KNOWN_VERSION_PREFIXES)

    def run(self, prompt: str, workdir: Path, ctx: RunContext) -> Result:
        args = ["-p", "--output-format", "json"]
        if ctx.allow_yolo:
            args.append("--yolo")
        else:
            args += ["--permission-mode", "accept_edits"]
        return self._finalize(self._exec(args, workdir, ctx, stdin_data=prompt), ctx)

    def _parse(self, proc, workdir: Path, ctx: RunContext) -> Result:
        stdout = strip_ansi(proc.stdout or "")
        stderr = strip_ansi(proc.stderr or "")
        if proc.returncode != 0:
            kind = ErrorKind.AUTH if ("auth" in stderr.lower() or "token" in stderr.lower()
                                      or "login" in stderr.lower()) else ErrorKind.CRASH
            return Result(ok=False, exit_code=proc.returncode, channel=self.channel,
                          stdout=stdout, stderr=stderr, text=stdout,
                          tool_version=self._version, error_kind=kind)
        if not self._version_known():
            return self._unknown_version(proc, "未知 qoder 版本")
        text = self._extract(stdout)
        if text is None:
            return self._unknown_version(proc, "qoder JSON 解析失败")
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
