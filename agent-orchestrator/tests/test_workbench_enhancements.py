"""工作台项目管理、审批附注、自优化 HTTP 端点与 agy 补丁探针测试。"""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from orchestrator.adapters.cli_agy import AgyAdapter
from orchestrator.config import load_config
from orchestrator.contracts import RunContext
from orchestrator.dashboard.server import create_server
from orchestrator.engine import Engine


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


class TestWorkbenchEnhancements(unittest.TestCase):
    def test_agy_patch_probe_opt_in_and_strict_help_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = RunContext(run_id='r', stage='coding', kind='code', task='t', workdir=root, runs_dir=root)
            valid_help = 'Usage: --print --input-format --output-format --mode --sandbox --add-dir'

            # 默认关闭 patch probe：1.5.15 拒绝并在错误里指明检测到的版本
            with mock.patch('orchestrator.adapters.cli_base.resolve_exe', return_value='agy-bin'), \
                    mock.patch('orchestrator.adapters.cli_base.run_cli_process',
                               return_value=subprocess.CompletedProcess([], 0, '1.5.15', '')):
                res = AgyAdapter('agy').run('hi', root, ctx)
                self.assertFalse(res.ok)
                self.assertIn('1.5.15', res.stderr)

            # 开启 allow_patch_probe=True 且同 1.5.x + --help 关键参数齐全 => 放行
            with mock.patch('orchestrator.adapters.cli_base.resolve_exe', return_value='agy-bin'), \
                    mock.patch('orchestrator.adapters.cli_base.run_cli_process',
                               return_value=subprocess.CompletedProcess([], 0, '1.5.15', '')), \
                    mock.patch('orchestrator.adapters.cli_agy.run_cli_process',
                               return_value=subprocess.CompletedProcess([], 0, valid_help, '')):
                adapter = AgyAdapter('agy', allow_patch_probe=True)
                probe = adapter.preflight()
                self.assertTrue(probe.ok)
                self.assertEqual(probe.tool_version, '1.5.15')

            # 缺少关键 flag 或跨次版本 1.6.0 必须拒绝
            for ver, help_text in (('1.5.15', '--print only'), ('1.6.0', valid_help)):
                with mock.patch('orchestrator.adapters.cli_base.resolve_exe', return_value='agy-bin'), \
                        mock.patch('orchestrator.adapters.cli_base.run_cli_process',
                                   return_value=subprocess.CompletedProcess([], 0, ver, '')), \
                        mock.patch('orchestrator.adapters.cli_agy.run_cli_process',
                                   return_value=subprocess.CompletedProcess([], 0, help_text, '')):
                    adapter = AgyAdapter('agy', allow_patch_probe=True)
                    self.assertFalse(adapter.preflight().ok)

    def test_approval_notes_injected_into_downstream_prompt_and_autonomy_hash(self):
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

[run_budget]
max_tokens = 20000
max_duration_s = 300

[autonomy]
enabled = true
allowed_files = ["app.py"]
auto_approve = ["repair_plan"]

[adapters.mock]
type = "fake"

[[stages]]
name = "repair_plan"
kind = "report"
adapter = "mock"
approval = true
prompt = "方案：{{task}}"

[[stages]]
name = "coding"
kind = "code"
adapter = "mock"
prompt = "编码：{{task}}"
''',
                encoding='utf-8',
            )
            cfg = load_config(cfg_path)
            engine = Engine(cfg, {}, 'run-note', '修复项目')
            before_prompt = engine._render_prompt(cfg.stages[1], 1)
            before_hash = engine._inputs_hash(cfg.stages[1], 1)
            self.assertNotIn('人工审批附注与修订指令', before_prompt)

            engine.state.control['approval_notes'] = {'repair_plan': '不要降级 pinia，只修改 README 端口'}
            after_prompt = engine._render_prompt(cfg.stages[1], 1)
            after_hash = engine._inputs_hash(cfg.stages[1], 1)
            self.assertIn('人工审批附注与修订指令', after_prompt)
            self.assertIn('不要降级 pinia，只修改 README 端口', after_prompt)
            self.assertNotEqual(after_hash, before_hash)

    def test_dashboard_http_project_register_lessons_and_route_recommend(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        target_repo = root / 'sample-repo'
        target_repo.mkdir()
        seed_cfg = root / 'seed.toml'
        seed_state = root / 'seed-state'
        seed_cfg.write_text(
            f'''
state_dir = "{seed_state.as_posix()}"
task_workdir = "{target_repo.as_posix()}"

[adapters.mock]
type = "fake"

