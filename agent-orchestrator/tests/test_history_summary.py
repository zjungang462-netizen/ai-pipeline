"""历史列表只读摘要，不写投影、不加载正文，变化仍即时失效。"""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.collaboration import projection
from orchestrator.collaboration.projection import SummaryCache
from orchestrator.collaboration.service import Service
from orchestrator.state import atomic_write_json


class HistorySummary(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.config=self.root/'pipeline.toml'
        self.config.write_text('task_workdir="ws"\nstate_dir="state"\n'
                               '[[stages]]\nname="review"\nkind="report"\nadapter="fake"\nprompt="fixture"\n'
                               '[adapters.fake]\ntype="fake"\n',encoding='utf8')
        self.service=Service(self.root/'db.sqlite3',[self.config])
        self.project=self.service.store.projects()[0]['id']

    def run_record(self,name='r',task='标题',status='done'):
        key=self.service.store.register_run(self.project,name,self.config,self.root/'state')
        root=self.root/'state/runs'/name
        atomic_write_json(root/'state.json',{'task':task,'revision':1,
            'stages':{'review':{'status':'done'}},
            'events':[{'kind':'attempt_done','stage':'review','n':1,'ts':1}]})
        atomic_write_json(root/'job.json',{'status':status})
        attempt=root/'stages/review/attempts/1'
        atomic_write_json(attempt/'success.json',{})
        (attempt/'final_text.txt').write_text('PRIVATE_BODY',encoding='utf8')
        return key,root

    def test_cold_page_and_legacy_list_do_not_write_sql_or_read_artifact_bodies(self):
        key,_=self.run_record()
        reads=[]
        original=Path.read_text
        def read(path,*args,**kwargs):
            reads.append(path.name)
            if path.name not in ('state.json','job.json'):
                raise AssertionError('摘要不得读取正文或尝试元数据: '+path.name)
            return original(path,*args,**kwargs)
        sql=[]
        with self.service.store.session():
            with self.service.store.connect() as db:db.set_trace_callback(sql.append)
            with mock.patch.object(Path,'read_text',read), mock.patch(
                    'orchestrator.collaboration.service.project',side_effect=AssertionError('摘要不得运行完整投影')):
                self.assertEqual(self.service.list_runs_page(1)['runs'][0]['id'],key)
                self.assertEqual(self.service.list_runs_page(1,q='标题',status='done')['runs'][0]['id'],key)
                self.assertEqual(self.service.list_runs(1)[0]['id'],key)
            with self.service.store.connect() as db:db.set_trace_callback(None)
        writes=[s for s in sql if s.split(None,1)[0].upper() in ('INSERT','UPDATE','DELETE','REPLACE')]
        self.assertEqual(writes,[])
        self.assertEqual(sorted(reads),['job.json','state.json'])
        self.assertEqual(self.service.store.last_cursor(key),0)

    def test_list_cache_does_not_suppress_later_room_projection(self):
        key,_=self.run_record()
        self.service.list_runs_page(q='标题')
        self.assertEqual(self.service.store.events(key),[])
        snapshot=self.service.snapshot(key)
        self.assertEqual(snapshot['task'],'标题')
        events=self.service.store.events(key)
        self.assertEqual(sum(e['kind']=='message' for e in events),1)
        self.assertEqual(sum(e['kind']=='engine_event' for e in events),1)
        cursor=self.service.store.last_cursor(key)
        self.service.list_runs_page()
        self.service.snapshot(key)
        self.assertEqual(self.service.store.last_cursor(key),cursor)

    def test_updated_state_and_job_invalidate_summary_without_full_snapshot(self):
        key,root=self.run_record()
        self.assertEqual(self.service.list_runs_page(status='running')['runs'],[])
        state=json.loads((root/'state.json').read_text(encoding='utf8'))
        state.update(task='Straße 中文新标题',revision=2)
        atomic_write_json(root/'state.json',state)
        atomic_write_json(root/'job.json',{'status':'recovering'})
        page=self.service.list_runs_page(q='STRASSE',status='running')
        self.assertEqual([r['id'] for r in page['runs']],[key])
        self.assertEqual(page['runs'][0]['task'],state['task'])
        self.assertEqual(page['runs'][0]['lifecycle'],'recovering')

    def test_summary_retries_pair_changed_during_read(self):
        _,root=self.run_record(task='旧标题')
        original=projection.read_json
        changed=False
        def read(path,default=None):
            nonlocal changed
            value=original(path,default)
            if Path(path)==root/'state.json' and not changed:
                changed=True
                atomic_write_json(path,{**value,'task':'新提交标题','revision':2})
                atomic_write_json(root/'job.json',{'status':'failed'})
            return value
        with mock.patch.object(projection,'read_json',side_effect=read):
            page=self.service.list_runs_page()
        self.assertTrue(changed)
        self.assertEqual(page['runs'][0]['task'],'新提交标题')
        self.assertEqual(page['runs'][0]['lifecycle'],'failed')

    def test_cache_is_bounded_and_eviction_preserves_query_results(self):
        cache=SummaryCache(limit=2)
        expected=[]
        for i in range(4):
            key,_=self.run_record(str(i),task='task-'+str(i))
            entry=self.service.store.run(key)
            expected.append((entry,'task-'+str(i)))
            self.assertEqual(cache.read(entry),('task-'+str(i),'done'))
            self.assertLessEqual(len(cache.rows),2)
        for entry,task in expected:
            self.assertEqual(cache.read(entry),(task,'done'))

    def test_concurrent_readers_share_only_complete_summaries(self):
        key,_=self.run_record(task='并发标题')
        entry=self.service.store.run(key)
        errors=[]
        def read():
            try:
                for _ in range(30):
                    self.assertEqual(self.service._summary_cache.read(entry),('并发标题','done'))
            except Exception as error:errors.append(error)
        threads=[threading.Thread(target=read) for _ in range(4)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(10)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors,[])


if __name__=='__main__':unittest.main()
