"""S1 崩溃恢复测试：三种 kill 点续跑后产物哈希集合一致（评审 S1/B4）。

kill 点：
①写产物中途（attempt 目录有半成品，无 success.json）
②临时文件已写、未改名（state.json.tmp 残留）
③attempt 完成但 current 未切换（success.json 在，current_attempt 未指）
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.config import parse_config
from orchestrator.contracts import StageStatus
from orchestrator.engine import Engine
from orchestrator.state import RunState, atomic_write_json
from orchestrator.adapters.fake import FakeAdapter


def make_cfg(tmp: Path):
    raw = {
        "task_workdir": "workspace", "state_dir": ".orchestrator",
        "stages": [
            {"name": "requirements", "adapter": "fake", "prompt": "需求", "kind": "report"},
            {"name": "coding", "adapter": "fake", "prompt": "编码", "kind": "code"},
            {"name": "review", "adapter": "fake", "prompt": "审查", "kind": "report"},
        ],
        "adapters": {"fake": {"type": "fake"}},
    }
    return parse_config(raw, base_dir=tmp)


def mk(cfg):
    return Engine(cfg, {"fake": FakeAdapter()}, run_id="r1", task="demo")


def hashes(eng):
    return {s.name: eng.state.stage(s.name).outputs_hash for s in eng.config.stages}


class TestCrashRecovery(unittest.TestCase):
    def _baseline(self, tmp):
        cfg = make_cfg(tmp)
        eng = mk(cfg)
        eng.run()
        return cfg, hashes(eng)

    def test_kill_mid_artifact(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg, base = self._baseline(tmp)
            # 模拟 coding 阶段写产物中途被 kill：新建一个无 success.json 的 attempt
            rs = RunState(cfg.state_dir, "r1")
            rs.load()
            n, adir = rs.new_attempt_dir("coding")
            (adir / "half.txt").write_text("半成品", encoding="utf-8")
            rs.stage("coding").current_attempt = n
            rs.stage("coding").status = StageStatus.RUNNING
            rs.save()
            # 续跑
            eng = mk(cfg)
            res = eng.run()
            self.assertEqual(res["coding"], StageStatus.DONE.value)
            self.assertEqual(hashes(eng), base)

    def test_kill_tmp_state_file(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg, base = self._baseline(tmp)
            # 模拟 state.json 临时文件已写未改名残留
            state_file = cfg.state_dir / "runs" / "r1" / "state.json"
            tmp_residue = state_file.with_name("state.json.tmp.deadbeef")
            tmp_residue.write_text('{"corrupt": true', encoding="utf-8")
            self.assertTrue(tmp_residue.exists())
            # 续跑：权威 state.json 完好，残留 tmp 不影响
            eng = mk(cfg)
            res = eng.run()
            for name in res:
                self.assertEqual(res[name], StageStatus.DONE.value)
            self.assertEqual(hashes(eng), base)

    def test_kill_success_not_committed(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg, base = self._baseline(tmp)
            rs = RunState(cfg.state_dir, "r1")
            rs.load()
            # 模拟 coding 的 current 指针丢失（success.json 在但 current 未指）
            st = rs.stage("coding")
            old_current = st.current_attempt
            st.current_attempt = None
            st.status = StageStatus.INTERRUPTED
            rs.save()
            eng = mk(cfg)
            res = eng.run()
            self.assertEqual(res["coding"], StageStatus.DONE.value)
            self.assertEqual(hashes(eng), base)


if __name__ == "__main__":
    unittest.main()