[[stages]]
name = "design"
kind = "report"
adapter = "mock"
prompt = "设计：{{task}}"
''',
            encoding='utf-8',
        )
        # 预置历史失败与 metrics 以验证自优化端点
        for rid in ('r1', 'r2'):
            _write_json(
                seed_state / 'runs' / rid / 'stages' / 'review' / 'attempts' / '1' / 'review_verdict.json',
                {'verdict': 'fail', 'issues': [{'file': 'app.py', 'problem': '未校验空值', 'expected': '判空保护'}]},
            )
            _write_json(
                seed_state / 'runs' / rid / 'metrics.json',
                {
                    'run_id': rid,
                    'status': 'done',
                    'stages': {'coding': {'kind': 'code', 'tool': 'cli_agy', 'model': 'gemini-3.8-flash-high', 'status': 'done', 'attempts': 1}},
                    'stage_usage': {'coding': {'backend_model': 'gemini-3.8-flash-high', 'duration_s': 12.5, 'tokens': 3000, 'unknown_calls': 0}},
                },
            )

        server = create_server(root / 'wb.sqlite3', [seed_cfg], 0, recover=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def _stop():
            server.application.closed.set()
            server.shutdown()
            server.server_close()
            thread.join(5)
        self.addCleanup(_stop)

        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=15)
        self.addCleanup(conn.close)

        def req(method, path, body=None, headers=None):
            h = dict(headers or {})
            payload = None
            if body is not None:
                payload = json.dumps(body).encode('utf-8')
                h['Content-Type'] = 'application/json'
            conn.request(method, path, payload, h)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, json.loads(raw), resp

        status, sess, resp = req('POST', '/api/session', {'token': server.application.bootstrap},
                                 {'Origin': server.application.origin})
        self.assertEqual(status, 200)
        cookie = resp.getheader('Set-Cookie').split(';')[0]
        auth_headers = {'Cookie': cookie, 'Origin': server.application.origin,
                        'X-Workbench-CSRF': sess['csrf'], 'Idempotency-Key': 'idem-key-12345678'}

        # 1. 列出项目 -> 获取 seed project id
        status, proj_data, _ = req('GET', '/api/projects', headers={'Cookie': cookie})
        self.assertEqual(status, 200)
        seed_id = proj_data['projects'][0]['id']

        # 2. 自优化：lessons 扫描 + approve
        status, lessons_data, _ = req('GET', f'/api/projects/{seed_id}/lessons?min_occurrences=2',
                                      headers={'Cookie': cookie})
        self.assertEqual(status, 200)
        self.assertEqual(len(lessons_data['proposal']['items']), 1)

        status, approved, _ = req('POST', f'/api/projects/{seed_id}/lessons/approve',
                                  {'proposal': lessons_data['proposal']}, auth_headers)
        self.assertEqual(status, 200)
        self.assertEqual(approved['items'], 1)
        self.assertTrue((seed_state / 'approved_lessons.json').is_file())

        # 3. 自优化：route-recommend
        status, route_data, _ = req('GET', f'/api/projects/{seed_id}/route-recommend?min_runs=2',
                                    headers={'Cookie': cookie})
        self.assertEqual(status, 200)
        self.assertEqual(route_data['recommendations']['code']['recommended'],
                         {'tool': 'cli_agy', 'model': 'gemini-3.8-flash-high'})

        # 4. 可视化热登记新项目（按节点独立配置适配器与模型）
        status, created, _ = req('POST', '/api/projects/register', {
            'name': 'demo-app',
            'workdir': str(target_repo),
            'preset': 'review_plan_fix',
            'stage_adapters': {
                'code_review': {'type': 'cli_agy', 'model': 'gemini-3.8-pro-high'},
                'repair_plan': {'type': 'openai_compat', 'model': 'deepseek-reasoner'},
                'coding': {'type': 'cli_agy', 'model': 'gemini-3.8-flash-high'},
                'final_review': {'type': 'openai_compat', 'model': 'deepseek-chat'},
            },
            'proxy_url': 'http://127.0.0.1:7897',
            'max_duration_s': 600,
            'max_model_calls': 10,
            'allow_patch_probe': True,
        }, {**auth_headers, 'Idempotency-Key': 'idem-reg-12345678'})
        self.assertEqual(status, 201)
        self.assertEqual(created['name'], 'demo-app')
        models_by_name = {m['name']: m for m in created['models']}
        self.assertEqual(models_by_name['code_review_agent']['stages'], ['code_review'])
        self.assertEqual(models_by_name['repair_plan_agent']['model'], 'deepseek-reasoner')
        self.assertEqual(models_by_name['coding_agent']['model'], 'gemini-3.8-flash-high')
        self.assertEqual(models_by_name['final_review_agent']['stages'], ['final_review'])

        # 拒绝不存在的目录
        status, _, _ = req('POST', '/api/projects/register', {
            'name': 'bad-app',
            'workdir': str(root / 'missing-dir'),
            'preset': 'review_only',
        }, {**auth_headers, 'Idempotency-Key': 'idem-bad-12345678'})
        self.assertEqual(status, 400)

        # 5. 本机模型目录探测端点（mock agy/api 探测，离线确定性验证）
        from orchestrator.collaboration.model_catalog import _parse_agy_models
        parsed = _parse_agy_models('Fetching available models...\ngemini-4-neon-high\tGemini 4 Neon (High)\nclaude-opus-5-5-high\tClaude Opus 5.5 (High)\n')
        self.assertEqual([m['id'] for m in parsed], ['gemini-4-neon-high', 'claude-opus-5-5-high'])
        with mock.patch('orchestrator.collaboration.model_catalog._probe_agy_models', return_value=parsed), \
                mock.patch('orchestrator.collaboration.model_catalog._probe_openai_compat_models', return_value=(True, [{'id': 'deepseek-chat', 'label': 'deepseek-chat'}])):
            status, catalog, _ = req('GET', '/api/models/catalog?refresh=1', headers={'Cookie': cookie})
            self.assertEqual(status, 200)
            self.assertEqual([m['id'] for m in catalog['adapters']['cli_agy']['models']],
                             ['gemini-4-neon-high', 'claude-opus-5-5-high'])
            self.assertTrue(catalog['adapters']['openai_compat']['installed'])


if __name__ == '__main__':
    unittest.main()
