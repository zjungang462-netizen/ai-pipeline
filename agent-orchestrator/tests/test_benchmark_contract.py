"""基准配置必须可复读，不能将脚本机制验证混入真实模型完成率。"""
import tempfile
import tomllib
import unittest
from unittest import mock
from pathlib import Path

from scripts.benchmark import save_config, Scripted, run_case
from orchestrator.contracts import PreflightReport


class BenchmarkContract(unittest.TestCase):
    def test_generated_toml_roundtrip_preserves_adapter_arrays_and_booleans(self):
        raw = {'task_workdir': 'D:/目录/work', 'adapters': {'x': {'type': 'fake', 'extra_env': ['A'], 'enabled': True}},
               'stages': [{'name': 'test', 'kind': 'test', 'test_cmd': ['python', '-c', 'assert True']}],
               'run_budget': {'unlimited_tokens': True, 'max_duration_s': 240}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.toml'
            save_config(path, raw)
            self.assertEqual(tomllib.loads(path.read_text(encoding='utf8')), raw)

    def test_scripted_adapter_is_explicitly_fake(self):
        self.assertEqual(Scripted('coding', 'first_pass').channel, 'fake')

    def test_unavailable_role_blocks_all_model_calls_before_design(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'roles.toml'
            save_config(config, {'adapters': {'role': {'type': 'fake'}}, 'stages': [
                {'name': kind, 'kind': kind, 'adapter': 'role'} for kind in ('report', 'code', 'review')]})
            unavailable = mock.Mock()
            unavailable.preflight.return_value = PreflightReport('role', False, detail='unavailable')
            unavailable._version_known.return_value = False
            with mock.patch('scripts.benchmark.build_all', return_value={'role': unavailable}):
                record = run_case(root, 'blocked', config)
            self.assertEqual(record['failure_category'], 'preflight_blocked')
            self.assertEqual(record['model_calls'], 0)
            unavailable.run.assert_not_called()


class PairedBenchmarkContract(unittest.TestCase):
    def test_aggregation_includes_blocked_trials_and_keeps_unknown_usage_separate(self):
        from scripts.paired_benchmark import summarize

        rows = [
            {'outcome': 'completed', 'protected_unchanged': True, 'acceptance_exit': 0,
             'elapsed_s': 2, 'model_calls': 1, 'usage': {'reported_tokens': 60}},
            {'outcome': 'failed', 'protected_unchanged': False, 'acceptance_exit': 0,
             'elapsed_s': 4, 'model_calls': 1, 'usage': {'tokens': 6000, 'unknown_calls': 1}},
            {'outcome': 'blocked', 'model_calls': 0},
        ]
        summary = summarize(rows)
        self.assertEqual((summary['completed'], summary['failed'], summary['blocked']), (1, 1, 1))
        self.assertEqual(summary['completion_rate'], 1 / 3)
        self.assertEqual(summary['protected_pass_rate'], 1 / 3)
        self.assertEqual(summary['median_wall_time_s'], 3)
        self.assertEqual(summary['median_reported_tokens'], 60)
        self.assertEqual(summary['unknown_calls'], 1)

    def test_pairs_are_matched_by_case_and_repeat_and_keep_candidate_order(self):
        from scripts.paired_benchmark import compare_pairs

        rows = [
            {'project': 'invoice', 'repeat': 1, 'candidate': 'b', 'elapsed_s': 6, 'outcome': 'failed'},
            {'project': 'invoice', 'repeat': 1, 'candidate': 'a', 'elapsed_s': 4, 'outcome': 'completed'},
            {'project': 'invoice', 'repeat': 2, 'candidate': 'a', 'outcome': 'blocked'},
        ]
        self.assertEqual(compare_pairs(['invoice'], 2, ['a', 'b'], rows), [
            {'case': 'invoice', 'repeat': 1, 'outcomes': ['completed', 'failed'], 'wall_time_delta_s': 2},
        ])

    def test_shadow_comparison_uses_matched_models_and_reports_insufficient_samples(self):
        from scripts.paired_benchmark import compare_routing

        data = {
            'candidates': {'a': {'type': 'openai_compat', 'model': 'first'},
                           'b': {'type': 'cli_codex', 'model': 'second'}},
            'historical_routing': {'candidates': [
                {'tool': 'openai_compat', 'model': 'first', 'eligible': True},
                {'tool': 'cli_codex', 'model': 'second', 'eligible': True},
            ]},
            'summary': {'a': {'scheduled': 9, 'blocked': 0, 'completion_rate': .5, 'protected_pass_rate': .5},
                        'b': {'scheduled': 9, 'blocked': 0, 'completion_rate': 1, 'protected_pass_rate': 1}},
            'results': [],
        }
        self.assertEqual(compare_routing(data), {'status': 'compared', 'historical_order': ['a', 'b'],
                                                 'paired_order': ['b', 'a'], 'agree': False})
        data['historical_routing']['candidates'][1]['eligible'] = False
        self.assertEqual(compare_routing(data), {'status': 'insufficient_history'})


if __name__ == '__main__':
    unittest.main()
