import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from orchestrator.adapters.cli_doubao import BINARY_SHA256, IMAGE_ID, VERSION, DoubaoAdapter
from orchestrator.cli_telemetry import CLITrace
from orchestrator.contracts import ErrorKind, Result, RunContext


def _completed(stdout='', stderr='', code=0):
    from subprocess import CompletedProcess
    return CompletedProcess([], code, stdout, stderr)


def _result(text='done', denials=None):
    return {'type': 'result', 'subtype': 'success', 'is_error': False, 'tool_calls_truncated': False,
            'permission_denials': [] if denials is None else denials,
            'result': {'type': 'text', 'content': text}}


class DoubaoContainer(unittest.TestCase):
    def test_success_terminal_has_no_token_usage(self):
        adapter = DoubaoAdapter('doubao')
        adapter._version = VERSION
        clean = _result('TEMP_NEW')
        del clean['permission_denials']
        stdout = '\n'.join(json.dumps(row) for row in (
            {'type': 'system', 'subtype': 'init'}, clean))
        result = adapter._parse(_completed(stdout), Path('.'), RunContext('t', 'r', 's', Path('.'), Path('.')))
        self.assertTrue(result.ok)
        self.assertEqual(result.text, 'TEMP_NEW')
        self.assertTrue(result.usage['usage_unknown'])
        self.assertNotIn('tokens', result.usage)

    def test_verified_permission_denial_stays_successful_and_visible(self):
        adapter = DoubaoAdapter('doubao')
        adapter._version = VERSION
        denial = {'prompt': '是否授权 /protected/outside.txt'}
        stdout = json.dumps(_result('外部写入被拒绝', [denial]))
        result = adapter._parse(_completed(stdout), Path('.'), RunContext('t', 'r', 's', Path('.'), Path('.')))
        self.assertTrue(result.ok)
        self.assertIn('权限拒绝 1', result.stderr)

    def test_duplicate_or_failed_terminal_is_rejected(self):
        adapter = DoubaoAdapter('doubao')
        adapter._version = VERSION
        end = _result('done')
        for rows in ([end, end], [{'type': 'result', 'subtype': 'error_during_execution', 'is_error': True}]):
            with self.subTest(rows=rows):
                stdout = '\n'.join(map(json.dumps, rows))
                result = adapter._parse(_completed(stdout), Path('.'), RunContext('t', 'r', 's', Path('.'), Path('.')))
                self.assertFalse(result.ok)
                self.assertTrue(result.parse_unknown)

    def test_login_required_is_auth_and_unknown_version_does_not_succeed(self):
        adapter = DoubaoAdapter('doubao')
        adapter._version = 'doubao 1.0'
        auth = adapter._parse(_completed('login_required: 当前未登录', code=4), Path('.'),
                              RunContext('t', 'r', 's', Path('.'), Path('.')))
        self.assertEqual(auth.error_kind, ErrorKind.AUTH)
        unknown = adapter._parse(_completed(json.dumps(_result('done'))), Path('.'),
                                 RunContext('t', 'r', 's', Path('.'), Path('.')))
        self.assertTrue(unknown.parse_unknown)

    def test_prompt_uses_stdin_and_non_code_workspace_is_readonly(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        adapter = DoubaoAdapter(
            'doubao', binary=str(root / 'doubao'), image='pipeline-doubao-probe:2.37.2',
            machine_id=str(root / 'machine-id'), profile='pipeline-doubao-profile')
        adapter._version = VERSION
        adapter._resolved = 'docker'
        ctx = RunContext('t', 'r', 'review', root, root, kind='review')
        with mock.patch('orchestrator.adapters.cli_doubao.sha256_file', return_value=BINARY_SHA256), \
                mock.patch.object(adapter, '_exec', return_value=Result(True, 0)) as execute, \
                mock.patch('orchestrator.adapters.cli_doubao.run_cli_process'):
            adapter.run('完整提示词', root, ctx)
        args = execute.call_args.args[0]
        self.assertEqual(args[-2:], ['-p', '-'])
        self.assertIn('PYTHONDONTWRITEBYTECODE=1', args)
        self.assertIn('--platform', args)
        self.assertIn(IMAGE_ID, args)
        self.assertNotIn('pipeline-doubao-probe:2.37.2', args)
        self.assertEqual(execute.call_args.kwargs['stdin_data'], '完整提示词')
        self.assertNotIn('完整提示词', args)
        workspace = next(item for item in args if isinstance(item, str) and item.startswith('type=bind')
                         and 'target=/workspace' in item)
        self.assertIn('readonly', workspace)

    def test_crash_with_auth_words_in_output_is_not_misclassified_and_missing_binary_is_rejected(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        with self.assertRaises(ValueError):
            DoubaoAdapter('doubao', binary=str(root / 'doubao'), image='--privileged',
                          machine_id=str(root / 'machine-id'), profile='pipeline-doubao-profile')
        adapter = DoubaoAdapter(
            'doubao', binary=str(root / 'doubao-custom'), image='pipeline-doubao-probe:2.37.2',
            machine_id=str(root / 'machine-id'), profile='pipeline-doubao-profile')
        adapter._version = VERSION
        adapter._resolved = 'docker'
        crash = adapter._parse(_completed('author 登录模块异常', code=1), root,
                               RunContext('t', 'r', 's', root, root))
        self.assertEqual(crash.error_kind, ErrorKind.CRASH)
        missing = adapter.run('prompt', root, RunContext('t', 'r', 'code', root, root, kind='code'))
        self.assertFalse(missing.ok)
        self.assertEqual(missing.error_kind, ErrorKind.REJECTED)

    def test_read_and_edit_events_are_tracked_without_usage(self):
        root = Path('.')
        trace = CLITrace(RunContext('t', 'r', 'code', root, root, kind='code'), channel='cli_doubao')
        call = 'tool-1'
        trace.feed('stdout', json.dumps({
            'type': 'assistant', 'message': {'content': [{
                'type': 'tool_use', 'id': call, 'name': 'Edit', 'input': {'file_path': 'pricing.py'}}]}}) + '\n')
        trace.feed('stdout', json.dumps({
            'type': 'user', 'message': {'content': [{
                'type': 'tool_result', 'tool_use_id': call, 'is_error': False}]}}) + '\n')
        self.assertIn(call, trace.active_tools)
        self.assertFalse(trace.ledger.complete)
        self.assertEqual(trace.ledger.snapshot()['tokens'], 0)
