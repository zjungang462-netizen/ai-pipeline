"""复核发现的长 Unicode 查询与游标类型边界。"""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from orchestrator.collaboration.service import Service
from orchestrator.state import atomic_write_json

class HistoryFilterEdges(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name)
        self.cfg=self.root/'c.toml';self.cfg.write_text('task_workdir="ws"\nstate_dir="state"\n[[stages]]\nname="test"\nkind="test"\ntest_cmd=["python","-c","print(1)"]\n',encoding='utf8')
        self.s=Service(self.root/'db.sqlite3',[self.cfg]);self.project=self.s.store.projects()[0]['id']
    def populate(self,term):
        keys=[]
        for i in range(3):
            name='old-'+str(i);key=self.s.store.register_run(self.project,name,self.cfg,self.root/'state');keys.append(key)
            atomic_write_json(self.root/'state/runs'/name/'state.json',{'task':term,'stages':{'test':{'status':'done'}}})
        return keys
    def test_max_length_unicode_query_generates_usable_cursor(self):
        for term in ('中'*200,'😀'*200,'ﬃ'*200):
            with self.subTest(term=term[:1]):
                self.populate(term)
                page=self.s.list_runs_page(1,q=term);self.assertTrue(page['has_more'])
                seen=[page['runs'][0]['id']]
                while page['has_more']:
                    page=self.s.list_runs_page(1,cursor=page['next_cursor']);seen.extend(r['id'] for r in page['runs'])
                self.assertEqual(len(seen),3);self.assertEqual(len(set(seen)),3)
    def test_cursor_does_not_coerce_bad_filter_types_or_unknown_status(self):
        self.populate('match');cursor=self.s.list_runs_page(1,q='match')['next_cursor']
        payload=json.loads(base64.urlsafe_b64decode(cursor))
        for field,value in (('qh',[]),('pi',{}),('st',[]),('st','bogus')):
            with self.subTest(field=field,value=value):
                bad=base64.urlsafe_b64encode(json.dumps({**payload,field:value}).encode()).decode()
                with self.assertRaises(ValueError):self.s.list_runs_page(1,cursor=bad)

if __name__=='__main__':unittest.main()
