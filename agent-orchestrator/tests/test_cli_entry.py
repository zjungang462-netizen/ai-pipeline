"""第 4 阶段：命令行入口冒烟测试（run/status/approve/resume/rerun-from）。"""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from orchestrator import cli
from orchestrator.cli import main

ROOT = Path(__file__).resolve().parents[1]


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
            # design 等待审批 => 未完成，退出码 11（与 worker 对齐）
            self.assertEqual(rc, 11)
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

    def test_piped_chinese_output_survives_narrow_encoding(self):
        # 非中文 Windows 上 stdout 走管道时编码是 cp1252，中文输出不得让进程崩溃。
        env = {k: v for k, v in os.environ.items()
               if k not in ("PYTHONUTF8", "PYTHONIOENCODING", "PIPELINE_LANG")}
        env["PYTHONIOENCODING"] = "cp1252"
        run = subprocess.run([sys.executable, "-m", "orchestrator.cli", "--help"], cwd=ROOT, env=env,
                             capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr.decode("utf-8", "replace"))
        self.assertIn("多智能体开发流水线编排器", run.stdout.decode("utf-8"))

    def test_imported_main_hardens_piped_output_without_module_entrypoint(self):
        stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        with mock.patch.object(cli.sys, "stdout", stdout), mock.patch.object(cli.sys, "stderr", stderr), \
                mock.patch.dict(os.environ, {"PIPELINE_LANG": "zh"}):
            with self.assertRaises(SystemExit) as raised:
                main(["--lang", "zh", "--help"])
        self.assertEqual(raised.exception.code, 0)
        stdout.flush()
        self.assertIn("多智能体开发流水线编排器", stdout.buffer.getvalue().decode("utf-8"))

    def test_english_help_supports_flag_after_command_and_environment(self):
        for argv in (["--lang", "en", "--help"], ["doctor", "--lang", "en", "--help"]):
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                main(argv)
            self.assertEqual(raised.exception.code, 0)
            self.assertFalse(any("\u4e00" <= char <= "\u9fff" for char in output.getvalue()))
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"PIPELINE_LANG": "en"}), redirect_stdout(output):
            with self.assertRaises(SystemExit):
                main(["--help"])
        self.assertIn("Multi-agent development pipeline orchestrator", output.getvalue())

    def test_every_english_subcommand_help_is_cjk_free(self):
        commands = ("state-check state-backup state-restore state-migrate deploy worker-serve "
                    "export-usage doctor clean run resume status approve rerun-from tune "
                    "lessons-propose lessons-approve route-recommend approve-tune autotune "
                    "unattended dashboard").split()
        for command in commands:
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                main(["--lang", "en", command, "--help"])
            self.assertEqual(raised.exception.code, 0, command)
            self.assertFalse(any("\u4e00" <= char <= "\u9fff" for char in output.getvalue()), command)

    def test_english_doctor_demo_has_no_chinese_owned_text(self):
        output = io.StringIO()
        with redirect_stdout(output):
            rc = main(["--lang", "en", "doctor", str(ROOT / "kiroween-demo.example.toml")])
        self.assertEqual(rc, 0, output.getvalue())
        self.assertFalse(any("\u4e00" <= char <= "\u9fff" for char in output.getvalue()))

    def test_dashboard_startup_language_selects_url_and_message(self):
        from orchestrator.dashboard.server import serve
        for language, prefix, marker in (("en", "Workbench: ", "/?lang=en#token="),
                                         ("zh", "工作台：", "/#token=")):
            app = SimpleNamespace(origin="http://127.0.0.1:8799", bootstrap="fixture", closed=mock.Mock())
            server = mock.Mock(application=app)
            output = io.StringIO()
            with mock.patch("orchestrator.dashboard.server.create_server", return_value=server), \
                    redirect_stdout(output):
                serve(Path("fixture.sqlite3"), [], 8799, language=language)
            self.assertIn(prefix + app.origin + marker, output.getvalue())
            app.closed.set.assert_called_once_with()
            server.server_close.assert_called_once_with()

    def test_dashboard_forwards_explicit_host_to_server(self):
        args = SimpleNamespace(config=[], database='fixture.sqlite3', port=8766,
                               open=False, host='0.0.0.0', origin='http://workbench.test:8766', lang='en')
        with mock.patch('orchestrator.dashboard.server.serve') as serve:
            self.assertEqual(cli.cmd_dashboard(args), 0)
        serve.assert_called_once_with(Path('fixture.sqlite3').resolve(), [], 8766,
                                      host='0.0.0.0', open_browser=False, language='en',
                                      origin='http://workbench.test:8766')

    def test_dashboard_requires_origin_for_non_loopback_host(self):
        args = SimpleNamespace(config=[], database='fixture.sqlite3', port=8766,
                               open=False, host='0.0.0.0', origin=None, lang='en')
        with mock.patch('orchestrator.dashboard.server.serve') as serve:
            with self.assertRaisesRegex(ValueError, '--origin is required'):
                cli.cmd_dashboard(args)
        serve.assert_not_called()

    def test_dashboard_rejects_empty_host(self):
        args = SimpleNamespace(config=[], database='fixture.sqlite3', port=8766,
                               open=False, host='  ', origin=None, lang='en')
        with mock.patch('orchestrator.dashboard.server.serve') as serve:
            with self.assertRaisesRegex(ValueError, '--host must not be empty'):
                cli.cmd_dashboard(args)
        serve.assert_not_called()

    def test_worker_serve_closes_server_on_exit_and_failure(self):
        args = SimpleNamespace(secret_env='WORKER_TEST_SECRET', worker_id='worker-test', host='127.0.0.1', port=8766)
        for error in (None, RuntimeError('join failed'), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                server = mock.Mock(port=8766)
                server._thread.join.side_effect = error
                with mock.patch.dict(os.environ, {'WORKER_TEST_SECRET': 'test-secret'}), \
                        mock.patch('orchestrator.remote_worker.RemoteWorkerServer', return_value=server), \
                        redirect_stdout(io.StringIO()):
                    if isinstance(error, RuntimeError):
                        with self.assertRaisesRegex(RuntimeError, 'join failed'):
                            cli.cmd_worker_serve(args)
                    else:
                        self.assertEqual(cli.cmd_worker_serve(args), 0)
                server.start.assert_called_once_with()
                server.close.assert_called_once_with()

    def test_worker_serve_closes_server_when_thread_start_fails(self):
        args = SimpleNamespace(secret_env='WORKER_TEST_SECRET', worker_id='worker-test', host='127.0.0.1', port=8766)
        server = mock.Mock(port=8766)
        server.start.side_effect = RuntimeError('thread start failed')
        with mock.patch.dict(os.environ, {'WORKER_TEST_SECRET': 'test-secret'}), \
                mock.patch('orchestrator.remote_worker.RemoteWorkerServer', return_value=server), \
                redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'thread start failed'):
                cli.cmd_worker_serve(args)
        server.close.assert_called_once_with()

    def test_english_wait_result_explains_exit_eleven(self):
        output = io.StringIO()
        with redirect_stdout(output):
            rc = cli._print_results({"acceptance": "awaiting_approval"}, "en")
        self.assertEqual(rc, 11)
        self.assertIn("awaits human approval", output.getvalue())

    def test_rejected_approval_does_not_claim_unproven_artifact_change(self):
        config = SimpleNamespace(stages=[SimpleNamespace(name="design")])
        engine = mock.Mock()
        engine.state.load.return_value = True
        engine.state.pending_approvals = {"design"}
        engine.approve.return_value = False
        output = io.StringIO()
        with mock.patch.object(cli, "load_config", return_value=config), \
                mock.patch.object(cli, "_build_engine", return_value=engine), redirect_stdout(output):
            rc = main(["--lang", "zh", "approve", "fixture.toml", "--run-id", "r", "--stage", "design"])
        self.assertEqual(rc, 1)
        self.assertNotIn("产物在申请审批后发生变化", output.getvalue())
        self.assertIn("请刷新状态后重试", output.getvalue())

    def test_unknown_approval_stage_is_reported_before_building_engine(self):
        config = SimpleNamespace(stages=[SimpleNamespace(name="design")])
        output = io.StringIO()
        with mock.patch.object(cli, "load_config", return_value=config), \
                mock.patch.object(cli, "_build_engine") as build, redirect_stdout(output):
            rc = main(["--lang", "en", "approve", "fixture.toml", "--run-id", "r", "--stage", "other"])
        self.assertEqual(rc, 2)
        self.assertIn("Unknown stage: other", output.getvalue())
        build.assert_not_called()

    def test_deploy_separator_is_extracted_only_for_the_real_subcommand(self):
        deploy = ["--lang", "en", "deploy", "fixture.toml", "--run-id", "r",
                  "--require-stage", "acceptance", "--", "python", "--lang", "en"]
        command, remaining = cli._split_deploy_argv(deploy)
        self.assertEqual(command, ["python", "--lang", "en"])
        self.assertEqual(remaining, deploy[:8])
        self.assertEqual(cli._requested_language(remaining), "en")
        run = ["run", "deploy", "--task", "x", "--", "unexpected"]
        self.assertEqual(cli._split_deploy_argv(run), ([], run))
        self.assertEqual(cli._requested_language(["deploy", "fixture.toml", "--", "--lang", "en"]), "zh")
        with mock.patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(run)
        self.assertEqual(raised.exception.code, 2)

    def test_english_configuration_and_deploy_errors_are_localized(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "missing.toml")
            for argv, expected in (
                (["run", missing, "--task", "x"], "Configuration file does not exist"),
                (["doctor", missing], "Cannot read UTF-8 TOML"),
                (["deploy", missing, "--run-id", "r", "--require-stage", "acceptance"],
                 "deploy requires a fixed argv command after --"),
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    rc = main(["--lang", "en", *argv])
                self.assertEqual(rc, 2 if argv[0] == "run" else 1 if argv[0] == "doctor" else 2)
                self.assertIn(expected, output.getvalue())
                self.assertFalse(any("\u4e00" <= char <= "\u9fff" for char in output.getvalue()))

    def test_english_required_configuration_errors_are_localized(self):
        cases = (
            ('state_dir="state"\n', 'Missing required field: task_workdir'),
            ('task_workdir="workspace"\n', 'Missing required field: state_dir'),
            ('task_workdir="workspace"\nstate_dir="state"\n',
             'At least one [[stages]] entry is required'),
            ('task_workdir="workspace"\nstate_dir="state"\nserial=false\n'
             '[[stages]]\nname="test"\nkind="test"\ntest_cmd=["python","-c","pass"]\n',
             'Only serial=true is currently supported'),
        )
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'invalid.toml'
            for content, expected in cases:
                with self.subTest(expected=expected):
                    config.write_text(content, encoding='utf-8')
                    output = io.StringIO()
                    with redirect_stdout(output):
                        rc = main(['--lang', 'en', 'run', str(config), '--task', 'x'])
                    self.assertEqual(rc, 2)
                    self.assertIn(expected, output.getvalue())
                    self.assertFalse(any('\u4e00' <= char <= '\u9fff' for char in output.getvalue()))

    def test_english_deploy_rejection_is_localized(self):
        from orchestrator.deploy import DeployRejected

        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            config = SimpleNamespace(state_dir=tmp / ".state", task_workdir=tmp)
            state = mock.Mock()
            state.load.return_value = True
            output = io.StringIO()
            with mock.patch.object(cli, "load_config", return_value=config), \
                    mock.patch("orchestrator.state.RunState", return_value=state), \
                    mock.patch("orchestrator.deploy.execute_deploy",
                               side_effect=DeployRejected("审批 run_id 与当前运行不一致")), \
                    redirect_stdout(output):
                rc = main(["--lang", "en", "deploy", "fixture.toml", "--run-id", "r",
                           "--require-stage", "acceptance", "--", "python", "-V"])
            self.assertEqual(rc, 2)
            self.assertIn("Deployment rejected: Approval run_id does not match the current run",
                          output.getvalue())
            self.assertFalse(any("\u4e00" <= char <= "\u9fff" for char in output.getvalue()))


if __name__ == "__main__":
    unittest.main()
