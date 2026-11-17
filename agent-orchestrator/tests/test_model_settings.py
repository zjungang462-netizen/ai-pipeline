"""控制台模型/思考选项和显式无限token，不修改来源配置或放开文件权限。"""
import io
import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.config import ConfigError,load_config,parse_config
from orchestrator.engine import Engine
from orchestrator.contracts import Result,RunContext
from orchestrator.adapters.openai_compat import OpenAICompatAdapter
from orchestrator.adapters.factory import build_all
from orchestrator.collaboration.model_settings import config_copy,prepare_config,validate_options
from orchestrator.collaboration.service import Service
from orchestrator.collaboration.storage import Conflict


class ModelSettings(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.path=self.root/'c.toml'
        (self.root/'prompt.md').write_text('read only review',encoding='utf8')
        self.path.write_text('''task_workdir="workspace"
state_dir="state"
[run_budget]
max_tokens=1000
max_duration_s=120
[adapters.api]
type="openai_compat"
base_url="https://example.invalid/v1"
model="existing-model"
api_key_env="MODEL_TEST_KEY"
max_tokens=100
[[stages]]
name="review"
kind="report"
adapter="api"
prompt="prompt.md"
max_tokens=500
''',encoding='utf8')

    def test_copy_removes_all_local_token_caps_preserves_original_and_prompt_base(self):
        cfg=load_config(self.path);before=self.path.read_bytes()
        options={'models':{'api':{'model':'account-best-model','thinking':'enabled','reasoning_effort':'high'}},'unlimited_tokens':True}
        copied=config_copy(cfg,options,'run-1')
        self.assertEqual(self.path.read_bytes(),before)
        self.assertTrue(copied.run_budget.unlimited_tokens)
        self.assertIsNone(copied.run_budget.max_tokens)
        self.assertIsNone(copied.stages[0].max_tokens)
        self.assertNotIn('max_tokens',copied.adapters['api'].options)
        self.assertEqual(copied.adapters['api'].options['model'],'account-best-model')
        self.assertEqual(copied.task_workdir,cfg.task_workdir)
        self.assertEqual(Path(copied.stages[0].prompt),self.root/'prompt.md')
        self.assertEqual(config_copy(cfg,options,'run-1').source_hash,copied.source_hash)

    def test_options_reject_commands_credentials_and_unknown_tools(self):
        cfg=load_config(self.path)
        for opts in ({'exe':'other'},{'unlimited_tokens':'yes'},
                     {'models':{'api':{'base_url':'https://other.invalid'}}},
                     {'models':{'api':{'model':'model\ncommand'}}},
                     {'models':{'missing':{'model':'x'}}}):
            with self.subTest(opts=opts),self.assertRaises(ValueError):validate_options(cfg,opts)

    def test_unlimited_autonomy_does_not_send_output_cap_but_keeps_wall_budget(self):
        cfg=load_config(self.path)
        raw=tomllib.loads(prepare_config(cfg,{'unlimited_tokens':True}))
        raw['autonomy']={'enabled':True}
        config=parse_config(raw,base_dir=self.root)
        adapters=build_all(config.adapters,{'api':'report'})
        engine=Engine(config,adapters,'test','task')
        self.assertEqual(engine._call_limits(config.stages[0],adapters['api'],'x'*50000),(None,0))
        self.assertEqual(config.run_budget.max_duration_s,120)
        raw['run_budget'].pop('max_duration_s')
        with self.assertRaises(ConfigError):parse_config(raw,base_dir=self.root)

    def test_unlimited_requires_explicit_boolean_and_no_conflicting_caps(self):
        raw=tomllib.loads(self.path.read_text(encoding='utf8'))
        raw['run_budget']['unlimited_tokens']=True
        with self.assertRaises(ConfigError):parse_config(raw,base_dir=self.root)
        raw['run_budget']['unlimited_tokens']='true'
        with self.assertRaises(ConfigError):parse_config(raw,base_dir=self.root)

    def test_api_payload_preserves_selected_reasoning_and_omits_local_output_cap(self):
        adapter=OpenAICompatAdapter('api','https://example.invalid/v1','chosen','MODEL_TEST_KEY',
                                    kind='report',reasoning_effort='high',thinking='enabled')
        class Response(io.BytesIO):
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        seen=[]
        def open_url(request,**kwargs):
            seen.append(json.loads(request.data));return Response(json.dumps({'choices':[{'message':{'content':'report'}}], 'usage':{'total_tokens':15}}).encode())
        ctx=RunContext(task='test',run_id='run',stage='report',workdir=self.root,runs_dir=self.root,kind='report')
        with mock.patch.dict('os.environ',{'MODEL_TEST_KEY':'test-value'}),mock.patch('urllib.request.urlopen',side_effect=open_url):
            result=adapter.run('task',self.root,ctx)
        self.assertTrue(result.ok)
        self.assertEqual(seen[0]['reasoning_effort'],'high')
        self.assertEqual(seen[0]['thinking'],{'type':'enabled'})
        self.assertNotIn('temperature',seen[0]);self.assertNotIn('max_tokens',seen[0])
        def without_usage(request,**kwargs):
            return Response(json.dumps({'choices':[{'message':{'content':'report'}}]}).encode())
        with mock.patch.dict('os.environ',{'MODEL_TEST_KEY':'test-value'}),mock.patch('urllib.request.urlopen',side_effect=without_usage):
            unknown=adapter.run('task',self.root,ctx)
        self.assertTrue(unknown.usage['usage_unknown'],'不限token也不能把服务端缺失计量视作免费')

    def test_run_creation_binds_override_hash_and_idempotency(self):
        service=Service(self.root/'db.sqlite3',[self.path]);self.addCleanup(service._background_sync.close)
        project=service.list_projects()[0]
        options={'models':{'api':{'model':'strong-model','reasoning_effort':'high','review_json_mode':True}},'unlimited_tokens':True}
        with mock.patch.object(service,'launch'):
            run=service.create_run(project['id'],'task','run-override-001',expected_config_hash=project['config_hash'],execution_options=options)
            self.assertTrue(run['budget']['unlimited_tokens'])
            self.assertEqual(run['models'][0]['model'],'strong-model')
            self.assertTrue(run['models'][0]['review_json_mode'])
            self.assertEqual(run['config_integrity'],'valid')
            again=service.create_run(project['id'],'task','run-override-001',expected_config_hash=project['config_hash'],execution_options=options)
            self.assertEqual(again['id'],run['id'])
            with self.assertRaises(Conflict):
                service.create_run(project['id'],'task','run-override-001',expected_config_hash=project['config_hash'],execution_options={'unlimited_tokens':True})
        self.assertEqual(load_config(self.path).adapters['api'].options['model'],'existing-model')

    def test_derived_run_accepts_untampered_bound_override_but_not_changed_source(self):
        from orchestrator import artifacts
        from orchestrator.state import atomic_write_json
        service=Service(self.root/'derive.sqlite3',[self.path]);self.addCleanup(service._background_sync.close)
        project=service.list_projects()[0]
        options={'models':{'api':{'model':'strong-model'}},'unlimited_tokens':True}
        with mock.patch.object(service,'launch'):
            parent=service.create_run(project['id'],'parent','derive-parent-001',expected_config_hash=project['config_hash'],execution_options=options)
            workspace=load_config(self.path).task_workdir
            artifacts.ensure_repo(workspace)
            (workspace/'code.py').write_text('VALUE=1\n',encoding='utf8')
            commit=artifacts.commit_files(workspace,['code.py'],'fixed parent code')
            record=service.store.run(parent['id']);directory=Path(record['state_dir'])/'runs'/record['run_id']
            job=json.loads((directory/'job.json').read_bytes());job['status']='done';atomic_write_json(directory/'job.json',job)
            atomic_write_json(directory/'state.json',{'stages':{'review':{'status':'done','commit_sha':commit}},'events':[],'usage_total':{'tokens':0,'duration_s':0}})
            child=service.create_run(project['id'],'child','derive-child-001',parent=parent['id'],expected_config_hash=project['config_hash'],execution_options=options)
            self.assertNotEqual(child['execution_scope']['workdir'],str(workspace))
            self.assertEqual(child['models'][0]['model'],'strong-model')
            self.path.write_text(self.path.read_text(encoding='utf8')+'\n# changed\n',encoding='utf8')
            changed_hash=load_config(self.path).source_hash
            with self.assertRaises(Conflict):
                service.create_run(project['id'],'changed','derive-child-002',parent=parent['id'],expected_config_hash=changed_hash,execution_options=options)

    def test_json_mode_only_changes_review_wire_format_and_retains_strict_parser(self):
        adapter=OpenAICompatAdapter('api','https://example.invalid/v1','chosen','MODEL_TEST_KEY',
                                    reasoning_effort='high',thinking='enabled',review_json_mode=True)
        class Response(io.BytesIO):
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        requests=[]
        def send(request,**kwargs):
            requests.append(json.loads(request.data))
            return Response(json.dumps({'choices':[{'message':{'content':'{"verdict":"pass","issues":[]}'}}],
                                        'usage':{'total_tokens':10}}).encode())
        with mock.patch.dict('os.environ',{'MODEL_TEST_KEY':'test-value'}),mock.patch('urllib.request.urlopen',side_effect=send):
            for kind in ('review','report'):
                ctx=RunContext(task='test',run_id='run',stage=kind,workdir=self.root,runs_dir=self.root,kind=kind)
                result=adapter.run('Return JSON verdict',self.root,ctx)
                self.assertTrue(result.ok)
                if kind=='review':self.assertIsNotNone(result.verdict_file)
        self.assertEqual(requests[0]['response_format'],{'type':'json_object'})
        self.assertNotIn('response_format',requests[1])
        self.assertIsNone(adapter._write_verdict_file('{"verdict":"pass","issues":[]}\nextra prose',self.root))
        with self.assertRaises(ValueError):
            validate_options(load_config(self.path),{'models':{'api':{'review_json_mode':'true'}}})
