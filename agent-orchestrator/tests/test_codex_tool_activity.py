"""Codex 0.169 协议构造事件、真实拒绝日志重放和本机子进程护栏。"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from orchestrator.adapters.cli_codex import CodexAdapter
from orchestrator.cli_telemetry import CLITrace
from orchestrator.cli_tools import simple_read
from orchestrator.contracts import ErrorKind, RunContext


FIXTURES = Path(__file__).parent / 'fixtures'


def command(identifier, phase='item.started', status='in_progress', code=None,
            body='Get-Content -LiteralPath src/check.py'):
    return {'type': phase, 'item': {'id': identifier, 'type': 'command_execution',
            'status': status, 'command': body, 'exit_code': code, 'aggregated_output': 'PRIVATE_OUTPUT'}}


class CodexToolActivity(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ctx = RunContext('fixture', 'codex-tools', 'coding', self.root, self.root,
                              attempt_dir=self.root/'attempt', timeout_s=12,
                              max_tool_errors=2, max_repeated_reads=2)
        self.trace = CLITrace(self.ctx, channel='cli_codex')
        self.addCleanup(self.trace.finish, 'exited')

    def emit(self, event):
        if (isinstance(event.get('item'), dict) and event['item'].get('type') == 'command_execution'
                and event['item'].get('command') == 'Get-Content -LiteralPath src/check.py'):
            event['item']['command'] = 'Get-Content -LiteralPath "' + str(self.root/'src/check.py') + '"'
        self.trace.feed('stdout', json.dumps(event) + '\n')

    def test_command_lifecycle_exposes_only_summary_and_deduplicates(self):
        event = command('cmd-1')
        self.emit(event)
        self.emit(event)
        event['type'] = 'item.updated'
        self.emit(event)
        self.assertEqual(self.trace.snapshot()['tool'], 'command_execution')
        self.assertEqual(self.trace.snapshot()['file'], 'src/check.py')
        self.emit(command('cmd-1', 'item.completed', 'completed', 0))
        self.emit(command('cmd-1', 'item.completed', 'completed', 0))
        self.emit(command('cmd-1'))  # 延迟到达的开始事件不重新计数。
        self.emit(command('cmd-2'))
        self.assertEqual(self.trace.tick(), '')
        self.emit(command('cmd-3'))
        self.assertEqual(self.trace.tick(), 'repeated_read_limit')
        snapshot = json.dumps(self.trace.snapshot())
        self.assertNotIn('PRIVATE_OUTPUT', snapshot)
        self.assertNotIn('Get-Content', snapshot)

    def test_failed_declined_and_nonzero_exit_are_counted_once(self):
        for status, code in (('failed', None), ('declined', None), ('completed', 7)):
            with self.subTest(status=status):
                trace = CLITrace(self.ctx, channel='cli_codex')
                try:
                    failure = command('bad-1', 'item.completed', status, code, 'exit 7')
                    for _ in range(3):
                        trace.feed('stdout', json.dumps(failure) + '\n')
                    self.assertEqual(trace.snapshot()['consecutive_tool_errors'], 1)
                    self.assertEqual(trace.tick(), '')
                    failure['item']['id'] = 'bad-2'
                    trace.feed('stdout', json.dumps(failure) + '\n')
                    self.assertEqual(trace.tick(), 'tool_error_limit')
                finally:
                    trace.finish('exited')

    def test_successful_command_resets_errors_but_not_repeated_reads(self):
        self.emit(command('bad', 'item.completed', 'failed', 1, 'exit 1'))
        self.emit(command('good', 'item.completed', 'completed', 0, 'echo ok'))
        self.assertEqual(self.trace.snapshot()['consecutive_tool_errors'], 0)
        for i in range(3):
            self.emit(command(f'read-{i}', 'item.completed', 'completed', 0))
        self.assertEqual(self.trace.tick(), 'repeated_read_limit')

    def test_terminal_failure_does_not_infer_an_unobserved_read(self):
        self.ctx.max_repeated_reads = 1
        self.emit(command('read', 'item.completed', 'completed', 0))
        self.emit(command('failure-1', 'item.completed', 'failed', 1))
        self.assertEqual(self.trace.tick(), '')
        self.emit(command('failure-2', 'item.completed', 'failed', 1))
        self.assertEqual(self.trace.tick(), 'tool_error_limit')

    def test_only_successful_file_change_resets_reads(self):
        self.emit(command('read-1'))
        self.emit(command('read-2'))
        change = {'type': 'item.completed', 'item': {'id': 'edit', 'type': 'file_change',
                  'status': 'completed', 'changes': [{'path': 'src/check.py', 'kind': 'update'}]}}
        self.emit(change)
        self.assertEqual(self.trace.snapshot()['tool'], 'file_change')
        self.assertEqual(self.trace.snapshot()['file'], 'src/check.py')
        self.emit(command('read-3'))
        self.assertEqual(self.trace.tick(), '')
        change['item'].update(id='failed-edit', status='failed')
        self.emit(change)
        self.emit(command('read-4'))
        self.assertEqual(self.trace.tick(), '')
        self.emit(command('read-5'))
        self.assertEqual(self.trace.tick(), 'repeated_read_limit')

    def test_mcp_lifecycle_uses_structured_arguments_and_failure(self):
        event = {'type': 'item.started', 'item': {'id': 'mcp-1', 'type': 'mcp_tool_call',
                 'server': 'files', 'tool': 'read_file', 'status': 'in_progress',
                 'arguments': {'path': 'src/check.py'}, 'result': None, 'error': None}}
        self.emit(event)
        self.assertEqual(self.trace.snapshot()['tool'], 'mcp:files/read_file')
        self.assertEqual(self.trace.snapshot()['file'], 'src/check.py')
        event['type'] = 'item.completed'
        event['item'].update(status='completed', error={'message': 'PRIVATE_MCP_ERROR'})
        self.emit(event)
        self.emit(event)
        self.assertEqual(self.trace.snapshot()['consecutive_tool_errors'], 1)
        self.assertNotIn('PRIVATE_MCP_ERROR', json.dumps(self.trace.snapshot()))
        event['item'].update(id='mcp-2', status='failed')
        self.emit(event)
        self.assertEqual(self.trace.tick(), 'tool_error_limit')

    def test_mcp_write_resets_reads_and_unrelated_tool_clears_stale_file(self):
        self.emit(command('read-1'))
        self.emit(command('read-2'))
        event = {'type': 'item.completed', 'item': {'id': 'mcp-write', 'type': 'mcp_tool_call',
                 'server': 'files', 'tool': 'write_file', 'status': 'completed',
                 'arguments': {'path': 'src/check.py'}, 'error': None}}
        self.emit(event)
        self.emit(command('read-3'))
        self.assertEqual(self.trace.tick(), '')
        self.emit(command('other', body='echo ok'))
        self.assertEqual(self.trace.snapshot()['file'], '')

    def test_distinct_threads_do_not_share_item_identity(self):
        for identifier in ('thread-1', 'thread-2'):
            self.emit({'type': 'thread.started', 'thread_id': identifier})
            self.emit(command('same-id', 'item.completed', 'failed', 7, 'exit 7'))
        self.assertEqual(self.trace.tick(), 'tool_error_limit')

    def test_reasoning_and_malformed_items_do_not_become_activity(self):
        self.emit({'type': 'item.completed', 'item': {'id': 'reason', 'type': 'reasoning',
                   'text': 'PRIVATE_REASONING', 'status': 'completed'}})
        events = [command('x'), command('x', 'item.completed', 'completed', None),
                  {'type': 'item.completed', 'item': []},
                  {'type': 'item.completed', 'item': {'id': 'edit', 'type': 'file_change',
                   'status': 'completed', 'changes': [{'path': [], 'kind': 'update'}]}}]
        events[0]['item']['id'] = []
        for event in events:
            self.emit(event)
        self.assertEqual(self.trace.snapshot()['tool'], '')
        self.assertEqual(self.trace.tick(), '')
        self.assertNotIn('PRIVATE_REASONING', json.dumps(self.trace.snapshot()))

    def test_path_boundaries_and_secrets_still_apply(self):
        self.emit(command('outside', body='cat "' + str(self.root.parent/'outside.txt') + '"'))
        self.assertEqual(self.trace.snapshot()['file'], '[工作区外路径]')
        secret = 'fixture-private-secret'
        trace = CLITrace(self.ctx, [secret], channel='cli_codex')
        try:
            body = 'cat "' + str(self.root/'src'/(secret + '.py')) + '"'
            trace.feed('stdout', json.dumps(command('secret', body=body)) + '\n')
            self.assertEqual(trace.snapshot()['file'], 'src/[REDACTED].py')
            self.assertNotIn(secret, json.dumps(trace.snapshot()))
        finally:
            trace.finish('exited')

    def test_relative_command_path_is_not_guessed_without_working_directory(self):
        for i in range(4):
            self.emit(command(f'relative-{i}', body='cat ./same.py'))
        self.assertEqual(self.trace.snapshot()['tool'], 'command_execution')
        self.assertEqual(self.trace.snapshot()['file'], '')
        self.assertEqual(self.trace.tick(), '')

    def test_real_policy_log_matches_its_provenance_and_stops(self):
        path = FIXTURES/'codex_0.169_policy_stderr.txt'
        provenance = json.loads((FIXTURES/'cli-capture-provenance.json').read_text(encoding='utf8'))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), provenance[path.name]['fixture_sha256'])
        raw = path.read_text(encoding='utf8')
        for offset in range(0, len(raw), 7):
            self.trace.feed('stderr', raw[offset:offset+7])
        self.assertEqual(self.trace.tick(), 'tool_error_limit')
        self.assertEqual(self.trace.snapshot()['consecutive_tool_errors'], 3)
        self.assertNotIn('Rejected(', json.dumps(self.trace.snapshot()))

    def test_policy_log_without_newline_is_counted_at_finish(self):
        self.ctx.max_tool_errors = 1
        line = (FIXTURES/'codex_0.169_policy_stderr.txt').read_text(encoding='utf8').splitlines()[0]
        self.trace.feed('stderr', line)
        self.trace.finish('exited')
        self.assertEqual(self.trace.snapshot()['reason'], 'tool_error_limit')
        self.assertEqual(self.trace.snapshot()['status'], 'stopped')
        self.trace.finish('exited')
        self.assertEqual(self.trace.snapshot()['consecutive_tool_errors'], 1)

    def test_policy_log_requires_exact_stderr_prefix_and_channel(self):
        line = (FIXTURES/'codex_0.169_policy_stderr.txt').read_text(encoding='utf8').splitlines()[0]
        for channel, stream, body in (('cli_codex', 'stdout', line),
                                      ('cli_agy', 'stderr', line),
                                      ('cli_codex', 'stderr', 'Example: ' + line),
                                      ('cli_codex', 'stderr', 'error=exec_command failed: ordinary error')):
            with self.subTest(channel=channel, stream=stream):
                trace = CLITrace(self.ctx, channel=channel)
                try:
                    trace.feed(stream, body + '\n')
                    self.assertEqual(trace.snapshot()['consecutive_tool_errors'], 0)
                finally:
                    trace.finish('exited')


class ReadCommandRecognition(unittest.TestCase):
    def test_simple_reads_and_shell_wrappers_keep_paths(self):
        for body, path in (
            ('Get-Content -LiteralPath ./probe.txt', './probe.txt'),
            (r'Get-Content -LiteralPath C:\workspace\file.py', r'C:\workspace\file.py'),
            ('Get-Content "src/中文 文件.py"', 'src/中文 文件.py'),
            ('cat -- src/check.py', 'src/check.py'),
            ('"C:/Program Files/PowerShell/7/pwsh.exe" -NoProfile -Command "Get-Content -LiteralPath src/check.py"', 'src/check.py'),
            ("/bin/sh -lc 'cat src/check.py'", 'src/check.py')):
            with self.subTest(body=body):
                filename, signature = simple_read(body)
                self.assertEqual(filename, path)
                self.assertTrue(signature)

    def test_complex_or_ambiguous_commands_are_not_assumed_read_only(self):
        for body in ('cat file.py; echo changed > file.py', 'cat file.py | python -',
                     'echo Get-Content file.py', 'Get-Content $file', 'cat `whoami`',
                     'cat file.py && touch changed', 'cat *.py', 'cat -',
                     'Get-Content -LiteralPath a.py b.py', "cat 'unterminated",
                     'Get-Content @(Remove-Item("a.py"))', 'Get-Content @paths',
                     'cat #comment', 'pwsh -EncodedCommand Y2F0', 'bash -c cat file.py', None, []):
            with self.subTest(body=body):
                self.assertEqual(simple_read(body), ('', ''))


class CodexToolProcess(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def execute(self, script, errors=2):
        adapter = CodexAdapter('fixture')
        adapter._resolved = sys.executable
        adapter._version = 'codex-cli 0.169.0'
        context = RunContext('fixture', 'process-tools', 'coding', self.root, self.root,
                             attempt_dir=self.root/'attempt', timeout_s=12,
                             max_tool_errors=errors, max_repeated_reads=1)
        # 与真实 CLI 一样等待 stdin：父进程绑定 Windows Job 后才发送并关闭输入，
        # 避免测试子进程过早退出，导致尚未绑定 Job 就出现 WinError 5。
        script = 'import sys\nsys.stdin.buffer.read()\n' + script
        return adapter._exec(['-u', '-c', script], self.root, context)

    def test_real_child_repeated_reads_stop_before_timeout_and_keep_usage(self):
        body = 'Get-Content -LiteralPath "' + str(self.root/'check.py') + '"'
        events = [{'type': 'turn.completed', 'usage': {'input_tokens': 70, 'output_tokens': 30}},
                  {'type': 'turn.started'}, command('read-1', body=body), command('read-2', body=body)]
        script = 'import json,time\n'
        script += ''.join('print(' + repr(json.dumps(e)) + ',flush=True)\n' for e in events)
        script += 'time.sleep(30)\n'
        result = self.execute(script)
        self.assertEqual(result.error_kind, ErrorKind.TOOL_LIMIT)
        self.assertIn('repeated_read_limit', result.stderr)
        self.assertEqual(result.usage['known_tokens'], 100)
        self.assertTrue(result.usage['usage_unknown'])

    def test_real_child_policy_rejections_stop_before_timeout(self):
        raw = (FIXTURES/'codex_0.169_policy_stderr.txt').read_text(encoding='utf8')
        script = 'import sys,time\nsys.stderr.write(' + repr(raw) + ');sys.stderr.flush()\ntime.sleep(30)\n'
        result = self.execute(script)
        self.assertEqual(result.error_kind, ErrorKind.TOOL_LIMIT)
        self.assertIn('tool_error_limit', result.stderr)

    def test_final_unterminated_policy_error_overrides_successful_exit(self):
        line = (FIXTURES/'codex_0.169_policy_stderr.txt').read_text(encoding='utf8').splitlines()[0]
        events = [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'done'}},
                  {'type': 'turn.completed', 'usage': {'input_tokens': 70, 'output_tokens': 30}}]
        script = 'import sys\n' + ''.join('print(' + repr(json.dumps(e)) + ',flush=True)\n' for e in events)
        script += 'sys.stderr.write(' + repr(line) + ');sys.stderr.flush()\n'
        result = self.execute(script, errors=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, ErrorKind.TOOL_LIMIT)
        self.assertIn('tool_error_limit', result.stderr)
        self.assertEqual(result.usage['known_tokens'], 100)

    def test_final_unterminated_tool_event_overrides_successful_exit(self):
        events = [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'done'}},
                  {'type': 'turn.completed', 'usage': {'input_tokens': 70, 'output_tokens': 30}}]
        script = 'import sys\n' + ''.join('print(' + repr(json.dumps(e)) + ',flush=True)\n' for e in events)
        failure = command('last-error', 'item.completed', 'failed', 7, 'exit 7')
        script += 'sys.stdout.write(' + repr(json.dumps(failure)) + ');sys.stdout.flush()\n'
        result = self.execute(script, errors=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, ErrorKind.TOOL_LIMIT)
        self.assertEqual(result.usage['known_tokens'], 100)


if __name__ == '__main__':
    unittest.main()
