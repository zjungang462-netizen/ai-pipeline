"""Structured wait receipts are sealed and resumed only from a verified checkpoint."""
from __future__ import annotations

import json
import contextlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.adapters.base import Adapter
from orchestrator.adapters.fake import FakeAdapter
from orchestrator.config import parse_config
from orchestrator.contracts import PreflightReport, Result, StageStatus
from orchestrator.cli import _print_results
from orchestrator.collaboration.projection import lifecycle
from orchestrator.collaboration.control import Control
from orchestrator.collaboration.storage import Store
from orchestrator.engine import Engine
from orchestrator.stage_result import extract_deferred, validate_deferred
from orchestrator import worker


def receipt(outcome="waiting_external", *, next_check_at=None):
    return {"outcome": outcome, "reason": "等待部署完成", "condition": "线上版本号更新",
            "resume_action": "核对版本号后继续剩余验收", "next_check_at": next_check_at}


class DeferredAdapter(Adapter):
    channel = "fake"

    def __init__(self, name="defer", first_result=None):
        self.name = name
        self.first_result = first_result
        self.calls = 0

    def preflight(self):
        return PreflightReport(adapter=self.name, ok=True)

    def run(self, prompt, workdir, ctx):
        self.calls += 1
        report = ctx.attempt_dir / "output.md"
        report.write_text("检查进度", encoding="utf-8")
        result = Result(ok=True, exit_code=0, channel=self.channel, text="检查进度",
                        report_files=[report])
        if self.calls == 1 and self.first_result is not None:
            value = validate_deferred(self.first_result)
            result.deferred = extract_deferred(
                "```json\n# file: stage_result.json\n" + json.dumps(value, ensure_ascii=False)
                + "\n```", ctx.attempt_dir)
        return result


