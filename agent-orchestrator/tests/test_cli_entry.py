"""第 4 阶段：命令行入口冒烟测试（run/status/approve/resume/rerun-from）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.cli import main


def write_config(tmp: Path, approval=False) -> Path:
    cfg = tmp / "config.toml"
    cfg.write_text(f'''
task_workdir = "workspace"
state_dir = ".state"
[[stages]]
name = "requirements"
adapter = "fake"
prompt = "需求"
kind = "report"
[[stages]]
name = "design"
adapter = "fake"
prompt = "设计"
kind = "report"
approval = {str(approval).lower()}
[[stages]]
name = "coding"
adapter = "fake"
prompt = "编码"
kind = "code"
[adapters.fake]
type = "fake"
''', encoding="utf-8")
    return cfg


class TestCLIEntry(unittest.TestCase):
    def test_run_and_status(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = write_config(tmp)
            rc = main(["run", str(cfg), "--task", "demo", "--run-id", "t1"])
            self.assertEqual(rc, 0)
            self.assertTrue((tmp / ".state" / "runs" / "t1" / "preflight.json").is_file())
            rc = main(["status", str(cfg), "--run-id", "t1"])
            self.assertEqual(rc, 0)

    def test_approval_flow(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = write_config(tmp, approval=True)
            rc = main(["run", str(cfg), "--task", "demo", "--run-id", "t2"])
            self.assertEqual(rc, 0)
            # design 等待审批
            rc = main(["approve", str(cfg), "--run-id", "t2", "--stage", "design"])
            self.assertEqual(rc, 0)

    def test_resume_and_rerun(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = write_config(tmp)
            main(["run", str(cfg), "--task", "demo", "--run-id", "t3"])
            rc = main(["resume", str(cfg), "--run-id", "t3"])
            self.assertEqual(rc, 0)
            rc = main(["rerun-from", str(cfg), "--run-id", "t3", "--stage", "coding"])
            self.assertEqual(rc, 0)

    def test_missing_run(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = write_config(tmp)
            rc = main(["status", str(cfg), "--run-id", "nope"])
            self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
