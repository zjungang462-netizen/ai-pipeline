"""分页接口补充边界，独立于模型开发过程。"""
import base64
import json
import math
import tempfile
import unittest
from pathlib import Path

from orchestrator.collaboration.service import Service


class PaginationEdges(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        cfg=root/'config.toml'
        cfg.write_text('task_workdir="ws"\nstate_dir="state"\n[run_budget]\nmax_tokens=1000\nmax_duration_s=60\n[[stages]]\nname="test"\nkind="test"\ntest_cmd=["python","-c","print(1)"]\n',encoding='utf8')
        self.service=Service(root/'db.sqlite3',[cfg])
        self.project=self.service.store.projects()[0]['id']
        self.cfg=cfg

    def test_empty_page_and_single_record(self):
        page=self.service.list_runs_page()
        self.assertEqual(page,{'runs':[],'has_more':False,'next_cursor':None})
        key=self.service.store.register_run(self.project,'only',self.cfg,self.cfg.parent/'state')
        page=self.service.list_runs_page(1)
        self.assertEqual([r['id'] for r in page['runs']],[key])
        self.assertFalse(page['has_more'])

    def test_zero_negative_limit_and_legacy_list_return_type(self):
        for i in range(3):
            self.service.store.register_run(self.project,str(i),self.cfg,self.cfg.parent/'state')
        self.assertEqual(len(self.service.list_runs_page(0)['runs']),1)
        self.assertEqual(len(self.service.list_runs_page(-2)['runs']),1)
        self.assertIsInstance(self.service.list_runs(1),list)

    def test_before_restriction_is_kept_when_next_request_only_supplies_cursor(self):
        expected=[]
        for i in range(8):
            key=self.service.store.register_run(self.project,str(i),self.cfg,self.cfg.parent/'state')
            with self.service.store.connect() as db:
                db.execute('UPDATE runs SET created=? WHERE id=?',(float(i),key))
            if i<5:
                expected.insert(0,key)
        page=self.service.list_runs_page(2,before=5)
        seen=[r['id'] for r in page['runs']]
        while page['has_more']:
            page=self.service.list_runs_page(2,cursor=page['next_cursor'])
            seen.extend(r['id'] for r in page['runs'])
        self.assertEqual(seen,expected)

    def test_malformed_cursor_fields_and_nonfinite_boundaries_are_rejected(self):
        valid={'v':1,'ch':float.hex(1.0),'id':'id','bh':None}
        for item in ({**valid,'v':True},{**valid,'id':[]},{**valid,'ch':None},
                     {**valid,'ch':'nan'},{**valid,'bh':'inf'},{**valid,'unexpected':1}):
            cursor=base64.urlsafe_b64encode(json.dumps(item).encode()).decode()
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.service.list_runs_page(cursor=cursor)
        for before in (math.inf,-math.inf,math.nan):
            with self.assertRaises(ValueError):
                self.service.list_runs_page(before=before)

    def test_hex_float_exponent_overflow_is_a_controlled_value_error(self):
        for field in ('ch','bh'):
            item={'v':1,'ch':float.hex(1.0),'id':'id','bh':None}
            item[field]='0x1p+999999'
            cursor=base64.urlsafe_b64encode(json.dumps(item).encode()).decode()
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.service.list_runs_page(cursor=cursor)


if __name__=='__main__':
    unittest.main()
