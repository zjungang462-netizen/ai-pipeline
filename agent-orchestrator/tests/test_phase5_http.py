"""新工作台接口沿用会话、同源/CSRF和固定配置约束。"""
import http.client
import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote, urlencode

from orchestrator.dashboard.server import Handler, create_server
from orchestrator.state import atomic_write_json


class Phase5HTTP(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.config=self.root/'c.toml'
        self.config.write_text('task_workdir="ws"\nstate_dir="state"\n[run_budget]\nmax_tokens=100\nmax_duration_s=60\n[[stages]]\nname="testing"\nkind="test"\ntest_cmd=["python","-c","pass"]\n',encoding='utf8')
        self.server=create_server(self.root/'db.sqlite3',[self.config],0)
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        thread=threading.Thread(target=self.server.serve_forever,daemon=True);thread.start()
        def close():
            self.server.application.closed.set();self.server.shutdown();self.server.server_close();thread.join(5)
        self.addCleanup(close)
        self.cookie='';self.csrf=''
        status,data,headers=self.request('/api/session',{'token':self.server.application.bootstrap})
        self.assertEqual(status,200);self.cookie=headers['Set-Cookie'].split(';')[0];self.csrf=data['csrf']
        self.service=self.server.application.service
        self.project=self.service.list_projects()[0]

    def request(self,path,body=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
        h={'Cookie':self.cookie}
        if body is not None:h.update({'Origin':self.server.application.origin,'Content-Type':'application/json','X-Workbench-CSRF':self.csrf,'Idempotency-Key':'template-test-001'})
        h.update(headers or {})
        try:
            c.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,h)
            response=c.getresponse();raw=response.read()
            return response.status,json.loads(raw) if response.getheader('Content-Type','').startswith('application/json') else raw,dict(response.getheaders())
        finally:c.close()

    def seed(self,name,tokens):
        directory=self.root/'state/runs'/name
        atomic_write_json(directory/'state.json',{'revision':1,'stages':{'testing':{'status':'failed'}},
            'usage_total':{'tokens':tokens,'duration_s':0},'events':[{'kind':'test_fail','stage':'testing'}]})
        atomic_write_json(directory/'job.json',{'status':'failed'})
        return self.service.store.register_run(self.project['id'],name,self.config,self.root/'state')

    def test_template_post_requires_csrf_and_hash_and_never_launches(self):
        body=dict(project_id=self.project['id'],name='常用',task='核验',expected_config_hash=self.project['config_hash'])
        with mock.patch.object(self.service,'launch',side_effect=AssertionError('模板不得启动任务')):
            self.assertEqual(self.request('/api/templates',body,{'X-Workbench-CSRF':'bad'})[0],403)
            self.assertEqual(self.request('/api/templates',{**body,'expected_config_hash':'changed'})[0],409)
            self.assertEqual(self.request('/api/templates',body)[0],201)
        status,data,_=self.request('/api/templates')
        self.assertEqual(status,200);self.assertEqual(len(data['templates']),1)
        self.assertTrue(data['templates'][0]['usable'])
        self.assertFalse(self.service.store.runs())

    def test_read_endpoints_need_cookie_and_reject_bad_compare(self):
        for path in ('/api/templates','/api/failures','/api/compare?left=x&right=y'):
            self.assertEqual(self.request(path,headers={'Cookie':''})[0],401)
        self.assertEqual(self.request('/api/compare?left=a&left=b&right=c')[0],400)
        self.assertEqual(self.request('/api/failures?limit=500')[0],400)

    def test_health_keeps_ok_and_reports_projection_diagnostics(self):
        secret='health-projection-private-token'
        stats={'errors':2,'last_error':f'bad projection: {secret}'}
        with mock.patch.dict(os.environ,{'HEALTH_PROJECTION_TOKEN':secret}), \
                mock.patch.object(self.service._background_sync,'sync',return_value=stats):
            self.service.sync(incremental=True)
            status,data,_=self.request('/api/health')
        self.assertEqual(status,200)
        self.assertEqual(data['status'],'ok')
        self.assertEqual(data['projection']['errors'],2)
        self.assertIn('bad projection',data['projection']['last_error'])
        self.assertNotIn(secret,data['projection']['last_error'])
        self.assertIn('[REDACTED]',data['projection']['last_error'])

    def test_comparison_failure_page_and_budget_warning_through_http(self):
        a=self.seed('one',80);b=self.seed('two',90)
        status,data,_=self.request('/api/compare?'+urlencode(dict(left=a,right=b)))
        self.assertEqual(status,200);self.assertEqual(data['delta']['tokens'],10)
        self.assertFalse(data['same_config'])
        status,page,_=self.request('/api/failures?limit=1')
        self.assertTrue(page['has_more']);self.assertEqual(page['counts'],{'test_failure':1})
        status,snapshot,_=self.request('/api/runs/'+quote(a,safe='')+'/snapshot')
        self.assertEqual(status,200);self.assertEqual(snapshot['budget_alerts'][0]['ratio'],.8)

    def test_insights_static_has_csp_and_is_served_without_secrets(self):
        status,data,headers=self.request('/insights.js')
        self.assertEqual(status,200);self.assertIn('Content-Security-Policy',headers)
        self.assertIn(b'compareText',data)

    def test_explicit_wildcard_bind_uses_configured_origin_and_host_checks(self):
        server=create_server(self.root/'wildcard.sqlite3',[self.config],0,host='0.0.0.0',
                             origin='http://workbench.test:8766')
        self.assertEqual(server.server_address[0], '0.0.0.0')
        self.assertEqual(server.application.origin, 'http://workbench.test:8766')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda: (server.application.closed.set(),server.shutdown(),
                                 server.server_close(),thread.join(5)))
        def request(path, body=None, headers=None):
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            request_headers={'Host':server.application.origin.split('://',1)[1]}
            if body is not None:
                request_headers.update({'Content-Type':'application/json','Origin':server.application.origin})
            request_headers.update(headers or {})
            try:
                connection.request('POST' if body is not None else 'GET',path,
                                   json.dumps(body) if body is not None else None,request_headers)
                response=connection.getresponse();raw=response.read()
                return response.status,json.loads(raw)
            finally:connection.close()
        self.assertEqual(request('/api/health')[0],200)
        self.assertEqual(request('/api/health',headers={'Host':'untrusted.invalid'})[0],403)
        self.assertEqual(request('/api/session',{'token':server.application.bootstrap},
                                 {'Origin':'https://untrusted.invalid'})[0],403)
        self.assertEqual(request('/api/session',{'token':server.application.bootstrap})[0],200)

    def test_container_entry_starts_dashboard_and_excludes_local_state(self):
        root=Path(__file__).resolve().parents[2]
        dockerfile=(root/'Dockerfile').read_text(encoding='utf8')
        command=next(line for line in dockerfile.splitlines() if line.startswith('CMD '))
        argv=json.loads(command[4:])
        self.assertIn('dashboard',argv)
        self.assertEqual(argv[argv.index('--host')+1],'0.0.0.0')
        self.assertEqual(argv[argv.index('--origin')+1],'http://localhost:8766')
        self.assertEqual(argv[argv.index('--config')+1],'kiroween-demo.example.toml')
        self.assertIn('USER workbench',dockerfile)
        ignored=(root/'.dockerignore').read_text(encoding='utf8').splitlines()
        for pattern in ('**/.workbench','**/.env','**/*.sqlite3','**/__pycache__'):
            self.assertIn(pattern,ignored)

    def test_i18n_static_is_served_as_executable_javascript(self):
        status,data,headers=self.request('/i18n.js')
        self.assertEqual(status,200)
        self.assertEqual(headers['Content-Type'],'text/javascript; charset=utf-8')
        self.assertIn(b'WorkbenchI18n',data)

    def test_localhost_origin_and_https_secure_cookie(self):
        local_srv=create_server(self.root/'localhost.sqlite3',[self.config],0,host='localhost')
        self.assertEqual(local_srv.application.origin,f'http://localhost:{local_srv.server_port}')
        local_srv.server_close()
        https_srv=create_server(self.root/'https.sqlite3',[self.config],0,host='127.0.0.1',
                                origin='https://workbench.example')
        t=threading.Thread(target=https_srv.serve_forever,daemon=True);t.start()
        self.addCleanup(lambda:(https_srv.application.closed.set(),https_srv.shutdown(),
                                https_srv.server_close(),t.join(5)))
        c=http.client.HTTPConnection('127.0.0.1',https_srv.server_port,timeout=5)
        try:
            c.request('POST','/api/session',json.dumps({'token':https_srv.application.bootstrap}),
                      {'Host':'workbench.example','Origin':'https://workbench.example','Content-Type':'application/json'})
            resp=c.getresponse();resp.read()
            self.assertEqual(resp.status,200)
            self.assertIn('Secure',resp.getheader('Set-Cookie',''))
        finally:
            c.close()


