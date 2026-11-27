"""第九轮审查修复的回归：安全边界、审查覆盖、续跑收敛、审批、watch、git 基线等。"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from orchestrator import artifacts
from orchestrator.adapters.base import Adapter
from orchestrator.adapters.fake import FakeAdapter
from orchestrator.config import ConfigError, parse_config
from orchestrator.contracts import (ErrorKind, PreflightReport, Result, RunContext,
                                    StageStatus)
from orchestrator.engine import Engine
from orchestrator.safety import WriteRootViolation, relative_target
from orchestrator.state import StageLock


def _raw(tmp: Path, stages, **extra):
    raw = {"task_workdir": "ws", "state_dir": ".s", "stages": stages,
           "adapters": {"fake": {"type": "fake"}}}
    raw.update(extra)
    return raw


def _report_stage(name="requirements", **extra):
    stage = {"name": name, "adapter": "fake", "prompt": "x", "kind": "report"}
    stage.update(extra)
    return stage


def _write_report(ctx, name: str, text: str = "ok\n") -> Path:
    directory = Path(ctx) / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}_report.md"
    path.write_text(text, encoding="utf8")
    return path


class _ReportAdapter(Adapter):
    channel = "fake"

    def __init__(self, name="fake", marker="REPORT"):
        self.name = name
        self.marker = marker
        self.prompts = []

    def preflight(self):
        return PreflightReport(adapter=self.name, ok=True)

    def run(self, prompt, workdir, ctx):
        self.prompts.append(prompt)
        path = _write_report(ctx.runs_dir, ctx.stage, self.marker + "\n")
        return Result(ok=True, exit_code=0, channel=self.channel,
                      text=path.read_text(encoding="utf8"), report_files=[path])


class _ReviewAdapter(Adapter):
    """按脚本写结论；verdict pass/fail 可配，fail 时可带文件级 issue。"""

    channel = "fake"

    def __init__(self, name="fake", verdict="pass", issue=None):
        self.name = name
        self.verdict = verdict
        self.issue = issue
        self.calls = 0
        self.prompts = []

    def preflight(self):
        return PreflightReport(adapter=self.name, ok=True)

    def run(self, prompt, workdir, ctx):
        self.calls += 1
        self.prompts.append(prompt)
        runs = Path(ctx.runs_dir)
        runs.mkdir(parents=True, exist_ok=True)
        report = _write_report(ctx.runs_dir, ctx.stage, "# 审查报告\n")
        payload = {"verdict": self.verdict,
                   "issues": [self.issue] if self.issue else []}
        (runs / "review_verdict.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf8")
        return Result(ok=True, exit_code=0, channel=self.channel, text="报告",
                      report_files=[report],
                      usage={"tokens": 10, "duration_s": 0.01})


class _CodeAdapter(Adapter):
    """在 worktree 写文件并上报变更；content 可固定或按调用序号生成。"""

    channel = "fake"

    def __init__(self, name="fake", filename="impl.py",
                 contents=None, content="X = 1\n"):
        self.name = name
        self.filename = filename
        self.contents = contents
        self.content = content
        self.calls = 0
        self.prompts = []

    def preflight(self):
        return PreflightReport(adapter=self.name, ok=True)

    def run(self, prompt, workdir, ctx):
        self.calls += 1
        self.prompts.append(prompt)
        text = self.content
        if self.contents is not None:
            text = self.contents[min(self.calls - 1, len(self.contents) - 1)]
        path = Path(workdir) / self.filename
        path.write_text(text, encoding="utf8")
        return Result(ok=True, exit_code=0, channel=self.channel, text="done",
                      changed_files=[str(path)])


class SafetyBoundary(unittest.TestCase):
    def test_git_variants_rejected_for_read_and_write(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text(
                '[remote "origin"]\n\turl = https://' 'user:secret@example/x\n', encoding="utf8")
            normal = relative_target(root, "a.py")
            self.assertEqual(normal, root / "a.py")
            for name in (".git/config", ".git./config", ".git.../config",
                         ".git /config", ".git./hooks/pre-commit", "../escape.txt"):
                with self.subTest(name=name):
                    with self.assertRaises(WriteRootViolation):
                        relative_target(root, name)

    def test_unsafe_rel_paths_includes_git_variants(self):
        bad = artifacts.unsafe_rel_paths(
            [".git./config", ".git /hooks/pre-commit", "../outside.txt", "ok.py"])
        self.assertIn(".git./config", bad)
        self.assertIn(".git /hooks/pre-commit", bad)
        self.assertIn("../outside.txt", bad)
        self.assertNotIn("ok.py", bad)

    def test_remove_git_metadata_keeps_worktree_pointer(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".git").write_text("gitdir: somewhere\n", encoding="utf8")
            artifacts.remove_git_metadata_files(root, [".git", ".git."])
            self.assertTrue((root / ".git").is_file())
            (root / ".git").unlink()
            (root / ".git").mkdir()
            hook = root / ".git" / "hooks" / "pre-commit"
            hook.parent.mkdir(parents=True)
            hook.write_text("x", encoding="utf8")
            if os.name == "nt":
                # Windows 把 `.git.` 规范化成 `.git`；POSIX 上它是另一个目录。
                artifacts.remove_git_metadata_files(root, [".git./hooks/pre-commit"])
                self.assertFalse(hook.exists())


class ReviewCoverage(unittest.TestCase):
    def _code_review_config(self, tmp: Path):
        raw = _raw(tmp, [
            {"name": "coding", "adapter": "fake", "prompt": "x", "kind": "code"},
            {"name": "review", "adapter": "review", "prompt": "{upstream}\n{task}",
             "kind": "review"},
        ])
        raw["adapters"]["review"] = {"type": "fake"}
        return parse_config(raw, base_dir=tmp)

    def test_truncated_diff_injects_full_changed_files(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = self._code_review_config(tmp)
            content = "\n".join(f"LINE_{i:04d} = {i}" for i in range(1, 901)) + "\n"
            review = _ReviewAdapter(verdict="pass")
            eng = Engine(cfg, {"fake": _CodeAdapter(content=content), "review": review},
                         run_id="trunc", task="t")
            res = eng.run()
            self.assertEqual(res["review"], StageStatus.DONE.value)
            self.assertIn("LINE_0900", review.prompts[0])
            self.assertIn("按 commit 注入的变更文件全文", review.prompts[0])

    def test_incomplete_review_coverage_requires_manual_confirmation(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = self._code_review_config(tmp)
            content = "\n".join(f"LINE_{i:04d} = {i}" for i in range(1, 4001)) + "\n"
            review = _ReviewAdapter(verdict="pass")
            eng = Engine(cfg, {"fake": _CodeAdapter(content=content), "review": review},
                         run_id="incomplete", task="t")
            res = eng.run()
            self.assertEqual(res["review"], StageStatus.AWAITING_APPROVAL.value)
            st = eng.state.stage("review")
            self.assertIn("覆盖不完整", st.error or "")
            self.assertEqual(eng.state.pending_approvals["review"]["requested_by"],
                             "review_incomplete")

    def _review_only_config(self, tmp: Path, **extra):
        raw = _raw(tmp, [{"name": "review", "adapter": "review",
                          "prompt": "{upstream}\n{task}", "kind": "review"}], **extra)
        raw["adapters"]["review"] = {"type": "fake"}
        return parse_config(raw, base_dir=tmp)

    def _seed_workspace(self, tmp: Path) -> Path:
        ws = tmp / "ws"
        ws.mkdir()
        (ws / "app.py").write_text("VALUE = 1\n", encoding="utf8")
        return tmp

    def test_existing_code_review_recomputes_on_source_change(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = self._seed_workspace(Path(d))
            cfg = self._review_only_config(tmp)
            first = _ReviewAdapter(verdict="pass")
            self.assertEqual(Engine(cfg, {"review": first}, run_id="self", task="t")
                             .run()["review"], StageStatus.DONE.value)
            (tmp / "ws" / "app.py").write_text("VALUE = 2  # CRITICAL BUG\n", encoding="utf8")
            second = _ReviewAdapter(verdict="pass")
            Engine(cfg, {"review": second}, run_id="self", task="t").run()
            self.assertEqual(second.calls, 1, "源码变化后必须重新审查")
            self.assertIn("CRITICAL BUG", second.prompts[0])

    def test_autonomy_cached_review_also_invalidated_by_source_change(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = self._seed_workspace(Path(d))
            cfg = self._review_only_config(
                tmp, autonomy={"enabled": True},
                run_budget={"max_tokens": 50000, "max_duration_s": 600})
            first = _ReviewAdapter(verdict="pass")
            self.assertEqual(Engine(cfg, {"review": first}, run_id="au", task="t")
                             .run()["review"], StageStatus.DONE.value)
            (tmp / "ws" / "app.py").write_text("VALUE = 2\n", encoding="utf8")
            second = _ReviewAdapter(verdict="pass")
            Engine(cfg, {"review": second}, run_id="au", task="t").run()
            self.assertEqual(second.calls, 1)


class ResumeConvergence(unittest.TestCase):
    def _config(self, tmp: Path):
        script = ("import pathlib,time\n"
                  "p=pathlib.Path('junit.xml')\n"
                  "p.write_text(str(time.time()))\n"
                  "print('tests ok')\n")
        raw = _raw(tmp, [{"name": "testing", "kind": "test",
                          "test_cmd": ["python", "-X", "utf8", "-c", script]}])
        return parse_config(raw, base_dir=tmp)

    def test_test_artifacts_do_not_force_rerun(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = self._config(tmp)
            eng = Engine(cfg, {}, run_id="conv", task="t")
            self.assertEqual(eng.run()["testing"], StageStatus.DONE.value)
            self.assertEqual(sum(e["kind"] == "test_start" for e in eng.state.events), 1)
            for round_no in range(1, 4):
                eng2 = Engine(cfg, {}, run_id="conv", task="t")
                self.assertEqual(eng2.run()["testing"], StageStatus.DONE.value)
                starts = sum(e["kind"] == "test_start" for e in eng2.state.events)
                self.assertEqual(starts, 1, f"第 {round_no} 次续跑不应重跑测试")
                self.assertEqual(eng2.state.stage("testing").current_attempt, 1)
            (cfg.task_workdir / "source.py").write_text("VALUE = 2\n", encoding="utf8")
            eng3 = Engine(cfg, {}, run_id="conv", task="t")
            eng3.run()
            self.assertEqual(eng3.state.stage("testing").current_attempt, 2)
            self.assertIn("junit.xml", eng3.state.test_artifacts["testing"])


class ApprovalFixes(unittest.TestCase):
    def test_code_stage_approval_after_context_file_modified(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "ws"
            ws.mkdir()
            (ws / "a.py").write_text("VALUE = 1\n", encoding="utf8")
            raw = _raw(tmp, [{"name": "coding", "adapter": "fake", "prompt": "x",
                              "kind": "code", "approval": True,
                              "context_files": ["a.py"]}])
            cfg = parse_config(raw, base_dir=tmp)
            eng = Engine(cfg, {"fake": _CodeAdapter(filename="a.py", content="VALUE = 2\n")},
                         run_id="appr", task="t")
            self.assertEqual(eng.run()["coding"], StageStatus.AWAITING_APPROVAL.value)
            self.assertTrue(eng.approve("coding"), "阶段写自身输入后审批不应被误拒")
            self.assertEqual(eng.state.stage("coding").status, StageStatus.DONE)

    def test_test_stage_approval_with_artifact(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            script = ("import pathlib\n"
                      "pathlib.Path('junit.xml').write_text('x')\n"
                      "print('ok')\n")
            raw = _raw(tmp, [{"name": "acceptance", "kind": "test",
                              "approval": True,
                              "test_cmd": ["python", "-X", "utf8", "-c", script]}])
            cfg = parse_config(raw, base_dir=tmp)
            eng = Engine(cfg, {}, run_id="testappr", task="t")
            self.assertEqual(eng.run()["acceptance"], StageStatus.AWAITING_APPROVAL.value)
            self.assertTrue(eng.approve("acceptance"))

    def test_report_stage_approval_still_rejects_input_change(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "prompt.md").write_text("A {task}\n", encoding="utf8")
            raw = _raw(tmp, [{"name": "design", "adapter": "fake",
                              "prompt": "prompt.md", "kind": "report", "approval": True}])
            cfg = parse_config(raw, base_dir=tmp)
            eng = Engine(cfg, {"fake": _ReportAdapter()}, run_id="reportappr", task="t")
            self.assertEqual(eng.run()["design"], StageStatus.AWAITING_APPROVAL.value)
            (tmp / "prompt.md").write_text("B {task} changed\n", encoding="utf8")
            self.assertFalse(eng.approve("design"), "输入模板变化后审批必须被拒绝")


class WatchEnforce(unittest.TestCase):
    def _config(self, tmp: Path, watch_root=".", enforce=True):
        raw = _raw(tmp, [_report_stage(max_attempts=1)],
                   watch_root=watch_root, watch_enforce=enforce)
        return parse_config(raw, base_dir=tmp)

    def test_new_outside_file_preserved_and_resume_keeps_failure(self):
        class Escape(_ReportAdapter):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def run(self, prompt, workdir, ctx):
                self.calls += 1
                (Path(workdir).parent / "escaped.txt").write_text("evil", encoding="utf8")
                return super().run(prompt, workdir, ctx)

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = self._config(tmp)
            adapter = Escape()
            eng = Engine(cfg, {"fake": adapter}, run_id="we", task="t")
            self.assertEqual(eng.run()["requirements"], StageStatus.FAILED.value)
            escaped = cfg.task_workdir.parent / "escaped.txt"
            self.assertTrue(escaped.exists(), "无法归属的原文件必须保留")
            records = list((eng.state.run_dir / "violations").rglob("*.json"))
            self.assertEqual(len(records), 1, "根外变化应保存元数据留证")
            data = json.loads(records[0].read_text(encoding="utf8"))
            self.assertTrue(data["source_files_preserved"])
            self.assertEqual(data["changes"][0]["path"], str(escaped.resolve()))
            calls = adapter.calls
            eng2 = Engine(cfg, {"fake": Escape()}, run_id="we", task="t")
            self.assertEqual(eng2.run()["requirements"], StageStatus.FAILED.value)
            self.assertEqual(adapter.calls, calls, "同一输入的越界记录应阻止续跑再调用模型")

    def test_modified_existing_outside_file_not_washed_out_on_resume(self):
        class Touch(_ReportAdapter):
            def run(self, prompt, workdir, ctx):
                (Path(workdir).parent / "existing.txt").write_text("changed", encoding="utf8")
                return super().run(prompt, workdir, ctx)

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = self._config(tmp)
            sibling = cfg.task_workdir.parent / "existing.txt"
            sibling.write_text("old", encoding="utf8")
            eng = Engine(cfg, {"fake": Touch()}, run_id="we2", task="t")
            self.assertEqual(eng.run()["requirements"], StageStatus.FAILED.value)
            fresh = Touch()
            eng2 = Engine(cfg, {"fake": fresh}, run_id="we2", task="t")
            self.assertEqual(eng2.run()["requirements"], StageStatus.FAILED.value)
            self.assertEqual(fresh.prompts, [], "续跑不应再次调用模型洗白改写")

    def test_watch_root_inside_workspace_is_disabled_with_event(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = self._config(tmp, watch_root="ws", enforce=True)
            eng = Engine(cfg, {"fake": _ReportAdapter()}, run_id="we3", task="t")
            self.assertEqual(eng.run()["requirements"], StageStatus.DONE.value)
            self.assertTrue(any(e["kind"] == "watch_disabled" for e in eng.state.events))


class GitBaseline(unittest.TestCase):
    def test_existing_non_git_workspace_gets_source_baseline(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "ws"
            ws.mkdir()
            (ws / "app.py").write_text("VALUE = 1\n", encoding="utf8")
            raw = _raw(tmp, [{"name": "coding", "adapter": "fake", "prompt": "x",
                              "kind": "code"}])
            cfg = parse_config(raw, base_dir=tmp)
            adapter = _CodeAdapter(filename="app.py", content="VALUE = 2\n")
            eng = Engine(cfg, {"fake": adapter}, run_id="base", task="t")
            self.assertEqual(eng.run()["coding"], StageStatus.DONE.value)
            self.assertEqual((ws / "app.py").read_text(encoding="utf8"), "VALUE = 2\n")
            self.assertIn("VALUE = 2", artifacts._git(["show", "HEAD:app.py"], ws).stdout)
            self.assertIn("baseline: existing workspace",
                          artifacts._git(["log", "--format=%s"], ws).stdout)

    def test_zero_net_change_code_stage_fails(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "ws"
            ws.mkdir()
            (ws / "impl.py").write_text("VALUE = 1\n", encoding="utf8")
            raw = _raw(tmp, [{"name": "coding", "adapter": "fake", "prompt": "x",
                              "kind": "code", "max_attempts": 1}])
            cfg = parse_config(raw, base_dir=tmp)
            eng = Engine(cfg, {"fake": _CodeAdapter(content="VALUE = 1\n")},
                         run_id="noop", task="t")
            res = eng.run()
            self.assertEqual(res["coding"], StageStatus.FAILED.value)
            self.assertIn("代码未发生变化", eng.state.stage("coding").error or "")


class ConfigValidation(unittest.TestCase):
    def test_unknown_keys_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cases = [
                {"task_workdir": "ws", "state_dir": ".s", "watch_enforc": True,
                 "stages": [_report_stage()], "adapters": {"fake": {"type": "fake"}}},
                {"task_workdir": "ws", "state_dir": ".s",
                 "stages": [_report_stage(aprovall=True)],
                 "adapters": {"fake": {"type": "fake"}}},
                {"task_workdir": "ws", "state_dir": ".s",
                 "stages": [_report_stage()],
                 "adapters": {"fake": {"type": "fake"}},
                 "run_budget": {"max_token": 100}},
            ]
            for raw in cases:
                with self.subTest(raw=list(raw)):
                    with self.assertRaises(ConfigError):
                        parse_config(raw, base_dir=tmp)

    def test_non_json_config_keys_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            raw = {"task_workdir": "ws", "state_dir": ".s",
                   "stages": [_report_stage(
                       config_keys={"when": datetime.datetime(2026, 1, 1)})],
                   "adapters": {"fake": {"type": "fake"}}}
            with self.assertRaises(ConfigError):
                parse_config(raw, base_dir=tmp)


class LockReclaim(unittest.TestCase):
    def _lock(self, tmp: Path) -> Path:
        return tmp / "stage.lock"

    def test_fresh_corrupt_lock_is_not_immediately_reclaimed(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._lock(Path(d))
            path.write_text("{incomplete", encoding="utf8")
            lock = StageLock(path)
            self.assertFalse(lock.acquire())
            lock.release()

    def test_stale_corrupt_lock_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._lock(Path(d))
            path.write_text("{incomplete", encoding="utf8")
            old = time.time() - 60
            os.utime(path, (old, old))
            lock = StageLock(path)
            self.assertTrue(lock.acquire())
            lock.release()
            self.assertFalse(path.exists())

    def test_dead_holder_lock_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._lock(Path(d))
            path.write_text(json.dumps({"pid": 999999999, "token": "x"}),
                            encoding="utf8")
            lock = StageLock(path)
            self.assertTrue(lock.acquire())
            lock.release()

    def test_fresh_reclaim_claim_blocks_acquire(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._lock(Path(d))
            path.write_text(json.dumps({"pid": 999999999, "token": "x"}),
                            encoding="utf8")
            claim = path.with_name(path.name + ".reclaim")
            claim.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}),
                             encoding="utf8")
            self.assertFalse(StageLock(path).acquire())
            self.assertTrue(path.exists(), "被阻塞时不得删除他人锁")
            self.assertTrue(claim.exists())


class GitHelpers(unittest.TestCase):
    def test_tree_hash_ignores_quotepath_and_includes_gitlink(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            subprocess.run(["git", "init", "-q"], cwd=repo, env=dict(os.environ), check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, env=dict(os.environ), check=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=repo, env=dict(os.environ), check=True)
            (repo / "中文文件.py").write_text("A = 1\n", encoding="utf8")
            subprocess.run(["git", "add", "-A"], cwd=repo, env=dict(os.environ), check=True)
            subprocess.run(["git", "commit", "-qm", "one"], cwd=repo, env=dict(os.environ), check=True)
            sha = artifacts.head_sha(repo)
            first = artifacts.tree_content_hash(repo, sha)
            subprocess.run(["git", "config", "core.quotePath", "false"], cwd=repo, env=dict(os.environ), check=True)
            self.assertEqual(first, artifacts.tree_content_hash(repo, sha))
            subprocess.run(["git", "update-index", "--add", "--cacheinfo",
                            "160000,1111111111111111111111111111111111111111,submod"],
                           cwd=repo, env=dict(os.environ), check=True)
            tree_a = subprocess.run(["git", "write-tree"], cwd=repo, env=dict(os.environ), check=True,
                                    capture_output=True, text=True).stdout.strip()
            subprocess.run(["git", "rm", "-q", "--cached", "submod"], cwd=repo, env=dict(os.environ), check=True)
            subprocess.run(["git", "update-index", "--add", "--cacheinfo",
                            "160000,2222222222222222222222222222222222222222,submod"],
                           cwd=repo, env=dict(os.environ), check=True)
            tree_b = subprocess.run(["git", "write-tree"], cwd=repo, env=dict(os.environ), check=True,
                                    capture_output=True, text=True).stdout.strip()
            self.assertNotEqual(tree_a, tree_b)

    def test_commit_files_returns_none_with_unrelated_staged_change(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            subprocess.run(["git", "init", "-q"], cwd=repo, env=dict(os.environ), check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, env=dict(os.environ), check=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=repo, env=dict(os.environ), check=True)
            (repo / "a.py").write_text("A = 1\n", encoding="utf8")
            (repo / "other.py").write_text("O = 1\n", encoding="utf8")
            subprocess.run(["git", "add", "-A"], cwd=repo, env=dict(os.environ), check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, env=dict(os.environ), check=True)
            (repo / "other.py").write_text("O = 2\n", encoding="utf8")
            subprocess.run(["git", "add", "other.py"], cwd=repo, env=dict(os.environ), check=True)
            self.assertIsNone(artifacts.commit_files(repo, ["a.py"], "noop"))


class RerunClearsFeedback(unittest.TestCase):
    def test_rerun_from_upstream_drops_stale_review_feedback(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            design = _ReportAdapter(marker="DESIGN_MARKER")
            coding = _CodeAdapter(contents=["VALUE = 1\n", "VALUE = 1b\n"])
            review = _ReviewAdapter(verdict="fail", issue={
                "file": "impl.py", "problem": "STALE_ISSUE_MARKER",
                "expected": "fix"})
            raw = _raw(tmp, [
                {"name": "design", "adapter": "design", "prompt": "x", "kind": "report"},
                {"name": "coding", "adapter": "coding", "prompt": "{upstream}\n{task}",
                 "kind": "code"},
                {"name": "review", "adapter": "review", "prompt": "{upstream}\n{task}",
                 "kind": "review", "feedback_to": "coding", "max_rounds": 2},
            ])
            raw["adapters"] = {"design": {"type": "fake"}, "coding": {"type": "fake"},
                               "review": {"type": "fake"}}
            cfg = parse_config(raw, base_dir=tmp)
            eng = Engine(cfg, {"design": design, "coding": coding, "review": review},
                         run_id="rerun", task="t")
            eng.run()
            self.assertIn("coding", eng.state.feedback,
                          "返工后应保留审查反馈（下一轮上游重跑要清掉它）")

            design2 = _ReportAdapter(marker="FRESH_DESIGN_V2")
            coding2 = _CodeAdapter(contents=["VALUE = 2\n"])
            review2 = _ReviewAdapter(verdict="fail", issue={
                "file": "impl.py", "problem": "FRESH_ISSUE_MARKER",
                "expected": "fix"})
            eng2 = Engine(cfg, {"design": design2, "coding": coding2, "review": review2},
                          run_id="rerun", task="t")
            eng2.rerun_from("design")
            self.assertTrue(coding2.prompts, "coding 应被重新执行")
            # 重跑后的第一次 coding 提示词必须是正常上游上下文（不是旧返工分支）
            self.assertIn("FRESH_DESIGN_V2", coding2.prompts[0],
                          "重跑上游后应注入新的设计产物")
            self.assertNotIn("STALE_ISSUE_MARKER", coding2.prompts[0],
                             "旧审查反馈不应残留")
            # 若本轮再次返工，提示词里应是新反馈而不是旧 issue
            for prompt in coding2.prompts:
                self.assertNotIn("STALE_ISSUE_MARKER", prompt)


if __name__ == "__main__":
    unittest.main()
