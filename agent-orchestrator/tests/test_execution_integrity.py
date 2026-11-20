"""执行事实、审批、恢复、并发和路径边界的行为回归。"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import subprocess
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from orchestrator import artifacts
from orchestrator.adapters.fake import FakeAdapter
from orchestrator.adapters.factory import build_all
from orchestrator.config import ConfigError, parse_config
from orchestrator.contracts import Result, StageStatus
from orchestrator.engine import Engine
from orchestrator.review import VerdictError, load_verdict
from orchestrator.safety import WriteRootGuard


class Reviewer(FakeAdapter):
    def __init__(self, verdict=None):
        super().__init__(kind="report")
        self.verdict = verdict
        self.prompts = []

    def run(self, prompt, workdir, ctx):
        self.prompts.append(prompt)
        result = super().run(prompt, workdir, ctx)
        if ctx.kind == "review" and self.verdict is not None:
            (ctx.runs_dir / "review_verdict.json").write_text(
                json.dumps(self.verdict), encoding="utf-8")
        return result


class ExecutionIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def config(self, stages=None, **kwargs):
        return parse_config({"task_workdir": "ws", "state_dir": ".s",
                             "stages": stages or [self.report()],
                             "adapters": {"fake": {"type": "fake"}}, **kwargs},
                            base_dir=self.base)

    @staticmethod
    def report(name="draft", **kwargs):
        return dict(name=name, adapter="fake", kind="report", prompt="p", **kwargs)

    def engine(self, cfg, adapter=None):
        with mock.patch("orchestrator.engine.artifacts.git_available", return_value=False):
            return Engine(cfg, {"fake": adapter or FakeAdapter(kind="report")}, "r", "task")

    def test_report_tamper_rejected_by_approval(self):
        cfg = self.config([self.report(approval=True)])
        engine = self.engine(cfg)
        self.assertEqual(engine.run()["draft"], "awaiting_approval")
        report = engine.state.current_attempt_dir("draft") / "draft_report.md"
        original = report.read_bytes()
        report.write_text("changed", encoding="utf-8")
        self.assertFalse(engine.approve("draft"))
        report.write_bytes(original)
        self.assertTrue(engine.approve("draft"))
        self.assertEqual(engine.run()["draft"], "done")

    def test_deleted_output_is_regenerated(self):
        cfg = self.config()
        engine = self.engine(cfg)
        engine.run()
        (engine.state.current_attempt_dir("draft") / "draft_report.md").unlink()
        adapter = FakeAdapter(kind="report")
        self.assertEqual(self.engine(cfg, adapter).run()["draft"], "done")
        self.assertEqual(adapter.calls, 1)

    def test_added_archived_report_cannot_change_downstream_context(self):
        cfg = self.config()
        engine = self.engine(cfg)
        engine.run()
        (engine.state.current_attempt_dir("draft") / "injected.md").write_text("unverified context", encoding="utf-8")
        adapter = FakeAdapter(kind="report")
        self.engine(cfg, adapter).run()
        self.assertEqual(adapter.calls, 1)

    def test_legacy_unverifiable_success_is_not_adopted(self):
        cfg = self.config()
        engine = self.engine(cfg)
        engine.run()
        (engine.state.current_attempt_dir("draft") / "manifest.json").unlink()
        adapter = FakeAdapter(kind="report")
        self.engine(cfg, adapter).run()
        self.assertEqual(adapter.calls, 1)

    def test_enabling_approval_reuses_valid_output_but_pauses(self):
        cfg = self.config()
        self.engine(cfg).run()
        cfg.stages[0].approval = True
        adapter = FakeAdapter(kind="report")
        engine = self.engine(cfg, adapter)
        self.assertEqual(engine.run()["draft"], "awaiting_approval")
        self.assertEqual(adapter.calls, 0)
        self.assertTrue(engine.approve("draft"))
        self.assertEqual(engine.run()["draft"], "done")

    def test_adapter_model_is_in_fingerprint(self):
        cfg = self.config()
        cfg.stages[0].adapter = "api"
        from orchestrator.config import AdapterConfig
        cfg.adapters = {"api": AdapterConfig("api", "openai_compat", {
            "base_url": "https://example.invalid", "model": "v1", "api_key_env": "REVIEW_TEST_KEY"})}
        def response(*args, **kwargs):
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": "body"}}]}).encode())
        with mock.patch.dict("os.environ", {"REVIEW_TEST_KEY": "dummy"}), \
                mock.patch("urllib.request.urlopen", side_effect=response) as http, \
                mock.patch("orchestrator.engine.artifacts.git_available", return_value=False):
            Engine(cfg, build_all(cfg.adapters, {}), "r", "task").run()
            cfg.adapters["api"].options["model"] = "v2"
            Engine(cfg, build_all(cfg.adapters, {}), "r", "task").run()
        self.assertEqual(http.call_count, 2)

    def test_standalone_review_fail_is_failed(self):
        cfg = self.config([dict(name="review", kind="review", adapter="fake", prompt="p")])
        engine = self.engine(cfg, Reviewer({"verdict": "fail", "issues": [
            {"file": "a.py", "problem": "bug", "expected": "fix"}]}))
        self.assertEqual(engine.run()["review"], "failed")
        decision = json.loads((engine.state.current_attempt_dir("review") / "decision.json").read_text(encoding="utf-8"))
        self.assertEqual(decision["verdict"], "fail")

    def test_new_review_cannot_reuse_old_pass(self):
        cfg = self.config([dict(name="review", kind="review", adapter="fake", prompt="p")])
        reviewer = Reviewer({"verdict": "pass", "issues": []})
        engine = self.engine(cfg, reviewer)
        self.assertEqual(engine.run()["review"], "done")
        reviewer.verdict = None
        self.assertEqual(engine.rerun_from("review")["review"], "awaiting_approval")

    def test_manual_approval_receipt_survives_lost_current(self):
        cfg = self.config([dict(name="review", kind="review", adapter="fake", prompt="p")])
        engine = self.engine(cfg, Reviewer())
        self.assertEqual(engine.run()["review"], "awaiting_approval")
        self.assertTrue(engine.approve("review"))
        state = engine.state.stage("review")
        state.status, state.current_attempt = StageStatus.INTERRUPTED, None
        engine.state.save()
        reviewer = Reviewer()
        self.assertEqual(self.engine(cfg, reviewer).run()["review"], "done")
        self.assertEqual(reviewer.calls, 0)

    def test_test_approval_and_budget_are_enforced(self):
        cfg = self.config([dict(name="testing", kind="test", approval=True,
                               test_cmd=[sys.executable, "-c", "print('ok')"])])
        engine = self.engine(cfg)
        self.assertEqual(engine.run()["testing"], "awaiting_approval")
        self.assertTrue(engine.approve("testing"))
        self.assertEqual(engine.run()["testing"], "done")
        cfg.run_budget.max_duration_s = 0
        cfg.stages[0].approval = False
        with mock.patch("orchestrator.testrun.run_test_cmd") as command:
            self.assertEqual(engine.rerun_from("testing")["testing"], "failed")
            command.assert_not_called()

    def test_changed_test_input_is_not_cached(self):
        cfg = self.config([dict(name="testing", kind="test", test_cmd=[sys.executable, "-c",
            "from pathlib import Path; assert Path('flag.txt').read_text()=='good'"])])
        cfg.task_workdir.mkdir()
        flag = cfg.task_workdir / "flag.txt"
        flag.write_text("good", encoding="utf-8")
        self.assertEqual(self.engine(cfg).run()["testing"], "done")
        flag.write_text("bad", encoding="utf-8")
        self.assertEqual(self.engine(cfg).run()["testing"], "failed")

    def test_test_round_limit_is_not_cut_by_review_guard(self):
        cfg = self.config([self.report("coding"), dict(name="testing", kind="test",
            test_cmd=[sys.executable, "-c", "raise SystemExit(1)"],
            feedback_to="coding", max_rounds=5)])
        engine = self.engine(cfg)
        self.assertEqual(engine.run()["testing"], "failed")
        self.assertEqual(engine.state.review_rounds["testing"], 5)
        self.assertNotIn("护栏", engine.state.stage("testing").error)

    def test_test_record_reaches_reviewer(self):
        cfg = self.config([dict(name="testing", kind="test",
            test_cmd=[sys.executable, "-c", "print('UNIQUE_TEST_PROOF')"]),
            dict(name="review", kind="review", adapter="fake", prompt="{upstream}")])
        reviewer = Reviewer({"verdict": "pass", "issues": []})
        self.assertEqual(self.engine(cfg, reviewer).run()["review"], "done")
        self.assertIn("UNIQUE_TEST_PROOF", reviewer.prompts[-1])

    def test_feedback_cannot_read_absolute_or_parent_paths(self):
        cfg = self.config()
        cfg.task_workdir.mkdir()
        (self.base / "outside.txt").write_text("PRIVATE_MARKER", encoding="utf-8")
        engine = self.engine(cfg)
        for name in ((self.base / "outside.txt").as_posix(), "../outside.txt", "C:/outside.txt"):
            self.assertNotIn("PRIVATE_MARKER", engine._rework_context(f"文件 `{name}` — bug"))
        (cfg.task_workdir / "中文.py").write_text("SAFE_MARKER", encoding="utf-8")
        self.assertIn("SAFE_MARKER", engine._rework_context("文件 `中文.py` — bug"))

    def test_invalid_issue_location_awaits_human(self):
        file = self.base / "verdict.json"
        file.write_text(json.dumps({"verdict": "fail", "issues": [
            {"file": "C:/outside.txt", "problem": "x"}]}), encoding="utf-8")
        with self.assertRaises(VerdictError):
            load_verdict(file, workdir=self.base)

    def test_parent_watch_is_opt_in(self):
        with mock.patch("orchestrator.safety.os.walk") as walk:
            self.assertEqual(WriteRootGuard([self.base]).snapshot(), {})
            walk.assert_not_called()

    def test_command_argv_boundaries_change_fingerprint(self):
        cfg = self.config([dict(name="testing", kind="test", test_cmd=["a b", "c"])])
        engine = self.engine(cfg)
        first = engine._inputs_hash(cfg.stages[0], 0)
        cfg.stages[0].test_cmd = ["a", "b c"]
        self.assertNotEqual(first, engine._inputs_hash(cfg.stages[0], 0))

    def test_concurrent_run_does_not_double_execute_or_lose_usage(self):
        entered, release = threading.Event(), threading.Event()
        class Blocking(FakeAdapter):
            def run(self, *args):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("测试同步超时")
                result = super().run(*args)
                result.usage = {"tokens": 7}
                return result
        cfg = self.config()
        first, second = Blocking(kind="report"), FakeAdapter(kind="report")
        engine1, engine2 = self.engine(cfg, first), self.engine(cfg, second)
        with ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(engine1.run)
            try:
                self.assertTrue(entered.wait(10))
                self.assertEqual(engine2.run()["draft"], "running")
                self.assertEqual(second.calls, 0)
            finally:
                release.set()
            self.assertEqual(task.result(timeout=10)["draft"], "done")
        self.assertEqual(engine2.run()["draft"], "done")
        self.assertEqual(engine2.state.usage_total["tokens"], 7)
        self.assertEqual(second.calls, 0)

    def test_config_rejects_invalid_limits_and_accepts_test_only(self):
        for patch in ({"timeout_s": 0}, {"max_attempts": False}, {"max_rounds": -1},
                      {"max_tokens": True}, {"max_duration_s": float("nan")}):
            with self.subTest(patch=patch), self.assertRaises(ConfigError):
                self.config([self.report(**patch)])
        cfg = parse_config({"task_workdir": "ws", "state_dir": ".s", "stages": [
            {"name": "testing", "kind": "verify", "test_cmd": [sys.executable, "-c", "pass"]}]},
            base_dir=self.base)
        self.assertEqual(cfg.stages[0].kind, "test")

    def test_real_two_processes_preserve_single_execution_and_usage(self):
        child = '''import json,sys,time
from pathlib import Path
from unittest import mock
from orchestrator.adapters.fake import FakeAdapter
from orchestrator.config import parse_config
from orchestrator.engine import Engine
base=Path(sys.argv[1])
class Blocking(FakeAdapter):
    def run(self,*args):
        (base/'entered').write_text('1')
        deadline=time.monotonic()+20
        while not (base/'release').exists():
            if time.monotonic()>deadline: raise RuntimeError('timeout')
            time.sleep(.02)
        result=super().run(*args)
        result.usage={'tokens':7}
        return result
cfg=parse_config({'task_workdir':'ws','state_dir':'.s','stages':[{'name':'draft','kind':'report','adapter':'fake','prompt':'p'}],'adapters':{'fake':{'type':'fake'}}},base_dir=base)
with mock.patch('orchestrator.engine.artifacts.git_available',return_value=False):
    engine=Engine(cfg,{'fake':Blocking(kind='report')},'r','task')
print(json.dumps(engine.run()),flush=True)
'''
        from orchestrator.safety import build_subprocess_env
        arguments = [sys.executable, "-X", "utf8", "-c", child, str(self.base)]
        cwd = Path(__file__).resolve().parents[1]
        first = subprocess.Popen(arguments, cwd=cwd, env=build_subprocess_env(),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 15
            while not (self.base / "entered").exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((self.base / "entered").exists())
            second = subprocess.run(arguments, cwd=cwd, env=build_subprocess_env(),
                                    capture_output=True, timeout=15, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(second.stdout)["draft"], "running")
            (self.base / "release").touch()
            output, error = first.communicate(timeout=15)
            self.assertEqual(first.returncode, 0, error)
            self.assertEqual(json.loads(output)["draft"], "done")
            saved = json.loads((self.base / ".s/runs/r/state.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["usage_total"]["tokens"], 7)
        finally:
            if first.poll() is None:
                first.kill()
            first.communicate(timeout=5)


@unittest.skipUnless(artifacts.git_available(), "需要 git")
class GitIntegrity(unittest.TestCase):
    def test_tests_run_at_upstream_commit_despite_dirty_main_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = parse_config({"task_workdir": "ws", "state_dir": ".s", "stages": [
                {"name": "coding", "kind": "code", "adapter": "fake", "prompt": "p"},
                {"name": "testing", "kind": "test", "test_cmd": [sys.executable, "-c",
                    "from pathlib import Path; assert Path('coding_output.py').read_text(encoding='utf8') != 'dirty'"]}],
                "adapters": {"fake": {"type": "fake"}}}, base_dir=Path(directory))
            engine = Engine(cfg, {"fake": FakeAdapter(kind="code")}, "r", "task")
            with mock.patch.object(artifacts, "tree_content_hash", wraps=artifacts.tree_content_hash) as tree_hash:
                self.assertEqual(engine._run_stage(cfg.stages[0], 0), StageStatus.DONE)
            tree_hash.assert_called_once_with(cfg.task_workdir, engine.state.stage("coding").commit_sha, git=engine.git)
            file = cfg.task_workdir / "coding_output.py"
            file.write_text("dirty", encoding="utf-8")
            self.assertEqual(engine._run_stage(cfg.stages[1], 1), StageStatus.DONE)
            self.assertEqual(file.read_text(encoding="utf-8"), "dirty")

    def test_declared_commit_preserves_unrelated_staging_and_unicode(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            artifacts.ensure_repo(repo)
            artifacts._git(["config", "core.quotepath", "true"], repo)
            (repo / "unrelated.txt").write_text("user staged", encoding="utf-8")
            artifacts._git(["add", "--", "unrelated.txt"], repo)
            (repo / "中文.py").write_text("print(1)", encoding="utf-8")
            changed = artifacts.changed_files_since(repo, artifacts.head_sha(repo))
            self.assertIn("中文.py", changed)
            commit = artifacts.commit_files(repo, ["中文.py"], "stage")
            names = artifacts._git(["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit], repo).stdout
            self.assertEqual(names.split("\0")[0], "中文.py")
            staged = artifacts._git(["diff", "--cached", "--name-only"], repo).stdout
            self.assertIn("unrelated.txt", staged)

    def test_worktree_failure_never_falls_back_to_user_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = parse_config({"task_workdir": "ws", "state_dir": ".s", "stages": [
                {"name": "coding", "kind": "code", "adapter": "fake", "prompt": "p", "max_attempts": 1}],
                "adapters": {"fake": {"type": "fake"}}}, base_dir=Path(directory))
            adapter = FakeAdapter(kind="code")
            engine = Engine(cfg, {"fake": adapter}, "r", "task")
            with mock.patch("orchestrator.artifacts.add_worktree", side_effect=artifacts.GitError("unavailable")):
                self.assertEqual(engine.run()["coding"], "failed")
            self.assertEqual(adapter.calls, 0)
            self.assertFalse((cfg.task_workdir / "coding_output.py").exists())


if __name__ == "__main__":
    unittest.main()