class StreamSessionTests(unittest.TestCase):
    def test_each_poll_reuses_one_session_without_holding_it_during_io(self):
        store=mock.Mock()
        store.active=0
        store.session_count=0
        observations=[]

        @contextmanager
        def session():
            store.active+=1
            store.session_count+=1
            try:yield
            finally:store.active-=1

        store.session.side_effect=session
        app=SimpleNamespace(streams=threading.BoundedSemaphore(1),closed=threading.Event())
        polls=0

        def sync_run_events(run):
            nonlocal polls
            observations.append(('sync',store.active))
            polls+=1
            if polls==2:app.closed.set()

        def events(run_id,cursor,limit):
            observations.append(('events',store.active))
            return []

        store.events.side_effect=events
        app.service=SimpleNamespace(
            store=store,sync_run_events=sync_run_events,redact=lambda value:value)
        handler=object.__new__(Handler)
        handler.server=SimpleNamespace(application=app)
        handler.headers={'Last-Event-ID':'0'}
        handler.connection=mock.Mock()
        handler.send_response=mock.Mock()
        handler.headers_common=mock.Mock()
        handler.send_header=mock.Mock()
        handler.end_headers=mock.Mock()
        handler.wfile=mock.Mock()
        handler.wfile.write.side_effect=lambda data:observations.append(('write',store.active))
        handler.wfile.flush.side_effect=lambda:observations.append(('flush',store.active))

        def wait(timeout):
            observations.append(('wait',store.active))
            return app.closed.is_set()

        with mock.patch.object(app.closed,'wait',side_effect=wait):
            handler.stream({'id':'run-1'},{})

        self.assertEqual(polls,2)
        self.assertEqual(store.session_count,2)
        self.assertEqual(store.events.call_count,2)
        for operation,depth in observations:
            self.assertEqual(depth,1 if operation in ('sync','events') else 0,operation)