class StageResultProtocol(unittest.TestCase):
    def test_strict_result_validation_and_extraction(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            text = "```json\n# file: stage_result.json\n" + json.dumps(receipt()) + "\n```"
            self.assertEqual(extract_deferred(text, folder), receipt())
            self.assertEqual(json.loads((folder / "stage_result.json").read_text(encoding="utf-8")), receipt())
            for invalid in (
                {**receipt(), "command": "delete everything"},
                {**receipt(), "condition": ""},
                {**receipt(), "next_check_at": float("nan")},
                {**receipt(), "resume_action": " "},
            ):
                with self.assertRaises(ValueError):
                    validate_deferred(invalid)
            with self.assertRaises(ValueError):
                extract_deferred(text + "\n" + text, folder)

    def _engine(self, root, adapter, *, stages=None, run_id="wait-1"):
        config = parse_config({
            "task_workdir": "workspace", "state_dir": ".state",
            "stages": stages or [
                {"name": "requirements", "adapter": "fake", "prompt": "requirements", "kind": "report"},
                {"name": "deployment", "adapter": "defer", "prompt": "deployment", "kind": "report",
                 "allow_wait": True},
            ],
            "adapters": {"fake": {"type": "fake"}, "defer": {"type": "fake"}},
        }, base_dir=root)
        return Engine(config, {"fake": FakeAdapter(name="fake"), "defer": adapter}, run_id, "deploy service")

    def test_wait_is_nonterminal_sealed_and_resumes_only_unfinished_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapter = DeferredAdapter(first_result=receipt(next_check_at=0))
            engine = self._engine(root, adapter)
            result = engine.run()
            self.assertEqual(result["deployment"], StageStatus.WAITING_EXTERNAL.value)
            self.assertFalse((engine.state.run_dir / "metrics.json").exists())
            wait = engine.state.pending_waits["deployment"]
            attempt = engine.state.current_attempt_dir("deployment")
            from orchestrator.evidence import validate
            self.assertTrue(validate(attempt, {"n": wait["n"], "inputs_hash": wait["inputs_hash"],
                                               "outputs_hash": wait["outputs_hash"]}))

            resumed = self._engine(root, adapter)
            self.assertEqual(resumed.run()["deployment"], StageStatus.WAITING_EXTERNAL.value)
            self.assertEqual(adapter.calls, 1)
            result = resumed.run(resume_wait=True)
            self.assertEqual(result["requirements"], StageStatus.DONE.value)
            self.assertEqual(result["deployment"], StageStatus.DONE.value)
            self.assertEqual(resumed.adapters["fake"].calls, 0)
            self.assertEqual(adapter.calls, 2)
            self.assertFalse(resumed.state.pending_waits)
            self.assertEqual(resumed.run()["deployment"], StageStatus.DONE.value)
            self.assertEqual(adapter.calls, 2)

    def test_wait_exit_code_is_nonterminal(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(_print_results({"deployment": StageStatus.WAITING_EXTERNAL.value}), 11)
            self.assertEqual(_print_results({"deployment": StageStatus.WAITING_OWNER.value}), 11)

    def test_worker_returns_distinct_nonterminal_wait_codes(self):
        class EngineStub:
            def __init__(self, config, adapters, run_id, task, control=None):
                self.state = type("State", (), {"usage_total": {}})()

            def run(self):
                return {"stage": self.status}

        config = type("Config", (), {"source_hash": "hash", "adapters": {}, "stages": []})()
        with tempfile.TemporaryDirectory() as temp:
            job_path = Path(temp) / "job.json"
            job_path.write_text(json.dumps({"config": "config.toml", "config_hash": "hash",
                                            "run_id": "r", "task": "task"}), encoding="utf-8")
            for status, code in ((StageStatus.WAITING_EXTERNAL.value, 13),
                                 (StageStatus.WAITING_OWNER.value, 14)):
                engine_type = type("EngineStub", (EngineStub,), {"status": status})
                with mock.patch.object(worker, "load_config", return_value=config), \
                     mock.patch.object(worker, "build_all", return_value={}), \
                     mock.patch.object(worker, "Engine", engine_type), \
                     mock.patch.object(worker.sys, "argv", ["worker", str(job_path)]), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(worker.main(), code)

    def test_future_external_wait_does_not_call_agent_or_write_terminal_metrics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapter = DeferredAdapter(first_result=receipt(next_check_at=time.time() + 3600))
            engine = self._engine(root, adapter)
            self.assertEqual(engine.run()["deployment"], StageStatus.WAITING_EXTERNAL.value)
            resumed = self._engine(root, adapter)
            self.assertEqual(resumed.run(resume_wait=True)["deployment"], StageStatus.WAITING_EXTERNAL.value)
            self.assertEqual(adapter.calls, 1)
            self.assertFalse((resumed.state.run_dir / "metrics.json").exists())

    def test_tampered_wait_attempt_fails_closed_without_repeating_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapter = DeferredAdapter(first_result=receipt(next_check_at=0))
            engine = self._engine(root, adapter)
            engine.run()
            attempt = engine.state.current_attempt_dir("deployment")
            (attempt / "output.md").write_text("altered", encoding="utf-8")
            resumed = self._engine(root, adapter)
            result = resumed.run()
            self.assertEqual(result["deployment"], StageStatus.FAILED.value)
            self.assertEqual(adapter.calls, 1)
            self.assertIn("拒绝自动重跑", resumed.state.stage("deployment").error)

    def test_owner_wait_resumes_only_after_explicit_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapter = DeferredAdapter(first_result=receipt("waiting_owner", next_check_at=time.time() + 3600))
            engine = self._engine(root, adapter)
            self.assertEqual(engine.run()["deployment"], StageStatus.WAITING_OWNER.value)
            resumed = self._engine(root, adapter)
            self.assertEqual(resumed.run()["deployment"], StageStatus.WAITING_OWNER.value)
            self.assertEqual(resumed.run(resume_wait=True)["deployment"], StageStatus.DONE.value)
            self.assertEqual(adapter.calls, 2)

    def test_managed_resume_is_bound_to_the_wait_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / 'workflow.toml'
            config_path.touch()
            adapter = DeferredAdapter(first_result=receipt('waiting_owner'))
            engine = self._engine(root, adapter)
            engine.run()
            store = Store(root / 'commands.sqlite3')
            project = store.register_project(config_path)
            key = store.register_run(project, engine.run_id, config_path, engine.config.state_dir,
                                     managed=True)
            resumed = self._engine(root, adapter)
            resumed.control = Control(store.path, key)
            stage = engine.state.stage('deployment')
            store.enqueue(key, 'resume', {'wait_target': ['deployment', 9, stage.outputs_hash]}, 'stale-wait')
            self.assertEqual(resumed.run()['deployment'], 'waiting_owner')
            self.assertEqual(adapter.calls, 1)
            store.enqueue(key, 'resume', {'wait_target': ['deployment', 1, stage.outputs_hash]}, 'current-wait')
            self.assertEqual(resumed.run()['deployment'], 'done')
            self.assertEqual(resumed.run()['deployment'], 'done')
            self.assertEqual(adapter.calls, 2)

    def test_complete_wait_is_recovered_after_state_commit_crash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapter = DeferredAdapter(first_result=receipt(next_check_at=0))
            engine = self._engine(root, adapter)
            engine.run()
            saved = json.loads(engine.state.state_file.read_text(encoding="utf-8"))
            saved["stages"]["deployment"].update(status="running", current_attempt=None)
            saved["pending_waits"] = {}
            engine.state.state_file.write_text(json.dumps(saved), encoding="utf-8")

            recovered = self._engine(root, adapter)
            result = recovered.run()
            self.assertEqual(result["deployment"], StageStatus.WAITING_EXTERNAL.value)
            self.assertEqual(adapter.calls, 1)
            self.assertEqual(recovered.state.pending_waits["deployment"]["n"], 1)
            self.assertEqual(lifecycle({"stages": {"deployment": {"status": "waiting_external"}}}, {}),
                             "waiting_external")

    def test_partial_wait_attempt_fails_closed_after_crash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapter = DeferredAdapter(first_result=receipt(next_check_at=0))
            engine = self._engine(root, adapter)
            engine.run()
            attempt = engine.state.current_attempt_dir("deployment")
            (attempt / "success.json").unlink()
            (attempt / "decision.json").unlink()
            saved = json.loads(engine.state.state_file.read_text(encoding="utf-8"))
            saved["stages"]["deployment"].update(status="interrupted", current_attempt=None)
            saved["pending_waits"] = {}
            engine.state.state_file.write_text(json.dumps(saved), encoding="utf-8")

            recovered = self._engine(root, adapter)
            result = recovered.run()
            self.assertEqual(result["deployment"], StageStatus.FAILED.value)
            self.assertEqual(adapter.calls, 1)
            self.assertIn("attempt 未完整封存", recovered.state.stage("deployment").error)


if __name__ == "__main__":
    unittest.main()
