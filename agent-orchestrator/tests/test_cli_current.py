"""当前 CLI 的真实文件工具协议、预调用版本门禁与流式停止护栏。"""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from orchestrator.adapters.cli_agy import AgyAdapter
from orchestrator.adapters.cli_codex import CodexAdapter
from orchestrator.adapters.cli_qwen import QwenAdapter
from orchestrator.cli_telemetry import CLITrace, UsageLedger
from orchestrator.contracts import RunContext

FIXTURES = Path(__file__).parent / 'fixtures'


class CurrentCLI(unittest.TestCase):
    def test_qwen_explicit_connection_changes_fingerprint_without_credentials(self):
        adapter = QwenAdapter('q', extra_env=['OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_MODEL'])
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'private-test-material',
                'OPENAI_BASE_URL': 'https://one.invalid/v1', 'OPENAI_MODEL': 'one'}):
            original = adapter.input_fingerprint()
            self.assertNotIn('private-test-material', json.dumps(original))
            os.environ['OPENAI_BASE_URL'] = 'https://two.invalid/v1'
            self.assertNotEqual(adapter.input_fingerprint(), original)
            os.environ['OPENAI_BASE_URL'] = 'https://one.invalid/v1'
            os.environ['OPENAI_MODEL'] = 'two'
            self.assertNotEqual(adapter.input_fingerprint(), original)
            isolated = QwenAdapter('q').input_fingerprint()
            self.assertIsNone(isolated['backend_endpoint'])
            self.assertIsNone(isolated['model'])

    def test_qwen_credential_bearing_endpoint_is_rejected_before_fingerprinting(self):
        for endpoint in ('https://' + 'user:password@host', 'https://host/?token=private', 'file:///private'):
            with self.subTest(endpoint=endpoint), mock.patch.dict(os.environ, {'OPENAI_BASE_URL': endpoint}):
                with self.assertRaises(ValueError):
                    QwenAdapter('q', extra_env=['OPENAI_BASE_URL']).input_fingerprint()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ctx = RunContext('fixture', 'current-cli', 'coding', self.root, self.root,
                              kind='code', attempt_dir=self.root / 'attempt', timeout_s=30)

    def fixture(self, name):
        return (FIXTURES / name).read_text(encoding='utf8')

    def parse(self, adapter, raw, version, code=0):
        adapter._version = version
        return adapter._parse(subprocess.CompletedProcess([], code, raw, ''), self.root, self.ctx)

    def test_fixtures_match_real_capture_provenance(self):
        provenance = json.loads((FIXTURES / 'cli-capture-provenance.json').read_text(encoding='utf8'))
        for name in ('codex_0.170.0_tools_real.jsonl', 'agy_1.5.12_tools_real.jsonl', 'qwen_0.26.0_tools_real.jsonl'):
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest(), provenance[name]['fixture_sha256'])
                self.assertTrue(provenance[name]['user_configs_unchanged'])
                self.assertEqual(provenance[name]['external_file_check_exit'], 0)

    def test_codex_0170_real_file_changes_and_usage(self):
        raw = self.fixture('codex_0.170.0_tools_real.jsonl')
        result = self.parse(CodexAdapter('c'), raw, 'codex-cli 0.170.0')
        self.assertTrue(result.ok)
        self.assertEqual(result.text, 'PROBE_EDIT_OK')
        self.assertEqual(result.usage['tokens'], 71579)
        events = [json.loads(line) for line in raw.splitlines()]
        changes = [e for e in events if e['type'] == 'item.completed' and e.get('item', {}).get('type') == 'file_change']
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['item']['status'], 'completed')

    def test_codex_0170_missing_duplicate_nonterminal_or_failed_completion_is_rejected(self):
        raw = self.fixture('codex_0.170.0_tools_real.jsonl')
        lines = raw.splitlines()
        for value in ('\n'.join(lines[:-1]), raw + lines[-1], raw + '{"type":"turn.started"}',
                      raw + '{"type":"turn.failed"}'):
            with self.subTest(value=value[-80:]):
                self.assertFalse(self.parse(CodexAdapter('c'), value, 'codex-cli 0.170.0').ok)
        self.assertFalse(self.parse(CodexAdapter('c'), raw, 'codex-cli 0.170.0', 7).ok)
        self.assertFalse(self.parse(CodexAdapter('c'), raw, 'codex-cli 0.170.1').ok)

    def test_agy_1512_real_tools_and_conservative_cache_accounting(self):
        raw = self.fixture('agy_1.5.12_tools_real.jsonl')
        result = self.parse(AgyAdapter('a'), raw, '1.5.12')
        self.assertTrue(result.ok)
        self.assertEqual(result.usage['tokens'], 57313)
        self.assertEqual(result.usage['cli_reported_tokens'], 22060)
        self.assertEqual(result.text.strip(), 'PROBE_EDIT_OK')
        trace = CLITrace(self.ctx, channel='cli_agy')
        try:
            trace.feed('stdout', raw)
            self.assertTrue(any(key for key in trace.finished_tools))
            self.assertEqual(trace.ledger.snapshot()['tokens'], 57313)
        finally:
            trace.finish('exited')

    def test_agy_1512_ambiguous_or_trailing_events_are_rejected(self):
        raw = self.fixture('agy_1.5.12_tools_real.jsonl')
        last = raw.splitlines()[-1]
        for value in (raw + last, raw + '{"event":"step_update"}', '\n'.join(raw.splitlines()[:-1])):
            self.assertFalse(self.parse(AgyAdapter('a'), value, '1.5.12').ok)
        self.assertFalse(self.parse(AgyAdapter('a'), raw, '1.5.13').ok)
        self.assertFalse(self.parse(AgyAdapter('a'), raw, 'agy 9.999.0').ok)

    def test_qwen_real_stream_and_legacy_array_share_strict_terminal_parser(self):
        raw = self.fixture('qwen_0.26.0_tools_real.jsonl')
        for value in (raw, json.dumps([json.loads(line) for line in raw.splitlines()])):
            result = self.parse(QwenAdapter('q'), value, '0.26.0')
            self.assertTrue(result.ok)
            self.assertEqual(result.usage['tokens'], 13708)
            self.assertEqual(result.usage['cache_read_tokens'], 8832)
            self.assertEqual(result.text, 'PROBE_EDIT_OK')
        events = [json.loads(line) for line in raw.splitlines()]
        events[-1]['permission_denials'] = [{'tool_name': 'edit'}]
        self.assertFalse(self.parse(QwenAdapter('q'), json.dumps(events), '0.26.0').ok)

    def test_qwen_stream_counts_partial_usage_and_deduplicates_cache(self):
        events = [json.loads(line) for line in self.fixture('qwen_0.26.0_tools_real.jsonl').splitlines()]
        ledger = UsageLedger()
        for event in events[:-1]:
            ledger.observe_qwen(event)
            ledger.observe_qwen(event)
        partial = ledger.snapshot()
        self.assertGreater(partial['tokens'], 0)
        self.assertFalse(partial['usage_complete'])
        ledger.observe_qwen(events[-1])
        ledger.observe_qwen(events[-1])
        self.assertEqual(ledger.snapshot()['tokens'], 13708)
        self.assertEqual(ledger.snapshot()['cache_read_tokens'], 8832)
        self.assertTrue(ledger.snapshot()['usage_complete'])

    def test_qwen_real_read_edit_events_drive_activity_and_tool_errors(self):
        raw = self.fixture('qwen_0.26.0_tools_real.jsonl')
        trace = CLITrace(self.ctx, channel='cli_qwen')
        try:
            trace.feed('stdout', raw)
            self.assertEqual(len(trace.finished_tools), 2)
            self.assertEqual(trace.tool_errors, 0)
            self.assertEqual(trace.ledger.snapshot()['tokens'], 13708)
        finally:
            trace.finish('exited')
        events = [json.loads(line) for line in raw.splitlines()]
        result = next(e for e in events if e.get('type') == 'user')
        result['message']['content'][0]['is_error'] = True
        self.ctx.attempt_dir = self.root / 'failed'
        self.ctx.max_tool_errors = 1
        trace = CLITrace(self.ctx, channel='cli_qwen')
        try:
            for event in events:
                trace.feed('stdout', json.dumps(event) + '\n')
            self.assertEqual(trace.reason, 'tool_error_limit')
        finally:
            trace.finish('exited')

    def test_qwen_repeated_reads_stop_during_stream_before_terminal(self):
        events = [json.loads(line) for line in self.fixture('qwen_0.26.0_tools_real.jsonl').splitlines()]
        read = next(e for e in events if e.get('type') == 'assistant'
                    and any(b.get('name') == 'read_file' for b in e.get('message', {}).get('content', [])))
        result = next(e for e in events if e.get('type') == 'user')
        repeated, completed = copy.deepcopy(read), copy.deepcopy(result)
        repeated['message']['content'][0]['id'] = 'second-read'
        completed['message']['content'][0]['tool_use_id'] = 'second-read'
        self.ctx.max_repeated_reads = 1
        trace = CLITrace(self.ctx, channel='cli_qwen')
        try:
            for event in (read, result, repeated, completed):
                trace.feed('stdout', json.dumps(event) + '\n')
            self.assertEqual(trace.reason, 'repeated_read_limit')
        finally:
            trace.finish('exited')

    def test_unknown_or_failed_version_prevents_model_execution(self):
        for cls, known, unknown in ((CodexAdapter, 'codex-cli 0.170.0', 'codex-cli 0.170.1'),
                                    (AgyAdapter, '1.5.12', '1.5.13'),
                                    (QwenAdapter, '0.26.0', 'qwen-code 9.0')):
            for code, stdout, stderr in ((0, unknown, ''), (1, known, ''), (0, '', known)):
                with self.subTest(cls=cls.__name__, code=code, stdout=stdout), \
                        mock.patch('orchestrator.adapters.cli_base.resolve_exe', return_value='fixture-cli'), \
                        mock.patch('orchestrator.adapters.cli_base.run_cli_process',
                                   return_value=subprocess.CompletedProcess([], code, stdout, stderr)), \
                        mock.patch.object(cls, '_exec') as execute:
                    self.assertFalse(cls('fixture').run('request', self.root, self.ctx).ok)
                    execute.assert_not_called()

    def test_qwen_command_uses_isolated_file_tools_and_no_permission_bypass(self):
        adapter = QwenAdapter('q', model='chosen')
        adapter._resolved, adapter._version = 'qwen', '0.26.0'
        raw = self.fixture('qwen_0.26.0_tools_real.jsonl')
        with mock.patch('orchestrator.adapters.cli_base.run_cli_process',
                        return_value=subprocess.CompletedProcess([], 0, raw, '')) as execute:
            self.assertTrue(adapter.run('private prompt', self.root, self.ctx).ok)
        command = execute.call_args.args[0]
        self.assertIn('--safe-mode', command)
        self.assertIn('--bare', command)
        self.assertIn('stream-json', command)
        self.assertIn('auto-edit', command)
        self.assertIn('run_shell_command', command)
        self.assertIn('task', command)
        self.assertNotIn('--yolo', command)
        self.assertNotIn('private prompt', command)
        self.assertEqual(execute.call_args.kwargs['input'], 'private prompt')


if __name__ == '__main__':
    unittest.main()
