"""第 1 阶段离线测试（零依赖，unittest）。

覆盖：配置校验、双哈希/stale、attempts 隔离/中断扫描、租约锁、
五阶段离线跑通、空跑拒绝、重试、审批哈希校验、rerun-from。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.config import ConfigError, parse_config
from orchestrator.contracts import ErrorKind, StageStatus
from orchestrator.engine import Engine
from orchestrator.state import (RunState, StageLock, compute_inputs_hash,
                                atomic_write_json)
from orchestrator.adapters.fake import FakeAdapter


def make_config(tmp: Path, stages=None, adapters=None):
    raw = {
        "task_workdir": "workspace",
        "state_dir": ".orchestrator",
        "stages": stages or [
            {"name": "requirements", "adapter": "fake", "prompt": "需求", "kind": "report"},
            {"name": "design", "adapter": "fake", "prompt": "设计", "kind": "report"},
            {"name": "coding", "adapter": "fake", "prompt": "编码", "kind": "code"},
            {"name": "testing", "adapter": "fake", "prompt": "测试", "kind": "report"},
            {"name": "review", "adapter": "fake", "prompt": "审查", "kind": "report"},
        ],
        "adapters": adapters or {"fake": {"type": "fake"}},
    }
    return parse_config(raw, base_dir=tmp)


def make_engine(cfg, task="demo", adapters=None, run_id="r1"):
    adapters = adapters or {name: FakeAdapter(name=name)
                            for name in {s.adapter for s in cfg.stages}}
    return Engine(cfg, adapters, run_id=run_id, task=task)


class TestConfig(unittest.TestCase):
    def test_valid(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_config(Path(d))
            self.assertEqual(len(cfg.stages), 5)

    def test_missing_adapter_ref(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ConfigError):
                make_config(Path(d), stages=[
                    {"name": "a", "adapter": "nope", "prompt": "x"}])

    def test_unknown_adapter_type(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ConfigError):
                make_config(Path(d), adapters={"fake": {"type": "bogus"}})

    def test_openai_requires_key_env(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ConfigError):
                make_config(Path(d), adapters={"api": {
                    "type": "openai_compat", "base_url": "http://x", "model": "m"}})

    def test_duplicate_stage(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ConfigError):
                make_config(Path(d), stages=[
                    {"name": "a", "adapter": "fake", "prompt": "x"},
                    {"name": "a", "adapter": "fake", "prompt": "y"}])


class TestState(unittest.TestCase):
    def test_inputs_hash_changes_with_prompt(self):
        h1 = compute_inputs_hash("p1", ["u1"], {})
        h2 = compute_inputs_hash("p2", ["u1"], {})
        self.assertNotEqual(h1, h2)

    def test_inputs_hash_changes_with_upstream(self):
        h1 = compute_inputs_hash("p", ["u1"], {})
        h2 = compute_inputs_hash("p", ["u2"], {})
        self.assertNotEqual(h1, h2)

    def test_atomic_write_and_load(self):
        with tempfile.TemporaryDirectory() as d:
            rs = RunState(Path(d), "r1")
            rs.stage("a").status = StageStatus.DONE
            rs.save()
            rs2 = RunState(Path(d), "r1")
            self.assertTrue(rs2.load())
            self.assertEqual(rs2.stage("a").status, StageStatus.DONE)

    def test_scan_interrupted(self):
        with tempfile.TemporaryDirectory() as d:
            rs = RunState(Path(d), "r1")
            n, adir = rs.new_attempt_dir("a")
            rs.stage("a").current_attempt = n
            rs.stage("a").status = StageStatus.DONE
            rs.save()
            # 无 success.json => interrupted
            rs2 = RunState(Path(d), "r1")
            rs2.load()
            interrupted = rs2.scan_interrupted()
            self.assertIn("a", interrupted)
            self.assertEqual(rs2.stage("a").status, StageStatus.INTERRUPTED)

    def test_lock_exclusive(self):
        with tempfile.TemporaryDirectory() as d:
            lp = Path(d) / ".lock"
            l1 = StageLock(lp)
            l2 = StageLock(lp)
            self.assertTrue(l1.acquire())
            self.assertFalse(l2.acquire())  # 双进程只允许一个写
            l1.release()
            self.assertTrue(l2.acquire())
            l2.release()


class TestEngine(unittest.TestCase):
    def test_five_stages_offline(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_config(Path(d))
            eng = make_engine(cfg)
            res = eng.run()
            self.assertEqual(len(res), 5)
            for name, status in res.items():
                self.assertEqual(status, StageStatus.DONE.value, f"{name}={status}")

    def test_resume_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_config(Path(d))
            eng = make_engine(cfg)
            eng.run()
            hashes1 = {n: eng.state.stage(n).outputs_hash for n in res_names(cfg)}
            attempts1 = {n: len(eng.state.stage(n).attempts) for n in res_names(cfg)}
            # 续跑：全部 skip_done，产物哈希不变，无新 attempt
            eng2 = make_engine(cfg)
            res2 = eng2.run()
            hashes2 = {n: eng2.state.stage(n).outputs_hash for n in res_names(cfg)}
            self.assertEqual(hashes1, hashes2)
            # 五阶段终态全 done，且每个阶段的 attempt 数未增加（没有重跑）
            for n in res_names(cfg):
                self.assertEqual(res2[n], StageStatus.DONE.value)
                self.assertEqual(len(eng2.state.stage(n).attempts), attempts1[n])

    def test_empty_run_not_complete(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_config(Path(d), stages=[
                {"name": "coding", "adapter": "fake", "prompt": "x", "kind": "code"}])
            eng = make_engine(cfg, adapters={"fake": FakeAdapter(empty_run=True)})
            res = eng.run()
            self.assertEqual(res["coding"], StageStatus.FAILED.value)

    def test_retry_then_success(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_config(Path(d), stages=[
                {"name": "coding", "adapter": "fake", "prompt": "x",
                 "kind": "code", "max_attempts": 3}])
            eng = make_engine(cfg, adapters={"fake": FakeAdapter(fail_times=2)})
            res = eng.run()
            self.assertEqual(res["coding"], StageStatus.DONE.value)

    def test_stale_propagation(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_config(Path(d))
            eng = make_engine(cfg)
            eng.run()
            # 改第一阶段 prompt => 下游应 stale
            cfg.stages[0].prompt = "需求-改"
            eng2 = make_engine(cfg)
            eng2.prepare()
            eng2._run_stage(cfg.stages[0], 0)
            statuses = [eng2.state.stage(s.name).status for s in cfg.stages[1:]]
            self.assertTrue(all(s == StageStatus.STALE for s in statuses))

    def test_approval_hash_check(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_config(Path(d), stages=[
                {"name": "design", "adapter": "fake", "prompt": "x",
                 "kind": "report", "approval": True}])
            eng = make_engine(cfg)
            res = eng.run()
            self.assertEqual(res["design"], StageStatus.AWAITING_APPROVAL.value)
            # 篡改产物哈希 => 审批拒绝
            eng.state.stage("design").outputs_hash = "tampered"
            eng.state.save()  # 审批在取得 run 锁后重新读取磁盘权威状态。
            self.assertFalse(eng.approve("design"))
            # 恢复正确哈希 => 通过
            eng.state.stage("design").outputs_hash = \
                eng.state.pending_approvals["design"]["outputs_hash"]
            eng.state.save()
            self.assertTrue(eng.approve("design"))

    def test_rerun_from(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_config(Path(d))
            eng = make_engine(cfg)
            eng.run()
            eng2 = make_engine(cfg)
            res = eng2.rerun_from("coding")
            self.assertEqual(res["coding"], StageStatus.DONE.value)


def res_names(cfg):
    return [s.name for s in cfg.stages]


if __name__ == "__main__":
    unittest.main()
