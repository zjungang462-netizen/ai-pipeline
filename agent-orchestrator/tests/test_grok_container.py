import base64
import csv
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest import mock

from orchestrator.adapters.cli_grok import GrokAdapter, _readonly_mount
from orchestrator.adapters.grok_container import grok_args
from orchestrator.cli_telemetry import CLITrace, UsageLedger
from orchestrator.contracts import RunContext
from orchestrator.safety import WriteRootViolation


class GrokContainer(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.source = self.root / 'pricing.py'
        self.source.write_bytes(b'before')
        self.ctx = RunContext('test', 'run', 'code', self.root, self.root,
                              kind='code', editable_files=('pricing.py',))
        self.adapter = GrokAdapter('grok', binary=str(self.root / 'grok'))

    def test_only_declared_files_can_be_written_back(self):
        original = {'pricing.py': b'before'}
        value = base64.b64encode(b'after').decode('ascii')
        with self.assertRaises(ValueError):
            self.adapter._apply_updates(self.root, self.ctx, original, {'outside.py': value})
        self.assertEqual(self.source.read_bytes(), b'before')
        with self.assertRaises((ValueError, WriteRootViolation)):
            self.adapter._source_files(self.root, ('.grok/config.toml',))
        self.assertEqual(self.adapter._apply_updates(self.root, self.ctx, original, {'pricing.py': value}),
                         ['pricing.py'])
        self.assertEqual(self.source.read_bytes(), b'after')
        with self.assertRaises(ValueError):
            self.adapter._apply_updates(self.root, self.ctx, original, {'pricing.py': value})

    def test_failed_replace_restores_files_already_written(self):
        invoice = self.root / 'invoice.py'
        invoice.write_bytes(b'invoice')
        ctx = RunContext('test', 'run', 'code', self.root, self.root, kind='code',
                         editable_files=('pricing.py', 'invoice.py'))
        original = {'pricing.py': b'before', 'invoice.py': b'invoice'}
        updates = {name: base64.b64encode(original[name] + b'-new').decode('ascii') for name in original}
        real = os.replace
        calls = {'n': 0}

        def flaky(source, target):
            calls['n'] += 1
            if calls['n'] == 2:
                raise OSError('disk')
            return real(source, target)

        with mock.patch('orchestrator.adapters.cli_grok.os.replace', flaky):
            with self.assertRaises(OSError):
                self.adapter._apply_updates(self.root, ctx, original, updates)
        self.assertEqual(self.source.read_bytes(), b'before')
        self.assertEqual(invoice.read_bytes(), b'invoice')

    def test_returned_file_containing_session_secret_is_not_written(self):
        secret = 'session-token-0123456789'
        value = base64.b64encode(b'after ' + secret.encode('ascii')).decode('ascii')
        with self.assertRaises(ValueError):
            self.adapter._apply_updates(self.root, self.ctx, {'pricing.py': b'before'},
                                        {'pricing.py': value}, secrets=(secret,))
        self.assertEqual(self.source.read_bytes(), b'before')

    def test_prompt_stays_in_a_private_file(self):
        prompt = 'P' * 200000
        args = grok_args(('pricing.py',), Path('/tmp/pipeline-grok/prompt.txt'))
        self.assertNotIn('-p', args)
        self.assertNotIn(prompt, args)
        self.assertEqual(args[args.index('--prompt-file') + 1], '/tmp/pipeline-grok/prompt.txt')

    def _feed(self, trace, events):
        trace.feed('stdout', ''.join(json.dumps(event) + '\n' for event in events))

    def _tool(self, name, path, status, call):
        field = 'target_file' if name == 'read_file' else 'file_path'
        return ({'type': 'tool_call', 'toolCallId': call, 'toolName': name, 'rawInput': {field: path}},
                {'type': 'tool_call_update', 'toolCallId': call, 'status': status})

    def test_undeclared_paths_do_not_consume_tool_error_budget(self):
        ctx = RunContext('test', 'run', 'code', self.root, self.root, kind='code',
                         context_files=('pricing.py', 'invoice.py'), editable_files=('pricing.py',),
                         max_tool_errors=3)
        trace = CLITrace(ctx, channel='cli_grok')
        events = []
        for name, path, status, call in (
                ('read_file', '/workspace', 'failed', 'a'),
                ('search_replace', 'invoice.py', 'failed', 'b'),
                ('read_file', 'design_report.md', 'failed', 'c'),
                ('read_file', '../outside.py', 'failed', 'd')):
            events.extend(self._tool(name, path, status, call))
        self._feed(trace, events)
        self.assertEqual(trace.reason, '')
        self.assertEqual(trace.tool_errors, 0)
        self._feed(trace, self._tool('read_file', 'pricing.py', 'completed', 'e'))
        self.assertEqual(trace.reason, '')
        denied = CLITrace(ctx, channel='cli_grok')
        self._feed(denied, self._tool('read_file', 'design_report.md', 'completed', 'outside'))
        self.assertEqual(denied.reason, 'tool_error_limit')

    def test_normalized_declared_paths_match_container_policy(self):
        ctx = RunContext('test', 'run', 'code', self.root, self.root, kind='code',
                         context_files=('pricing.py',), editable_files=('pricing.py',),
                         max_tool_errors=2)
        trace = CLITrace(ctx, channel='cli_grok')
        self._feed(trace, self._tool('read_file', './pricing.py', 'completed', 'rel'))
        self._feed(trace, self._tool('search_replace', '/workspace/pricing.py', 'completed', 'abs'))
        self.assertEqual(trace.reason, '')
        self.assertEqual(trace.tool_errors, 0)
        self._feed(trace, self._tool('search_replace', './pricing.py', 'failed', 'f1'))
        self._feed(trace, self._tool('search_replace', '/workspace/pricing.py', 'failed', 'f2'))
        self.assertEqual(trace.tool_errors, 2)
        self.assertEqual(trace.reason, 'tool_error_limit')

    def test_stage_timeout_and_writeback_error_detail_are_preserved(self):
        from orchestrator.adapters.cli_grok import BINARY_SHA256, VERSION
        from orchestrator.contracts import ErrorKind, Result

        adapter = GrokAdapter('grok', binary=str(self.root / 'grok'), timeout_s=60)
        adapter._version = VERSION
        adapter._resolved = 'docker'
        ctx = RunContext('test', 'run', 'code', self.root, self.root,
                         kind='code', editable_files=('pricing.py',), timeout_s=180)
        bad_payload = json.dumps({'type': 'pipeline_files', 'files': {'outside.py': ''}})
        fake_result = Result(True, 0, stdout=bad_payload, stderr='prior stderr', channel='cli_grok')
        with mock.patch('orchestrator.adapters.cli_grok.sha256_file', return_value=BINARY_SHA256), \
                mock.patch.object(adapter, '_read_auth', return_value=('{}', ('secret-12345678',))), \
                mock.patch.object(adapter, '_exec', return_value=fake_result) as execute, \
                mock.patch('orchestrator.adapters.cli_grok.run_cli_process'):
            result = adapter.run('fix', self.root, ctx)
        sent = json.loads(execute.call_args.kwargs['stdin_data'])
        self.assertEqual(sent['timeout'], 180)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, ErrorKind.REJECTED)
        self.assertIn('prior stderr', result.stderr)
        self.assertIn('拒绝未声明文件', result.stderr)

    def test_declared_tool_failures_still_stop_the_run(self):
        ctx = RunContext('test', 'run', 'code', self.root, self.root, kind='code',
                         editable_files=('pricing.py',), max_tool_errors=3)
        trace = CLITrace(ctx, channel='cli_grok')
        events = []
        for number in range(3):
            events.extend(self._tool('search_replace', 'pricing.py', 'failed', f'edit-{number}'))
        self._feed(trace, events)
        self.assertEqual(trace.tool_errors, 3)
        self.assertEqual(trace.reason, 'tool_error_limit')

    def test_docker_mount_preserves_commas_in_binary_path(self):
        source = self.root / 'vendor, verified' / 'grok'
        mount = _readonly_mount(source, '/opt/grok')
        self.assertEqual(next(csv.reader([mount])),
                         ['type=bind', f'source={source.as_posix()}', 'target=/opt/grok', 'readonly'])

    def test_zero_exit_with_duplicate_or_cancelled_terminal_is_rejected(self):
        usage = dict(input_tokens=10, output_tokens=2, cache_read_input_tokens=5,
                     cache_creation_input_tokens=0, reasoning_tokens=1, total_tokens=17)
        text = {'type': 'text', 'data': 'done'}
        end = {'type': 'end', 'stopReason': 'end_turn', 'usage': usage}
        files = {'type': 'pipeline_files', 'files': {}}
        for rows, accepted in (([text, end, files], True), ([text, end, end, files], False),
                               ([text, end, files, text], False),
                               ([text, {**end, 'stopReason': 'cancelled'}, files], False)):
            with self.subTest(rows=rows):
                proc = SimpleNamespace(returncode=0, stderr='', stdout='\n'.join(map(json.dumps, rows)))
                self.assertEqual(self.adapter._parse(proc, self.root, self.ctx).ok, accepted)

    def test_usage_deduplicates_progress_and_counts_cache_once(self):
        usage = dict(input_tokens=10, output_tokens=2, cache_read_input_tokens=5,
                     cache_creation_input_tokens=0, reasoning_tokens=1)
        ledger = UsageLedger()
        progress = {'type': 'usage', 'signature': 'response', 'usage': usage}
        ledger.observe_grok(progress)
        ledger.observe_grok(progress)
        self.assertEqual(ledger.snapshot()['tokens'], 17)
        ledger.observe_grok({'type': 'end', 'stopReason': 'end_turn', 'usage': {**usage, 'total_tokens': 17}})
        self.assertTrue(ledger.complete)
        self.assertEqual(ledger.snapshot()['tokens'], 17)
