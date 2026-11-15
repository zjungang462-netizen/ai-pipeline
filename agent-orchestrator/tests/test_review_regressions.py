"""审查阻断项回归测试（B2/B3/B4/M1）。

B2 锁占用 => 整轮中止，下游不跑。
B3 审批续跑 => 不重跑、不覆盖 pending 哈希。
B4 只打印文字 => code/report 均不得判完成。
M1 success.json 指纹匹配 => 采纳不重跑。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.config import parse_config
from orchestrator.contracts import (ErrorKind, PreflightReport, Result,
                                    RunContext, StageStatus)
from orchestrator.engine import Engine
from orchestrator.state import RunState, StageLock
from orchestrator.adapters.base import Adapter
from orchestrator.adapters.fake import FakeAdapter


def cfg_two(tmp: Path, approval=False):
    raw = {"task_workdir": "ws", "state_dir": ".s",
           "stages": [
               {"name": "s1", "adapter": "fake", "prompt": "p1", "kind": "report",
                "approval": approval},
               {"name": "s2", "adapter": "fake", "prompt": "p2", "kind": "report"}],
           "adapters": {"fake": {"type": "fake"}}}
    return parse_config(raw, base_dir=tmp)


class TestB2LockBusy(unittest.TestCase):
    def test_lock_busy_aborts_round(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = cfg_two(tmp)
            eng = Engine(cfg, {"fake": FakeAdapter()}, run_id="b2", task="t")
            eng.prepare()
            # 预置 s1 的锁 => s1 拿不到锁
            lock = StageLock(eng.state.stage_dir("s1") / ".lock")
            self.assertTrue(lock.acquire())
            try:
                res = eng.run()
            finally:
                lock.release()
            self.assertEqual(res.get("s1"), StageStatus.RUNNING.value)
            # 下游 s2 不得执行
            self.assertNotIn("s2", res)
            self.assertEqual(len(eng.state.stage("s2").attempts), 0)


class TestB3ApprovalResume(unittest.TestCase):
    def test_resume_does_not_overwrite_pending(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = cfg_two(tmp, approval=True)
            eng = Engine(cfg, {"fake": FakeAdapter()}, run_id="b3", task="t")
            res = eng.run()
            self.assertEqual(res["s1"], StageStatus.AWAITING_APPROVAL.value)
            pending_hash = eng.state.pending_approvals["s1"]["outputs_hash"]
            attempts_before = len(eng.state.stage("s1").attempts)
            # 续跑：不得重跑 s1，pending 哈希不变
            eng2 = Engine(cfg, {"fake": FakeAdapter()}, run_id="b3", task="t")
            res2 = eng2.run()
            self.assertEqual(res2["s1"], StageStatus.AWAITING_APPROVAL.value)
            self.assertEqual(eng2.state.pending_approvals["s1"]["outputs_hash"], pending_hash)
            self.assertEqual(len(eng2.state.stage("s1").attempts), attempts_before)
            # 批准后才放行
            self.assertTrue(eng2.approve("s1"))


class TextOnlyAdapter(Adapter):
    """退出 0 但只回文字、零文件产出（B4 复现）。"""
    channel = "fake"

    def __init__(self, name="fake"):
        self.name = name

    def preflight(self):
        return PreflightReport(adapter=self.name, ok=True)

    def run(self, prompt, workdir, ctx):
        return Result(ok=True, exit_code=0, channel=self.channel, text="已完成")


class TestB4TextOnly(unittest.TestCase):
    def _run(self, kind):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            raw = {"task_workdir": "ws", "state_dir": ".s",
                   "stages": [{"name": "st", "adapter": "fake", "prompt": "p", "kind": kind}],
                   "adapters": {"fake": {"type": "fake"}}}
            cfg = parse_config(raw, base_dir=tmp)
            eng = Engine(cfg, {"fake": TextOnlyAdapter()}, run_id="b4", task="t")
            return eng.run()

    def test_code_text_only_rejected(self):
        self.assertEqual(self._run("code")["st"], StageStatus.FAILED.value)

    def test_report_text_only_rejected(self):
        self.assertEqual(self._run("report")["st"], StageStatus.FAILED.value)


class TestM1Adopt(unittest.TestCase):
    def test_adopt_successful_no_rerun(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = cfg_two(tmp)
            eng = Engine(cfg, {"fake": FakeAdapter()}, run_id="m1", task="t")
            eng.run()
            attempts_before = len(eng.state.stage("s1").attempts)
            # 模拟 kill 点③：success.json 在，current 指针丢失
            rs = RunState(cfg.state_dir, "m1")
            rs.load()
            rs.stage("s1").current_attempt = None
            rs.stage("s1").status = StageStatus.INTERRUPTED
            rs.save()
            # 续跑：应采纳已成功 attempt，不新增 attempt
            eng2 = Engine(cfg, {"fake": FakeAdapter()}, run_id="m1", task="t")
            res = eng2.run()
            self.assertEqual(res["s1"], StageStatus.DONE.value)
            self.assertEqual(len(eng2.state.stage("s1").attempts), attempts_before)
            self.assertTrue(any(e["kind"] == "adopted" for e in eng2.state.events))


if __name__ == "__main__":
    unittest.main()
