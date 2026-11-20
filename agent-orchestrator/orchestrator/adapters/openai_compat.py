"""OpenAI 兼容 API 适配器（百炼/方舟/任何兼容端点，评审 M4）。

- 零依赖：urllib 发 HTTP。
- Key 只从环境变量读取（api_key_env 指定变量名），绝不写入配置/日志。
- 错误分类：auth(401/403) / timeout / crash / parse_failed。
- 报告类产物：把模型输出写成报告文件落盘。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import http.client
from pathlib import Path

from ..contracts import ErrorKind, PreflightReport, Result, RunContext
from .base import Adapter


class OpenAICompatAdapter(Adapter):
    channel = "api"

    def __init__(self, name: str, base_url: str, model: str, api_key_env: str,
                 kind: str = "report", temperature: float = 0.2,
                 max_tokens: int | None = None, timeout_s: int = 120,
                 reasoning_effort: str | None = None, thinking: str | None = None,
                 review_json_mode: bool = False):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key_env = api_key_env
        self.kind = kind
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        if reasoning_effort is not None and reasoning_effort not in ('none', 'low', 'medium', 'high', 'xhigh'):
            raise ValueError('reasoning_effort 非法')
        if thinking is not None and thinking not in ('enabled', 'disabled'):
            raise ValueError('thinking 必须为 enabled/disabled')
        self.reasoning_effort, self.thinking = reasoning_effort, thinking
        if type(review_json_mode) is not bool:raise ValueError('review_json_mode 必须为布尔值')
        self.review_json_mode = review_json_mode

    def _key(self) -> str | None:
        return os.environ.get(self.api_key_env)

    def preflight(self) -> PreflightReport:
        key = self._key()
        if not key:
            return PreflightReport(
                adapter=self.name, ok=False,
                detail=f"环境变量 {self.api_key_env} 未设置（API Key 缺失）",
                needs_login=True)
        return PreflightReport(
            adapter=self.name, ok=True, tool_version=self.model,
            detail=f"已读取 {self.api_key_env}，端点 {self.base_url}")

    def run(self, prompt: str, workdir: Path, ctx: RunContext) -> Result:
        key = self._key()
        if not key:
            return Result(ok=False, exit_code=-1, error_kind=ErrorKind.AUTH,
                          channel=self.channel,
                          stderr=f"环境变量 {self.api_key_env} 未设置")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你是软件开发流水线中的一个专业角色，输出简洁、可执行、严谨。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
        }
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens
        if self.reasoning_effort is not None:
            payload['reasoning_effort'] = self.reasoning_effort
        if self.thinking is not None:
            payload['thinking'] = {'type': self.thinking}
            if self.thinking == 'enabled': payload.pop('temperature', None)
        if self.review_json_mode and (getattr(ctx, 'kind', None) or self.kind) == 'review':
            payload['response_format'] = {'type': 'json_object'}
        if ctx.output_tokens is not None:
            payload['max_tokens'] = min(payload.get('max_tokens', ctx.output_tokens), ctx.output_tokens)
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {key}"},
            method="POST",
        )
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=min(self.timeout_s, ctx.timeout_s)) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            # 错误分类（评审 M6）：401/403→AUTH，429/5xx→RATE_LIMIT（可退避），其余 CRASH
            if e.code in (401, 403):
                kind = ErrorKind.AUTH
            elif e.code == 429 or e.code >= 500:
                kind = ErrorKind.RATE_LIMIT
            else:
                kind = ErrorKind.CRASH
            return Result(ok=False, exit_code=e.code, error_kind=kind,
                          channel=self.channel, stderr=f"HTTP {e.code}: {e.reason}",
                          usage={'duration_s': time.monotonic()-t0,
                                 'usage_unknown': e.code not in (400, 401, 403, 404, 429)})
        except http.client.HTTPException as e:
            partial = getattr(e, 'partial', b'')
            return Result(ok=False, exit_code=-1, error_kind=ErrorKind.TIMEOUT,
                          channel=self.channel, stderr=f'HTTP 响应不完整: {e}',
                          stdout=partial.decode('utf8', errors='replace') if isinstance(partial, bytes) else '',
                          usage={'usage_unknown': True, 'duration_s': time.monotonic()-t0})
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return Result(ok=False, exit_code=-1, error_kind=ErrorKind.TIMEOUT,
                          channel=self.channel, stderr=str(e),
                          usage={'usage_unknown': True, 'duration_s': time.monotonic()-t0})
        duration = time.monotonic() - t0

        try:
            data = json.loads(body)
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            return Result(ok=False, exit_code=0, error_kind=ErrorKind.PARSE_FAILED,
                          channel=self.channel, stdout=body, parse_unknown=True,
                          stderr=f"响应解析失败: {e}",
                          usage={'usage_unknown': True, 'duration_s': duration})

        usage = data.get("usage", {}) or {}
        result = Result(
            ok=True, exit_code=0, channel=self.channel, text=text,
            stdout=body, tool_version=self.model,
            usage={"tokens": usage.get("total_tokens", 0),
                   "prompt_tokens": usage.get("prompt_tokens", 0),
                   "completion_tokens": usage.get("completion_tokens", 0),
                   "duration_s": duration},
        )
        if 'total_tokens' not in usage:
            result.usage['usage_unknown'] = True
        if not isinstance(text, str):
            result.ok, result.parse_unknown = False, True
            result.error_kind = ErrorKind.PARSE_FAILED
            result.stderr = '响应 content 必须是字符串'
            return result
        if data['choices'][0].get('finish_reason') == 'length':
            result.ok, result.parse_unknown = False, True
            result.error_kind = ErrorKind.PARSE_FAILED
            result.stderr = '响应达到输出上限，请压缩为小补丁或只给出 JSON 结论'
            return result
        # 按当前阶段类型产出（评审 B4）：优先用 ctx.kind，兼容固定 kind
        # review 与 report 同为报告类产物（G5 同类：审查输出是结论不是代码块）
        try:
            kind = getattr(ctx, "kind", None) or self.kind
            if kind in ("report", "review"):
                report_dir = Path(ctx.runs_dir) / "reports"
                report_dir.mkdir(parents=True, exist_ok=True)
                report = report_dir / f"{ctx.stage}_report.md"
                report.write_text(text, encoding="utf-8")
                result.report_files = [report]
                if kind == "report" and ctx.allow_wait and ctx.attempt_dir is not None:
                    from ..stage_result import extract_deferred
                    result.deferred = extract_deferred(text, ctx.attempt_dir)
                if kind == "review":
                    # 结论文件独立落盘到 run 目录；Markdown 只给人看，不参与判定。
                    result.verdict_file = self._write_verdict_file(
                        text, ctx.attempt_dir or Path(ctx.runs_dir))
            else:
                # 代码类产物：从输出提取代码块写成文件（code 阶段须有文件产出）
                files = self._write_code_files(text, Path(workdir), ctx.stage)
                result.changed_files = files
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            # HTTP 调用已完成，产物协议失败也必须保留真实用量供引擎入账。
            result.ok = False
            result.error_kind = ErrorKind.PARSE_FAILED
            result.parse_unknown = True
            result.stderr = f"产物处理失败: {error}"
        return result

    @staticmethod
    def _write_verdict_file(text: str, runs_dir: Path) -> Path | None:
        """从输出里取出 review_verdict.json 代码块，写到 run 目录固定路径。

        只做「搬运」不做判断：内容是否合法由引擎 load_verdict 严格校验。
        找不到该文件名的代码块 => 不写，引擎按结论缺失置待人工确认。
        """
        import re
        blocks = re.findall(r"```[^\n]*\n(.*?)```", text, re.DOTALL)
        stripped = text.strip()
        if stripped.startswith('# file: review_verdict.json\n'):
            stripped = stripped.split('\n', 1)[1].strip()
        if not blocks and stripped.startswith('{'):
            # 裸 JSON 必须占满整个响应；不从说明文字中猜测结论。
            try:
                value = json.loads(stripped)
                if isinstance(value, dict) and 'verdict' in value:
                    blocks = [stripped]
            except ValueError:
                pass
        for block in blocks:
            lines = block.splitlines()
            if not lines:
                continue
            head = lines[0].strip()
            if head.startswith("# file:"):
                fname = head.split("# file:", 1)[1].strip().replace("\\", "/")
                if fname.rsplit("/", 1)[-1] != "review_verdict.json":
                    continue
                body = "\n".join(lines[1:]).strip()
            elif head.startswith("{") and '"verdict"' in block:
                body = block.strip()      # 无文件名标注但明显是结论对象
            else:
                continue
            runs_dir.mkdir(parents=True, exist_ok=True)
            dest = runs_dir / "review_verdict.json"
            dest.write_text(body + "\n", encoding="utf-8")
            return dest
        return None

    @staticmethod
    def _write_code_files(text: str, workdir: Path, stage: str) -> list[str]:
        """提取 ```lang ... ``` 代码块写盘。支持块首行 `# file: 路径` 指定文件名。

        无代码块时返回空（引擎按 B4 拒绝判完成）。
        """
        import re
        workdir.mkdir(parents=True, exist_ok=True)
        blocks = re.findall(r"```[^\n]*\n(.*?)```", text, re.DOTALL)
        if text.lstrip().startswith("# patch:"):
            blocks = []
            # 只有完整的显式补丁流才被接受；尾随散文和坏 JSON 整体拒绝。
            remaining = text.strip()
            while remaining:
                header, separator, body = remaining.partition("\n")
                if not separator or not header.strip().startswith("# patch:") or len(blocks) >= 32:
                    raise ValueError("非法补丁流边界")
                body = body.lstrip()
                value, end = json.JSONDecoder().raw_decode(body)
                blocks.append(header.strip() + "\n" + json.dumps(value, ensure_ascii=False))
                remaining = body[end:].strip()
        written: list[str] = []
        from ..safety import relative_target, WriteRootViolation
        # 回退名称不应覆盖同一响应后续显式指定的合法文件。
        explicit_targets: set[Path] = set()
        for block in blocks:
            first = block.splitlines()[0].strip() if block.splitlines() else ''
            if first.startswith(('# file:', '# patch:')):
                try:
                    explicit_targets.add(relative_target(workdir, first.split(':', 1)[1].strip()))
                except WriteRootViolation:
                    pass
        for i, block in enumerate(blocks):
            lines = block.splitlines()
            fname = None
            if lines and lines[0].strip().startswith("# patch:"):
                name = lines[0].split("# patch:", 1)[1].strip()
                dest = OpenAICompatAdapter._apply_text_patch(
                    workdir, name, "\n".join(lines[1:]))
                written.append(str(dest))
                continue
            if lines and lines[0].strip().startswith("# file:"):
                fname = lines[0].split("# file:", 1)[1].strip()
                lines = lines[1:]
            default_name = f"{stage}_{i + 1}.py"
            if not fname:
                fname = default_name
            try:
                dest = relative_target(workdir, fname)
            except WriteRootViolation:
                # 保留既有非法名称回退语义；回退本身仍必须通过链接和边界校验。
                dest = relative_target(workdir, default_name)
                used = {Path(path) for path in written}
                for suffix in range(65):
                    if dest not in explicit_targets and dest not in used and not dest.exists():
                        break
                    dest = relative_target(workdir, f'{stage}_{i + 1}_{suffix + 1}.py')
                else:
                    raise ValueError('安全回退文件名已被占用')
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
            written.append(str(dest))
        return written

    @staticmethod
    def _apply_text_patch(workdir: Path, name: str, payload: str) -> Path:
        """精确文本替换，不执行模型命令；整组匹配成功后才写入。"""
        from ..safety import relative_target
        dest = relative_target(workdir, name)
        if not dest.is_file() or dest.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("补丁目标必须是工作区内不超过 2 MiB 的现有文件")
        spec = json.loads(payload)
        if not isinstance(spec, dict) or set(spec) != {"edits"}:
            raise ValueError("补丁必须是仅包含 edits 的对象")
        edits = spec["edits"]
        if not isinstance(edits, list) or not 1 <= len(edits) <= 32:
            raise ValueError("edits 必须包含 1 到 32 项")
        content = dest.read_bytes().decode("utf-8")
        newline = "\r\n" if "\r\n" in content else "\n"
        for edit in edits:
            if (not isinstance(edit, dict) or set(edit) != {"old", "new"}
                    or not isinstance(edit["old"], str) or not edit["old"]
                    or not isinstance(edit["new"], str)):
                raise ValueError("每项补丁必须包含非空 old 和字符串 new")
            old = edit["old"].replace("\r\n", "\n").replace("\n", newline)
            new = edit["new"].replace("\r\n", "\n").replace("\n", newline)
            if content.count(old) != 1:
                raise ValueError("补丁 old 必须唯一匹配原文")
            content = content.replace(old, new, 1)
        dest.write_bytes(content.encode("utf-8"))
        return dest
