"""第 4 阶段：安全约束 + worktree 隔离测试（评审 M5/B3/补测4/补测8）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_rerun_in_same_workspace_commits_on_first_attempt(self):
        # 同一工作区的第二次运行必须产出不同内容，否则零净变更会让编码阶段先失败一次。
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            raw = {"task_workdir": "workspace", "state_dir": ".state",
                   "stages": [{"name": "coding", "adapter": "fake", "prompt": "编码", "kind": "code"}],
                   "adapters": {"fake": {"type": "fake"}}}
            cfg = parse_config(raw, base_dir=tmp)
            Engine(cfg, {"fake": FakeAdapter()}, run_id="first", task="demo").run()
            second = Engine(cfg, {"fake": FakeAdapter()}, run_id="second", task="demo")
            res = second.run()
            self.assertEqual(res["coding"], StageStatus.DONE.value)
            self.assertEqual(second.state.stage("coding").current_attempt, 1)
            self.assertEqual([e for e in second.state.events if e["kind"] == "attempt_fail"], [])

    def test_archive_failure_reason_is_recorded(self):
        # 界面靠事件里的 reason 解释一次失败；归档失败以前只留下 error_kind=archive。
        class SameOutput(FakeAdapter):
            def run(self, prompt, workdir, ctx):
                result = super().run(prompt, workdir, ctx)
                (Path(workdir) / "coding_output.py").write_text("VALUE = 1\n", encoding="utf-8")
                return result
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            raw = {"task_workdir": "workspace", "state_dir": ".state",
                   "stages": [{"name": "coding", "adapter": "fake", "prompt": "编码",
                               "kind": "code", "max_attempts": 1}],
                   "adapters": {"fake": {"type": "fake"}}}
            cfg = parse_config(raw, base_dir=tmp)
            Engine(cfg, {"fake": SameOutput()}, run_id="first", task="demo").run()
            second = Engine(cfg, {"fake": SameOutput()}, run_id="second", task="demo")
            res = second.run()
            self.assertEqual(res["coding"], StageStatus.FAILED.value)
            failure = [e for e in second.state.events if e["kind"] == "attempt_fail"]
            self.assertEqual(len(failure), 1)
            self.assertEqual(failure[0]["error_kind"], "archive")
            self.assertIn("代码未发生变化", failure[0]["reason"])

    def test_transient_archive_error_does_not_persist_git_diagnostics(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = parse_config({"task_workdir": "workspace", "state_dir": ".state",
                "stages": [{"name": "coding", "kind": "code", "adapter": "fake",
                            "prompt": "code", "max_attempts": 2}],
                "adapters": {"fake": {"type": "fake"}}}, base_dir=Path(d))
            engine = Engine(cfg, {"fake": FakeAdapter()}, "archive-safe", "demo")
            from orchestrator.archive import ArchiveOperations
            original = ArchiveOperations.archive
            calls = 0
            secret = "private-filter-credential-123456789"
            def archive(instance, *args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise artifacts.GitError(f"git filter failed: {secret}")
                return original(instance, *args, **kwargs)
            with mock.patch.object(ArchiveOperations, "archive", archive):
                self.assertEqual(engine.run()["coding"], "done")
            failures = [e for e in engine.state.events if e["kind"] == "attempt_fail"]
            self.assertEqual(failures[0]["reason_code"], "archive_failed")
            for name in ("events.jsonl", "state.json"):
                self.assertNotIn(secret, (engine.state.run_dir / name).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
