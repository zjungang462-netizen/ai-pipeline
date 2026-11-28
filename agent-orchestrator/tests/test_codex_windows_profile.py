"""隔离配置保留显式 Windows 沙箱；成功工具抓包的离线回归。"""
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.adapters.cli_codex import CodexAdapter
from orchestrator.cli_telemetry import CLITrace
from orchestrator.cli_tools import codex_activity
from orchestrator.contracts import RunContext


FIXTURES = Path(__file__).parent/'fixtures'


class CodexWindowsProfile(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = self.root/'config.toml'
        self.env = mock.patch.dict(os.environ, {'CODEX_HOME': str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.ctx = RunContext('fixture', 'windows-profile', 'design', self.root, self.root,
                              kind='report', attempt_dir=self.root/'attempt')
        self.raw = (FIXTURES/'codex_0.169_tools_real.jsonl').read_text(encoding='utf8')

    def write_profile(self, tail=''):
        self.profile.write_text('model="fixture"\nmodel_provider="custom"\n'
                                '[model_providers.custom]\nname="fixture"\n'
                                'base_url="https://example.invalid"\nwire_api="responses"\n'
                                'experimental_bearer_token="sk-private-'
                                'profile-secret"\n'
                                + tail, encoding='utf8')

    def run_adapter(self, adapter):
        adapter._resolved = 'codex'
        adapter._version = 'codex-cli 0.169.0'
        with mock.patch('orchestrator.adapters.cli_base.run_cli_process',
                        return_value=subprocess.CompletedProcess([], 0, self.raw, '')) as execute:
            result = adapter.run('fixture', self.root, self.ctx)
        self.assertTrue(result.ok)
        return execute.call_args.args[0]

    @unittest.skipUnless(os.name == 'nt', 'Windows 沙箱配置仅适用于 Windows')
    def test_isolated_profile_keeps_explicit_backend_without_global_extensions(self):
        for backend in ('unelevated', 'elevated', 'mxc'):
            with self.subTest(backend=backend):
                self.write_profile('[windows]\nsandbox="' + backend + '"\n'
                                   '[mcp_servers.desktop]\ncommand="private-desktop"\n'
                                   '[features]\nunknown_feature=true\n')
                before = self.profile.read_bytes()
                adapter = CodexAdapter('codex', isolate_config=True)
                args = self.run_adapter(adapter)
                self.assertIn('windows.sandbox="' + backend + '"', args)
                self.assertIn('--ignore-user-config', args)
                self.assertEqual(args[args.index('--sandbox')+1], 'read-only')
                self.assertEqual(adapter.input_fingerprint()['windows_sandbox'], backend)
                self.assertNotIn('sk-private-profile-secret', json.dumps(args))  # pragma: allowlist secret
                self.assertNotIn('sk-private-profile-secret', json.dumps(adapter.input_fingerprint()))  # pragma: allowlist secret
                self.assertNotIn('mcp_servers', json.dumps(args))
                self.assertNotIn('unknown_feature', json.dumps(args))
                self.assertEqual(self.profile.read_bytes(), before)

    def test_missing_backend_does_not_invent_or_disable_sandbox(self):
        self.write_profile()
        args = self.run_adapter(CodexAdapter('codex', isolate_config=True))
        self.assertFalse(any('windows.sandbox' in arg for arg in args))
        self.assertEqual(args[args.index('--sandbox')+1], 'read-only')

    def test_nonisolated_mode_does_not_override_backend(self):
        self.write_profile('[windows]\nsandbox="unelevated"\n')
        args = self.run_adapter(CodexAdapter('codex', isolate_config=False))
        self.assertNotIn('--ignore-user-config', args)
        self.assertFalse(any('windows.sandbox' in arg for arg in args))

    @unittest.skipUnless(os.name == 'nt', 'Windows 沙箱配置仅适用于 Windows')
    def test_backend_change_invalidates_profile_fingerprint(self):
        adapter = CodexAdapter('codex', isolate_config=True)
        self.write_profile('[windows]\nsandbox="unelevated"\n')
        old = adapter.input_fingerprint()
        self.write_profile('[windows]\nsandbox="elevated"\n')
        self.assertNotEqual(adapter.input_fingerprint(), old)

    @unittest.skipUnless(os.name == 'nt', 'Windows 沙箱配置仅适用于 Windows')
    def test_invalid_backend_and_shape_are_rejected_before_execution(self):
        for value in ('"disabled"', '"danger-full-access"', 'true', '[]', '{}'):
            with self.subTest(value=value):
                self.write_profile('[windows]\nsandbox=' + value + '\n')
                with self.assertRaisesRegex(ValueError, 'windows.sandbox'):
                    CodexAdapter('codex', isolate_config=True).input_fingerprint()
        self.profile.write_text('windows="bad"\n', encoding='utf8')
        with self.assertRaisesRegex(ValueError, 'windows'):
            CodexAdapter('codex', isolate_config=True).input_fingerprint()


class RealCodexTools(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ctx = RunContext('fixture', 'real-tools', 'design', self.root, self.root,
                              attempt_dir=self.root/'attempt', max_repeated_reads=2)
        self.raw = (FIXTURES/'codex_0.169_tools_real.jsonl').read_text(encoding='utf8')

    def test_real_fixture_provenance_and_adapter_completion(self):
        path = FIXTURES/'codex_0.169_tools_real.jsonl'
        source = json.loads((FIXTURES/'cli-capture-provenance.json').read_text(encoding='utf8'))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), source[path.name]['fixture_sha256'])
        adapter = CodexAdapter('codex')
        adapter._version = 'codex-cli 0.169.0'
        result = adapter._parse(subprocess.CompletedProcess([], 0, self.raw, ''), self.root, self.ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.usage['tokens'], 48719)
        self.assertEqual(result.usage['cached_input_tokens'], 40931)
        self.assertIn('CODEX_NORMAL_TOOL_20261116', result.text)

    def test_real_tools_expose_summary_and_deduplicate_replays(self):
        trace = CLITrace(self.ctx, channel='cli_codex')
        try:
            trace.feed('stdout', self.raw)
            trace.feed('stdout', self.raw)
            self.assertEqual(trace.tick(), '')
            self.assertEqual(trace.snapshot()['tool'], 'command_execution')
            self.assertEqual(trace.snapshot()['consecutive_tool_errors'], 0)
            self.assertNotIn('CODEX_NORMAL_TOOL_20261116', json.dumps(trace.snapshot()))
            activities = [codex_activity(json.loads(line)) for line in self.raw.splitlines()]
            activities = [a for a in activities if a is not None]
            self.assertEqual([a.state for a in activities], ['ACTIVE', 'DONE', 'ACTIVE', 'DONE'])
            self.assertTrue(all(a.filename == 'C:/fixture/workspace/probe.txt' for a in activities))
            self.assertEqual(len(trace.reads), 1)
            self.assertEqual(list(trace.reads.values()), [2])
        finally:
            trace.finish('exited')

    def test_real_second_read_triggers_preconfigured_limit(self):
        self.ctx.max_repeated_reads = 1
        trace = CLITrace(self.ctx, channel='cli_codex')
        try:
            trace.feed('stdout', self.raw)
            self.assertEqual(trace.tick(), 'repeated_read_limit')
        finally:
            trace.finish('stopped')


if __name__ == '__main__':
    unittest.main()
