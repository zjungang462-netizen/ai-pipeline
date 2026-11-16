"""第八轮：审查失败续跑、预算入账、越界改写、hook、命令行提示词。"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import artifacts
from orchestrator.adapters.base import Adapter
from orchestrator.adapters.cli_codex import CodexAdapter
from orchestrator.adapters.cli_doubao import DoubaoAdapter
from orchestrator.adapters.cli_dsh import DshAdapter
from orchestrator.adapters.fake import FakeAdapter
from orchestrator.adapters.openai_compat import OpenAICompatAdapter
from orchestrator.cli import main
from orchestrator.config import parse_config
from orchestrator.contracts import PreflightReport, Result, RunContext, StageStatus
from orchestrator.engine import Engine
from orchestrator.review import VerdictError, load_verdict
from orchestrator.safety import WriteRootGuard
from orchestrator.state import StageLock


class AlwaysFailReview(Adapter):
    channel = "fake"

    def __init__(self):
        self.name = "review"
        self.calls = 0

    def preflight(self):
        return PreflightReport(adapter=self.name, ok=True)

    def run(self, prompt, workdir, ctx):
        self.calls += 1
        report_dir = Path(ctx.runs_dir) / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report = report_dir / "review_report.md"
        report.write_text("# 审查报告\n\n存在缺陷未修。\n", encoding="utf-8")
        # 结论走独立文件（带文件位置，才可自动返工）
        Path(ctx.runs_dir).mkdir(parents=True, exist_ok=True)
        (Path(ctx.runs_dir) / "review_verdict.json").write_text(
            json.dumps({"verdict": "fail", "issues": [
                {"file": "a.py", "problem": "缺陷未修", "expected": "修复"}]}),
            encoding="utf-8")
        return Result(ok=True, exit_code=0, channel=self.channel, text="报告",
                      report_files=[report], usage={"tokens": 10, "duration_s": 0.1})


def _review_cfg(tmp: Path, max_rounds: int = 1):
    raw = {"task_workdir": "ws", "state_dir": ".s",
           "stages": [
               {"name": "coding", "adapter": "fake", "prompt": "x", "kind": "code"},
               {"name": "review", "adapter": "review", "prompt": "x", "kind": "review",
                "feedback_to": "coding", "max_rounds": max_rounds}],
           "adapters": {"fake": {"type": "fake"}, "review": {"type": "fake"}}}
    return parse_config(raw, base_dir=tmp)


class TestReviewResumeKeepsFailure(unittest.TestCase):
    def test_resume_does_not_adopt_exhausted_review(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _review_cfg(Path(d), max_rounds=1)
            review = AlwaysFailReview()
            eng = Engine(cfg, {"fake": FakeAdapter(), "review": review}, run_id="rv", task="t")
            res = eng.run()
            self.assertEqual(res["review"], StageStatus.FAILED.value)
            self.assertIn("仍未通过", eng.state.stage("review").error)
            review2 = AlwaysFailReview()
            eng2 = Engine(cfg, {"fake": FakeAdapter(), "review": review2}, run_id="rv", task="t")
            res2 = eng2.run()
            self.assertEqual(res2["review"], StageStatus.FAILED.value)
            self.assertEqual(review2.calls, 0)

    def test_legacy_success_without_verdict_stays_failed(self):
        """旧 success.json 没有 verdict 字段时，失败状态本身就够拦住续跑。"""
        with tempfile.TemporaryDirectory() as d:
            cfg = _review_cfg(Path(d), max_rounds=1)
            eng = Engine(cfg, {"fake": FakeAdapter(), "review": AlwaysFailReview()},
                         run_id="lg", task="t")
            eng.run()
            attempt = eng.state.current_attempt_dir("review")
            sj = attempt / "success.json"
            data = json.loads(sj.read_text(encoding="utf-8"))
            data.pop("verdict", None)
            data.pop("error", None)
            sj.write_text(json.dumps(data), encoding="utf-8")
            review2 = AlwaysFailReview()
            eng2 = Engine(cfg, {"fake": FakeAdapter(), "review": review2}, run_id="lg", task="t")
            res = eng2.run()
            self.assertEqual(res["review"], StageStatus.FAILED.value)
            self.assertEqual(review2.calls, 0)


class TestBudgetAccounted(unittest.TestCase):
    def test_over_budget_is_recorded_and_resume_does_not_call(self):
        class Spend(Adapter):
            channel = "fake"

            def __init__(self):
                self.name = "fake"
                self.calls = 0

            def preflight(self):
                return PreflightReport(adapter=self.name, ok=True)

            def run(self, prompt, workdir, ctx):
                self.calls += 1
                rd = Path(ctx.runs_dir) / "reports"
                rd.mkdir(parents=True, exist_ok=True)
                rp = rd / "requirements_report.md"
                rp.write_text("body\n", encoding="utf-8")
                return Result(ok=True, exit_code=0, channel=self.channel, text="body",
                              report_files=[rp], usage={"tokens": 5000, "duration_s": 1})

        with tempfile.TemporaryDirectory() as d:
            raw = {"task_workdir": "ws", "state_dir": ".s",
                   "run_budget": {"max_tokens": 100},
                   "stages": [{"name": "requirements", "adapter": "fake",
                               "prompt": "short", "kind": "report"}],
                   "adapters": {"fake": {"type": "fake"}}}
            cfg = parse_config(raw, base_dir=Path(d))
            ad = Spend()
            eng = Engine(cfg, {"fake": ad}, run_id="bd", task="t")
            res = eng.run()
            self.assertEqual(res["requirements"], StageStatus.FAILED.value)
            self.assertEqual(ad.calls, 1)
            self.assertEqual(eng.state.usage_total["tokens"], 5000)
            ad2 = Spend()
            eng2 = Engine(cfg, {"fake": ad2}, run_id="bd", task="t")
            res2 = eng2.run()
            self.assertEqual(res2["requirements"], StageStatus.FAILED.value)
            self.assertEqual(ad2.calls, 0)
            self.assertEqual(eng2.state.usage_total["tokens"], 5000)


class TestOutsideWrite(unittest.TestCase):
    def test_hidden_dir_and_modification(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            root = tmp / "ws"
            root.mkdir()
            guard = WriteRootGuard([root], watch_roots=[tmp])
            existing = tmp / "existing.txt"
            existing.write_text("old", encoding="utf-8")
            before = guard.snapshot()
            existing.write_text("overwritten-content", encoding="utf-8")
            hidden = tmp / ".hidden"
            hidden.mkdir()
            escaped = hidden / "escaped.txt"
            escaped.write_text("x", encoding="utf-8")
            (tmp / "new.txt").write_text("n", encoding="utf-8")
            after = guard.snapshot()
            detected = guard.diff_outside(before, after)
            self.assertIn(str(existing.resolve()), detected)
            self.assertIn(str(escaped.resolve()), after)
            self.assertIn(str(escaped.resolve()), detected)
            self.assertIn(str((tmp / "new.txt").resolve()), detected)

    def test_inside_files_do_not_crowd_out_outside(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            root = tmp / "ws"
            root.mkdir()
            for i in range(30):
                (root / f"f{i}.txt").write_text("inside", encoding="utf-8")
            outside = tmp / "escaped.txt"
            outside.write_text("out", encoding="utf-8")
            guard = WriteRootGuard([root], watch_roots=[tmp])
            snap = guard.snapshot(max_files=1)
            self.assertIn(str(outside.resolve()), snap)

    def test_modify_outside_fails_stage(self):
        class Touch(Adapter):
            channel = "fake"

            def __init__(self):
                self.name = "fake"

            def preflight(self):
                return PreflightReport(adapter=self.name, ok=True)

            def run(self, prompt, workdir, ctx):
                target = Path(workdir).parent / "existing.txt"
                target.write_text("overwritten-content", encoding="utf-8")
                rd = Path(ctx.runs_dir) / "reports"
                rd.mkdir(parents=True, exist_ok=True)
                rp = rd / "requirements_report.md"
                rp.write_text("ok\n", encoding="utf-8")
                return Result(ok=True, exit_code=0, channel=self.channel, text="ok",
                              report_files=[rp])

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            raw = {"task_workdir": "ws", "state_dir": ".s", "watch_root": ".", "watch_enforce": True,
                   "stages": [{"name": "requirements", "adapter": "fake",
                               "prompt": "x", "kind": "report", "max_attempts": 2}],
                   "adapters": {"fake": {"type": "fake"}}}
            cfg = parse_config(raw, base_dir=tmp)
            sibling = cfg.task_workdir.parent / "existing.txt"
            sibling.write_text("old", encoding="utf-8")
            eng = Engine(cfg, {"fake": Touch()}, run_id="mo", task="t")
            res = eng.run()
            self.assertEqual(res["requirements"], StageStatus.FAILED.value)
            self.assertIn("可写根外", eng.state.stage("requirements").error)
            self.assertEqual(sibling.read_text(encoding="utf-8"), "overwritten-content")

    def test_new_outside_file_is_preserved_and_not_retried(self):
        class Escape(Adapter):
            channel = "fake"

            def __init__(self):
                self.name = "fake"
                self.calls = 0

            def preflight(self):
                return PreflightReport(adapter=self.name, ok=True)

            def run(self, prompt, workdir, ctx):
                self.calls += 1
                (Path(workdir).parent / "escaped.txt").write_text("evil", encoding="utf-8")
                rd = Path(ctx.runs_dir) / "reports"
                rd.mkdir(parents=True, exist_ok=True)
                rp = rd / "requirements_report.md"
                rp.write_text("ok\n", encoding="utf-8")
                return Result(ok=True, exit_code=0, channel=self.channel, text="ok",
                              report_files=[rp])

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            raw = {"task_workdir": "ws", "state_dir": ".s", "watch_root": ".", "watch_enforce": True,
                   "stages": [{"name": "requirements", "adapter": "fake",
                               "prompt": "x", "kind": "report", "max_attempts": 2}],
                   "adapters": {"fake": {"type": "fake"}}}
            cfg = parse_config(raw, base_dir=tmp)
            ad = Escape()
            eng = Engine(cfg, {"fake": ad}, run_id="es", task="t")
            res = eng.run()
            escaped = cfg.task_workdir.parent / "escaped.txt"
            self.assertEqual(res["requirements"], StageStatus.FAILED.value)
            self.assertEqual(ad.calls, 1)
            self.assertEqual(escaped.read_text(encoding='utf8'),'evil')
            violation = eng.state.control['write_violations']['requirements']
            self.assertTrue(violation['source_files_preserved'])
            self.assertTrue((eng.state.run_dir/violation['evidence']).is_file())
            self.assertEqual(eng.run()['requirements'],StageStatus.FAILED.value)
            self.assertEqual(ad.calls,1)

    def test_delete_outside_file_fails_stage(self):
        class Deleter(Adapter):
            channel = "fake"

            def __init__(self):
                self.name = "fake"

            def preflight(self):
                return PreflightReport(adapter=self.name, ok=True)

            def run(self, prompt, workdir, ctx):
                (Path(workdir).parent / "victim.txt").unlink()
                rd = Path(ctx.runs_dir) / "reports"
                rd.mkdir(parents=True, exist_ok=True)
                rp = rd / "requirements_report.md"
                rp.write_text("ok\n", encoding="utf-8")
                return Result(ok=True, exit_code=0, channel=self.channel, text="ok",
                              report_files=[rp])

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            raw = {"task_workdir": "ws", "state_dir": ".s", "watch_root": ".", "watch_enforce": True,
                   "stages": [{"name": "requirements", "adapter": "fake",
                               "prompt": "x", "kind": "report"}],
                   "adapters": {"fake": {"type": "fake"}}}
            cfg = parse_config(raw, base_dir=tmp)
            victim = cfg.task_workdir.parent / "victim.txt"
            victim.write_text("keep", encoding="utf-8")
            eng = Engine(cfg, {"fake": Deleter()}, run_id="del", task="t")
            res = eng.run()
            self.assertEqual(res["requirements"], StageStatus.FAILED.value)
            self.assertIn("可写根外", eng.state.stage("requirements").error)
            self.assertFalse(victim.exists())


class TestVerdictFileContract(unittest.TestCase):
    """结论只认独立文件 + 严格 schema，不再从散文推断。"""

    def _load(self, payload):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "review_verdict.json"
            p.write_text(payload if isinstance(payload, str)
                         else json.dumps(payload), encoding="utf-8")
            return load_verdict(p, workdir=(p).parent)

    def test_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(VerdictError):
                load_verdict(Path(d) / "review_verdict.json", workdir=(Path(d) / "review_verdict.json").parent)

    def test_invalid_json_raises(self):
        with self.assertRaises(VerdictError):
            self._load("{not json")

    def test_bad_verdict_value_raises(self):
        for v in ("有条件通过", "maybe", ""):
            with self.assertRaises(VerdictError):
                self._load({"verdict": v, "issues": []})

    def test_pass_with_issues_raises(self):
        with self.assertRaises(VerdictError):
            self._load({"verdict": "pass",
                        "issues": [{"file": "a.py", "problem": "x"}]})

    def test_pass_clean(self):
        v = self._load({"verdict": "pass", "issues": []})
        self.assertTrue(v.passed)
        self.assertEqual(v.issues, [])

    def test_fail_with_locations(self):
        v = self._load({"verdict": "fail", "issues": [
            {"file": "todo/store.py", "problem": "非原子写", "expected": "os.replace"}]})
        self.assertFalse(v.passed)
        self.assertEqual(len(v.located_issues()), 1)
        self.assertIn("todo/store.py", v.issue_texts()[0])

    def test_issue_without_file_is_not_located(self):
        v = self._load({"verdict": "fail", "issues": ["整体质量差"]})
        self.assertFalse(v.passed)
        self.assertEqual(v.located_issues(), [])   # 无位置 => 不可自动返工

    def test_issue_missing_problem_raises(self):
        with self.assertRaises(VerdictError):
            self._load({"verdict": "fail", "issues": [{"file": "a.py"}]})


class TestGitHardening(unittest.TestCase):
    def test_api_writer_rejects_git_hooks_path(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            text = "```python\n# file: .git/hooks/pre-commit\n#!/bin/sh\necho pwned\n```\n"
            files = OpenAICompatAdapter._write_code_files(text, repo, "coding")
            self.assertFalse((repo / ".git" / "hooks" / "pre-commit").exists())
            for f in files:
                self.assertTrue(str(Path(f).resolve()).startswith(str(repo.resolve())))

    @unittest.skipUnless(artifacts.git_available(), "git 不可用")
    def test_engine_git_does_not_run_hooks(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            artifacts.ensure_repo(repo)
            hook = repo / ".git" / "hooks" / "pre-commit"
            hook.parent.mkdir(parents=True, exist_ok=True)
            hook.write_text("#!/bin/sh\necho hooked > hook-ran.txt\nexit 0\n", encoding="utf-8")
            (repo / "a.txt").write_text("a\n", encoding="utf-8")
            artifacts.commit_files(repo, ["a.txt"], "m")
            self.assertFalse((repo / "hook-ran.txt").exists())

    @unittest.skipUnless(artifacts.git_available(), "git 不可用")
    def test_outside_git_path_fails_stage_and_cleans_worktree(self):
        class Bad(Adapter):
            channel = "fake"

            def __init__(self):
                self.name = "fake"

            def preflight(self):
                return PreflightReport(adapter=self.name, ok=True)

            def run(self, prompt, workdir, ctx):
                src = Path(workdir) / "ok.py"
                src.write_text("x = 1\n", encoding="utf-8")
                return Result(ok=True, exit_code=0, channel=self.channel, text="done",
                              changed_files=["../outside.txt", str(src)])

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            raw = {"task_workdir": "ws", "state_dir": ".s",
                   "stages": [{"name": "coding", "adapter": "fake",
                               "prompt": "x", "kind": "code", "max_attempts": 2}],
                   "adapters": {"fake": {"type": "fake"}}}
            cfg = parse_config(raw, base_dir=tmp)
            eng = Engine(cfg, {"fake": Bad()}, run_id="ge", task="t")
            res = eng.run()
            self.assertEqual(res["coding"], StageStatus.FAILED.value)
            self.assertIn("越界", eng.state.stage("coding").error)
            wt = cfg.state_dir / "runs" / "ge" / "stages" / "coding" / "worktree"
            self.assertFalse((wt / "1").exists())


class TestLockAndExit(unittest.TestCase):
    def test_dead_pid_lock_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / ".lock"
            path.write_text(json.dumps({"pid": 999999, "token": "dead", "ts": 0}),
                            encoding="utf-8")
            path.touch()
            lock = StageLock(path)
            self.assertTrue(lock.acquire())
            lock.release()

    def test_live_pid_lock_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / ".lock"
            path.write_text(json.dumps({"pid": os.getpid(), "token": "live", "ts": 0}),
                            encoding="utf-8")
            path.touch()
            lock = StageLock(path)
            self.assertFalse(lock.acquire())

    def test_cli_failed_stage_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = tmp / "config.toml"
            cfg.write_text(
                'task_workdir = "ws"\nstate_dir = ".s"\n'
                '[run_budget]\nmax_tokens = 1\n'
                '[[stages]]\nname = "requirements"\nadapter = "fake"\n'
                'prompt = "' + ("很长" * 40) + '"\nkind = "report"\n'
                '[adapters.fake]\ntype = "fake"\n',
                encoding="utf-8")
            rc = main(["run", str(cfg), "--task", "t", "--run-id", "x"])
            self.assertEqual(rc, 1)


class TestPromptStdin(unittest.TestCase):
    def _assert_stdin(self, adapter, secret="SECRET_PROMPT_VALUE"):
        adapter._resolved = adapter.exe_name
        adapter._version = f"{adapter.exe_name} 1.0"
        ctx = RunContext(task="t", run_id="r", stage="coding",
                         workdir=Path("."), runs_dir=Path("."))
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("orchestrator.adapters.cli_base.run_cli_process") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr="")
            adapter.run(secret, Path(d), ctx)
        argv = run.call_args.args[0]
        self.assertNotIn(secret, argv)
        self.assertEqual(run.call_args.kwargs["input"], secret)
        self.assertIn("-" if adapter.exe_name == 'codex' else "--", argv)

    def test_codex_doubao_dsh_keep_prompt_off_argv(self):
        self._assert_stdin(CodexAdapter("codex"))
        self._assert_stdin(DoubaoAdapter("doubao"))
        self._assert_stdin(DshAdapter("dsh"))


if __name__ == "__main__":
    unittest.main()
