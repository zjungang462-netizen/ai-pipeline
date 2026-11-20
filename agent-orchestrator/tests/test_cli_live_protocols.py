"""真实 CLI 协议抓包回归；离线重放，不发送模型请求。"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.adapters.cli_agy import AgyAdapter
from orchestrator.adapters.cli_codex import CodexAdapter, parse_codex_jsonl
from orchestrator.adapters.cli_base import run_cli_process
from orchestrator.adapters.factory import build_adapter
from orchestrator.config import AdapterConfig, parse_config
from orchestrator.contracts import RunContext
from orchestrator.engine import Engine
from orchestrator.safety import build_subprocess_env

FIXTURES = Path(__file__).parent / 'fixtures'


class RealCLIProtocols(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def ctx(self, kind='report', stage='design'):
        return RunContext('task', 'run', stage, self.root, self.root / 'run', kind=kind,
                          attempt_dir=self.root / 'attempts' / stage, timeout_s=12)

    def parse(self, adapter, raw, version, code=0):
        adapter._version = version
        return adapter._parse(subprocess.CompletedProcess([], code, raw, ''), self.root, self.ctx())

    def test_real_codex_final_text_and_usage(self):
        raw = (FIXTURES / 'codex_0.169_real.jsonl').read_text(encoding='utf8')
        result = self.parse(CodexAdapter('codex'), raw, 'codex-cli 0.169.0')
        self.assertTrue(result.ok)
        self.assertEqual(result.text, 'CODEX_CLI_OK')
        self.assertEqual(result.usage['tokens'], 30107)

    def test_codex_uses_final_agent_message_and_does_not_double_count_cache(self):
        raw = '\n'.join(json.dumps(e) for e in [
            {'type':'item.completed', 'item':{'type':'agent_message','phase':'commentary','text':'准备处理'}},
            {'type':'item.completed', 'item':{'type':'command_execution','text':'不应成为报告'}},
            {'type':'item.completed', 'item':{'type':'agent_message','text':'最终报告'}},
            {'type':'turn.completed','usage':{'input_tokens':100,'output_tokens':20,'cached_input_tokens':70}}])
        result = self.parse(CodexAdapter('codex'), raw, 'codex-cli 0.169.0')
        self.assertEqual(result.text, '最终报告')
        self.assertEqual(result.usage['tokens'], 120)

    def test_codex_partial_or_failed_turn_cannot_pass(self):
        for raw in ('{"type":"item.started","item":{"type":"agent_message","text":"partial"}}',
                    '{"type":"item.completed","item":{"type":"agent_message","text":"partial"}}',
                    '{"type":"turn.failed","error":{"message":"failed"}}'):
            with self.subTest(raw=raw):
                self.assertFalse(self.parse(CodexAdapter('codex'), raw, 'codex-cli 0.169.0').ok)

    def test_codex_unknown_future_version_is_rejected(self):
        raw = (FIXTURES / 'codex_0.169_real.jsonl').read_text(encoding='utf8')
        self.assertTrue(self.parse(CodexAdapter('codex'), raw, 'codex-cli 9.999.0').parse_unknown)

    def test_codex_bare_json_values_do_not_crash(self):
        self.assertFalse(parse_codex_jsonl('[]\nnull\n7\n')[1])

    def test_real_agy_result_and_usage_are_counted_once(self):
        raw = (FIXTURES / 'agy_1.5.9_real.jsonl').read_text(encoding='utf8')
        result = self.parse(AgyAdapter('agy'), raw, '1.5.9')
        self.assertTrue(result.ok)
        self.assertEqual(result.text.strip(), 'AGY_CLI_OK')
        self.assertEqual(result.usage['tokens'], 12898)
        self.assertEqual(result.usage['model'], 'gemini-3.8-flash-high')

    def test_agy_partial_failed_or_ambiguous_results_are_rejected(self):
        values = [
            {'event':'step_update','step_update':{'text_delta':'not final'}},
            {'event':'result','result':{'status':'FAILED','response':'not pass'}},
            {'event':'result','result':[]},
            {'event':'result','result':{'status':'SUCCESS','response':['bad shape']}}]
        for value in values:
            with self.subTest(value=value):
                self.assertFalse(self.parse(AgyAdapter('agy'), json.dumps(value), '1.5.9').ok)
        raw = json.dumps({'event':'result','result':{'status':'SUCCESS','response':'one'}})
        self.assertFalse(self.parse(AgyAdapter('agy'), raw + '\n' + raw, '1.5.9').ok)

    def test_agy_unknown_numeric_version_is_rejected(self):
        raw = (FIXTURES / 'agy_1.5.9_real.jsonl').read_text(encoding='utf8')
        self.assertTrue(self.parse(AgyAdapter('agy'), raw, '1.5.10').parse_unknown)

    def test_agy_missing_usage_stays_unknown(self):
        raw = json.dumps({'event':'result','result':{'status':'SUCCESS','response':'body'}})
        result = self.parse(AgyAdapter('agy'), raw, '1.5.9')
        self.assertTrue(result.ok)
        self.assertTrue(result.usage['usage_unknown'])

    def test_agy_cached_reads_are_included_in_conservative_budget(self):
        raw = json.dumps({'event':'result','result':{'status':'SUCCESS','response':'body',
            'usage':{'input_tokens':27370,'output_tokens':2377,'total_tokens':29747,'cache_read_tokens':75973}}})
        result = self.parse(AgyAdapter('agy'),raw,'1.5.9')
        self.assertEqual(result.usage['cli_reported_tokens'],29747)
        self.assertEqual(result.usage['tokens'],105720)

    def test_cache_read_budget_overrun_stops_downstream(self):
        raw = json.dumps({'event':'result','result':{'status':'SUCCESS','response':'body',
            'usage':{'total_tokens':29747,'cache_read_tokens':75973}}})
        result = self.parse(AgyAdapter('agy'),raw,'1.5.9')
        report = self.root / 'report.md'
        report.write_text('body',encoding='utf8')
        result.report_files = [report]
        class Replay(AgyAdapter):
            calls = 0
            def run(inner,prompt,workdir,ctx):
                inner.calls += 1
                return result
        cfg = parse_config({'task_workdir':'ws','state_dir':'.state','run_budget':{'max_tokens':100000},'stages':[
            {'name':'one','kind':'report','adapter':'agy','prompt':'p'},
            {'name':'two','kind':'report','adapter':'agy','prompt':'p'}],
            'adapters':{'agy':{'type':'cli_agy'}}},base_dir=self.root)
        replay = Replay('agy')
        engine = Engine(cfg,{'agy':replay},'r','task')
        statuses = engine.run()
        self.assertEqual(statuses['one'],'failed')
        self.assertEqual(replay.calls,1)
        self.assertGreater(engine.state.usage_total['tokens'],100000)

    def test_agy_prompt_uses_stdin_and_does_not_bypass_permissions(self):
        adapter = AgyAdapter('agy', model='model', proxy_url='http://127.0.0.1:7897')
        adapter._resolved, adapter._version = 'agy', '1.5.9'
        raw = (FIXTURES / 'agy_1.5.9_real.jsonl').read_text(encoding='utf8')
        with mock.patch('orchestrator.adapters.cli_base.run_cli_process', return_value=subprocess.CompletedProcess([],0,raw,'')) as execute:
            adapter.run('SECRET_PROMPT', self.root, self.ctx('code','coding'))
        args = execute.call_args.args[0]
        self.assertNotIn('SECRET_PROMPT', args)
        self.assertIn('--print=', args)
        self.assertIn('accept-edits', args)
        self.assertNotIn('--dangerously-skip-permissions', args)
        self.assertEqual(json.loads(execute.call_args.kwargs['input'])['message']['content'], 'SECRET_PROMPT')
        self.assertEqual(execute.call_args.kwargs['env']['HTTPS_PROXY'], 'http://127.0.0.1:7897')

    def test_proxy_credentials_cannot_be_written_in_config(self):
        with self.assertRaises(ValueError):
            AgyAdapter('agy', proxy_url='http://user:secret@127.0.0.1:7897')

    def test_codex_reports_are_readonly_and_attempt_scoped(self):
        adapter = CodexAdapter('codex')
        adapter._resolved, adapter._version = 'codex', 'codex-cli 0.169.0'
        raw = (FIXTURES / 'codex_0.169_real.jsonl').read_text(encoding='utf8')
        with mock.patch('orchestrator.adapters.cli_base.run_cli_process', return_value=subprocess.CompletedProcess([],0,raw,'')) as execute:
            result = adapter.run('PRIVATE_PROMPT', self.root, self.ctx())
        args = execute.call_args.args[0]
        self.assertIn('read-only', args)
        self.assertIn('--ephemeral', args)
        self.assertEqual(args[-1], '-')
        self.assertNotIn('PRIVATE_PROMPT', args)
        self.assertEqual(result.report_files[0].parent, self.ctx().attempt_dir)

    def test_isolated_codex_profile_keeps_secrets_off_argv_and_global_config(self):
        profile = self.root / 'profile'
        profile.mkdir()
        path = profile / 'config.toml'
        path.write_text('model="chosen-model"\nmodel_provider="custom"\n[model_providers.custom]\n'
                        'name="local"\nbase_url="https://example.invalid"\nwire_api="responses"\n'
                        'experimental_bearer_token="sk-private-test-value"\n[mcp_servers.desktop]\ntype="unsupported"\n', encoding='utf8')
        before = path.read_bytes()
        adapter = CodexAdapter('codex', isolate_config=True)
        adapter._resolved, adapter._version = 'codex', 'codex-cli 0.169.0'
        raw = (FIXTURES / 'codex_0.169_real.jsonl').read_text(encoding='utf8')
        with mock.patch.dict(os.environ, {'CODEX_HOME':str(profile)}), mock.patch(
                'orchestrator.adapters.cli_base.run_cli_process', return_value=subprocess.CompletedProcess([],0,raw,'')) as execute:
            fingerprint = adapter.input_fingerprint()
            adapter.run('prompt', self.root, self.ctx())
        args = execute.call_args.args[0]
        self.assertIn('--ignore-user-config', args)
        self.assertNotIn('sk-private-test-value', json.dumps(args))
        self.assertNotIn('sk-private-test-value', json.dumps(fingerprint))
        self.assertEqual(execute.call_args.kwargs['env']['AI_PIPELINE_CODEX_KEY'], 'sk-private-test-value')
        self.assertEqual(path.read_bytes(), before)

    def test_codex_role_models_and_registry_options(self):
        adapter = build_adapter(AdapterConfig('codex','cli_codex', {'model':'chosen','isolate_config':True}))
        self.assertEqual(adapter.model, 'chosen')
        self.assertTrue(adapter.isolate_config)
        agy = build_adapter(AdapterConfig('agy','cli_agy', {'model':'gemini','proxy_url':'http://127.0.0.1:7897'}))
        self.assertEqual(agy.model, 'gemini')

    def test_runtime_cli_profile_is_part_of_stage_fingerprint(self):
        cfg = parse_config({'task_workdir':'ws','state_dir':'.state','stages':[
            {'name':'design','kind':'report','adapter':'codex','prompt':'p'}],
            'adapters':{'codex':{'type':'cli_codex'}}},base_dir=self.root)
        adapter = CodexAdapter('codex', model='one')
        engine = Engine(cfg, {'codex':adapter}, 'r','task')
        old = engine._inputs_hash(cfg.stages[0],0)
        adapter.model = 'two'
        self.assertNotEqual(engine._inputs_hash(cfg.stages[0],0),old)

    def test_cli_timeout_stops_descendants(self):
        flag = self.root / 'late-write'
        child = f"import time; from pathlib import Path; time.sleep(60); Path({str(flag)!r}).write_text('leak')"
        parent = f"import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c',{child!r}]); print('CHILD_PID='+str(p.pid),flush=True); time.sleep(90)"
        with self.assertRaises(subprocess.TimeoutExpired) as error:
            run_cli_process([sys.executable,'-u','-c',parent],cwd=str(self.root),env=build_subprocess_env(),input='',timeout=10)
        self.assertIn('CHILD_PID=',error.exception.stdout)
        from orchestrator.state import _pid_alive
        child_pid = int(error.exception.stdout.split('CHILD_PID=')[1].splitlines()[0])
        if sys.platform == 'linux' and _pid_alive(child_pid):
            # SIGKILL 已生效的孤儿可能仍由 PID 1 等待回收，不能再执行用户代码。
            try:
                status = Path(f'/proc/{child_pid}/stat').read_text(encoding='utf8')
            except FileNotFoundError:
                self.assertFalse(_pid_alive(child_pid))
            else:
                self.assertIn(status.rsplit(')', 1)[1].split()[0], ('Z', 'X'))
        else:
            self.assertFalse(_pid_alive(child_pid))
        self.assertFalse(flag.exists())


if __name__ == '__main__':
    unittest.main()
