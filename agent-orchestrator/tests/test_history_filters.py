"""全历史筛选独立 HTTP 契约；开发模型不得修改。"""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode,quote

from orchestrator.dashboard.server import create_server
from orchestrator.state import atomic_write_json


class HistoryFilters(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.configs=[]
        for name in ('alpha','beta'):
            cfg=self.root/(name+'.toml')
            cfg.write_text('task_workdir="'+name+'-ws"\nstate_dir="'+name+'-state"\n[run_budget]\nmax_tokens=1000\nmax_duration_s=60\n[[stages]]\nname="verify"\nkind="test"\ntest_cmd=["python","-c","print(1)"]\n',encoding='utf8')
            self.configs.append(cfg)
        self.server=create_server(self.root/'db.sqlite3',self.configs,0)
        thread=threading.Thread(target=self.server.serve_forever,daemon=True);thread.start()
        def shutdown():
            self.server.application.closed.set();self.server.shutdown();self.server.server_close();thread.join(5)
        self.addCleanup(shutdown)
        self.cookie='';status,_,headers=self.request('/api/session',{'token':self.server.application.bootstrap})
        self.assertEqual(status,200);self.cookie=headers['Set-Cookie'].split(';')[0]
        self.service=self.server.application.service;store=self.service.store
        self.projects=[p['id'] for p in store.projects()];self.rows=[]
        states=['done','failed','timeout','cancelled','running','pending','recovering','paused','waiting_approval']
        with store.session():
            for i in range(407):
                p=i%2;run_id=f'history-{i:04}';state_dir=self.root/(('alpha' if p==0 else 'beta')+'-state')
                key=store.register_run(self.projects[p],run_id,self.configs[p],state_dir)
                created=float(2000-i//17)
                with store.connect() as db:db.execute('UPDATE runs SET created=? WHERE id=?',(created,key))
                task=('旧历史 needle%_ '+str(i)) if i>=121 else '首页任务 '+str(i)
                if i==404:task="Straße 单引号 ' 字面%_"
                status=states[i%len(states)]
                folder=state_dir/'runs'/run_id;folder.mkdir(parents=True)
                atomic_write_json(folder/'state.json',{'task':task,'revision':1,'stages':{'verify':{'status':'done'}},'events':[]})
                atomic_write_json(folder/'job.json',{'status':status})
                self.rows.append({'id':key,'run_id':run_id,'project_id':self.projects[p],
                                  'created':created,'task':task,'lifecycle':status,'folder':folder})
        self.rows.sort(key=lambda r:(r['created'],r['id']),reverse=True)

    def request(self,path,body=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=20)
        headers={'Cookie':self.cookie}
        if body is not None:headers.update({'Content-Type':'application/json','Origin':self.server.application.origin})
        try:
            c.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,headers)
            response=c.getresponse();return response.status,json.loads(response.read()),dict(response.getheaders())
        finally:c.close()

    def page(self,**params):
        status,page,_=self.request('/api/runs?'+urlencode(params))
        self.assertEqual(status,200,page);return page

    def expected(self,q='',project_id='',status='all'):
        groups={'running':{'pending','running','recovering'},'failed':{'failed','timeout','cancelled'}}
        return [r['id'] for r in self.rows if (not q or q.strip().casefold() in r['task'].casefold() or q.strip().casefold() in r['run_id'].casefold())
                and (not project_id or r['project_id']==project_id)
                and (status=='all' or r['lifecycle'] in groups.get(status,{status}))]

    def test_keyword_finds_records_beyond_unfiltered_first_page(self):
        self.assertFalse(any('needle' in r['task'] for r in self.page(limit=100)['runs']))
        page=self.page(limit=17,q='NEEDLE%_')
        self.assertEqual([r['id'] for r in page['runs']],self.expected(q='needle%_')[:17])
        self.assertTrue(page['has_more'])

    def test_combined_query_ties_cursor_only_continuation_has_no_gaps(self):
        params={'limit':7,'q':'needle%_','project_id':self.projects[1],'status':'failed'}
        page=self.page(**params);found=[];cursors=set()
        while True:
            found.extend(r['id'] for r in page['runs'])
            if not page['has_more']:self.assertIsNone(page['next_cursor']);break
            cursor=page['next_cursor'];self.assertNotIn(cursor,cursors);cursors.add(cursor)
            page=self.page(limit=7,cursor=cursor)
        self.assertEqual(found,self.expected(q='needle%_',project_id=self.projects[1],status='failed'))
        self.assertEqual(len(found),len(set(found)))

    def test_query_groups_and_literal_unicode_casefold(self):
        for state in ('running','failed','paused','waiting_approval','done'):
            with self.subTest(status=state):
                page=self.page(limit=100,status=state)
                self.assertEqual([r['id'] for r in page['runs']],self.expected(status=state)[:100])
        for term in ('STRASSE',"'",'%_','history-0404'):
            with self.subTest(q=term):
                self.assertEqual([r['id'] for r in self.page(q=term,limit=100)['runs']],self.expected(q=term)[:100])

    def test_cursor_rejects_changed_filters_and_replays_same_query(self):
        first=self.page(limit=11,q='needle%_',project_id=self.projects[0],status='all')
        cursor=first['next_cursor'];self.assertTrue(cursor)
        original=self.page(limit=11,cursor=cursor)
        self.assertEqual(original,self.page(limit=11,cursor=cursor,q=' NEEDLE%_ ',project_id=self.projects[0],status='all'))
        for changed in ({'q':'other'},{'q':''},{'project_id':self.projects[1]},{'status':'failed'}):
            code,_,_=self.request('/api/runs?'+urlencode({'cursor':cursor,**changed}))
            self.assertEqual(code,400,changed)

    def test_before_remains_bound_and_new_head_does_not_shift_cursor(self):
        first=self.page(limit=13,q='needle%_',before=1980)
        cursor=first['next_cursor'];self.assertTrue(cursor)
        second=self.page(limit=13,cursor=cursor)
        row=self.rows[0]
        with self.service.store.connect() as db:db.execute('UPDATE runs SET created=? WHERE id=?',(3000,row['id']))
        self.assertEqual(second,self.page(limit=13,cursor=cursor))
        self.assertTrue(all(r['created']<1980 for r in second['runs']))

    def test_invalid_filters_do_not_break_session_and_no_match_is_empty(self):
        for params in ({'q':'x'*201},{'project_id':'x'*513},{'status':'invalid-status'}):
            self.assertEqual(self.request('/api/runs?'+urlencode(params))[0],400)
        self.assertEqual(self.page(q='no-such-task'),{'runs':[],'has_more':False,'next_cursor':None})
        self.assertEqual(self.page(project_id='project-unknown'),{'runs':[],'has_more':False,'next_cursor':None})
        self.assertEqual(self.request('/api/projects')[0],200)

    def test_latest_state_is_used_and_list_does_not_load_full_rooms(self):
        target=next(r for r in self.rows if r['run_id']=='history-0300')
        self.assertEqual(self.page(q=target['run_id'],status='done')['runs'],[])
        atomic_write_json(target['folder']/'job.json',{'status':'done'})
        with mock.patch.object(self.service,'snapshot',side_effect=AssertionError('不能加载完整房间')):
            self.assertEqual([r['id'] for r in self.page(q=target['run_id'],status='done')['runs']],[target['id']])
        self.assertIsInstance(self.service.list_runs(1),list)

    def test_old_room_remains_readable_outside_filter(self):
        row=self.rows[-1]
        self.assertEqual(self.page(q='no-such-task')['runs'],[])
        code,value,_=self.request('/api/runs/'+quote(row['id'],safe='')+'/snapshot')
        self.assertEqual(code,200);self.assertEqual(value['id'],row['id'])


if __name__=='__main__':unittest.main()
