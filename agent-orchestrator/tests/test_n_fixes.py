"""第五轮 N1/N2/N3 修复回归。"""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from orchestrator import artifacts
from orchestrator.cli import _print_results, main


class TestN1ExitCodes(unittest.TestCase):
    """等待审批不是成功完成：CLI 退出码与 worker 对齐为 11。"""

    def test_awaiting_approval_returns_11(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = _print_results({"requirements": "done", "design": "awaiting_approval"})
        self.assertEqual(rc, 11)

    def test_all_done_returns_0(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = _print_results({"requirements": "done", "coding": "done"})
        self.assertEqual(rc, 0)

    def test_failed_returns_1(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = _print_results({"coding": "failed"})
        self.assertEqual(rc, 1)

    def test_failed_beats_awaiting(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = _print_results({"coding": "failed", "design": "awaiting_approval"})
        self.assertEqual(rc, 1)

    def test_cli_run_stops_at_approval_returns_11(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = tmp / "config.toml"
            cfg.write_text('''
task_workdir = "workspace"
state_dir = ".state"
[[stages]]
name = "design"
adapter = "fake"
prompt = "设计"
kind = "report"
approval = true
[adapters.fake]
type = "fake"
''', encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                rc = main(["run", str(cfg), "--task", "t", "--run-id", "n1"])
            self.assertEqual(rc, 11)


class TestN2PublicConstant(unittest.TestCase):
    def test_skip_dirs_is_public(self):
        self.assertTrue(hasattr(artifacts, "SKIP_DIRS"))
        self.assertIn("__pycache__", artifacts.SKIP_DIRS)
        self.assertIn(".git", artifacts.SKIP_DIRS)

    def test_no_private_skip_dirs_reference(self):
        # 引擎源码不应再引用私有 _SKIP_DIRS
        import orchestrator.engine as eng
        import inspect
        src = inspect.getsource(eng)
        self.assertNotIn("artifacts._SKIP_DIRS", src)


class TestN3NoCommitAll(unittest.TestCase):
    def test_commit_all_removed(self):
        self.assertFalse(hasattr(artifacts, "commit_all"))

    def test_commit_files_still_works(self):
        if not artifacts.git_available():
            self.skipTest("git 不可用")
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            artifacts.ensure_repo(repo)
            (repo / "a.py").write_text("x=1\n", encoding="utf-8")
            sha = artifacts.commit_files(repo, ["a.py"], "m")
            self.assertIsNotNone(sha)


if __name__ == "__main__":
    unittest.main()
