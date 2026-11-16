"""异常 CLI 输出不得中断观测、降低已知用量或绕过停止边界。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.adapters.cli_codex import CodexAdapter
from orchestrator.cli_telemetry import CLITrace, MAX_LINE
from orchestrator.config import ConfigError, parse_config
from orchestrator.contracts import RunContext


class CLITelemetryEdges(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def context(self):
        return RunContext('fixture', 'run', 'coding', self.root, self.root,
                          kind='code', attempt_dir=self.root / 'attempt', timeout_s=15)

    def adapter(self):
        adapter = CodexAdapter('fixture')
        adapter._resolved = sys.executable
        adapter._version = 'codex-cli 0.169.0'
        return adapter

    def test_malformed_tool_results_do_not_discard_later_usage(self):
        trace = CLITrace(self.context())
        self.addCleanup(trace.finish, 'exited')
        for identifier in ([], {}, None, 1):
            trace.feed('stdout', json.dumps({'type': 'tool_result', 'callId': identifier}) + '\n')
        trace.feed('stdout', json.dumps({'type': 'turn.completed',
                   'usage': {'input_tokens': 70, 'output_tokens': 30}}) + '\n')
        self.assertEqual(trace.snapshot()['usage']['known_tokens'], 100)
        self.assertEqual(trace.tick(), '')

    def test_oversized_line_stops_regardless_of_chunk_boundary(self):
        for split in (None, MAX_LINE, MAX_LINE + 1):
            with self.subTest(split=split):
                trace = CLITrace(self.context())
                self.addCleanup(trace.finish, 'stopped')
                value = 'x' * (MAX_LINE + 1) + '\n'
                if split is None:
                    trace.feed('stdout', value)
                else:
                    trace.feed('stdout', value[:split])
                    trace.feed('stdout', value[split:])
                self.assertTrue(trace.snapshot()['truncated'])
                self.assertEqual(trace.tick(), 'output_limit')

    def test_line_at_limit_remains_valid(self):
        trace = CLITrace(self.context())
        self.addCleanup(trace.finish, 'exited')
        trace.feed('stdout', 'x' * MAX_LINE + '\n')
        self.assertFalse(trace.snapshot()['truncated'])
        self.assertEqual(trace.tick(), '')

    def test_failed_call_keeps_usage_observed_before_stdout_tail(self):
        # 管道返回值只保留尾部，实时观测已见到的完整事件仍须入账。
        tail = json.dumps({'type': 'turn.started'}) + '\n'
        completed = json.dumps({'type': 'turn.completed',
                                'usage': {'input_tokens': 70, 'output_tokens': 30,
                                          'cached_input_tokens': 50}}) + '\n'
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                def execute(*args, **kwargs):
                    kwargs['trace'].feed('stdout', completed + tail)
                    if timeout:
                        raise subprocess.TimeoutExpired(args[0], 15, output=tail)
                    return subprocess.CompletedProcess(args[0], 1, tail, 'interrupted')

                with mock.patch('orchestrator.adapters.cli_base.run_cli_process', side_effect=execute):
                    result = self.adapter()._exec([], self.root, self.context())
                self.assertFalse(result.ok)
                self.assertTrue(result.usage['usage_unknown'])
                self.assertEqual(result.usage['known_tokens'], 100)
                self.assertEqual(result.usage['tokens'], 100)
                self.assertEqual(result.usage['cache_read_tokens'], 50)

    def test_real_child_stdout_truncation_keeps_earlier_response_usage(self):
        script = (
            'import json,sys\n'
            'print(json.dumps({"type":"turn.completed","usage":'
            '{"input_tokens":70,"output_tokens":30}}),flush=True)\n'
            'print(json.dumps({"type":"turn.started"}),flush=True)\n'
            'sys.stdout.write(("diagnostic" * 100 + "\\n") * 4500)\n'
            'sys.exit(1)\n'
        )
        # 本用例验证完整大输出的截断，不能在机器繁忙时提前变成超时测试。
        # 显式检查夹具退出码，确保足量输出确实写完；生产调用超时保持不变。
        context = self.context()
        context.timeout_s = 60
        script = 'import sys\nsys.stdin.buffer.read()\n' + script
        result = self.adapter()._exec(['-u', '-c', script], self.root, context)
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 1, result.stderr)
        self.assertFalse('turn.completed' in result.stdout, '完整大输出应淘汰早期事件')
        self.assertTrue(result.usage['usage_unknown'])
        self.assertEqual(result.usage['known_tokens'], 100)


class CLIPolicyBounds(unittest.TestCase):
    def config(self, field, value):
        return parse_config({'task_workdir': '.', 'state_dir': '.state',
                             'autonomy': {field: value},
                             'adapters': {'fake': {'type': 'fake'}},
                             'stages': [{'name': 'report', 'adapter': 'fake', 'prompt': 'fixture'}]})

    def test_cli_limits_reject_out_of_range_values_and_accept_boundaries(self):
        limits = {'cli_idle_timeout_s': 3600, 'cli_max_tool_errors': 20,
                  'cli_max_repeated_reads': 20}
        for field, maximum in limits.items():
            for value in (0, -1, True, 1.5, maximum + 1):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ConfigError):
                        self.config(field, value)
            for value in (1, maximum):
                with self.subTest(field=field, value=value):
                    config = self.config(field, value)
                    self.assertEqual(getattr(config.autonomy, field), value)


if __name__ == '__main__':
    unittest.main()
