"""历史指标与人工批准的离线策略优化，不调用模型。"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.adapters.fake import FakeAdapter
from orchestrator.adapters.openai_compat import OpenAICompatAdapter
from orchestrator.cli import main
from orchestrator.config import ConfigError, load_config, parse_config
from orchestrator.contracts import PreflightReport, RunContext
from orchestrator.engine import Engine
from orchestrator.state import atomic_write_json, sha256_file
from orchestrator.tuning import approve_proposal, propose


class PolicyTuning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.state = self.base / ".s"
        self.config = self.base / "config.toml"
        self.config.write_text('''task_workdir = "ws"
state_dir = ".s"
[[stages]]
name = "coding"
kind = "report"
adapter = "fake"
prompt = "code"
[[stages]]
name = "review"
kind = "review"
adapter = "fake"
prompt = "review"
feedback_to = "coding"
max_rounds = 3 # 保留注释
[adapters.fake]
type = "fake"
''', encoding="utf-8")

    def records(self, count=5, tokens=100):
        for index in range(count):
            atomic_write_json(self.state / "runs" / f"r{index}" / "metrics.json", {
                "run_id": f"r{index}", "created_at": index,
                "config_path": str(self.config), "config_hash": sha256_file(self.config),
                "tokens": tokens, "status": "done", "verify_passed": True,
                "stages": {"review": {"status": "done", "rounds": 1}},
                "fail_reasons": [],
            })

    def test_tune_is_proposal_only_and_source_unchanged(self):
        self.records()
        original = self.config.read_bytes()
        source = Path(__file__).resolve().parents[1] / "orchestrator" / "engine.py"
        source_hash = sha256_file(source)
        proposal = propose(self.state, self.config)
        self.assertEqual(proposal["changes"][0]["new"], 2)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(sha256_file(source), source_hash)
        self.assertFalse((self.base / "tuned.toml").exists())

    def test_approve_writes_only_config_copy_and_preserves_paths(self):
        self.records()
        original = self.config.read_bytes()
        proposal = propose(self.state, self.config)
        output = approve_proposal(self.state, self.state / "tuning" / "tune_proposal.json",
                                  self.base / "tuned.toml")
        updated = load_config(output)
        self.assertEqual(updated.stage("review").max_rounds, 2)
        self.assertEqual(updated.tune_proposal_id, proposal["id"])
        self.assertEqual(updated.task_workdir, load_config(self.config).task_workdir)
        self.assertIn("# 保留注释", output.read_text(encoding="utf-8"))
        self.assertEqual(self.config.read_bytes(), original)
        with self.assertRaises(ConfigError):
            approve_proposal(self.state, self.state / "tuning" / "tune_proposal.json", output)

    def test_approval_refuses_unallowed_field_or_stale_source(self):
        self.records()
        proposal = propose(self.state, self.config)
        proposal["changes"][0]["key"] = "allow_yolo"
        target = self.state / "tuning" / "tune_proposal.json"
        atomic_write_json(target, proposal)
        with self.assertRaises(ConfigError):
            approve_proposal(self.state, target, self.base / "tuned.toml")
        proposal["changes"][0]["key"] = "max_rounds"
        atomic_write_json(target, proposal)
        with self.config.open("a", encoding="utf-8") as stream:
            stream.write("\n# changed\n")
        with self.assertRaises(ConfigError):
            approve_proposal(self.state, target, self.base / "tuned.toml")
        self.assertFalse((self.base / "tuned.toml").exists())

    def test_no_proposal_with_insufficient_samples(self):
        self.records(3)
        proposal = propose(self.state, self.config)
        self.assertEqual(proposal["changes"], [])
        self.assertIn("不足", proposal["reason"])

    def test_failed_run_with_successful_stage_does_not_reduce_rounds(self):
        self.records()
        path = self.state / 'runs/r2/metrics.json'
        record = json.loads(path.read_text(encoding='utf8'))
        record.update(status='failed', fail_reasons=['review_exhausted'])
        atomic_write_json(path, record)
        proposal = propose(self.state, self.config)
        self.assertEqual(proposal['changes'], [])

    def test_nonterminal_run_does_not_reduce_rounds(self):
        self.records()
        path = self.state / 'runs/r2/metrics.json'
        record = json.loads(path.read_text(encoding='utf8'))
        record['status'] = 'running'
        atomic_write_json(path, record)
        self.assertEqual(propose(self.state, self.config)['changes'], [])

    def test_cost_regression_stops_new_suggestions(self):
        identifier = "tune-000000000001"
        original = self.config.read_text(encoding="utf-8")
        self.config.write_text(f'tune_proposal_id = "{identifier}"\n' + original, encoding="utf-8")
        atomic_write_json(self.state / "tuning" / f"{identifier}.approved.json", {
            "baseline": {"tokens_median": 100, "failure_rate": 0.5}})
        self.records(tokens=121)
        proposal = propose(self.state, self.config)
        self.assertEqual(proposal["changes"], [])
        self.assertIn("20%", proposal["reason"])

    def test_two_ineffective_adjustments_stop_suggestions(self):
        identifier = "tune-000000000001"
        original = self.config.read_text(encoding="utf-8")
        self.config.write_text(f'tune_proposal_id = "{identifier}"\n' + original, encoding="utf-8")
        atomic_write_json(self.state / "tuning" / f"{identifier}.approved.json", {
            "baseline": {"tokens_median": 100, "failure_rate": 0}, "no_improvement_count": 1})
        self.records()
        proposal = propose(self.state, self.config)
        self.assertEqual(proposal["changes"], [])
        self.assertIn("连续两次", proposal["reason"])

    def test_cli_tune_and_approve_flow(self):
        self.records()
        original = self.config.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["tune", "--state-dir", str(self.state), "--last", "5"]), 0)
            self.assertEqual(self.config.read_bytes(), original)
            self.assertEqual(main(["approve-tune", "--state-dir", str(self.state),
                                   "--output", str(self.base / "tuned.toml")]), 0)
        self.assertEqual(load_config(self.base / "tuned.toml").stage("review").max_rounds, 2)

    def test_metrics_are_terminal_and_immutable(self):
        cfg = parse_config({"task_workdir": "ws", "state_dir": ".s", "stages": [
            {"name": "draft", "adapter": "fake", "kind": "report", "prompt": "p", "approval": True}],
            "adapters": {"fake": {"type": "fake"}}}, base_dir=self.base)
        with mock.patch("orchestrator.engine.artifacts.git_available", return_value=False):
            engine = Engine(cfg, {"fake": FakeAdapter(kind="report")}, "metrics", "task")
        engine.run()
        file = engine.state.run_dir / "metrics.json"
        self.assertFalse(file.exists())
        self.assertTrue(engine.approve("draft"))
        engine.run()
        first = file.read_bytes()
        engine.run()
        self.assertEqual(file.read_bytes(), first)

    def test_api_honors_stage_timeout(self):
        adapter = OpenAICompatAdapter("api", "https://example.invalid", "model", "POLICY_TEST_KEY")
        context = RunContext("task", "r", "report", self.base, self.base / "run", timeout_s=1, kind="report")
        response = io.BytesIO(json.dumps({"choices": [{"message": {"content": "body"}}]}).encode())
        with mock.patch.dict("os.environ", {"POLICY_TEST_KEY": "dummy"}), \
                mock.patch("urllib.request.urlopen", return_value=response) as request:
            self.assertTrue(adapter.run("p", self.base, context).ok)
        self.assertEqual(request.call_args.kwargs["timeout"], 1)

    def test_cli_adapter_reacquires_version_after_approval(self):
        import subprocess
        cfg = parse_config({"task_workdir": "ws", "state_dir": ".s", "stages": [
            {"name": "design", "adapter": "qwen", "kind": "report", "prompt": "p", "approval": True},
            {"name": "next", "adapter": "qwen", "kind": "report", "prompt": "p"}],
            "adapters": {"qwen": {"type": "cli_qwen", "exe": sys.executable}}}, base_dir=self.base)
        response = subprocess.CompletedProcess([], 0, '{"result":"body"}', "")
        def process(command, **kwargs):
            if command[-1] == '--version':
                return subprocess.CompletedProcess([], 0, 'qwen-code 0.5.0', '')
            return response
        with mock.patch("orchestrator.cli.load_config", return_value=cfg), \
                mock.patch("orchestrator.engine.artifacts.git_available", return_value=False), \
                mock.patch("orchestrator.adapters.cli_base.run_cli_process", side_effect=process), \
                contextlib.redirect_stdout(io.StringIO()):
            # design 等待审批 => 退出码 11（未完成）；approve 后全部完成 => 0
            self.assertEqual(main(["run", "unused.toml", "--run-id", "r", "--task", "task"]), 11)
            self.assertEqual(main(["approve", "unused.toml", "--run-id", "r", "--stage", "design"]), 0)


if __name__ == "__main__":
    unittest.main()
