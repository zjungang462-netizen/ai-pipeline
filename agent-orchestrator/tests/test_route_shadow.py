"""只读路由对照必须使用成对样本和独立的历史运行。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from orchestrator.cli import main


class RouteShadowTests(unittest.TestCase):
    models = {'codex': ('cli_codex', 'codex-known'), 'agy': ('cli_agy', 'agy-known')}
    commit = '1' * 40
    task_hash = '2' * 64
    protected = '3' * 64

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.state = self.root / 'state'
        self.report = self.root / 'paired.json'

    def write_history(self, name, candidate, *, run_status='done', stages=1, model_calls=1):
        tool, model = self.models[candidate]
        path = self.state / 'runs' / name / 'metrics.json'
        path.parent.mkdir(parents=True)
        stages = {f'coding{i}': {'kind': 'code', 'status': 'done', 'tool': tool, 'model': model, 'attempts': 1}
                  for i in range(stages)}
        data = {'run_id': name, 'status': run_status, 'stages': stages,
                'stage_usage': {stage: {'model_calls': model_calls} for stage in stages}}
        path.write_text(json.dumps(data), encoding='utf8')

    def paired(self):
        def result(candidate, completed):
            row = {'project': 'sample', 'repeat': 1, 'candidate': candidate,
                   'outcome': 'completed' if completed else 'failed', 'baseline_commit': self.commit,
                   'protected_test_sha256': self.protected, 'protected_unchanged': True,
                   'completed': completed, 'acceptance_exit': 0 if completed else 1}
            row['statuses'] = dict.fromkeys(('design', 'coding', 'testing', 'review'), 'done' if completed else 'failed')
            return row

        return {'mode': 'live', 'schema_version': 1, 'repeats': 1,
                'candidates': {label: {'type': tool, 'model': model} for label, (tool, model) in self.models.items()},
                'tasks': {'sample': {'baseline_commit': self.commit, 'task_sha256': self.task_hash,
                                     'protected_test_sha256': self.protected}},
                'results': [result('codex', True), result('agy', False)],
                'summary': {'codex': {'completion_rate': 0}, 'agy': {'completion_rate': 1}},
                'historical_routing': {'candidates': []}}

    def run_cli(self, data):
        self.report.write_text(json.dumps(data), encoding='utf8')
        stdout, stderr = io.StringIO(), io.StringIO()
        args = ['route-recommend', '--state-dir', str(self.state), '--stage-kind', 'code',
                '--paired-results', str(self.report)]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            ret = main(args)
        return ret, stdout.getvalue(), stderr.getvalue()

    def test_shadow_recounts_paired_samples_and_uses_current_history(self):
        for candidate in self.models:
            for number in range(2):
                self.write_history(f'{candidate}-{number}', candidate)
        ret, text, error = self.run_cli(self.paired())
        self.assertEqual((ret, error), (0, ''))
        report = json.loads(text)
        comparison = report['shadow_comparison']
        self.assertEqual(comparison['status'], 'compared')
        self.assertEqual(comparison['paired_order'], ['codex', 'agy'])
        self.assertEqual(comparison['historical_order'], ['agy', 'codex'])
        self.assertFalse(comparison['agree'])
        self.assertEqual(comparison['paired_quality']['codex']['completed'], 1)

    def test_same_run_with_multiple_stages_and_no_call_history_do_not_fill_threshold(self):
        self.write_history('one-codex-run', 'codex', stages=2)
        self.write_history('codex-running', 'codex', run_status='running')
        self.write_history('agy-no-call', 'agy', model_calls=0)
        ret, text, _ = self.run_cli(self.paired())
        self.assertEqual(ret, 0)
        report = json.loads(text)
        self.assertEqual(report['scanned_metrics'], 2)
        self.assertIsNone(report['recommendations']['code']['recommended'])
        comparison = report['shadow_comparison']
        self.assertEqual(comparison['status'], 'insufficient_history')
        self.assertEqual(comparison['history_coverage']['codex']['history_runs'], 1)
        self.assertEqual(comparison['history_coverage']['agy']['missing_runs'], 2)
        self.assertEqual(report['recommendations']['code']['candidates'][0]['stage_samples'], 2)

    def test_missing_trial_keeps_expected_denominator(self):
        data = self.paired()
        data['results'].pop()
        ret, text, _ = self.run_cli(data)
        self.assertEqual(ret, 0)
        comparison = json.loads(text)['shadow_comparison']
        self.assertEqual(comparison['status'], 'incomplete_trials')
        self.assertEqual(comparison['paired_quality']['agy']['scheduled'], 1)
        self.assertEqual(comparison['paired_quality']['agy']['recorded'], 0)

    def test_rejects_offline_fixtures_mismatched_hashes_or_duplicates(self):
        for branch in ('offline', 'hash', 'duplicate', 'invalid_pass'):
            with self.subTest(branch=branch):
                data = self.paired()
                if branch == 'offline':
                    data['mode'] = 'offline_fixture'
                elif branch == 'hash':
                    data['results'][0]['protected_test_sha256'] = '4' * 64
                elif branch == 'duplicate':
                    data['results'].append(data['results'][0])
                else:
                    data['results'][0]['statuses']['review'] = 'failed'
                ret, text, error = self.run_cli(data)
                self.assertNotEqual(ret, 0)
                self.assertIn('错误', text)
                self.assertEqual(error, '')
