"""第 4 阶段：安全约束 + worktree 隔离测试（评审 M5/B3/补测4/补测8）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator import artifacts
from orchestrator.config import parse_config
from orchestrator.contracts import StageStatus
from orchestrator.engine import Engine
from orchestrator.safety import WriteRootGuard, WriteRootViolation
from orchestrator.adapters.fake import FakeAdapter


class TestWriteRoot(unittest.TestCase):
    def test_inside_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "ws"
            root.mkdir()
            guard = WriteRootGuard([root])
            guard.check(root / "a.py")  # 不抛

    def test_outside_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "ws"
            root.mkdir()
            guard = WriteRootGuard([root])
            with self.assertRaises(WriteRootViolation):
                guard.check(Path(d) / "outside.py")

    def test_git_internal_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "ws"
            (root / ".git").mkdir(parents=True)
            guard = WriteRootGuard([root])
            with self.assertRaises(WriteRootViolation):
                guard.check(root / ".git" / "config")


@unittest.skipUnless(artifacts.git_available(), "git 不可用")
class TestWorktree(unittest.TestCase):
    def test_downstream_sees_commit_not_uncommitted(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            artifacts.ensure_repo(repo)
            (repo / "a.py").write_text("v1", encoding="utf-8")
            sha = artifacts.commit_files(repo, ["a.py"], "上游提交")
            # 未提交改动
            (repo / "b.py").write_text("未提交", encoding="utf-8")
            # 下游 worktree 基于上游 commit
            wt = Path(d) / "downstream"
            artifacts.add_worktree(repo, sha, "down", wt)
            self.assertTrue((wt / "a.py").is_file())       # 见到上游 commit
            self.assertFalse((wt / "b.py").exists())        # 不见未提交改动
            artifacts.remove_worktree(repo, wt)

    def test_worktree_with_space_and_chinese_path(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "项目 仓库"
            artifacts.ensure_repo(repo)
            (repo / "源.py").write_text("内容", encoding="utf-8")
            sha = artifacts.commit_files(repo, ["源.py"], "中文提交")
            wt = Path(d) / "下游 工作树"
            artifacts.add_worktree(repo, sha, "分支x", wt)
            self.assertTrue((wt / "源.py").is_file())
            self.assertEqual((wt / "源.py").read_text(encoding="utf-8"), "内容")
            artifacts.remove_worktree(repo, wt)

    def test_engine_code_stage_uses_worktree_and_merges(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            raw = {"task_workdir": "workspace", "state_dir": ".state",
                   "stages": [
                       {"name": "coding", "adapter": "fake", "prompt": "编码", "kind": "code"},
                       {"name": "review", "adapter": "fake", "prompt": "审查", "kind": "report"}],
                   "adapters": {"fake": {"type": "fake"}}}
            cfg = parse_config(raw, base_dir=tmp)
            eng = Engine(cfg, {"fake": FakeAdapter()}, run_id="wt", task="demo")
            res = eng.run()
            self.assertEqual(res["coding"], StageStatus.DONE.value)
            # 代码已合并回主线工作区
            self.assertTrue((cfg.task_workdir / "coding_output.py").is_file())
            self.assertIsNotNone(eng.state.stage("coding").commit_sha)


if __name__ == "__main__":
    unittest.main()
