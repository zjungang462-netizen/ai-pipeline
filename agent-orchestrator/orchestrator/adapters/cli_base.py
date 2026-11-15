"""CLI 适配器基类（评审 M1/M2/M3/M4）。

- 子进程：env 白名单（不透传 API Key）、超时、捕获 stdout/stderr。
- preflight：绝对路径解析 + 版本探测 + 落盘。
- 版本绑定解析：解析器按 tool_version 分派；未知版本 => parse_unknown，禁止判完成。
- changed_files 由子类填充；空跑不得判完成（引擎兜底）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..contracts import ErrorKind, PreflightReport, Result, RunContext
from ..safety import build_subprocess_env, preflight_cli, resolve_exe
from .base import Adapter


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
              stdin_data: str | None = None) -> Result:
        exe = self._ensure_resolved()
        if not exe:
            return Result(ok=False, exit_code=-1, error_kind=ErrorKind.AUTH,
                          channel=self.channel,
                          stderr=f"未找到 {self.exe_name}（配置 exe 填绝对路径）")
        allow = list(self.credential_env) + list(self.extra_env)
        env = build_subprocess_env(extra_allow=allow)
        try:
            proc = subprocess.run(
                [exe, *args], cwd=str(workdir), env=env,
                input=stdin_data,
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=ctx.timeout_s or self.timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            return Result(ok=False, exit_code=-1, error_kind=ErrorKind.TIMEOUT,
                          channel=self.channel, tool_version=self._version,
                          stdout=_dec(e.stdout), stderr=_dec(e.stderr) or "超时")
        except OSError as e:
            return Result(ok=False, exit_code=-1, error_kind=ErrorKind.CRASH,
                          channel=self.channel, stderr=str(e))
        return self._parse(proc, workdir, ctx)

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
                report_dir = Path(ctx.runs_dir) / "reports"
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
