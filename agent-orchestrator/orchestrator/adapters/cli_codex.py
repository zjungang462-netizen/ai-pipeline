"""Codex CLI 适配器（评审 M2：版本绑定解析）。

命令模板：codex exec "<prompt>" -C <dir> --sandbox workspace-write
          --skip-git-repo-check --json -o <out>
- --json 输出 JSONL 事件流；解析最终 agent 消息。
- 已知版本走 JSONL 解析；未知版本 => parse_unknown（禁止判完成）。
- 输出含中文/ANSI/超长时不崩溃。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..contracts import ErrorKind, Result, RunContext
from .cli_base import CLIAdapter

# 已验证可解析的 codex 版本前缀（fixture 绑定这些版本）
KNOWN_VERSION_PREFIXES = ("codex-cli ", "codex ")

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def parse_codex_jsonl(stdout: str) -> tuple[str, bool]:
    """解析 codex --json 的 JSONL 事件流，返回 (最终文本, 是否解析成功)。

    兼容多种事件形态：item.completed / agent_message / 含 text 的消息。
    解析不到任何有效消息 => (原始文本, False)。
    """
    texts: list[str] = []
    saw_json = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        saw_json = True
        # 形态 1: {"type":"item.completed","item":{"type":"agent_message","text":...}}
        item = ev.get("item") or {}
        if isinstance(item, dict):
            t = item.get("text") or item.get("content")
            if isinstance(t, str) and t:
                texts.append(t)
                continue
        # 形态 2: {"type":"agent_message","text":...} / {"message":...}
        for key in ("text", "message", "content"):
            v = ev.get(key)
            if isinstance(v, str) and v:
                texts.append(v)
                break
    if texts:
        return "\n".join(texts), True
    return stdout, False


class CodexAdapter(CLIAdapter):
    channel = "cli_codex"
    exe_name = "codex"
    version_args = ["--version"]
    # CI 场景 codex 需要 CODEX_API_KEY；默认不透传，须配置显式开启
    credential_env = []

    def _version_known(self) -> bool:
        v = (self._version or "").lower().strip()
        return any(v.startswith(p) for p in KNOWN_VERSION_PREFIXES)

    def run(self, prompt: str, workdir: Path, ctx: RunContext) -> Result:
        out_file = Path(ctx.runs_dir) / "codex_last_message.txt"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        # 提示词走 stdin。codex exec 在没有位置参数时从 stdin 读取。
        args = ["exec", "-C", str(workdir),
                "--sandbox", "workspace-write", "--skip-git-repo-check",
                "--json", "-o", str(out_file)]
        if ctx.allow_yolo:
            args.append("--yolo")
        res = self._exec_prompt(args, prompt, workdir, ctx)
        return self._finalize(res, ctx)

    def _parse(self, proc, workdir: Path, ctx: RunContext) -> Result:
        stdout = strip_ansi(proc.stdout or "")
        stderr = strip_ansi(proc.stderr or "")
        if proc.returncode != 0:
            kind = ErrorKind.AUTH if ("auth" in stderr.lower() or "login" in stderr.lower()) \
                else ErrorKind.CRASH
            return Result(ok=False, exit_code=proc.returncode, channel=self.channel,
                          stdout=stdout, stderr=stderr, text=stdout,
                          tool_version=self._version, error_kind=kind)
        # 版本绑定：未知版本不默认走老解析器（评审 M2）
        if not self._version_known():
            return self._unknown_version(proc, "未知 codex 版本，需人工确认输出格式")
        text, ok = parse_codex_jsonl(stdout)
        if not ok:
            return self._unknown_version(proc, "JSONL 解析未得到有效消息")
        # changed_files 由引擎 git 检测填充；此处给出文本产物
        return Result(ok=True, exit_code=0, channel=self.channel,
                      stdout=stdout, stderr=stderr, text=text,
                      tool_version=self._version)
