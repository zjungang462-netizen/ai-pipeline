"""受控失败经验沉淀与多模型自适应路由推荐测试。"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from orchestrator.cli import main as cli_main
from orchestrator.config import load_config
from orchestrator.engine import Engine
from orchestrator.lessons import (
    APPROVED_LESSONS_FILE,
    approve_lessons,
    load_approved_lessons_text,
    propose_lessons,
)
from orchestrator.model_router import recommend_adapters


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


class TestSelfOptimization(unittest.TestCase):
    def _seed_failure_runs(self, state_dir: Path, secret_token: str = '') -> None:
        secret_suffix = f' {secret_token}' if secret_token else ''
        for run_id in ('r1', 'r2'):
            _write_json(
                state_dir / 'runs' / run_id / 'stages' / 'review' / 'attempts' / '001' / 'review_verdict.json',
                {
                    'verdict': 'fail',
                    'issues': [
                        {
                            'file': 'src/calc.py',
                            'problem': f'未处理除零异常{secret_suffix}',
                            'expected': '显式抛出 ValueError',
                        }
                    ],
                },
            )
            _write_json(
                state_dir / 'runs' / run_id / 'stages' / 'testing' / 'attempts' / '001' / 'test_record.json',
                {
                    'ok': False,
                    'failure_ids': ['tests.test_calc.TestCalc.test_zero'],
                },
            )
        # 单次偶发失败：min_occurrences=2 时不应被纳入提案
        _write_json(
            state_dir / 'runs' / 'r3' / 'stages' / 'review' / 'attempts' / '001' / 'review_verdict.json',
            {
                'verdict': 'fail',
                'issues': [{'file': 'src/once.py', 'problem': '偶发拼写问题', 'expected': '修正拼写'}],
            },
        )
        _write_json(
            state_dir / 'runs' / 'r3' / 'stages' / 'testing' / 'attempts' / '001' / 'test_record.json',
            {'ok': False, 'failure_ids': ['tests.test_once.test_rare']},
        )

    def _seed_metrics_runs(self, state_dir: Path) -> None:
        samples = [
            # deepseek-chat: 2 runs, 100% pass, usage complete, 4.0s / 6.0s -> median 5.0s
            ('m1', 'done', 'api_openai', 'deepseek-chat', 'done', 4.0, 1200, 0),
            ('m2', 'done', 'api_openai', 'deepseek-chat', 'done', 6.0, 1400, 0),
            # cli-fast-unknown: 2 runs, 100% pass, 2.0s, but unknown_calls=1 (usage_complete=False)
            ('m3', 'done', 'cli_codex', 'codex-default', 'done', 2.0, 0, 1),
            ('m4', 'done', 'cli_codex', 'codex-default', 'done', 2.0, 0, 1),
            # flaky-model: 2 runs, 50% run pass
            ('m5', 'done', 'api_openai', 'flaky-v1', 'done', 1.5, 900, 0),
            ('m6', 'failed', 'api_openai', 'flaky-v1', 'failed', 1.5, 900, 0),
            # single-sample: only 1 run -> insufficient_data
            ('m7', 'done', 'api_openai', 'single-shot', 'done', 1.0, 500, 0),
        ]
        for run_id, run_status, tool, model, stage_status, dur, tokens, unknown in samples:
            _write_json(
                state_dir / 'runs' / run_id / 'metrics.json',
                {
                    'run_id': run_id,
                    'status': run_status,
                    'stages': {
                        'coding': {'kind': 'code', 'tool': tool, 'model': model, 'status': stage_status},
                    },
                    'stage_usage': {
                        'coding': {
                            'backend_model': model,
                            'duration_s': dur,
                            'tokens': tokens,
                            'unknown_calls': unknown,
                        }
                    },
                },
            )

    def test_propose_and_approve_lessons_with_redaction_and_hash_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / 'state'
            dummy_secret = 'DUMMY_SECRET_TOKEN_987654'
            self._seed_failure_runs(state_dir, secret_token=dummy_secret)

            proposal = propose_lessons(state_dir, min_occurrences=2, secrets=(dummy_secret,))
            self.assertEqual(proposal['scanned_runs'], 3)
            self.assertEqual(len(proposal['items']), 2)
            combined = '\n'.join(item['checklist'] for item in proposal['items'])
            self.assertIn('src/calc.py', combined)
            self.assertIn('tests.test_calc.TestCalc.test_zero', combined)
            self.assertNotIn('src/once.py', combined)
            self.assertNotIn('test_rare', combined)
            self.assertNotIn(dummy_secret, combined)

            # 篡改提案内容但未更新 proposal_hash 必须被拒绝
            tampered = json.loads(json.dumps(proposal))
            tampered['items'][0]['checklist'] = '任意篡改规则'
            with self.assertRaises(ValueError):
                approve_lessons(state_dir, tampered)
            self.assertFalse((state_dir / APPROVED_LESSONS_FILE).exists())

            # 正常批准后可读出检查清单文本
            approved_path = approve_lessons(state_dir, proposal)
            self.assertTrue(approved_path.is_file())
            text = load_approved_lessons_text(state_dir)
            self.assertIn('已批准的历史高频缺陷检查清单', text)
            self.assertIn('src/calc.py', text)

    def test_prompt_rendering_injects_approved_lessons_only_when_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / 'state'
            workdir = root / 'work'
            workdir.mkdir()
            cfg_path = root / 'pipeline.toml'
            cfg_path.write_text(
                f'''
state_dir = "{state_dir.as_posix()}"
task_workdir = "{workdir.as_posix()}"

[adapters.mock]
type = "fake"

[[stages]]
name = "design"
kind = "report"
adapter = "mock"
prompt = "设计任务：{{task}}"

[[stages]]
name = "coding"
kind = "code"
adapter = "mock"
prompt = "编码任务：{{task}}"

[[stages]]
name = "review"
kind = "review"
adapter = "mock"
prompt = "审查任务：{{task}}"
''',
                encoding='utf-8',
            )
            cfg = load_config(cfg_path)
            engine = Engine(cfg, {}, 'run-lessons', '实现计算器')

            # 未批准前不注入
            design_before = engine._render_prompt(cfg.stages[0], 0)
            code_before = engine._render_prompt(cfg.stages[1], 1)
            self.assertNotIn('已批准的历史高频缺陷检查清单', design_before)
            self.assertNotIn('已批准的历史高频缺陷检查清单', code_before)

            # 批准后仅注入 report/code，不注入 review
            self._seed_failure_runs(state_dir)
            proposal = propose_lessons(state_dir, min_occurrences=2)
            approve_lessons(state_dir, proposal)

            design_after = engine._render_prompt(cfg.stages[0], 0)
            code_after = engine._render_prompt(cfg.stages[1], 1)
            review_after = engine._render_prompt(cfg.stages[2], 2)
            self.assertIn('已批准的历史高频缺陷检查清单', design_after)
            self.assertIn('src/calc.py', code_after)
            self.assertNotIn('已批准的历史高频缺陷检查清单', review_after)

    def test_recommend_adapters_ranking_and_unknown_usage_accounting(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / 'state'
            self._seed_metrics_runs(state_dir)

            report = recommend_adapters(state_dir, stage_kind='code', min_runs=2)
            self.assertEqual(report['scanned_metrics'], 7)
            code_rec = report['recommendations']['code']
            # deepseek-chat 应排在 usage_complete=False 的 codex-default 之前
            self.assertEqual(code_rec['recommended'], {'tool': 'api_openai', 'model': 'deepseek-chat'})

            candidates = {c['model']: c for c in code_rec['candidates']}
            self.assertTrue(candidates['deepseek-chat']['usage_complete'])
            self.assertEqual(candidates['deepseek-chat']['median_reported_tokens'], 1300)
            self.assertFalse(candidates['codex-default']['usage_complete'])
            self.assertEqual(candidates['codex-default']['unknown_calls'], 2)
            self.assertIsNone(candidates['codex-default']['median_reported_tokens'])
            self.assertEqual(candidates['single-shot']['status'], 'insufficient_data')
            self.assertFalse(candidates['single-shot']['eligible'])

            # min_runs=5 时全部不达标，不自动推荐
            strict = recommend_adapters(state_dir, stage_kind='code', min_runs=5)
            self.assertIsNone(strict['recommendations']['code']['recommended'])

    def test_cli_self_optimization_subcommands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / 'state'
            self._seed_failure_runs(state_dir)
            self._seed_metrics_runs(state_dir)
            proposal_out = root / 'proposal.json'

            # --output 在 state_dir 内必须拒绝（返回非零退出码）
            err_buf = io.StringIO()
            with contextlib.redirect_stdout(err_buf), contextlib.redirect_stderr(err_buf):
                rc_bad = cli_main([
                    'lessons-propose',
                    '--state-dir', str(state_dir),
                    '--output', str(state_dir / 'inside.json'),
                ])
            self.assertNotEqual(rc_bad, 0)
            self.assertIn('必须位于状态目录之外', err_buf.getvalue())

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli_main([
                    'lessons-propose',
                    '--state-dir', str(state_dir),
                    '--output', str(proposal_out),
                ])
            self.assertEqual(rc, 0)
            self.assertTrue(proposal_out.is_file())
            proposed = json.loads(buf.getvalue())
            self.assertEqual(len(proposed['items']), 2)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli_main([
                    'lessons-approve',
                    '--state-dir', str(state_dir),
                    '--proposal', str(proposal_out),
                ])
            self.assertEqual(rc, 0)
            approved = json.loads(buf.getvalue())
            self.assertEqual(approved['items'], 2)
            self.assertEqual(approved['proposal_hash'], proposed['proposal_hash'])

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli_main([
                    'route-recommend',
                    '--state-dir', str(state_dir),
                    '--stage-kind', 'code',
                    '--min-runs', '2',
                ])
            self.assertEqual(rc, 0)
            routed = json.loads(buf.getvalue())
            self.assertEqual(
                routed['recommendations']['code']['recommended'],
                {'tool': 'api_openai', 'model': 'deepseek-chat'},
            )

    def test_model_router_skips_unexecuted_downstream_and_non_finite_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / 'state'
            # 上游 design 失败，下游 coding 未执行（pending / attempts=0），不应拉低 downstream-model
            for run_id in ('u1', 'u2'):
                _write_json(
                    state_dir / 'runs' / run_id / 'metrics.json',
                    {
                        'run_id': run_id,
                        'status': 'failed',
                        'stages': {
                            'design': {'kind': 'report', 'tool': 'api_openai', 'model': 'upstream-bad', 'status': 'failed', 'attempts': 1},
                            'coding': {'kind': 'code', 'tool': 'api_openai', 'model': 'downstream-clean', 'status': 'pending', 'attempts': 0},
                        },
                        'stage_usage': {},
                    },
                )
            # 正常执行记录包含非法 Infinity 耗时（手写 JSON 模拟异常文件），应安全忽略而非输出非法 JSON
            for run_id in ('u3', 'u4'):
                _write_json(
                    state_dir / 'runs' / run_id / 'metrics.json',
                    {
                        'run_id': run_id,
                        'status': 'done',
                        'stages': {
                            'coding': {'kind': 'code', 'tool': 'api_openai', 'model': 'downstream-clean', 'status': 'done', 'attempts': 1},
                        },
                        'stage_usage': {
                            'coding': {'backend_model': 'downstream-clean', 'duration_s': float('inf'), 'tokens': 800, 'unknown_calls': 0},
                        },
                    },
                )

            report = recommend_adapters(state_dir, stage_kind='code', min_runs=2)
            code_rec = report['recommendations']['code']
            self.assertEqual(code_rec['recommended'], {'tool': 'api_openai', 'model': 'downstream-clean'})
            entry = code_rec['candidates'][0]
            self.assertEqual(entry['runs'], 2)
            self.assertEqual(entry['stage_pass_rate'], 1.0)
            self.assertEqual(entry['run_pass_rate'], 1.0)
            self.assertIsNone(entry['median_duration_s'])
            # 确保可严格序列化为标准 JSON（禁止 Infinity/NaN）
            json.dumps(report, allow_nan=False)

    def test_autonomy_inputs_hash_updates_when_lessons_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / 'state'
            workdir = root / 'work'
            workdir.mkdir()
            cfg_path = root / 'autonomy.toml'
            cfg_path.write_text(
                f'''
state_dir = "{state_dir.as_posix()}"
task_workdir = "{workdir.as_posix()}"

[run_budget]
max_tokens = 10000
max_duration_s = 300

[autonomy]
enabled = true
allowed_files = ["app.py"]

[adapters.mock]
type = "fake"

[[stages]]
name = "coding"
kind = "code"
adapter = "mock"
prompt = "编码任务：{{task}}"
''',
                encoding='utf-8',
            )
            cfg = load_config(cfg_path)
            engine = Engine(cfg, {}, 'run-auto', '实现功能')
            before_hash = engine._inputs_hash(cfg.stages[0], 0)
            self.assertIn('coding', engine.state.autonomy['execution_inputs'])
            self.assertEqual(engine._inputs_hash(cfg.stages[0], 0), before_hash)

            self._seed_failure_runs(state_dir)
            proposal = propose_lessons(state_dir, min_occurrences=2)
            approve_lessons(state_dir, proposal)

            after_hash = engine._inputs_hash(cfg.stages[0], 0)
            self.assertNotEqual(after_hash, before_hash)

    def test_lessons_scrubs_env_and_pattern_credentials_and_validates_approve_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / 'state'
            env_secret = 'ENV_VAL_' + '998877665544'
            fake_key = 'sk' + '-' + 'pattern1234567890abcd'
            fake_url = 'https://' + 'alice' + ':' + 'pass98765' + '@example.com/api'
            fake_bearer = 'Bearer ' + 'jwt_payload_1234567890'
            for run_id in ('s1', 's2'):
                _write_json(
                    state_dir / 'runs' / run_id / 'stages' / 'review' / 'attempts' / '001' / 'review_verdict.json',
                    {
                        'verdict': 'fail',
                        'issues': [{
                            'file': 'src/auth.py',
                            'problem': f'泄漏 {env_secret} 与 {fake_key} 与 {fake_bearer}',
                            'expected': f'不要硬编码 {fake_url}',
                        }],
                    },
                )

            with mock.patch.dict(os.environ, {'MY_TEST_API_TOKEN': env_secret}):
                proposal = propose_lessons(state_dir, min_occurrences=2)

            self.assertEqual(len(proposal['items']), 1)
            text = proposal['items'][0]['checklist']
            for sensitive in (env_secret, fake_key, 'pass98765', 'jwt_payload_1234567890'):
                self.assertNotIn(sensitive, text)
            self.assertIn('[REDACTED]', text)

            # approve_lessons 拒绝非法 max_items 和 bool occurrences
            for bad_max in (0, -1, True, 51):
                with self.assertRaises(ValueError):
                    approve_lessons(state_dir, proposal, max_items=bad_max)

            bad_occ_proposal = json.loads(json.dumps(proposal))
            bad_occ_proposal['items'][0]['occurrences'] = True
            from orchestrator.state import sha256_text
            bad_occ_proposal['proposal_hash'] = sha256_text(
                json.dumps(bad_occ_proposal['items'], ensure_ascii=False, sort_keys=True)
            )
            with self.assertRaises(ValueError):
                approve_lessons(state_dir, bad_occ_proposal)

            # CLI lessons-approve --max-items 1 汇报条数必须与实际写入一致
            self._seed_failure_runs(state_dir)
            multi_proposal = propose_lessons(state_dir, min_occurrences=2)
            self.assertGreaterEqual(len(multi_proposal['items']), 2)
            proposal_file = root / 'multi.json'
            proposal_file.write_text(json.dumps(multi_proposal, ensure_ascii=False), encoding='utf-8')
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli_main([
                    'lessons-approve',
                    '--state-dir', str(state_dir),
                    '--proposal', str(proposal_file),
                    '--max-items', '1',
                ])
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertEqual(out['items'], 1)
            saved = json.loads((state_dir / APPROVED_LESSONS_FILE).read_text(encoding='utf-8'))
            self.assertEqual(len(saved['items']), 1)


if __name__ == '__main__':
    unittest.main()
