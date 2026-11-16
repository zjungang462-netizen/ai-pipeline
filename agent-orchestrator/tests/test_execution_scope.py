"""配置能力必须在创建前可见，固定测试通过不等同业务开发交付。"""
import hashlib
import http.client
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from orchestrator.collaboration.execution_scope import execution_scope
from orchestrator.config import parse_config
from orchestrator.dashboard.server import create_server
from orchestrator.state import atomic_write_json


class ExecutionScope(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)

    def start(self,configs):
        server=create_server(self.root/'dashboard.sqlite3',list(configs),0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def close():
            server.shutdown();server.server_close();thread.join(5)
        self.addCleanup(close)
        return server

    def http(self,server,method,path,body=None,cookie=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
        try:
            value=json.dumps(body).encode('utf8') if body is not None else None
            request_headers=dict(headers or {})
            if cookie:request_headers['Cookie']=cookie
            if value is not None:
                request_headers.update({'Content-Type':'application/json','Origin':server.application.origin,
                                        'Content-Length':str(len(value))})
            connection.request(method,path,value,request_headers)
            response=connection.getresponse();data=response.read().decode('utf8')
            return response.status,json.loads(data),{k.lower():v for k,v in response.getheaders()}
        finally:
            connection.close()

    def login(self,server):
        status,data,headers=self.http(server,'POST','/api/session',{'token':server.application.bootstrap})
        self.assertEqual(status,200,data)
        return headers['set-cookie'].split(';')[0]

    def post(self,server,path,body,cookie,idem):
        return self.http(server,'POST',path,body,cookie=cookie,
                         headers={'X-Workbench-CSRF':server.application.csrf,'Idempotency-Key':idem})

    def config(self,kind='test',adapter='fake'):
        stage={'name':'coding' if kind=='code' else 'verify','kind':kind,'adapter':'backend','prompt':'fixture'}
        if kind=='test':stage['test_cmd']=['python','-c','assert 2+2==4']
        return parse_config({'task_workdir':'fixed-workspace','state_dir':'state','stages':[stage],
                            'adapters':{'backend':{'type':adapter,'api_key':'private-fixture-never-expose'}}},base_dir=self.root)

    def test_fixed_tests_do_not_claim_development_or_model_execution(self):
        scope=execution_scope(self.config())
        self.assertEqual(scope['mode'],'fixed_tests')
        self.assertFalse(scope['uses_model']);self.assertFalse(scope['has_development_stage'])
        self.assertFalse(scope['task_text_changes_execution_scope'])
        self.assertEqual(Path(scope['workdir']),self.root/'fixed-workspace')
        self.assertIn('不执行开发阶段',scope['summary'])

    def test_real_developer_and_report_only_have_distinct_capabilities(self):
        code=execution_scope(self.config('code','cli_agy'))
        self.assertEqual(code['mode'],'development');self.assertTrue(code['uses_model']);self.assertTrue(code['has_development_stage'])
        report=execution_scope(self.config('report','cli_codex'))
        self.assertEqual(report['mode'],'analysis');self.assertFalse(report['has_development_stage'])

    def test_fake_executor_and_credentials_are_not_presented_as_real_delivery(self):
        cfg=self.config('code');scope=execution_scope(cfg)
        self.assertEqual(scope['mode'],'simulation');self.assertFalse(scope['uses_model'])
        self.assertNotIn('private-fixture-never-expose',json.dumps(scope))
        self.assertEqual(scope['stages'][0]['adapter_type'],'fake')

    def test_projects_and_old_run_snapshot_expose_scope_without_changing_run(self):
        cfgfile=self.root/'controls.toml'
        cfgfile.write_text('task_workdir="control-workspace"\nstate_dir="state"\n[run_budget]\nmax_tokens=1000\nmax_duration_s=60\n[[stages]]\nname="testing"\nkind="test"\ntest_cmd=["python","-c","assert 2+2==4"]\n',encoding='utf8')
        run=self.root/'state/runs/original';run.mkdir(parents=True)
        state={'task':'为D:/business-app开发新功能','stages':{'testing':{'status':'done'}},'events':[],'usage_total':{'tokens':0}}
        atomic_write_json(run/'state.json',state)
        atomic_write_json(run/'job.json',{'status':'done','config_hash':hashlib.sha256(cfgfile.read_bytes()).hexdigest()})
        before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in run.iterdir()}
        server=create_server(self.root/'store.sqlite3',[cfgfile],0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def close():
            server.shutdown();server.server_close();thread.join(5)
        self.addCleanup(close)
        c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
        self.addCleanup(c.close)
        c.request('POST','/api/session',json.dumps({'token':server.application.bootstrap}),
                  {'Content-Type':'application/json','Origin':server.application.origin})
        response=c.getresponse();response.read();self.assertEqual(response.status,200)
        cookie=response.getheader('Set-Cookie').split(';')[0]
        service=server.application.service
        with mock.patch.object(service,'launch',side_effect=AssertionError('读取范围不能启动执行器')):
            c.request('GET','/api/projects',headers={'Cookie':cookie})
            response=c.getresponse();data=json.loads(response.read());self.assertEqual(response.status,200)
            project=data['projects'][0]
            self.assertEqual(project['execution_scope']['mode'],'fixed_tests')
            key=project['id']+':original'
            c.request('GET','/api/runs/'+quote(key,safe='')+'/snapshot',headers={'Cookie':cookie})
            response=c.getresponse();snapshot=json.loads(response.read());self.assertEqual(response.status,200)
            self.assertEqual(snapshot['lifecycle'],'done')
            self.assertEqual(Path(snapshot['execution_scope']['workdir']),self.root/'control-workspace')
            self.assertTrue(snapshot['execution_scope']['matches_run_config'])
            cfgfile.write_text(cfgfile.read_text(encoding='utf8').replace('max_tokens=1000','max_tokens=2000'),encoding='utf8')
            self.assertFalse(service.snapshot(key)['execution_scope']['matches_run_config'])
        self.assertEqual(before,{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in run.iterdir()})

    def test_legacy_state_without_config_binding_does_not_verify_current_scope(self):
        from orchestrator.collaboration.service import Service
        cfgfile=self.root/'legacy.toml'
        cfgfile.write_text('task_workdir="ws"\nstate_dir="state"\n[[stages]]\nname="test"\nkind="test"\ntest_cmd=["python","-c","print(1)"]\n',encoding='utf8')
        atomic_write_json(self.root/'state/runs/old/state.json',{'task':'legacy','stages':{'test':{'status':'done'}},'events':[]})
        service=Service(self.root/'legacy.sqlite3',[cfgfile]);self.addCleanup(service.closed.set)
        key=service.store.projects()[0]['id']+':old'
        snapshot=service.snapshot(key)
        self.assertEqual(snapshot['lifecycle'],'done')
        self.assertFalse(snapshot['execution_scope']['matches_run_config'])

    def test_projects_stay_available_when_one_config_is_missing(self):
        first=self.root/'first.toml';second=self.root/'second.toml'
        first.write_text('task_workdir="ws-a"\nstate_dir="state-a"\n[[stages]]\nname="verify"\nkind="test"\n'
                         'test_cmd=["python","-c","print(1)"]\n',encoding='utf8')
        second.write_text('task_workdir="ws-b"\nstate_dir="state-b"\n[[stages]]\nname="verify"\nkind="test"\n'
                          'test_cmd=["python","-c","print(2)"]\n',encoding='utf8')
        expected=hashlib.sha256(first.read_bytes()).hexdigest()
        server=self.start([first,second]);cookie=self.login(server)
        second.unlink()
        time.sleep(1.2)  # 至少经过一次增量投影轮次
        status,data,_=self.http(server,'GET','/api/projects',cookie=cookie)
        self.assertEqual(status,200,data)
        projects={p['name']:p for p in data['projects']}
        self.assertIsNotNone(projects['first']['execution_scope'])
        self.assertEqual(projects['first']['config_hash'],expected)
        self.assertIsNone(projects['second']['execution_scope'])
        self.assertIsNone(projects['second']['config_hash'])
        self.assertTrue(projects['second']['unavailable'])
        self.assertNotIn('private-fixture-never-expose',json.dumps(data))
        self.assertTrue(server.application.projection_thread.is_alive())

    def test_create_run_rejects_stale_config_hash_and_accepts_current(self):
        cfgfile=self.root/'scope.toml'
        cfgfile.write_text('task_workdir="ws"\nstate_dir="state"\n[run_budget]\nmax_tokens=1000\nmax_duration_s=60\n'
                           '[[stages]]\nname="verify"\nkind="test"\ntest_cmd=["python","-c","assert 2+2==4"]\n',
                           encoding='utf8')
        server=self.start([cfgfile]);cookie=self.login(server)
        service=server.application.service
        project=service.store.projects()[0]
        current=hashlib.sha256(cfgfile.read_bytes()).hexdigest()
        with mock.patch.object(service,'launch') as launch:
            status,data,_=self.post(server,'/api/runs',
                                    {'project_id':project['id'],'task':'过期范围任务',
                                     'expected_config_hash':'0'*64},cookie,'stale-hash-001')
            self.assertEqual(status,409,data)
            self.assertIn('配置已变化',data['error'])
            status,data,_=self.post(server,'/api/runs',
                                    {'project_id':project['id'],'task':'当前范围任务',
                                     'expected_config_hash':current},cookie,'current-hash-001')
            self.assertEqual(status,202,data)
            self.assertEqual(data['project_id'],project['id'])
            self.assertEqual(data['lifecycle'],'pending')
            self.assertEqual(data['execution_scope']['mode'],'fixed_tests')
            self.assertTrue(launch.called)

    def test_derived_task_rejects_stale_parent_config_hash(self):
        from orchestrator.collaboration.service import Service
        from orchestrator.collaboration.storage import Conflict
        project_cfg=self.root/'project.toml'
        project_cfg.write_text('task_workdir="ws"\nstate_dir="state"\n[run_budget]\nmax_tokens=1000\nmax_duration_s=60\n'
                               '[[stages]]\nname="verify"\nkind="test"\n'
                               'test_cmd=["python","-c","print(1)"]\n',encoding='utf8')
        parent_cfg=self.root/'parent.toml'
        parent_cfg.write_text(project_cfg.read_text(encoding='utf8')+'\n# 父运行绑定的另一份固定配置\n',
                              encoding='utf8')
        service=Service(self.root/'derive.sqlite3',[project_cfg])
        self.addCleanup(service._background_sync.close)
        self.addCleanup(service.closed.set)
        project=service.store.projects()[0]['id']
        atomic_write_json(self.root/'state/runs/parent/state.json',{'task':'parent','stages':{},'events':[]})
        atomic_write_json(self.root/'state/runs/parent/job.json',{'status':'done'})
        key=service.store.register_run(project,'parent',parent_cfg,self.root/'state',managed=False)
        expected=hashlib.sha256(project_cfg.read_bytes()).hexdigest()
        with self.assertRaises(Conflict) as raised:
            service.create_run(project,'派生任务','derive-hash-001',parent=key,expected_config_hash=expected)
        self.assertIn('配置已变化',str(raised.exception))

    def test_task_creation_and_room_scope_frontend(self):
        node=shutil.which('node');self.assertIsNotNone(node)
        result=subprocess.run([node,str(Path(__file__).parent/'js/execution_scope.cjs')],
            cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,encoding='utf8',timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
        self.assertIn('EXECUTION_SCOPE_UI_OK',result.stdout)


if __name__=='__main__':unittest.main()
