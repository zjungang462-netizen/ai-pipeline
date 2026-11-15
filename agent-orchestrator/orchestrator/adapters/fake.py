"""离线 FakeAdapter：确定性产出，用于离线引擎测试与 demo（不依赖任何真实工具）。

行为可配置：
- fail_times: 前 N 次调用返回失败（测重试）。
- empty_run: 退出 0 但无产物（测空跑不得判完成）。
- 产出：code 阶段写一个源文件并交给引擎提交；report 阶段写报告文件。
"""
from __future__ import annotations

from pathlib import Path

from ..contracts import ErrorKind, PreflightReport, Result, RunContext
from .base import Adapter


class FakeAdapter(Adapter):
    channel = "fake"

    def __init__(self, name: str = "fake", fail_times: int = 0,
                 empty_run: bool = False, kind: str | None = None):
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
        if self.calls <= self.fail_times:
            return Result(ok=False, exit_code=1, error_kind=ErrorKind.CRASH,
                          channel=self.channel, stderr="模拟失败")
        if self.empty_run:
            # 退出 0 但什么都没干 => 引擎不得判完成
            return Result(ok=True, exit_code=0, channel=self.channel, text="")

        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
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
        src = workdir / f"{ctx.stage}_output.py"
        src.write_text(f'"""{ctx.stage} 产出"""\nTASK = {ctx.task!r}\nSTAGE = {ctx.stage!r}\n',
                       encoding="utf-8")
        return Result(ok=True, exit_code=0, channel=self.channel,
                      text=f"{ctx.stage} 完成", changed_files=[str(src)],
                      tool_version="fake-1.0")
