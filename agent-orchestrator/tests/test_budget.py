"""第 5 阶段：预算上限测试（评审 S3：超限 => rejected，不再重试）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.config import parse_config
from orchestrator.contracts import ErrorKind, Result, RunContext, StageStatus, PreflightReport
from orchestrator.engine import Engine
from orchestrator.adapters.base import Adapter


class TokenAdapter(Adapter):
    """每次返回固定 token 用量的假适配器。"""
    channel = "fake"

    def __init__(self, name="api", tokens=100):
        self.name = name
        self.tokens = tokens

    def preflight(self):
        return PreflightReport(adapter=self.name, ok=True)

    def run(self, prompt, workdir, ctx):
        report_dir = Path(ctx.runs_dir) / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report = report_dir / f"{ctx.stage}_report.md"
        report.write_text(f"{ctx.stage} 产出", encoding="utf-8")
        return Result(ok=True, exit_code=0, channel=self.channel,
                      text=f"{ctx.stage} 产出", report_files=[report],
                      usage={"tokens": self.tokens})


class TestBudget(unittest.TestCase):
    def _cfg(self, tmp, stage_max=None, run_max=None):
        stage = {"name": "requirements", "adapter": "api", "prompt": "x", "kind": "report"}
        if stage_max is not None:
            stage["max_tokens"] = stage_max
        raw = {"task_workdir": "ws", "state_dir": ".state",
               "stages": [stage], "adapters": {"api": {"type": "fake"}}}
        if run_max is not None:
            raw["run_budget"] = {"max_tokens": run_max}
        return parse_config(raw, base_dir=tmp)

    def test_stage_token_limit_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(Path(d), stage_max=50)
            eng = Engine(cfg, {"api": TokenAdapter(tokens=100)}, run_id="b1", task="t")
            res = eng.run()
            self.assertEqual(res["requirements"], StageStatus.FAILED.value)
            self.assertIn("超限", eng.state.stage("requirements").error)

    def test_run_token_limit_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(Path(d), run_max=50)
            eng = Engine(cfg, {"api": TokenAdapter(tokens=100)}, run_id="b2", task="t")
            res = eng.run()
            self.assertEqual(res["requirements"], StageStatus.FAILED.value)

    def test_within_budget_ok(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(Path(d), stage_max=200, run_max=1000)
            eng = Engine(cfg, {"api": TokenAdapter(tokens=100)}, run_id="b3", task="t")
            res = eng.run()
            self.assertEqual(res["requirements"], StageStatus.DONE.value)
            self.assertEqual(eng.state.usage_total["tokens"], 100)


if __name__ == "__main__":
    unittest.main()
