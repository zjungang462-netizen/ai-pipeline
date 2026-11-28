import json
import os
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
    def test_staging_chown_failure_stops_instead_of_adding_capabilities(self):
        from types import SimpleNamespace
        from orchestrator.adapters.workspace_sync import materialize_staging
        fake_os = SimpleNamespace(
            name='posix', geteuid=lambda: 0,
            chown=mock.Mock(side_effect=PermissionError('not owner')),
            walk=lambda target: [(str(target), [], ['editable.py'])],
        )
        with TemporaryDirectory() as directory, \
                mock.patch('orchestrator.adapters.workspace_sync.os', fake_os):
            with self.assertRaisesRegex(RuntimeError, '已停止执行'):
                materialize_staging(Path(directory), {'editable.py': b'x\n'}, ('editable.py',))

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

    def test_staging_requests_host_uid_override_only_when_chown_is_unavailable(self):
        from types import SimpleNamespace
        from orchestrator.adapters.workspace_sync import materialize_staging

        for chown_fails in (False, True):
            with self.subTest(chown_fails=chown_fails), TemporaryDirectory() as tmp:
                root = Path(tmp)
                chown = mock.Mock(side_effect=PermissionError('not owner') if chown_fails else None)
                fake_os = SimpleNamespace(
                    name='posix', geteuid=lambda: 1000, chown=chown,
                    walk=lambda target: [(str(target), [], ['app.py'])],
                )
                with mock.patch('orchestrator.adapters.workspace_sync.os', fake_os):
                    staging_uid = materialize_staging(root, {'app.py': b'x = 1\n'}, ('app.py',))
                self.assertEqual(staging_uid, 1000 if chown_fails else None)
                self.assertTrue(chown.called)

    def test_private_staging_fallback_uses_host_uid_and_dac_override(self):
        from types import SimpleNamespace

        adapter = DoubaoAdapter('doubao')
        with mock.patch('orchestrator.adapters.cli_doubao.os', SimpleNamespace(getegid=lambda: 4444)):
            args = adapter._common_run(staging_uid=1234)
        self.assertEqual(args[args.index('--user') + 1], '1234:4444')
        self.assertIn('DAC_OVERRIDE', args)
        with mock.patch('orchestrator.adapters.cli_doubao.os', SimpleNamespace()):
            win_args = adapter._common_run(staging_uid=1234)
        self.assertEqual(win_args[win_args.index('--user') + 1], '1234:65534')

    def test_code_stage_without_editable_files_is_rejected_without_mounting_workspace_rw(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        (root / 'doubao').write_bytes(b'bin')
        adapter = DoubaoAdapter(
            'doubao', binary=str(root / 'doubao'), image='pipeline-doubao-probe:2.37.2',
            machine_id=str(root / 'machine-id'), profile='pipeline-doubao-profile')
        adapter._version = VERSION
        adapter._resolved = 'docker'
        ctx = RunContext('t', 'r', 'code', root, root, kind='code', editable_files=())
        with mock.patch('orchestrator.adapters.cli_doubao.sha256_file', return_value=BINARY_SHA256), \
                mock.patch.object(adapter, '_exec') as execute:
            res = adapter.run('fix', root, ctx)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_kind, ErrorKind.REJECTED)
        execute.assert_not_called()

    def test_code_stage_with_editable_files_uses_isolated_staging_and_verifies_writeback(self):
        import csv
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        workspace = root / 'ws'
        workspace.mkdir()
        (workspace / 'pricing.py').write_bytes(b'old-price')
        (workspace / 'readonly.py').write_bytes(b'keep-readonly')
        (workspace / 'secret_test.py').write_bytes(b'protected-test')
        attempt_dir = root / 'attempt'
        adapter = DoubaoAdapter(
            'doubao', binary=str(root / 'doubao'), image='pipeline-doubao-probe:2.37.2',
            machine_id=str(root / 'machine-id'), profile='pipeline-doubao-profile')
        adapter._version = VERSION
        adapter._resolved = 'docker'
        ctx = RunContext('t', 'r', 'code', workspace, root, kind='code', attempt_dir=attempt_dir,
                         context_files=('pricing.py', 'readonly.py'), editable_files=('pricing.py',))

        def fake_exec(args, workdir, context, stdin_data=None):
            mounts = []
            for item in args:
                if not isinstance(item, str) or not item.startswith('type=bind'):
                    continue
                fields = {}
                for part in next(csv.reader([item])):
                    key, separator, value = part.partition('=')
                    fields[key] = value if separator else True
                mounts.append(fields)
            staging = Path(next(row['source'] for row in mounts if row['target'] == '/workspace'))
            readonly_mount = next(row for row in mounts if row['target'] == '/workspace/readonly.py')
            self.assertIn('readonly', readonly_mount)
            self.assertFalse(any(row['target'] == '/workspace/pricing.py' for row in mounts))
            # 容器只挂载隔离 staging 目录，未声明的保护测试文件不可见。
            self.assertNotEqual(staging.resolve(), workspace.resolve())
            self.assertTrue((staging / 'pricing.py').is_file())
            self.assertTrue((staging / 'readonly.py').is_file())
            self.assertFalse((staging / 'secret_test.py').exists())
            if os.name != 'nt':
                self.assertEqual(staging.stat().st_mode & 0o777, 0o700)
                self.assertEqual((staging / 'pricing.py').stat().st_mode & 0o777, 0o600)
                self.assertEqual((staging / 'readonly.py').stat().st_mode & 0o777, 0o400)
                if staging.stat().st_uid == 65534:
                    self.assertNotIn('DAC_OVERRIDE', args)
                    self.assertEqual(args[args.index('--user') + 1], '65534:65534')
                else:
                    self.assertIn('DAC_OVERRIDE', args)
                    self.assertEqual(args[args.index('--user') + 1], f'{os.geteuid()}:{os.getegid()}')
            # 模拟容器内原子替换可编辑文件、绕过只读挂载改写上下文并创建垃圾文件。
            replacement = staging / 'pricing.py.tmp'
            replacement.write_bytes(b'new-price')
            replacement.chmod(0o600)
            os.replace(replacement, staging / 'pricing.py')
            (staging / 'readonly.py').chmod(0o600)
            (staging / 'readonly.py').write_bytes(b'tampered-readonly')
            (staging / 'junk.tmp').write_bytes(b'junk')
            return Result(True, 0, text='done', channel='cli_doubao')

        with mock.patch('orchestrator.adapters.cli_doubao.sha256_file', return_value=BINARY_SHA256), \
                mock.patch.object(adapter, '_exec', side_effect=fake_exec), \
                mock.patch('orchestrator.adapters.cli_doubao.run_cli_process'):
            result = adapter.run('fix pricing', workspace, ctx)

        self.assertTrue(result.ok)
        self.assertEqual(result.changed_files, ['pricing.py'])
        self.assertEqual((workspace / 'pricing.py').read_bytes(), b'new-price')
        self.assertEqual((workspace / 'readonly.py').read_bytes(), b'keep-readonly')
        self.assertEqual((workspace / 'secret_test.py').read_bytes(), b'protected-test')
        self.assertFalse((workspace / 'junk.tmp').exists())

    def test_code_stage_rejects_writeback_when_host_baseline_changed(self):
        import csv
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        workspace = root / 'ws'
        workspace.mkdir()
        (workspace / 'pricing.py').write_bytes(b'v1')
        adapter = DoubaoAdapter(
            'doubao', binary=str(root / 'doubao'), image='pipeline-doubao-probe:2.37.2',
            machine_id=str(root / 'machine-id'), profile='pipeline-doubao-profile')
        adapter._version = VERSION
        adapter._resolved = 'docker'
        ctx = RunContext('t', 'r', 'code', workspace, root, kind='code', editable_files=('pricing.py',))

        def fake_exec(args, workdir, context, stdin_data=None):
            mount_arg = next(item for item in args if isinstance(item, str) and 'target=/workspace' in item)
            fields = dict(part.split('=', 1) for part in next(csv.reader([mount_arg])) if '=' in part)
            (Path(fields['source']) / 'pricing.py').write_bytes(b'from-container')
            # 模拟容器执行期间宿主机工作区被外部并发修改。
            (workspace / 'pricing.py').write_bytes(b'concurrent-host-edit')
            return Result(True, 0, text='done', channel='cli_doubao')

        with mock.patch('orchestrator.adapters.cli_doubao.sha256_file', return_value=BINARY_SHA256), \
                mock.patch.object(adapter, '_exec', side_effect=fake_exec), \
                mock.patch('orchestrator.adapters.cli_doubao.run_cli_process'):
            result = adapter.run('fix pricing', workspace, ctx)

        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, ErrorKind.REJECTED)
        self.assertIn('工作树已变化', result.stderr)
        self.assertEqual((workspace / 'pricing.py').read_bytes(), b'concurrent-host-edit')

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
