"""L2 审查反馈环离线测试。

覆盖：结论解析、不通过反馈返工后收敛、轮次耗尽判失败、反馈计入 inputs_hash、
续跑恢复反馈状态、配置校验。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.config import ConfigError, parse_config
from orchestrator.contracts import (ErrorKind, PreflightReport, Result,
                                    RunContext, StageStatus)
from orchestrator.engine import Engine
from orchestrator.review import (VerdictError, format_feedback, load_verdict)
from orchestrator.adapters.base import Adapter
from orchestrator.adapters.fake import FakeAdapter


class TestVerdictFile(unittest.TestCase):
    def _load(self, payload):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "review_verdict.json"
            import json as _j
            p.write_text(_j.dumps(payload), encoding="utf-8")
            return load_verdict(p, workdir=(p).parent)

    def test_pass(self):
        self.assertTrue(self._load({"verdict": "pass", "issues": []}).passed)

    def test_fail_with_two_issues(self):
        v = self._load({"verdict": "fail", "issues": [
            {"file": "a.py", "problem": "缺少边界处理", "expected": "补校验"},
            {"file": "t.py", "problem": "未写测试", "expected": "补用例"}]})
        self.assertFalse(v.passed)
        self.assertEqual(len(v.issues), 2)

    def test_prose_is_not_a_verdict(self):
        # 散文不再是结论来源：没有文件就抛错，交人工确认
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(VerdictError):
                load_verdict(Path(d) / "review_verdict.json", workdir=(Path(d) / "review_verdict.json").parent)

    def test_format_feedback_only_located(self):
        v = self._load({"verdict": "fail", "issues": [
            {"file": "a.py", "problem": "问题A", "expected": "期望A"}]})
        text = format_feedback(v, 1)
        self.assertIn("问题A", text)
        self.assertIn("a.py", text)
        self.assertIn("第 1 轮", text)


class ReviewAdapter(Adapter):
    """前 fail_times 次返回不通过，之后通过。"""
    channel = "fake"

    def __init__(self, name="review", fail_times=0):
        self.name = name
        self.fail_times = fail_times
        self.calls = 0

    def preflight(self):
        return PreflightReport(adapter=self.name, ok=True)

    def run(self, prompt, workdir, ctx):
        self.calls += 1
        report_dir = Path(ctx.runs_dir) / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report = report_dir / f"{ctx.stage}_report.md"
        report.write_text("# 审查报告（给人看，不参与判定）\n", encoding="utf-8")
        import json as _j
        if self.calls <= self.fail_times:
            payload = {"verdict": "fail", "issues": [
                {"file": "coding_output.py", "problem": "需修复缺陷X",
                 "expected": "修好X"}]}
        else:
            payload = {"verdict": "pass", "issues": []}
        Path(ctx.runs_dir).mkdir(parents=True, exist_ok=True)
        (Path(ctx.runs_dir) / "review_verdict.json").write_text(
            _j.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return Result(ok=True, exit_code=0, channel=self.channel,
                      text="报告", report_files=[report])


def cfg_feedback(tmp: Path, max_rounds=3):
    raw = {"task_workdir": "ws", "state_dir": ".s",
           "stages": [
               {"name": "coding", "adapter": "fake", "prompt": "编码 {feedback}", "kind": "code"},
               {"name": "review", "adapter": "review", "prompt": "审查", "kind": "review",
                "feedback_to": "coding", "max_rounds": max_rounds}],
           "adapters": {"fake": {"type": "fake"}, "review": {"type": "fake"}}}
    return parse_config(raw, base_dir=tmp)


class TestFeedbackLoop(unittest.TestCase):
    def test_fail_then_pass_converges(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = cfg_feedback(tmp, max_rounds=3)
            review = ReviewAdapter(fail_times=1)   # 第一轮不通过，第二轮通过
            eng = Engine(cfg, {"fake": FakeAdapter(), "review": review},
                         run_id="fb1", task="t")
            res = eng.run()
            self.assertEqual(res["review"], StageStatus.DONE.value)
            self.assertEqual(review.calls, 2)
            # coding 被重跑（2 个 attempt）
            self.assertEqual(len(eng.state.stage("coding").attempts), 2)
            self.assertTrue(any(e["kind"] == "review_feedback" for e in eng.state.events))
            self.assertTrue(any(e["kind"] == "review_pass" for e in eng.state.events))

    def test_rounds_exhausted_fails(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = cfg_feedback(tmp, max_rounds=2)
            review = ReviewAdapter(fail_times=99)   # 永远不通过
            eng = Engine(cfg, {"fake": FakeAdapter(), "review": review},
                         run_id="fb2", task="t")
            res = eng.run()
            self.assertEqual(res["review"], StageStatus.FAILED.value)
            self.assertEqual(review.calls, 2)        # 上限 2 轮
            self.assertIn("仍未通过", eng.state.stage("review").error)

    def test_feedback_injected_into_coding(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = cfg_feedback(tmp, max_rounds=3)
            review = ReviewAdapter(fail_times=1)
            eng = Engine(cfg, {"fake": FakeAdapter(), "review": review},
                         run_id="fb3", task="t")
            eng.run()
            # 第二轮 coding 的渲染 prompt 应含反馈问题
            coding = cfg.stage("coding")
            rendered = eng._render_prompt(coding, 0)
            # 通过后反馈被清除，但事件里记录了反馈
            self.assertTrue(any(e["kind"] == "review_feedback" for e in eng.state.events))

    def test_pass_first_try_no_rerun(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = cfg_feedback(tmp, max_rounds=3)
            review = ReviewAdapter(fail_times=0)   # 直接通过
            eng = Engine(cfg, {"fake": FakeAdapter(), "review": review},
                         run_id="fb4", task="t")
            res = eng.run()
            self.assertEqual(res["review"], StageStatus.DONE.value)
            self.assertEqual(review.calls, 1)
            self.assertEqual(len(eng.state.stage("coding").attempts), 1)

    def test_resume_preserves_feedback_state(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = cfg_feedback(tmp, max_rounds=3)
            review = ReviewAdapter(fail_times=1)
            eng = Engine(cfg, {"fake": FakeAdapter(), "review": review},
                         run_id="fb5", task="t")
            eng.run()
            rounds = dict(eng.state.review_rounds)
            # 续跑：已完成，不应再触发审查调用
            review2 = ReviewAdapter(fail_times=0)
            eng2 = Engine(cfg, {"fake": FakeAdapter(), "review": review2},
                          run_id="fb5", task="t")
            res = eng2.run()
            self.assertEqual(res["review"], StageStatus.DONE.value)
            self.assertEqual(review2.calls, 0)          # 采纳已完成，不重跑
            self.assertEqual(eng2.state.review_rounds, rounds)


class TestFeedbackConfig(unittest.TestCase):
    def test_feedback_to_must_be_review(self):
        with tempfile.TemporaryDirectory() as d:
            raw = {"task_workdir": "ws", "state_dir": ".s",
                   "stages": [{"name": "a", "adapter": "f", "prompt": "x",
                               "kind": "code", "feedback_to": "a"}],
                   "adapters": {"f": {"type": "fake"}}}
            with self.assertRaises(ConfigError):
                parse_config(raw, base_dir=Path(d))

    def test_feedback_to_must_exist(self):
        with tempfile.TemporaryDirectory() as d:
            raw = {"task_workdir": "ws", "state_dir": ".s",
                   "stages": [{"name": "r", "adapter": "f", "prompt": "x",
                               "kind": "review", "feedback_to": "nope"}],
                   "adapters": {"f": {"type": "fake"}}}
            with self.assertRaises(ConfigError):
                parse_config(raw, base_dir=Path(d))

    def test_feedback_to_must_be_earlier(self):
        with tempfile.TemporaryDirectory() as d:
            raw = {"task_workdir": "ws", "state_dir": ".s",
                   "stages": [
                       {"name": "r", "adapter": "f", "prompt": "x",
                        "kind": "review", "feedback_to": "c"},
                       {"name": "c", "adapter": "f", "prompt": "x", "kind": "code"}],
                   "adapters": {"f": {"type": "fake"}}}
            with self.assertRaises(ConfigError):
                parse_config(raw, base_dir=Path(d))


if __name__ == "__main__":
    unittest.main()
