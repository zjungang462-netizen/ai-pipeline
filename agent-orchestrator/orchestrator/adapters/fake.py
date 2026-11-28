"""离线 FakeAdapter：确定性产出，用于离线引擎测试与 demo（不依赖任何真实工具）。

行为可配置：
- fail_times: 前 N 次调用返回失败（测重试）。
- empty_run: 退出 0 但无产物（测空跑不得判完成）。
- 产出：code 阶段写一个源文件并交给引擎提交；report 阶段写报告文件。
"""
from __future__ import annotations

import ast
import json
import math
import time
from pathlib import Path

from ..contracts import ErrorKind, PreflightReport, Result, RunContext
from .base import Adapter


class FakeAdapter(Adapter):
    channel = "fake"

    def __init__(self, name: str = "fake", fail_times: int = 0,
                 empty_run: bool = False, kind: str | None = None,
                 scenario: str | None = None, delay_s: float = 0):
        if scenario not in (None, "normalize_repair"):
            raise ValueError("Unknown fake scenario")
        if (isinstance(delay_s, bool) or not isinstance(delay_s, (int, float))
                or not math.isfinite(delay_s) or not 0 <= delay_s <= 5):
            raise ValueError("delay_s must be a finite number between 0 and 5 seconds")
        self.scenario = scenario
        self.delay_s = delay_s
        self.name = name
        self.fail_times = fail_times
        self.empty_run = empty_run
        self.kind = kind  # None => 按阶段名推断（coding=code，其余=report）
        self.calls = 0

    def _kind_for(self, stage: str) -> str:
        if self.kind is not None:
            return self.kind
        return "code" if stage == "coding" else "report"

    def preflight(self) -> PreflightReport:
        return PreflightReport(adapter=self.name, ok=True, tool_version="fake-1.0",
                               detail="离线假适配器")

    def run(self, prompt: str, workdir: Path, ctx: RunContext) -> Result:
        self.calls += 1
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.calls <= self.fail_times:
            return Result(ok=False, exit_code=1, error_kind=ErrorKind.CRASH,
                          channel=self.channel, stderr="模拟失败")
        if self.empty_run:
            # 退出 0 但什么都没干 => 引擎不得判完成
            return Result(ok=True, exit_code=0, channel=self.channel, text="")

        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        if self.scenario == "normalize_repair":
            return self._normalize_repair(workdir, ctx)
        if self._kind_for(ctx.stage) == "review":
            report_dir = Path(ctx.runs_dir) / "reports"
            report_dir.mkdir(parents=True, exist_ok=True)
            report = report_dir / f"{ctx.stage}_report.md"
            report.write_text(f"# {ctx.stage} 审查报告\n\n任务: {ctx.task}\n结论: pass\n",
                              encoding="utf-8")
            verdict_file = Path(ctx.runs_dir) / "review_verdict.json"
            verdict_file.write_text(json.dumps({"verdict": "pass", "issues": []}, indent=2),
                                    encoding="utf-8")
            return Result(ok=True, exit_code=0, channel=self.channel,
                          text=report.read_text(encoding="utf-8"),
                          report_files=[report], verdict_file=verdict_file, tool_version="fake-1.0")
        if self._kind_for(ctx.stage) == "report":
            # 报告类产物落在运行目录（git 之外），不进代码提交树（评审 B3）
            report_dir = Path(ctx.runs_dir) / "reports"
            report_dir.mkdir(parents=True, exist_ok=True)
            report = report_dir / f"{ctx.stage}_report.md"
            report.write_text(f"# {ctx.stage} 报告\n\n任务: {ctx.task}\n提示: {prompt[:80]}\n",
                              encoding="utf-8")
            return Result(ok=True, exit_code=0, channel=self.channel,
                          text=report.read_text(encoding="utf-8"),
                          report_files=[report], tool_version="fake-1.0")
        # code 阶段：写一个源文件（引擎负责 git 提交）
        # 带上运行号和调用序号：新一轮运行和返工轮都必须产生真实内容变化，才能通过“零净变更”判据。
        src = workdir / f"{ctx.stage}_output.py"
        src.write_text(f'"""{ctx.stage} 产出"""\nTASK = {ctx.task!r}\nSTAGE = {ctx.stage!r}\n'
                       f'RUN = {ctx.run_id!r}\nCALL = {self.calls}\n', encoding="utf-8")
        return Result(ok=True, exit_code=0, channel=self.channel,
                      text=f"{ctx.stage} 完成", changed_files=[str(src)],
                      tool_version="fake-1.0")

    def _normalize_repair(self, workdir: Path, ctx: RunContext) -> Result:
        label = "OFFLINE SCRIPTED DEMO — not model output"
        kind = ctx.kind or self._kind_for(ctx.stage)
        if kind == "code":
            # Engine persists feedback per run before rewinding coding, and restores it
            # on resume. A new run starts without it even in an already repaired workspace.
            repair = bool(ctx.feedback)
            attempt = int(ctx.attempt_dir.name) if ctx.attempt_dir else (2 if repair else 1)
            body = "'-'.join(value.lower().split())" if repair else "value"
            src = workdir / "coding_output.py"
            src.write_text(
                f'# {label}\nRUN = {ctx.run_id!r}\nATTEMPT = {attempt}\n\n'
                f'def normalize(value):\n    return {body}\n', encoding="utf-8")
            return Result(ok=True, exit_code=0, channel=self.channel,
                          text=f"{label}\nCoding attempt {attempt}: " + (
                              "repair from persisted test feedback." if repair else
                              "intentional identity implementation; real tests must reject it."),
                          changed_files=[str(src)], tool_version="fake-1.0")

        report_dir = Path(ctx.runs_dir) / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report = report_dir / f"{ctx.stage}_report.md"
        verdict_file = None
        if kind == "review":
            problem = self._normalize_review(workdir / "coding_output.py")
            verdict = {"verdict": "fail" if problem else "pass", "issues": [
                {"file": "coding_output.py", "problem": problem,
                 "expected": "Only literal run/attempt metadata and normalize(value) returning "
                             "'-'.join(value.lower().split())."}
            ] if problem else []}
            verdict_file = Path(ctx.runs_dir) / "review_verdict.json"
            verdict_file.write_text(json.dumps(verdict, indent=2), encoding="utf-8")
            detail = ("Scripted review (nonexecuting AST inspection)\n\n"
                      "Inspected coding_output.py independently of the coder's result. "
                      "Only literal RUN/ATTEMPT metadata and the exact normalize function "
                      "are accepted; extra statements, decorators and side effects are rejected.\n\n"
                      f"Verdict: {verdict['verdict'].upper()}. " + (problem or
                      "AST matches lower(), whitespace split(), and hyphen join(). "
                      "Empty input yields an empty string; tabs/newlines and mixed case are handled.") +
                      "\n\nThis is a constrained scripted check, not a model or general code review. "
                      "Real test records remain separate evidence. Human approval is still required.\n")
        elif ctx.stage == "requirements":
            detail = ("Requirements: normalize(value)\n\n"
                      "Input: a Python string. Output: lowercase words separated by single hyphens. "
                      "Discard leading/trailing whitespace and collapse all whitespace between words.\n\n"
                      "Acceptance examples:\n- ' A  B ' -> 'a-b'\n- '' -> ''\n"
                      "- tabs/newlines between words -> single hyphens\n- mixed case -> lowercase\n\n"
                      "Scope: modify only coding_output.py. Fixed tests belong to the pipeline, "
                      "not to the coder. Demonstrate one real test failure, feedback repair, "
                      "scripted review, then a human approval gate.\n")
        else:
            detail = ("Design: a pure string normalization function\n\n"
                      "Implement normalize(value) in coding_output.py with str.lower(), "
                      "str.split() without a separator, and '-'.join(...). No imports, I/O, "
                      "dependencies or generated tests are needed.\n\n"
                      "The initial scripted attempt deliberately returns its input unchanged. "
                      "The engine executes the fixed Python assertions and persists failure evidence. "
                      "The next coding attempt uses that feedback to select the corrected implementation, "
                      "including after process restart. RUN and ATTEMPT identify each artifact.\n\n"
                      "Verification: rerun the same assertions, independently inspect the AST, "
                      "then wait for human approval. Repair is bounded by the pipeline budget.\n")
        text = f"# {label}\n\n## {detail}"
        report.write_text(text, encoding="utf-8")
        return Result(ok=True, exit_code=0, channel=self.channel, text=text,
                      report_files=[report], verdict_file=verdict_file, tool_version="fake-1.0")

    @staticmethod
    def _normalize_review(source: Path) -> str:
        """Fail closed on anything outside this small demo's AST; never execute source."""
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError, ValueError, RecursionError) as error:
            return f"Cannot parse implementation: {type(error).__name__}."
        body = []
        seen = set()
        for node in tree.body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id in ("RUN", "ATTEMPT")
                    and node.targets[0].id not in seen
                    and isinstance(node.value, ast.Constant)
                    and type(node.value.value) in (str, int)):
                seen.add(node.targets[0].id)
            else:
                body.append(node)
        tree.body = body
        expected = ast.parse("def normalize(value):\n    return '-'.join(value.lower().split())\n")
        if ast.dump(tree) != ast.dump(expected):
            return "Implementation does not match the constrained normalization AST."
        return ""


from .spec import AdapterSpec, Capabilities, OptionSpec  # noqa: E402

SPEC = AdapterSpec(
    type="fake",
    factory=FakeAdapter,
    options=(
        OptionSpec("kind", str, None, "固定产出类型；缺省按阶段名推断", choices=("report", "code", "review")),
        OptionSpec("scenario", str, None, "显式离线脚本演示；缺省保留普通 fake 行为",
                   choices=("normalize_repair",)),
        OptionSpec("delay_s", float, 0, "每次调用的演示延迟，0 至 5 秒", min=0,
                   check=lambda value: "不得超过 5 秒" if value > 5 else None),
    ),
    capabilities=Capabilities(file_tools=True, usage_reporting="unknown"),
)
