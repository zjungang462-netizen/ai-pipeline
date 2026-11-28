"""工作台项目管理、审批附注、自优化 HTTP 端点与 agy 补丁探针测试。"""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import subprocess
import sys
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

    def test_managed_project_roundtrip_preserves_api_connections_and_blocks_active_direct_run(self):
        from orchestrator.collaboration import project_manager
        from orchestrator.collaboration.service import Service
        from orchestrator.collaboration.storage import Conflict
        from orchestrator.state import StageLock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / 'work'
            workdir.mkdir()
            service = Service(root / 'wb.sqlite3', [], recover=False)
            self.addCleanup(service._background_sync.close)
            stages = [
                {'name': 'design', 'kind': 'report', 'adapter_type': 'openai_compat',
                 'model': 'model-a', 'base_url': 'https://a.example/v1', 'api_key_env': 'KEY_A'},
                {'name': 'review', 'kind': 'review', 'adapter_type': 'openai_compat',
                 'model': 'model-b', 'base_url': 'http://127.0.0.1:9/v1', 'api_key_env': 'KEY_B',
                 'allow_insecure_http': True, 'feedback_to': 'design'},
            ]
            created = project_manager.register_managed_project(service, {
                'name': 'dual-api', 'workdir': str(workdir), 'stages': stages,
            })
            definition = project_manager.project_stage_definition(service, created['id'])
            self.assertEqual(definition['stages'][0]['base_url'], 'https://a.example/v1')
            self.assertEqual(definition['stages'][1]['base_url'], 'http://127.0.0.1:9/v1')
            self.assertEqual(definition['stages'][1]['api_key_env'], 'KEY_B')
            self.assertTrue(definition['stages'][1]['allow_insecure_http'])

            payload = {
                'name': 'dual-api', 'workdir': str(workdir), 'stages': definition['stages'],
                'api_base_url': definition['api_base_url'], 'api_key_env': definition['api_key_env'],
                'api_allow_insecure_http': definition['api_allow_insecure_http'],
            }
            project_manager.register_managed_project(service, payload)
            cfg = load_config(created['config'])
            self.assertEqual(cfg.adapters['design_agent'].options['base_url'], 'https://a.example/v1')
            self.assertEqual(cfg.adapters['design_agent'].options['api_key_env'], 'KEY_A')
            self.assertEqual(cfg.adapters['review_agent'].options['base_url'], 'http://127.0.0.1:9/v1')
            self.assertEqual(cfg.adapters['review_agent'].options['api_key_env'], 'KEY_B')
            self.assertIs(cfg.adapters['review_agent'].options['allow_insecure_http'], True)

            run_key = service.store.register_run(created['id'], 'direct-cli', created['config'], cfg.state_dir)
            run_dir = cfg.state_dir / 'runs' / 'direct-cli'
            run_dir.mkdir(parents=True)
            lock = StageLock(run_dir / '.run.lock')
            self.assertTrue(lock.acquire())
            try:
                self.assertTrue(project_manager.has_active_project_work(service, created['id']))
                before = Path(created['config']).read_bytes()
                with self.assertRaises(Conflict):
                    project_manager.register_managed_project(service, {
                        **payload, 'stages': [{**stages[0], 'model': 'changed-model'}],
                    })
                self.assertEqual(Path(created['config']).read_bytes(), before)
            finally:
                lock.release()
            self.assertFalse(project_manager.has_active_project_work(service, created['id']))
            self.assertEqual(service.store.run(run_key)['run_id'], 'direct-cli')

    def test_custom_test_command_is_not_silently_rounded_to_preset(self):
        from orchestrator.collaboration import project_manager
        from orchestrator.collaboration.service import Service

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / 'work'
            workdir.mkdir()
            managed = root / 'managed_projects'
            managed.mkdir()
            config = managed / 'custom.toml'
            config.write_text(
                f'state_dir = "{(root / "state").as_posix()}"\n'
                f'task_workdir = "{workdir.as_posix()}"\n'
                '[[stages]]\nname="verify"\nkind="test"\n'
                'test_cmd=["python","-c","print(\'custom\')"]\n',
                encoding='utf-8',
            )
            service = Service(root / 'wb.sqlite3', [config], recover=False)
            self.addCleanup(service._background_sync.close)
            project_id = service.store.projects()[0]['id']
            definition = project_manager.project_stage_definition(service, project_id)
            self.assertEqual(definition['stages'][0]['test_preset'], 'custom')
            before = config.read_bytes()

            with self.assertRaisesRegex(ValueError, '测试预设'):
                project_manager.register_managed_project(service, {
                    'name': 'custom', 'workdir': str(workdir), 'stages': definition['stages'],
                })
            self.assertEqual(config.read_bytes(), before)

    def test_corrupted_managed_config_can_be_repaired_after_active_work_stops(self):
        from orchestrator.collaboration import project_manager
        from orchestrator.collaboration.service import Service
        from orchestrator.collaboration.storage import Conflict
        from orchestrator.state import StageLock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / 'work'
            workdir.mkdir()
            service = Service(root / 'wb.sqlite3', [], recover=False)
            self.addCleanup(service._background_sync.close)
            payload = {'name': 'repair', 'workdir': str(workdir), 'preset': 'review_only'}
            created = project_manager.register_managed_project(service, payload)
            cfg = load_config(created['config'])
            service.store.register_run(created['id'], 'active-run', created['config'], cfg.state_dir)
            run_dir = cfg.state_dir / 'runs' / 'active-run'
            run_dir.mkdir(parents=True)
            lock = StageLock(run_dir / '.run.lock')
            self.assertTrue(lock.acquire())
            config_path = Path(created['config'])
            config_path.write_text('{invalid', encoding='utf-8')
            before = config_path.read_bytes()
            try:
                with self.assertRaises(Conflict):
                    project_manager.register_managed_project(service, payload)
                self.assertEqual(config_path.read_bytes(), before)
            finally:
                lock.release()

            from orchestrator.collaboration import arena_jobs
            job_dir = arena_jobs._root(cfg, created['id']) / ('arena-' + '0' * 32)
            job_dir.mkdir(parents=True)
            state_path = job_dir / 'state.json'
            state_path.write_text('{"status":"running"}', encoding='utf-8')
            worker_lock = StageLock(job_dir / '.worker.lock')
            self.assertTrue(worker_lock.acquire())
            try:
                with self.assertRaises(Conflict):
                    project_manager.register_managed_project(service, payload)
            finally:
                worker_lock.release()
            state_path.write_text('{"status":"done"}', encoding='utf-8')

            project_manager.register_managed_project(service, payload)
            self.assertTrue(load_config(config_path).stages)

    def test_restarted_service_detects_surviving_discussion_process_lock(self):
        from orchestrator.collaboration import project_manager
        from orchestrator.collaboration.service import Service

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / 'work'
            workdir.mkdir()
            state_dir = root / 'state'
            config = root / 'pipeline.toml'
            config.write_text(
                f'state_dir = "{state_dir.as_posix()}"\n'
                f'task_workdir = "{workdir.as_posix()}"\n'
                '[[stages]]\nname="testing"\nkind="test"\n'
                'test_cmd=["python","-c","pass"]\n', encoding='utf-8')
            service = Service(root / 'wb.sqlite3', [config], recover=False)
            self.addCleanup(service._background_sync.close)
            project = service.store.projects()[0]['id']
            run = service.store.register_run(project, 'restart-run', config, state_dir)
            topic = service.store.create_thread(
                run, 'question', ['developer'], 1000, {}, 'restart-topic-001')
            restarted = Service(root / 'wb.sqlite3', [], recover=False)
            self.addCleanup(restarted._background_sync.close)
            lock_path = state_dir / 'discussions' / topic['id'] / '.discussion.lock'
            child_code = (
                'import json, os, sys\n'
                'path = sys.argv[1]\n'
                'os.makedirs(os.path.dirname(path), exist_ok=True)\n'
                'fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)\n'
                'with os.fdopen(fd, "w", encoding="utf-8") as stream:\n'
                '    json.dump({"pid": os.getpid()}, stream)\n'
                'print("ready", flush=True)\n'
                'sys.stdin.read()\n'
            )
            process = subprocess.Popen(
                [sys.executable, '-u', '-c', child_code, str(lock_path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8')
            try:
                self.assertEqual(process.stdout.readline().strip(), 'ready')
                self.assertFalse(restarted.active)
                self.assertFalse(restarted.active_topics)
                self.assertTrue(project_manager.has_active_project_work(restarted, project))
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)
                process.stdin.close()
                process.stdout.close()
                process.stderr.close()
            self.assertFalse(project_manager.has_active_project_work(restarted, project))
            lock_path.write_text('{invalid', encoding='utf-8')
            self.assertFalse(project_manager.has_active_project_work(restarted, project))
            lock_path.unlink()
            self.assertFalse(project_manager.has_active_project_work(restarted, project))

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

# 竞技场只接受代码开发项目；start_job 在测试中被替换，不会启动真实 CLI。
[adapters.backend]
type = "cli_agy"
model = "fixture-model"

[[stages]]
name = "coding"
kind = "code"
adapter = "backend"
prompt = "fixture"
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

        arena_endpoint = f'/api/projects/{seed_id}/arena'
        choices = [{'label': label, 'adapter_type': 'cli_agy'} for label in ('A', 'B')]
        with mock.patch('orchestrator.collaboration.arena_jobs.start_job',
                        return_value={'arena_id': 'arena-test'}) as run:
            status, data, _ = req('POST', arena_endpoint, {
                'task': '修改文件', 'candidates': choices, 'allowed_files': ['中文 文件.py'],
                'test_preset': 'python_unittest',
            }, auth_headers)
            self.assertEqual((status, data['arena_id']), (202, 'arena-test'))
            self.assertEqual(run.call_args.args[4]['allowed_files'], ['中文 文件.py'])
        approval = {'arena_id': 'arena-test', 'label': 'A', 'expected_base_sha': 'a' * 40,
                    'expected_candidate_sha': 'b' * 40, 'expected_evidence_sha256': 'c' * 64}
        with mock.patch('orchestrator.arena.apply_arena_winner', return_value={'applied': True}) as apply:
            status, data, _ = req('POST', arena_endpoint + '/apply', approval, auth_headers)
            self.assertEqual((status, data['applied']), (200, True))
            self.assertEqual(apply.call_args.args[1:], tuple(approval.values()))

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
        # 目录探测会先检查本机是否安装 agy；CI 干净 runner 没装，不能依赖宿主机。
        # 让“已安装”判定也进入 mock，才能稳定走到被 mock 的探测结果。
        with mock.patch('orchestrator.collaboration.model_catalog.resolve_exe',
                        side_effect=lambda name: '/fake/agy' if name == 'agy' else None), \
                mock.patch('orchestrator.collaboration.model_catalog._probe_agy_models', return_value=parsed), \
                mock.patch('orchestrator.collaboration.model_catalog._probe_openai_compat_models', return_value=(True, [{'id': 'deepseek-chat', 'label': 'deepseek-chat'}])):
            status, catalog, _ = req('GET', '/api/models/catalog?refresh=1', headers={'Cookie': cookie})
            self.assertEqual(status, 200)
            self.assertEqual([m['id'] for m in catalog['adapters']['cli_agy']['models']],
                             ['gemini-4-neon-high', 'claude-opus-5-5-high'])
            self.assertTrue(catalog['adapters']['openai_compat']['installed'])

        # 6. 动态多节点配置（自由添加 5 个自定义节点，支持不同工具/模型/审批/打回目标）
        status, dyn, _ = req('POST', '/api/projects/register', {
            'name': 'multi-5',
            'workdir': str(target_repo),
            'stages': [
                {'name': 'lead', 'kind': 'report', 'adapter_type': 'cli_agy', 'model': 'gemini-pro-orchestrator-neon'},
                {'name': 'design', 'kind': 'report', 'adapter_type': 'cli_agy', 'model': 'claude-opus-5-5-high', 'approval': True},
                {'name': 'coding', 'kind': 'code', 'adapter_type': 'cli_codex', 'model': 'gpt-5.4'},
                {'name': 'cross_review', 'kind': 'review', 'adapter_type': 'cli_agy', 'model': 'claude-sonnet-5-5-high', 'feedback_to': 'coding'},
                {'name': 'final_review', 'kind': 'review', 'adapter_type': 'openai_compat', 'model': 'deepseek-chat', 'feedback_to': 'coding'},
            ],
            'proxy_url': 'http://127.0.0.1:7897',
        }, {**auth_headers, 'Idempotency-Key': 'idem-dyn5-12345678'})
        self.assertEqual(status, 201)
        self.assertEqual(len(dyn['models']), 5)
        self.assertEqual([m['name'] for m in dyn['models']],
                         ['lead_agent', 'design_agent', 'coding_agent', 'cross_review_agent', 'final_review_agent'])

        # 读取已登记项目的当前节点定义（供「新建任务 → 增删/编排节点」回填）
        status, stage_def, _ = req('GET', f"/api/projects/{dyn['id']}/stages", headers={'Cookie': cookie})
        self.assertEqual(status, 200)
        self.assertTrue(stage_def['managed'])
        self.assertEqual([s['name'] for s in stage_def['stages']],
                         ['lead', 'design', 'coding', 'cross_review', 'final_review'])
        self.assertEqual(stage_def['stages'][2]['adapter_type'], 'cli_codex')
        # 外部 --config 项目应标记 managed=False，避免同名重复登记
        status, ext_def, _ = req('GET', f'/api/projects/{seed_id}/stages', headers={'Cookie': cookie})
        self.assertEqual(status, 200)
        self.assertFalse(ext_def['managed'])
        # 校验生成的 cli_codex 默认带 isolate_config = true
        self.assertIn('isolate_config = true', Path(dyn['config']).read_text(encoding='utf-8'))

        # 拒绝 bool / None 整数字段（必须返回 400 而非 500 或静默转 1）
        status, _, _ = req('POST', '/api/projects/register', {
            'name': 'bad-bool',
            'workdir': str(target_repo),
            'max_model_calls': True,
        }, {**auth_headers, 'Idempotency-Key': 'idem-bool-12345678'})
        self.assertEqual(status, 400)

        status, _, _ = req('POST', f'/api/projects/{seed_id}/lessons/approve', {
            'proposal': lessons_data['proposal'],
            'max_items': True,
        }, {**auth_headers, 'Idempotency-Key': 'idem-maxb-12345678'})
        self.assertEqual(status, 400)

        # _probe_agy_models 不透传宿主机无关凭据（如 DEEPSEEK_API_KEY）
        from orchestrator.collaboration.model_catalog import _probe_agy_models
        captured_env = {}
        def _fake_run(cmd, **kw):
            captured_env.update(kw.get('env') or {})
            return subprocess.CompletedProcess(cmd, 0, 'gemini-4-neon-high\tGemini 4 Neon\n', '')
        with mock.patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'DUMMY_HOST_ONLY_KEY_9999'}), \
                mock.patch('orchestrator.collaboration.model_catalog.subprocess.run', side_effect=_fake_run):
            models = _probe_agy_models('agy-bin')
            self.assertEqual([m['id'] for m in models], ['gemini-4-neon-high'])
            self.assertNotIn('DEEPSEEK_API_KEY', captured_env)

        # 拒绝重复节点名
        status, _, _ = req('POST', '/api/projects/register', {
            'name': 'dup-stage',
            'workdir': str(target_repo),
            'stages': [
                {'name': 'step1', 'kind': 'report', 'adapter_type': 'cli_agy'},
                {'name': 'step1', 'kind': 'code', 'adapter_type': 'cli_agy'},
            ],
        }, {**auth_headers, 'Idempotency-Key': 'idem-dup-12345678'})
        self.assertEqual(status, 400)

        # 白名单确定性 test 节点（允许 pytest 预设 + feedback_to，拒绝任意原始 test_cmd）
        status, _, _ = req('POST', '/api/projects/register', {
            'name': 'bad-test-cmd',
            'workdir': str(target_repo),
            'stages': [
                {'name': 'coding', 'kind': 'code', 'adapter_type': 'cli_agy'},
                {'name': 't1', 'kind': 'test', 'test_cmd': ['rm', '-rf', '/']},
            ],
        }, {**auth_headers, 'Idempotency-Key': 'idem-bad-tcmd-1234'})
        self.assertEqual(status, 400)

        status, with_test, _ = req('POST', '/api/projects/register', {
            'name': 'with-test-preset',
            'workdir': str(target_repo),
            'stages': [
                {'name': 'coding', 'kind': 'code', 'adapter_type': 'cli_agy', 'model': 'gemini-3.8-flash-high'},
                {'name': 'unit_test', 'kind': 'test', 'test_preset': 'pytest', 'feedback_to': 'coding'},
                {'name': 'review', 'kind': 'review', 'adapter_type': 'openai_compat', 'model': 'deepseek-chat', 'feedback_to': 'coding'},
            ],
        }, {**auth_headers, 'Idempotency-Key': 'idem-tpreset-12345'})
        self.assertEqual(status, 201)
        status, td_def, _ = req('GET', f"/api/projects/{with_test['id']}/stages", headers={'Cookie': cookie})
        self.assertEqual(status, 200)
        self.assertEqual(td_def['stages'][1]['kind'], 'test')
        self.assertEqual(td_def['stages'][1]['test_preset'], 'pytest')
        self.assertEqual(td_def['stages'][1]['feedback_to'], 'coding')

        # 严格校验布尔参数，拒绝字符串 "false" 被误判为 True
        status, _, _ = req(
            'POST', f"/api/projects/{with_test['id']}/remove",
            {'delete_config_file': 'false'},
            {**auth_headers, 'Idempotency-Key': 'idem-rm-badb-12345'},
        )
        self.assertEqual(status, 400)

        # 安全移除项目登记（删除托管 toml，绝不碰用户工作目录）
        managed_toml = Path(with_test['config'])
        self.assertTrue(managed_toml.is_file())
        status, removed, _ = req(
            'POST', f"/api/projects/{with_test['id']}/remove",
            {'delete_config_file': True},
            {**auth_headers, 'Idempotency-Key': 'idem-rm-proj-12345'},
        )
        self.assertEqual(status, 200)
        self.assertTrue(removed['removed'])
        self.assertTrue(removed['config_deleted'])
        self.assertFalse(managed_toml.exists())
        self.assertTrue(target_repo.is_dir())

        # 任务交付报告导出 (.md) + 无损归档/隐藏与已归档筛选
        _write_json(
            seed_state / 'runs' / 'r1' / 'state.json',
            {'run_id': 'r1', 'task': '修复导出报告校验', 'stages': {'coding': {'status': 'done', 'current_attempt': 1}}, 'events': []},
        )
        run_key = server.application.service.store.register_run(seed_id, 'r1', seed_cfg, seed_state)
        status, exp, _ = req('GET', f'/api/runs/{run_key}/export-report', headers={'Cookie': cookie})
        self.assertEqual(status, 200)
        self.assertEqual(exp['filename'], 'r1-report.md')
        self.assertIn('# 任务交付报告 · r1', exp['markdown'])
        self.assertIn('阶段执行汇总', exp['markdown'])

        status, _, _ = req(
            'POST', f'/api/runs/{run_key}/archive', {'archived': 'false'},
            {**auth_headers, 'Idempotency-Key': 'idem-arch-badb-1234'},
        )
        self.assertEqual(status, 400)

        status, arch, _ = req(
            'POST', f'/api/runs/{run_key}/archive', {'archived': True},
            {**auth_headers, 'Idempotency-Key': 'idem-arch-run-1234'},
        )
        self.assertEqual(status, 200)
        self.assertTrue(arch['archived'])
        # 默认列表隐藏已归档，status=archived 可查回，且磁盘 metrics/state 完整保留
        status, active_list, _ = req('GET', '/api/runs?status=all', headers={'Cookie': cookie})
        self.assertEqual(status, 200)
        self.assertNotIn(run_key, [r['id'] for r in active_list['runs']])
        status, archived_list, _ = req('GET', '/api/runs?status=archived', headers={'Cookie': cookie})
        self.assertEqual(status, 200)
        self.assertIn(run_key, [r['id'] for r in archived_list['runs']])
        self.assertTrue((seed_state / 'runs' / 'r1' / 'metrics.json').is_file())
        status, unarch, _ = req(
            'POST', f'/api/runs/{run_key}/archive', {'archived': False},
            {**auth_headers, 'Idempotency-Key': 'idem-unarch-123456'},
        )
        self.assertEqual(status, 200)
        self.assertFalse(unarch['archived'])

        # 7. 项目登记安全校验：拒绝 CLI flag 注入模型名、拒绝非 bool 开关、失败不留孤儿 state 目录
        managed_root = (root / 'wb.sqlite3').resolve().parent / 'managed_projects'
        status, _, _ = req('POST', '/api/projects/register', {
            'name': 'bad-model-flag',
            'workdir': str(target_repo),
            'stages': [{'name': 's1', 'kind': 'code', 'adapter_type': 'cli_agy', 'model': '--danger'}],
        }, {**auth_headers, 'Idempotency-Key': 'idem-badm-12345678'})
        self.assertEqual(status, 400)
        self.assertFalse((managed_root / 'state-bad-model-flag').exists())

        status, _, _ = req('POST', '/api/projects/register', {
            'name': 'bad-probe-bool',
            'workdir': str(target_repo),
            'preset': 'review_only',
            'allow_patch_probe': 'false',
        }, {**auth_headers, 'Idempotency-Key': 'idem-badp-12345678'})
        self.assertEqual(status, 400)
        self.assertFalse((managed_root / 'state-bad-probe-bool').exists())

        # preset 分支选择 cli_codex 也应写入 isolate_config = true
        status, preset_codex, _ = req('POST', '/api/projects/register', {
            'name': 'preset-codex',
            'workdir': str(target_repo),
            'preset': 'standard_dev',
            'stage_adapters': {'coding': {'type': 'cli_codex', 'model': 'gpt-5.4'}},
        }, {**auth_headers, 'Idempotency-Key': 'idem-pcod-12345678'})
        self.assertEqual(status, 201)
        self.assertIn('isolate_config = true', Path(preset_codex['config']).read_text(encoding='utf-8'))

        # 多余路径段必须 404，不误匹配 /api/runs/<key>/snapshot
        status, _, _ = req('GET', f'/api/runs/{run_key}/snapshot/extra', headers={'Cookie': cookie})
        self.assertEqual(status, 404)

    def test_export_report_includes_verified_cumulative_code_diff_and_dynamic_fence(self):
        from orchestrator.adapters.factory import build_all
        from orchestrator.collaboration.insights import export_run_report
        from orchestrator.collaboration.service import Service

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        ws = root / 'ws'
        ws.mkdir()
        (ws / 'doc.md').write_text('old\n', encoding='utf-8')
        cfg_path = root / 'code_project.toml'
        cfg_path.write_text(
            f'task_workdir = "{ws.as_posix()}"\n'
            f'state_dir = "{(root / "state").as_posix()}"\n'
            '[run_budget]\nmax_tokens = 20000\nmax_duration_s = 120\n'
            '[adapters.fake]\ntype = "fake"\n'
            '[[stages]]\nname = "coding"\nkind = "code"\nadapter = "fake"\n'
            'prompt = "这是一个很长的内联提示词：' + ('甲' * 120) + '"\n',
            encoding='utf-8',
        )
        cfg = load_config(cfg_path)
        eng = Engine(cfg, build_all(cfg.adapters, {}), 'run-diff-exp', '测试导出代码差异 ````fence')
        eng.run()

        svc = Service(root / 'wb.sqlite3', [cfg_path], recover=False)
        pid = svc.store.projects()[0]['id']
        run_key = svc.store.register_run(pid, 'run-diff-exp', cfg_path, root / 'state')
        report = export_run_report(svc, run_key)
        self.assertIn('## 累计代码差异（基线 → `coding`）', report['markdown'])
        self.assertIn('coding_output.py', report['markdown'])

        # 同时验证长内联提示词项目可安全 derive 派生后续工作区（不会因 Path.is_file 抛出 OSError）
        from orchestrator.collaboration.config_copy import derive
        derived_cfg = derive(svc.store.run(run_key), 'derived-long-prompt')
        self.assertTrue(derived_cfg.is_file())


if __name__ == '__main__':
    unittest.main()
