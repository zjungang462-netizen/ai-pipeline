"""Qwen Code CLI 适配器（命令 qwen，Apache-2.0 开源）。

命令模板：qwen -p "<prompt>" -m <model> --output-format json [--yolo]
- env: OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL（百炼兼容端点）。
- 这些凭据须配置显式开启（credential_env），默认不透传。
- --output-format json 输出结构化结果；未知版本 => parse_unknown。
"""
from __future__ import annotations

import json
from pathlib import Path

from ..contracts import ErrorKind, Result, RunContext
from .cli_base import CLIAdapter
from .cli_codex import strip_ansi

# 版本白名单（评审 M4）：只认带工具名的版本串，裸 "0." 不再放行。
# 真实 Qwen Code 当前为 0.26.x；fixture 须用真实抓包替换（见 tests/fixtures/README）。
KNOWN_VERSION_PREFIXES = ("qwen-code ", "qwen ")


class QwenAdapter(CLIAdapter):
    channel = "cli_qwen"
    exe_name = "qwen"
    version_args = ["--version"]
    # 需配置显式开启才透传（评审 M4）
    credential_env = []

    def __init__(self, name: str, exe: str | None = None,
                 extra_env: list[str] | None = None, timeout_s: int = 300,
                 model: str | None = None):
        super().__init__(name, exe, extra_env, timeout_s)
        self.model = model

    def _version_known(self) -> bool:
        v = (self._version or "").lower().strip()
        return any(v.startswith(p) for p in KNOWN_VERSION_PREFIXES)

    def run(self, prompt: str, workdir: Path, ctx: RunContext) -> Result:
        # 评审 M5：prompt 走 stdin，避免撞 Windows 命令行 32767 字符上限
        args = ["-p", "-", "--output-format", "json"]
        if self.model:
            args += ["-m", self.model]
        if ctx.allow_yolo:
            args.append("--yolo")
        return self._finalize(self._exec(args, workdir, ctx, stdin_data=prompt), ctx)

    def _parse(self, proc, workdir: Path, ctx: RunContext) -> Result:
        stdout = strip_ansi(proc.stdout or "")
        stderr = strip_ansi(proc.stderr or "")
        if proc.returncode != 0:
            kind = ErrorKind.AUTH if ("auth" in stderr.lower() or "api key" in stderr.lower()) \
                else ErrorKind.CRASH
            return Result(ok=False, exit_code=proc.returncode, channel=self.channel,
                          stdout=stdout, stderr=stderr, text=stdout,
                          tool_version=self._version, error_kind=kind)
        if not self._version_known():
            return self._unknown_version(proc, "未知 qwen 版本")
        text = self._extract_json_text(stdout)
        if text is None:
            return self._unknown_version(proc, "qwen JSON 解析失败")
        return Result(ok=True, exit_code=0, channel=self.channel,
                      stdout=stdout, stderr=stderr, text=text,
                      tool_version=self._version)

    @staticmethod
    def _extract_json_text(stdout: str) -> str | None:
        # 整体 JSON 或逐行 JSONL，取 result/text/response 字段
        candidates = [stdout] + stdout.splitlines()
        for chunk in candidates:
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
