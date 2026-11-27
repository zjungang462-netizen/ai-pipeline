"""实测硬缺陷回归测试（G1/G2/G3/G5）。

G1 git 探测兜底 + 代码阶段缺 git 明确报错（不静默降级误判空跑）。
G2 快照对比真正检测可写根外写入。
G3 审查结论不可解析 => 待人工确认，不默认放行。
G5 CLI 适配器按 ctx.kind 为 report/review 写报告文件。
"""
from __future__ import annotations

import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import artifacts
from orchestrator.config import parse_config
from orchestrator.contracts import (ErrorKind, PreflightReport, Result,
                                    RunContext, StageStatus)
from orchestrator.engine import Engine
from orchestrator.safety import WriteRootGuard
from orchestrator.adapters.base import Adapter
from orchestrator.adapters.fake import FakeAdapter
from orchestrator.adapters.cli_dsh import DshAdapter


class TestG1Git(unittest.TestCase):
    def test_git_fallback_when_not_in_path(self):
        # 模拟 PATH 无 git，但兜底路径有可用 git
        git = artifacts.GitContext()
        with mock.patch("orchestrator.artifacts.shutil.which", return_value=None), \
             mock.patch("orchestrator.artifacts._git_fallback_paths",
                        return_value=["/fake/git"]), \
             mock.patch("orchestrator.artifacts.Path.is_file", return_value=True), \
             mock.patch("orchestrator.artifacts.subprocess.run") as m:
            m.return_value = mock.Mock(returncode=0)
            self.assertTrue(artifacts.git_available(git))
            self.assertEqual(artifacts.resolve_git(git), "/fake/git")

    def test_code_stage_fails_loudly_without_git(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            raw = {"task_workdir": "ws", "state_dir": ".s",
                   "stages": [{"name": "coding", "adapter": "fake",
                               "prompt": "x", "kind": "code"}],
                   "adapters": {"fake": {"type": "fake"}}}
            cfg = parse_config(raw, base_dir=tmp)
            eng = Engine(cfg, {"fake": FakeAdapter()}, run_id="g1", task="t")
            eng._git_ready = False   # 模拟 git 不可用
            res = eng.run()
            self.assertEqual(res["coding"], StageStatus.FAILED.value)
            self.assertIn("git", eng.state.stage("coding").error)
            self.assertTrue(any(e.get("reason") == "git_unavailable"
                                for e in eng.state.events))

    def test_config_git_exe(self):
        with tempfile.TemporaryDirectory() as d:
            raw = {"task_workdir": "ws", "state_dir": ".s", "git_exe": "/custom/git",
                   "stages": [{"name": "a", "adapter": "f", "prompt": "x", "kind": "report"}],
                   "adapters": {"f": {"type": "fake"}}}
            cfg = parse_config(raw, base_dir=Path(d))
            self.assertEqual(cfg.git_exe, "/custom/git")

    def test_interleaved_engines_use_their_own_git_for_archive_and_resume(self):
        executable = artifacts.resolve_git()
        if not executable:
            self.skipTest('需要实际 Git 仓库验证证据复用')
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            aliases = [root / 'git-a.exe', root / 'git-b.exe']
            for alias in aliases:
                alias.touch()
            used = []
            execute = subprocess.run

            def route(argv, **kwargs):
                used.append(argv[0])
                self.assertIn(argv[0], [str(alias) for alias in aliases])
                return execute([executable, *argv[1:]], **kwargs)

            def make(index):
                cfg = parse_config({
                    'task_workdir': f'ws{index}', 'state_dir': f'state{index}',
                    'git_exe': str(aliases[index]),
                    'stages': [{'name': 'coding', 'adapter': 'fake', 'prompt': 'x', 'kind': 'code'}],
                    'adapters': {'fake': {'type': 'fake'}},
                }, base_dir=root)
                return Engine(cfg, {'fake': FakeAdapter(kind='code')}, 'run', 'task')

            with mock.patch.object(artifacts.subprocess, 'run', side_effect=route):
                first, second = make(0), make(1)
                for index, engine in ((0, first), (1, second), (0, first)):
                    used.clear()
                    self.assertEqual(engine.run()['coding'], 'done')
                    self.assertTrue(used)
                    self.assertEqual(set(used), {str(aliases[index])})
                self.assertEqual(first.adapters['fake'].calls, 1)
                self.assertEqual(second.adapters['fake'].calls, 1)

    def test_new_git_context_resolves_root_after_branch_replacement(self):
        if not artifacts.git_available():
            self.skipTest('需要实际 Git 仓库验证根提交')
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            previous = artifacts.GitContext()
            artifacts.ensure_repo(repo, git=previous)
            first = artifacts.baseline_sha(repo, git=previous)
            new_head = artifacts._git(['commit-tree', 'HEAD^{tree}', '-m', 'new root'], repo,
                                      git=previous).stdout.strip()
            artifacts._git(['update-ref', 'HEAD', new_head], repo, git=previous)
            self.assertNotEqual(first, new_head)
            self.assertEqual(artifacts.baseline_sha(repo, git=artifacts.GitContext()), new_head)


class TestG2WriteRoot(unittest.TestCase):
    def test_snapshot_detects_outside_write(self):
        """必须走真实 snapshot 产出，禁止手工往 after 塞路径（那是假绿根源）。"""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            root = tmp / "ws"
            root.mkdir()
            guard = WriteRootGuard([root], watch_roots=[tmp])  # 显式启用监视域
            before = guard.snapshot()
            # 模拟适配器：根内正常写 + 根外越界写
            (root / "ok.py").write_text("ok", encoding="utf-8")
            outside = tmp / "evil.txt"
            outside.write_text("x", encoding="utf-8")
            after = guard.snapshot()          # 真实快照，不做任何人工拼接
            # 根外文件必须真的进了快照，否则检测是空转
            self.assertIn(str(outside.resolve()), after)
            detected = guard.diff_outside(before, after)
            self.assertIn(str(outside.resolve()), detected)
            # 根内正常产物不得误报
            self.assertNotIn(str((root / "ok.py").resolve()), detected)

    def test_inside_write_not_flagged(self):
        """显式启用监视域，真实构造根内写入，并保留一个根外写入作为非空转对照。"""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            root = tmp / "ws"
            root.mkdir()
            guard = WriteRootGuard([root], watch_roots=[tmp])
            before = guard.snapshot()
            # 根内正常产物：不得被判定为越界。
            inside = root / "ok.py"
            inside.write_text("x", encoding="utf-8")
            # 对照：真实根外写入必须能被检测到，否则本用例仍是空转。
            outside = tmp / "evil.txt"
            outside.write_text("x", encoding="utf-8")
            after = guard.snapshot()
            self.assertNotIn(str(inside.resolve()), after)
            detected = guard.diff_outside(before, after)
            self.assertNotIn(str(inside.resolve()), detected)
            self.assertIn(str(outside.resolve()), detected)

    def test_engine_rejects_outside_write(self):
        class EvilAdapter(Adapter):
            channel = "fake"
            def __init__(self): self.name = "fake"
            def preflight(self): return PreflightReport(adapter=self.name, ok=True)
            def run(self, prompt, workdir, ctx):
                # 复审查场景：根外越界写 + 根内正常写并声明 changed_files
                evil = Path(workdir).parent / "escaped.txt"
                evil.write_text("evil", encoding="utf-8")
                ok = Path(workdir) / "ok.py"
                ok.write_text("ok", encoding="utf-8")
                report = Path(ctx.runs_dir) / "report.md"
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text("evidence", encoding="utf-8")
                return Result(ok=True, exit_code=0, channel=self.channel,
                              text="done", changed_files=[str(ok)], report_files=[report])
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            raw = {"task_workdir": "ws", "state_dir": ".s",
                   "watch_root": ".", "watch_enforce": True,
                   "stages": [{"name": "requirements", "adapter": "fake",
                               "prompt": "x", "kind": "report"}],
                   "adapters": {"fake": {"type": "fake"}}}
            cfg = parse_config(raw, base_dir=tmp)
            eng = Engine(cfg, {"fake": EvilAdapter()}, run_id="g2", task="t")
            res = eng.run()
            self.assertEqual(res["requirements"], StageStatus.FAILED.value)
            self.assertIn("可写根外", eng.state.stage("requirements").error)


class TestG3ReviewUnparsed(unittest.TestCase):
    def test_unparsed_review_awaits_confirmation(self):
        class VagueReview(Adapter):
            channel = "fake"
            def __init__(self): self.name = "review"
            def preflight(self): return PreflightReport(adapter=self.name, ok=True)
            def run(self, prompt, workdir, ctx):
                rd = Path(ctx.runs_dir) / "reports"; rd.mkdir(parents=True, exist_ok=True)
                rp = rd / "review_report.md"
                rp.write_text("一段没有明确结论的审查文字", encoding="utf-8")
                return Result(ok=True, exit_code=0, channel=self.channel,
                              text="一段没有明确结论的审查文字", report_files=[rp])
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            raw = {"task_workdir": "ws", "state_dir": ".s",
                   "stages": [
                       {"name": "coding", "adapter": "fake", "prompt": "x", "kind": "code"},
                       {"name": "review", "adapter": "review", "prompt": "x",
                        "kind": "review", "feedback_to": "coding", "max_rounds": 2}],
                   "adapters": {"fake": {"type": "fake"}, "review": {"type": "fake"}}}
            cfg = parse_config(raw, base_dir=tmp)
            eng = Engine(cfg, {"fake": FakeAdapter(), "review": VagueReview()},
                         run_id="g3", task="t")
            res = eng.run()
            self.assertEqual(res["review"], StageStatus.AWAITING_APPROVAL.value)
            self.assertIn("review", eng.state.pending_approvals)


@unittest.skipUnless(artifacts.git_available(), "git 不可用")
class TestM0CumulativeDiff(unittest.TestCase):
    """M0：审查阶段须看到跨多次提交的累积全量实现，而非仅末次提交。"""

    def test_cumulative_diff_includes_earlier_commits(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            artifacts.ensure_repo(repo)
            # 第 1 次提交：实现代码
            (repo / "impl.py").write_text("def add(a,b):\n    return a+b\n", encoding="utf-8")
            artifacts.commit_files(repo, ["impl.py"], "impl")
            # 第 2 次提交：仅测试改动（模拟返工轮）
            (repo / "test_impl.py").write_text("assert True\n", encoding="utf-8")
            sha2 = artifacts.commit_files(repo, ["test_impl.py"], "tests")
            # 末次提交 diff 不含实现
            last = artifacts.diff_summary(repo, sha2)
            self.assertNotIn("def add", last)
            # 累积 diff 必须含实现
            cum = artifacts.cumulative_diff(repo, sha2)
            self.assertIn("def add", cum)
            self.assertIn("impl.py", cum)
            self.assertIn("test_impl.py", cum)

    def test_review_prompt_gets_full_impl(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            raw = {"task_workdir": "ws", "state_dir": ".s",
                   "stages": [
                       {"name": "coding", "adapter": "fake", "prompt": "x", "kind": "code"},
                       {"name": "review", "adapter": "fake", "prompt": "审查 {upstream}",
                        "kind": "review", "feedback_to": "coding", "max_rounds": 2}],
                   "adapters": {"fake": {"type": "fake"}}}
            cfg = parse_config(raw, base_dir=tmp)
            eng = Engine(cfg, {"fake": FakeAdapter()}, run_id="m0", task="t")
            eng.prepare()
            repo = cfg.task_workdir
            (repo / "impl.py").write_text("def core():\n    return 42\n", encoding="utf-8")
            artifacts.commit_files(repo, ["impl.py"], "impl")
            (repo / "t.py").write_text("assert True\n", encoding="utf-8")
            sha2 = artifacts.commit_files(repo, ["t.py"], "t")
            st = eng.state.stage("coding")
            st.commit_sha = sha2
            st.outputs_hash = "x"
            st.current_attempt = 1
            eng.state.save()
            rendered = eng._render_prompt(cfg.stage("review"), 1)
            self.assertIn("def core", rendered)   # 审查提示词含完整实现


class TestRound3Fixes(unittest.TestCase):
    """第三轮复验：裸 JSON 嵌套清单、G6 预算前置、G4 护栏判失败。"""

    def test_nested_issues_kept_from_verdict_file(self):
        """嵌套 issues 从结论文件读取，不得丢清单。"""
        import json as _j
        from orchestrator.review import load_verdict
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "review_verdict.json"
            p.write_text(_j.dumps({"verdict": "fail", "issues": [
                {"file": "a.py", "problem": "空指针", "expected": "判空"}]}),
                encoding="utf-8")
            v = load_verdict(p, workdir=(p).parent)
        self.assertFalse(v.passed)
        self.assertTrue(v.issues)
        self.assertIn("a.py", v.issue_texts()[0])
        self.assertIn("空指针", v.issue_texts()[0])

    def test_g6_pre_call_budget_blocks(self):
        """预算不足时必须在调用前拦截，适配器不得被调用（不烧钱）。"""
        class CountingAdapter(Adapter):
            channel = "fake"
            def __init__(self): self.name = "fake"; self.calls = 0
            def preflight(self): return PreflightReport(adapter=self.name, ok=True)
            def run(self, prompt, workdir, ctx):
                self.calls += 1
                return Result(ok=True, exit_code=0, channel=self.channel, text="x")
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            raw = {"task_workdir": "ws", "state_dir": ".s",
                   "run_budget": {"max_tokens": 5},   # 极小预算
                   "stages": [{"name": "requirements", "adapter": "fake",
                               "prompt": "很长的提示词" * 50, "kind": "report"}],
                   "adapters": {"fake": {"type": "fake"}}}
            cfg = parse_config(raw, base_dir=tmp)
            ad = CountingAdapter()
            eng = Engine(cfg, {"fake": ad}, run_id="g6", task="t")
            res = eng.run()
            self.assertEqual(res["requirements"], StageStatus.FAILED.value)
            self.assertEqual(ad.calls, 0)      # 关键：调用前就拦住了
            self.assertIn("调用前拦截", eng.state.stage("requirements").error)

    def test_g4_loop_guard_marks_failed(self):
        """病态无限反馈时，护栏必须判 FAILED，不能静默 break 留下部分 done。"""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            raw = {"task_workdir": "ws", "state_dir": ".s",
                   "stages": [
                       {"name": "coding", "adapter": "fake", "prompt": "x", "kind": "code"},
                       {"name": "review", "adapter": "fake", "prompt": "x",
                        "kind": "review", "feedback_to": "coding", "max_rounds": 2}],
                   "adapters": {"fake": {"type": "fake"}}}
            cfg = parse_config(raw, base_dir=tmp)
            eng = Engine(cfg, {"fake": FakeAdapter()}, run_id="g4", task="t")
            # 模拟病态：coding 正常完成，review 永远要求返工 => 无护栏会死循环
            def fake_run_stage(spec, index):
                if spec.kind == "review":
                    return StageStatus.REVIEW_FEEDBACK
                return StageStatus.DONE
            with mock.patch.object(Engine, "_run_stage", side_effect=fake_run_stage):
                res = eng.run()
            self.assertTrue(any(e["kind"] == "feedback_loop_guard"
                                for e in eng.state.events))
            self.assertIn(StageStatus.FAILED.value, res.values())


class TestRerunResetsReviewRounds(unittest.TestCase):
    """rerun 必须重置审查轮次，否则跨次累积会提前判"轮次耗尽"。"""

    def test_rerun_clears_round_counter(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            raw = {"task_workdir": "ws", "state_dir": ".s",
                   "stages": [
                       {"name": "coding", "adapter": "fake", "prompt": "x", "kind": "code"},
                       {"name": "review", "adapter": "fake", "prompt": "x",
                        "kind": "review", "feedback_to": "coding", "max_rounds": 3}],
                   "adapters": {"fake": {"type": "fake"}}}
            cfg = parse_config(raw, base_dir=tmp)
            eng = Engine(cfg, {"fake": FakeAdapter()}, run_id="rr", task="t")
            eng.prepare()
            eng.state.review_rounds["review"] = 3   # 模拟历史累积
            eng.state.save()
            eng2 = Engine(cfg, {"fake": FakeAdapter()}, run_id="rr", task="t")
            # 只验证重置语义，不跑真实流水线（避免依赖环境与耗时）
            with mock.patch.object(Engine, "run", return_value={}):
                eng2.rerun_from("review")
            # 历史计数 3 必须被清空，否则跨次累积会提前判"轮次耗尽"
            self.assertNotIn("review", eng2.state.review_rounds)


class TestSelfReviewFindings(unittest.TestCase):
    """编排器自审查（dogfooding）发现问题的回归。"""

    def test_m1_drive_letter_and_ads_blocked(self):
        from orchestrator.adapters.openai_compat import OpenAICompatAdapter
        with tempfile.TemporaryDirectory() as d:
            wd = Path(d) / "ws"
            text = ("```python\n# file: C:/evil.py\nx=1\n```\n"
                    "```python\n# file: a:b\ny=2\n```\n"
                    "```python\n# file: ../out.py\nz=3\n```\n")
            files = OpenAICompatAdapter._write_code_files(text, wd, "coding")
            for f in files:
                self.assertTrue(str(Path(f).resolve()).startswith(str(wd.resolve())),
                                f"越界写入: {f}")

    def test_prose_never_decides(self):
        """散文（含条件语气）不再是结论来源：无结论文件即抛错交人工。"""
        from orchestrator.review import VerdictError, load_verdict
        with tempfile.TemporaryDirectory() as d:
            missing = Path(d) / "review_verdict.json"
            with self.assertRaises(VerdictError):
                load_verdict(missing, workdir=(missing).parent)
            # 即便正文写了"通过"，只要文件里的 verdict 不合法就不放行
            missing.write_text('{"verdict":"有条件通过","issues":[]}',
                               encoding="utf-8")
            with self.assertRaises(VerdictError):
                load_verdict(missing, workdir=(missing).parent)

    def test_pass_with_issues_is_invalid(self):
        """verdict=pass 却带问题 => 结论不合法，交人工，不替模型猜。"""
        import json as _j
        from orchestrator.review import VerdictError, load_verdict
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "review_verdict.json"
            p.write_text(_j.dumps({"verdict": "pass", "issues": [
                {"file": "a.py", "problem": "缺陷", "expected": "修复"}]}),
                encoding="utf-8")
            with self.assertRaises(VerdictError):
                load_verdict(p, workdir=(p).parent)
            # 干净的 pass 才通过
            p.write_text(_j.dumps({"verdict": "pass", "issues": []}),
                         encoding="utf-8")
            self.assertTrue(load_verdict(p, workdir=(p).parent).passed)

    def test_b4_parse_unknown_never_meaningful(self):
        r = Result(ok=True, exit_code=0, text="x", parse_unknown=True,
                   changed_files=["a.py"], report_files=[Path("r.md")])
        self.assertFalse(r.is_meaningful("code"))
        self.assertFalse(r.is_meaningful("report"))

    def test_m2_git_env_has_no_api_key(self):
        import os
        from orchestrator.safety import build_subprocess_env
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-secret",
                                          "PATH": "/usr/bin"}):
            env = build_subprocess_env()
        self.assertNotIn("DEEPSEEK_API_KEY", env)


class TestExistingCodeReview(unittest.TestCase):
    """审查现有代码库：无上游 coding 阶段时直接读工作区源码。"""

    def test_existing_code_context_reads_sources(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
            (tmp / "__pycache__").mkdir()
            (tmp / "__pycache__" / "x.pyc").write_text("junk", encoding="utf-8")
            ctx = artifacts.existing_code_context(tmp)
            self.assertIn("def main", ctx)
            self.assertIn("app.py", ctx)
            self.assertNotIn("junk", ctx)      # 跳过缓存目录

    def test_review_prompt_includes_existing_code(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "core.py").write_text("SECRET_MARKER = 1\n", encoding="utf-8")
            raw = {"task_workdir": ".", "state_dir": ".sr",
                   "stages": [{"name": "review", "adapter": "fake",
                               "prompt": "审查 {upstream}", "kind": "review"}],
                   "adapters": {"fake": {"type": "fake"}}}
            cfg = parse_config(raw, base_dir=tmp)
            eng = Engine(cfg, {"fake": FakeAdapter()}, run_id="sr", task="t")
            eng.prepare()
            rendered = eng._render_prompt(cfg.stage("review"), 0)
            self.assertIn("SECRET_MARKER", rendered)
            # 纯 review 不应给被审项目建 git 仓库
            self.assertFalse((tmp / ".git").exists())


class TestG5ApiReviewKind(unittest.TestCase):
    """G5 同类：API 适配器在 review 阶段须产报告，不能误当代码提取。"""

    def test_api_review_kind_produces_report(self):
        import io, json, os
        from orchestrator.adapters.openai_compat import OpenAICompatAdapter
        ad = OpenAICompatAdapter("api", "https://e.test/v1", "m", "TEST_K",
                                 kind="report")
        verdict = '```json\n{"verdict":"pass","issues":[]}\n```'
        body = json.dumps({"choices": [{"message": {"content": verdict}}],
                           "usage": {"total_tokens": 5}}).encode()
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ctx = RunContext(task="t", run_id="r", stage="review",
                             workdir=tmp, runs_dir=tmp / "runs", kind="review")
            with mock.patch.dict(os.environ, {"TEST_K": "sk"}), \
                 mock.patch("orchestrator.adapters.openai_compat._open_request", return_value=io.BytesIO(body)):
                res = ad.run("p", tmp, ctx)
            self.assertTrue(res.ok)
            self.assertEqual(len(res.report_files), 1)   # 产出报告
            self.assertEqual(res.changed_files, [])      # 不误当代码
            self.assertTrue(res.is_meaningful("review"))


class TestN1RerunClearsApproval(unittest.TestCase):
    def test_rerun_from_awaiting_review_actually_reruns(self):
        class CountingReview(Adapter):
            channel = "fake"
            def __init__(self):
                self.name = "review"; self.calls = 0
            def preflight(self):
                return PreflightReport(adapter=self.name, ok=True)
            def run(self, prompt, workdir, ctx):
                self.calls += 1
                rd = Path(ctx.runs_dir) / "reports"; rd.mkdir(parents=True, exist_ok=True)
                rp = rd / f"review_report.md"
                rp.write_text("# 审查报告（不参与判定）\n", encoding="utf-8")
                vf = Path(ctx.runs_dir) / "review_verdict.json"
                if self.calls == 1:
                    # 首轮不写结论文件 => 结论缺失 => awaiting
                    if vf.exists():
                        vf.unlink()
                else:
                    import json as _j
                    vf.write_text(_j.dumps({"verdict": "pass", "issues": []}),
                                  encoding="utf-8")
                return Result(ok=True, exit_code=0, channel=self.channel,
                              text="报告", report_files=[rp])
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            raw = {"task_workdir": "ws", "state_dir": ".s",
                   "stages": [
                       {"name": "coding", "adapter": "fake", "prompt": "x", "kind": "code"},
                       {"name": "review", "adapter": "review", "prompt": "x",
                        "kind": "review", "feedback_to": "coding", "max_rounds": 2}],
                   "adapters": {"fake": {"type": "fake"}, "review": {"type": "fake"}}}
            cfg = parse_config(raw, base_dir=tmp)
            review = CountingReview()
            eng = Engine(cfg, {"fake": FakeAdapter(), "review": review},
                         run_id="n1", task="t")
            res = eng.run()
            self.assertEqual(res["review"], StageStatus.AWAITING_APPROVAL.value)
            self.assertEqual(review.calls, 1)
            attempts_before = len(eng.state.stage("review").attempts)
            # rerun-from review：必须清审批 + 真正实跑（不被旧 success.json 采纳）
            eng2 = Engine(cfg, {"fake": FakeAdapter(), "review": review},
                          run_id="n1", task="t")
            res2 = eng2.rerun_from("review")
            self.assertEqual(review.calls, 2)                       # 适配器被再次调用
            self.assertGreater(len(eng2.state.stage("review").attempts),
                               attempts_before)                      # 产生新 attempt
            self.assertEqual(res2["review"], StageStatus.DONE.value)  # 重跑给出 pass
            self.assertTrue(any(e["kind"] == "rerun_reset"
                                for e in eng2.state.events))


class TestG5CliReport(unittest.TestCase):
    def test_cli_writes_report_for_review_kind(self):
        ad = DshAdapter(name="dsh")
        ad._version = "dsh 1.0"
        ad._resolved = "dsh"
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ctx = RunContext(task="t", run_id="r", stage="review",
                             workdir=tmp, runs_dir=tmp / "runs", kind="review")
            proc = mock.Mock(returncode=0, stdout="审查输出文本", stderr="")
            res = ad._parse(proc, tmp, ctx)
            res = ad._finalize(res, ctx)
            self.assertTrue(res.ok)
            self.assertEqual(len(res.report_files), 1)
            self.assertTrue(res.report_files[0].is_file())

    def test_cli_code_kind_no_report(self):
        ad = DshAdapter(name="dsh")
        ad._version = "dsh 1.0"
        ad._resolved = "dsh"
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ctx = RunContext(task="t", run_id="r", stage="coding",
                             workdir=tmp, runs_dir=tmp / "runs", kind="code")
            proc = mock.Mock(returncode=0, stdout="代码输出", stderr="")
            res = ad._finalize(ad._parse(proc, tmp, ctx), ctx)
            self.assertEqual(res.report_files, [])


if __name__ == "__main__":
    unittest.main()
