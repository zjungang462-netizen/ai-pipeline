"""真实文件导出与工作台运维摘要的只读性、未知计量与配置绑定。"""
import csv
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.reporting import budget_alerts, export_usage, failure_categories, number, usage_csv, usage_summary
from orchestrator.collaboration import insights
from orchestrator.collaboration.service import Service
from orchestrator.collaboration.storage import Conflict
from orchestrator.cli import main
from orchestrator.state import atomic_write_json


class Phase5Reporting(unittest.TestCase):
    def test_malformed_live_usage_stays_unknown_and_frozen_metrics_still_win(self):
        for value in (None, [], 'invalid', 7):
            with self.subTest(value=value):
                row = usage_summary('case', {'usage_total': value, 'stages': {}})
                self.assertIsNone(row['tokens'])
                self.assertIsNone(row['reported_tokens'])
                self.assertIsNone(row['duration_s'])
                self.assertEqual(row['token_status'], 'not_recorded')
        row = usage_summary('case', {'usage_total': None}, {'tokens': 19, 'duration_s': 2, 'stages': {}})
        self.assertEqual(row['tokens'], 19)
        self.assertEqual(row['source'], 'immutable_metrics')

    def test_malformed_job_does_not_hide_explicit_failure_categories(self):
        state = {'stages': {'coding': {'status': 'failed'}}, 'events': [{'kind': 'test_fail'}]}
        for job in (None, [], [1], 'invalid', 4, {'status': []}, {'status': {}}):
            with self.subTest(job=job):
                self.assertEqual(failure_categories(state, job), ['test_failure'])
        self.assertEqual(failure_categories({}, {'status': 'timeout'}), ['timeout'])
        self.assertEqual(failure_categories({}, {'status': 'cancelled'}), ['cancelled'])

    def test_large_integer_usage_does_not_overflow_float_conversion(self):
        self.assertEqual(number(10 ** 400), 10 ** 400)
        for value in (True, False, -1, -1.5, float('nan'), float('inf'), '10', None):
            with self.subTest(value=value):
                self.assertIsNone(number(value))

    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.state=self.root/'state'
        self.config=self.root/'project.toml'
        self.config.write_text('task_workdir="workspace"\nstate_dir="state"\n[run_budget]\nmax_tokens=100\nmax_duration_s=60\n[[stages]]\nname="testing"\nkind="test"\ntest_cmd=["python","-c","pass"]\n',encoding='utf8')
        self.service=Service(self.root/'db.sqlite3',[self.config])
        self.addCleanup(self.service._background_sync.close)
        self.project=self.service.store.projects()[0]['id']

    def seed(self,name,tokens=80,status='failed',metrics=None):
        directory=self.state/'runs'/name
        atomic_write_json(directory/'state.json',{'task':'task','stages':{'testing':{'status':status}},
            'usage_total':{'tokens':tokens,'reserved_tokens':20,'duration_s':30},
            'stage_usage':{'testing':{'tokens':tokens,'duration_s':30}},
            'events':[{'kind':'attempt_fail','stage':'testing','error_kind':'timeout'}]})
        atomic_write_json(directory/'job.json',{'status':status,'config_hash':'fixed'})
        if metrics is not None:atomic_write_json(directory/'metrics.json',metrics)
        return self.service.store.register_run(self.project,name,self.config,self.state)

    def test_export_preserves_source_and_unknown_cost_is_blank(self):
        self.seed('normal')
        def hashes():return {p:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.state.rglob('*') if p.is_file()}
        before=hashes();report=export_usage(self.state)
        self.assertEqual(hashes(),before)
        rows=list(csv.DictReader(io.StringIO(usage_csv(report))))
        self.assertEqual(rows[0]['scope'],'run_total')
        self.assertEqual(rows[0]['reported_tokens'],'60')
        self.assertEqual(rows[0]['tokens'],'80')
        self.assertEqual(rows[0]['billing_cost'],'')
        self.assertEqual(rows[1]['tool'],'unknown')

    def test_immutable_metrics_usage_and_tool_identity_win_over_changed_state(self):
        metrics={'tokens':12,'duration_s':2,'stages':{'testing':{'tool':'test_cmd','adapter':'test_cmd','status':'done'}},
                 'stage_usage':{'testing':{'tokens':12,'duration_s':2}}}
        self.seed('done',tokens=999,metrics=metrics)
        report=export_usage(self.state)['runs'][0]
        self.assertEqual(report['tokens'],12)
        self.assertEqual(report['stages'][0]['tool'],'test_cmd')
        legacy=usage_summary('legacy',{'stage_usage':{'test':{'tokens':99}}},{'tokens':4,'stages':{'test':{}}})
        self.assertIsNone(legacy['stages'][0]['tokens'])

    def test_csv_neutralizes_formula_ids_and_unknown_cost(self):
        summary=usage_summary(' =HYPERLINK("bad")',{'stages':{'@stage':{}},'usage_total':{}})
        rows=list(csv.DictReader(io.StringIO(usage_csv({'runs':[summary]}))))
        self.assertTrue(rows[0]['run_id'].startswith("'"))
        self.assertTrue(rows[1]['stage'].startswith("'"))
        self.assertEqual(rows[0]['tokens'],'')

    def test_cli_does_not_overwrite_export_or_state_files(self):
        self.seed('one');out=self.root/'usage.csv'
        with mock.patch('sys.stdout',new=io.StringIO()):
            self.assertEqual(main(['export-usage','--state-dir',str(self.state),'--output',str(out)]),0)
            self.assertEqual(main(['export-usage','--state-dir',str(self.state),'--output',str(out)]),2)
            self.assertEqual(main(['export-usage','--state-dir',str(self.state),'--output',str(self.state/'runs/one/state.json')]),2)
        self.assertIn('run_total',out.read_text(encoding='utf-8-sig'))

    def test_alerts_count_reserved_once_and_never_change_budget(self):
        usage={'tokens':80,'reserved_tokens':20,'duration_s':2};budget={'max_tokens':100,'max_duration_s':100}
        alerts=budget_alerts(usage,budget,deadline=110,now=100)
        self.assertEqual(alerts[0]['ratio'],.8)
        self.assertEqual(usage['tokens'],80)
        self.assertEqual(budget['max_tokens'],100)
        self.assertEqual(len(alerts),2)
        self.assertEqual(budget_alerts({'tokens':float('nan')},{'max_tokens':100}),[])
        self.assertEqual(budget_alerts({}, {'max_duration_s':100}, deadline=0, now=100),[])

    def test_templates_upsert_bind_hash_and_detect_config_changes(self):
        project=self.service.list_projects()[0]
        a=insights.save_template(self.service,self.project,'常用','first',project['config_hash'])
        b=insights.save_template(self.service,self.project,'常用','updated',project['config_hash'])
        self.assertEqual(a['id'],b['id'])
        self.assertTrue(insights.templates(self.service)[0]['usable'])
        self.config.write_text(self.config.read_text(encoding='utf8')+'\n# changed\n',encoding='utf8')
        self.assertFalse(insights.templates(self.service)[0]['usable'])
        with self.assertRaises(Conflict):insights.save_template(self.service,self.project,'old','task',project['config_hash'])
        self.assertEqual(len(self.service.store.runs()),0,'保存模板不得启动运行')

    def test_compare_is_readonly_handles_unknown_durations_and_different_configs(self):
        a=self.seed('left',10);b=self.seed('right',50)
        with mock.patch.object(self.service,'launch',side_effect=AssertionError('不可执行')):
            data=insights.compare(self.service,a,b)
        self.assertEqual(data['delta']['tokens'],40)
        self.assertTrue(data['same_config'])
        with self.assertRaises(ValueError):insights.compare(self.service,a,a)

    def test_failures_scan_all_pages_with_ties_and_bad_record_without_projection(self):
        for n in range(5):self.seed('run'+str(n))
        bad=self.seed('bad');(self.state/'runs/bad/state.json').write_text('[]',encoding='utf8')
        after=None;seen=[];errors=[]
        with mock.patch.object(self.service,'snapshot',side_effect=AssertionError('不能加载房间/产物')):
            while True:
                page=insights.failure_page(self.service,after,2)
                seen.extend(row['id'] for row in page['failures']);errors+=page['errors']
                self.assertLessEqual(page['scanned'],2)
                if not page['has_more']:break
                self.assertNotEqual(after,page['next_after']);after=page['next_after']
        self.assertEqual(len(seen),len(set(seen)))
        self.assertEqual(len(seen),5)
        self.assertEqual(errors[0]['id'],bad)

    def test_run_summary_prefetched_row_matches_lookup(self):
        key=self.seed('same')
        row=self.service.store.run(key)
        self.assertEqual(insights.run_summary(self.service,key),
                         insights.run_summary(self.service,key,run=row))

    def test_failure_page_reuses_listed_rows_without_per_run_query(self):
        self.seed('one');self.seed('two')
        with mock.patch.object(self.service.store,'run',
                               side_effect=AssertionError('不应逐行重查')):
            page=insights.failure_page(self.service,None,50)
        self.assertEqual(sorted(row['run_id'] for row in page['failures']),['one','two'])
        self.assertEqual(page['scanned'],2)
        self.assertEqual(set(page),{'failures','counts','scanned','errors',
                                    'has_more','next_after','scope'})

    def test_changed_config_does_not_show_budget_alert_from_wrong_limit(self):
        key=self.seed('changed',99)
        self.assertEqual(self.service.snapshot(key)['budget_alerts'],[])
