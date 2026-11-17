"""新工作台接口沿用会话、同源/CSRF和固定配置约束。"""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote, urlencode

from orchestrator.dashboard.server import create_server
from orchestrator.state import atomic_write_json


class Phase5HTTP(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.config=self.root/'c.toml'
        self.config.write_text('task_workdir="ws"\nstate_dir="state"\n[run_budget]\nmax_tokens=100\nmax_duration_s=60\n[[stages]]\nname="testing"\nkind="test"\ntest_cmd=["python","-c","pass"]\n',encoding='utf8')
        self.server=create_server(self.root/'db.sqlite3',[self.config],0)
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
