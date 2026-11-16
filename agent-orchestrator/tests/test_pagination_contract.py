"""历史任务分页的外部验收契约；模型不得修改本文件或配套 JS。"""
import http.client
import base64
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

from orchestrator.dashboard.server import create_server


class PaginationHTTPContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        config = self.root / 'history.toml'
        config.write_text('task_workdir="workspace"\nstate_dir="state"\n'
                          '[run_budget]\nmax_tokens=12000\nmax_duration_s=90\n'
                          '[[stages]]\nname="verify"\nkind="test"\ntest_cmd=["python","-c","print(1)"]\n',encoding='utf8')
        self.server = create_server(self.root/'collaboration.sqlite3',[config],0)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        def shutdown():
            self.server.application.closed.set()
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        self.addCleanup(shutdown)
        self.cookie = ''
        status, _, headers = self.request('/api/session', {'token':self.server.application.bootstrap})
        self.assertEqual(status,200)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        self.store = self.server.application.service.store
        self.project = self.store.projects()[0]['id']
        self.expected = []
        count = 200 if self._testMethodName=='test_exact_multiple_has_no_phantom_final_page' else 257
        with self.store.session():
            for i in range(count):
                key = self.store.register_run(self.project,f'history-{i:04}',config,self.root/'state')
                created = float(1000-i//37)
                with self.store.connect() as db:
                    db.execute('UPDATE runs SET created=? WHERE id=?',(created,key))
                self.expected.append({'id':key,'created':created})
        self.expected.sort(key=lambda r:(r['created'],r['id']),reverse=True)

    def request(self,path,body=None):
        connection = http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=15)
        headers = {'Cookie':self.cookie}
        if body is not None:
            headers.update({'Content-Type':'application/json','Origin':self.server.application.origin})
        try:
            connection.request('POST' if body is not None else 'GET',path,
                               json.dumps(body) if body is not None else None,headers)
            response = connection.getresponse()
            return response.status,json.loads(response.read()),dict(response.getheaders())
        finally:
            connection.close()

    def page(self,cursor=None,limit=100):
        path = '/api/runs?limit='+str(limit)
        if cursor is not None:
            path += '&cursor='+quote(cursor,safe='')
        status,data,_ = self.request(path)
        self.assertEqual(status,200,data)
        self.assertIsInstance(data['runs'],list)
        self.assertIs(type(data['has_more']),bool)
        self.assertIn('next_cursor',data)
        if data['has_more']:
            self.assertIsInstance(data['next_cursor'],str)
            self.assertTrue(data['next_cursor'])
        else:
            self.assertIsNone(data['next_cursor'])
        return data

    def test_257_rows_and_timestamp_ties_have_no_gaps_or_duplicates(self):
        rows, cursors, cursor = [],set(),None
        for _ in range(5):
            data = self.page(cursor)
            self.assertLessEqual(len(data['runs']),100)
            rows += data['runs']
            if not data['has_more']:
                break
            cursor = data['next_cursor']
            self.assertNotIn(cursor,cursors)
            cursors.add(cursor)
        self.assertEqual([r['id'] for r in rows],[r['id'] for r in self.expected])
        self.assertEqual(len({r['id'] for r in rows}),257)

    def test_exact_multiple_has_no_phantom_final_page(self):
        first = self.page()
        self.assertTrue(first['has_more'])
        second = self.page(first['next_cursor'])
        self.assertEqual(len(second['runs']),100)
        self.assertFalse(second['has_more'])

    def test_replaying_cursor_is_stable(self):
        first = self.page()
        self.assertEqual(self.page(first['next_cursor']),self.page(first['next_cursor']))

    def test_new_head_does_not_shift_existing_history_cursor(self):
        first = self.page()
        key = self.store.register_run(self.project,'new-head',self.root/'history.toml',self.root/'state')
        with self.store.connect() as db:
            db.execute('UPDATE runs SET created=? WHERE id=?',(2000.0,key))
        second = self.page(first['next_cursor'])
        self.assertEqual([r['id'] for r in second['runs']],[r['id'] for r in self.expected[100:200]])
        self.assertEqual(self.page()['runs'][0]['id'],key)

    def test_legacy_before_parameter_and_limit_cap_remain_supported(self):
        status,data,_ = self.request('/api/runs?limit=5&before=997')
        self.assertEqual(status,200)
        expected = [r['id'] for r in self.expected if r['created']<997][:5]
        self.assertEqual([r['id'] for r in data['runs']],expected)
        self.assertEqual(len(self.page(limit=1000)['runs']),100)
        self.assertEqual(len(self.page(limit=1)['runs']),1)

    def test_invalid_cursors_are_rejected_without_dropping_session(self):
        for cursor in ('not-a-cursor','null','[]','x'*4097):
            status,_,_ = self.request('/api/runs?cursor='+quote(cursor,safe=''))
            self.assertEqual(status,400,cursor[:20])
        self.assertEqual(self.request('/api/runs?limit=not-a-number')[0],400)
        self.assertEqual(self.request('/api/projects')[0],200)

    def test_old_run_snapshot_and_list_are_independent_of_first_page(self):
        key = self.expected[-1]['id']
        status,snapshot,_ = self.request('/api/runs/'+quote(key,safe='')+'/snapshot')
        self.assertEqual(status,200)
        self.assertEqual(snapshot['id'],key)
        self.assertNotIn(key,{r['id'] for r in self.page()['runs']})

    def test_list_pages_remain_lightweight(self):
        service = self.server.application.service
        with mock.patch.object(service,'snapshot',side_effect=AssertionError('分页列表不能逐条加载完整房间')):
            self.assertEqual(len(self.page()['runs']),100)

    def test_overflow_cursor_returns_400_and_session_remains_usable(self):
        for field in ('ch','bh'):
            item={'v':1,'ch':float.hex(1.0),'id':'id','bh':None}
            item[field]='0x1p+999999'
            cursor=base64.urlsafe_b64encode(json.dumps(item).encode()).decode()
            self.assertEqual(self.request('/api/runs?cursor='+quote(cursor,safe=''))[0],400)
        self.assertEqual(self.request('/api/projects')[0],200)


class PaginationFrontendContract(unittest.TestCase):
    def test_frontend_async_pagination_contract(self):
        node = shutil.which('node')
        self.assertIsNotNone(node,'验收需要已安装的 Node.js，无需 npm 依赖')
        result = subprocess.run([node,str(Path(__file__).parent/'js/pagination_contract.cjs')],
                                cwd=Path(__file__).resolve().parents[1],capture_output=True,
                                text=True,encoding='utf8',errors='replace',timeout=45)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
        self.assertIn('PAGINATION_FRONTEND_CONTRACT_OK',result.stdout)


if __name__=='__main__':
    unittest.main()
