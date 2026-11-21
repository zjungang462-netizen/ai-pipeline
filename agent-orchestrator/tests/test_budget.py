"""第 5 阶段：预算上限测试（评审 S3：超限 => rejected，不再重试）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.config import ConfigError, parse_config
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


class UnknownUsageAdapter(Adapter):
    """模拟终态不报告 token 的 CLI 适配器（如豆包 2.37.2）。"""
    channel = "cli_doubao"

    def __init__(self, name="doubao"):
        self.name = name
        self.calls = 0

    def preflight(self):
        return PreflightReport(adapter=self.name, ok=True)

    def run(self, prompt, workdir, ctx):
        self.calls += 1
        report_dir = Path(ctx.runs_dir) / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report = report_dir / f"{ctx.stage}_report.md"
        report.write_text(f"{ctx.stage} #{self.calls}", encoding="utf-8")
        return Result(ok=True, exit_code=0, channel=self.channel,
                      text=f"{ctx.stage} #{self.calls}", report_files=[report],
                      usage={"usage_unknown": True})


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

    def test_max_model_calls_blocks_before_third_call_without_counting_test_stage(self):
        import sys
        with tempfile.TemporaryDirectory() as d:
            raw = {
                "task_workdir": "ws", "state_dir": ".state",
                "run_budget": {"unlimited_tokens": True, "max_duration_s": 120, "max_model_calls": 2},
                "adapters": {"api": {"type": "fake"}},
                "stages": [
                    {"name": "s1", "adapter": "api", "prompt": "a", "kind": "report"},
                    {"name": "t1", "kind": "test", "test_cmd": [sys.executable, "-c", "print('ok')"]},
                    {"name": "s2", "adapter": "api", "prompt": "b", "kind": "report"},
                    {"name": "s3", "adapter": "api", "prompt": "c", "kind": "report"},
                ],
            }
            cfg = parse_config(raw, base_dir=Path(d))
            adapter = TokenAdapter(tokens=10)
            eng = Engine(cfg, {"api": adapter}, run_id="b-calls", task="t")
            res = eng.run()
            self.assertEqual(res["s1"], StageStatus.DONE.value)
            self.assertEqual(res["t1"], StageStatus.DONE.value)
            self.assertEqual(res["s2"], StageStatus.DONE.value)
            self.assertEqual(res["s3"], StageStatus.FAILED.value)
            self.assertIn("模型调用次数", eng.state.stage("s3").error)
            self.assertEqual(eng.state.usage_total["model_calls"], 2)

    def test_max_unknown_calls_allows_bounded_calls_then_blocks_before_exceeding(self):
        with tempfile.TemporaryDirectory() as d:
            raw = {
                "task_workdir": "ws", "state_dir": ".state",
                "run_budget": {"unlimited_tokens": True, "max_duration_s": 120, "max_unknown_calls": 2},
                "adapters": {"doubao": {"type": "fake"}},
                "stages": [
                    {"name": "s1", "adapter": "doubao", "prompt": "a", "kind": "report"},
                    {"name": "s2", "adapter": "doubao", "prompt": "b", "kind": "report"},
                    {"name": "s3", "adapter": "doubao", "prompt": "c", "kind": "report"},
                ],
            }
            cfg = parse_config(raw, base_dir=Path(d))
            adapter = UnknownUsageAdapter()
            eng = Engine(cfg, {"doubao": adapter}, run_id="b-unknown", task="t")
            res = eng.run()
            self.assertEqual(res["s1"], StageStatus.DONE.value)
            self.assertEqual(res["s2"], StageStatus.DONE.value)
            self.assertEqual(res["s3"], StageStatus.FAILED.value)
            self.assertIn("未知用量调用次数", eng.state.stage("s3").error)
            self.assertEqual(adapter.calls, 2)
            self.assertEqual(eng.state.usage_total["unknown_calls"], 2)
            import json
            from orchestrator.reporting import usage_summary
            metrics = json.loads((eng.state.run_dir / "metrics.json").read_text(encoding="utf-8"))
            self.assertEqual(metrics["run_budget"]["max_unknown_calls"], 2)
            self.assertIsNone(metrics["run_budget"]["max_model_calls"])
            summary = usage_summary("b-unknown", {}, metrics)
            self.assertEqual(summary["model_calls"], 2)

    def test_invalid_call_budgets_are_rejected_by_config(self):
        with tempfile.TemporaryDirectory() as d:
            for field in ("max_model_calls", "max_unknown_calls"):
                for bad in (0, -1, 1.5, True, "2"):
                    with self.subTest(field=field, bad=bad):
                        raw = {
                            "task_workdir": "ws", "state_dir": ".state",
                            "run_budget": {field: bad},
                            "adapters": {"api": {"type": "fake"}},
                            "stages": [{"name": "s1", "adapter": "api", "prompt": "a", "kind": "report"}],
                        }
                        with self.assertRaises(ConfigError):
                            parse_config(raw, base_dir=Path(d))


if __name__ == "__main__":
    unittest.main()
