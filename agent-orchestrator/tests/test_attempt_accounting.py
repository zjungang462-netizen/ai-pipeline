"""失败、超时、恢复与审批的 attempt 数量必须可靠且不重复。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from orchestrator.adapters.base import Adapter
from orchestrator.config import parse_config
from orchestrator.contracts import ErrorKind,PreflightReport,Result,StageStatus
from orchestrator.engine import Engine
from orchestrator.state import RunState,atomic_write_json


class AlwaysFails(Adapter):
    channel='fake'
    def preflight(self):return PreflightReport(adapter='failure',ok=True)
    def run(self,prompt,workdir,ctx):
        return Result(ok=False,exit_code=7,error_kind=ErrorKind.CRASH,stderr='injected failure')


class AttemptAccounting(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)

    def config(self,stage,budget=None):
        return parse_config({'task_workdir':'ws','state_dir':'state','adapters':{'fake':{'type':'fake'}},
                             'stages':[stage],**({'run_budget':budget} if budget else {})},base_dir=self.root)

    def test_new_attempt_survives_reload_before_adapter_execution(self):
        state=RunState(self.root,'run')
        number,directory=state.new_attempt_dir('coding')
        loaded=RunState(self.root,'run');self.assertTrue(loaded.load())
        rows=loaded.stage('coding').attempts
        self.assertEqual([(r.n,r.path,r.success) for r in rows],[(number,str(directory),False)])
        self.assertIsNone(loaded.stage('coding').current_attempt)

    def test_success_updates_record_instead_of_appending_duplicate(self):
        state=RunState(self.root,'run')
        number,directory=state.new_attempt_dir('testing')
        (directory/'report.txt').write_text('ok',encoding='utf8')
        state.commit_success('testing',number,directory,'input','ignored',None,None)
        self.assertEqual(len(state.stage('testing').attempts),1)
        self.assertTrue(state.stage('testing').attempts[0].success)
        loaded=RunState(self.root,'run');loaded.load()
        self.assertEqual(len(loaded.stage('testing').attempts),1)

    def test_failed_model_attempts_and_metrics_include_retries(self):
        stage={'name':'report','kind':'report','adapter':'fake','prompt':'report','max_attempts':3}
        cfg=self.config(stage)
        engine=Engine(cfg,{'fake':AlwaysFails()},run_id='failure',task='test')
        self.assertEqual(engine.run()['report'],'failed')
        self.assertEqual([r.success for r in engine.state.stage('report').attempts],[False]*3)
        metrics=json.loads((engine.state.run_dir/'metrics.json').read_bytes())
        self.assertEqual(metrics['stages']['report']['attempts'],3)
        self.assertEqual(metrics['retries'],2)
        self.assertEqual(metrics['failed_attempts'],3)

    def test_timeout_is_recorded_and_pre_call_budget_rejection_is_not_attempt(self):
        stage={'name':'testing','kind':'test','test_cmd':[sys.executable,'-c','import time; time.sleep(3)'],'timeout_s':0.1}
        cfg=self.config(stage)
        engine=Engine(cfg,{},run_id='timeout',task='test')
        self.assertEqual(engine.run()['testing'],'failed')
        self.assertEqual(len(engine.state.stage('testing').attempts),1)
        self.assertFalse(engine.state.stage('testing').attempts[0].success)
        cfg=self.config(stage,{'max_tokens':0,'max_duration_s':0})
        blocked=Engine(cfg,{},run_id='budget',task='test')
        self.assertEqual(blocked.run()['testing'],'failed')
        self.assertEqual(blocked.state.stage('testing').attempts,[])

    def test_orphan_attempt_recovered_as_failed_without_fabricating_success(self):
        state=RunState(self.root,'run');state.stage('report');state.save()
        orphan=state.attempts_dir('report')/'3';orphan.mkdir(parents=True)
        loaded=RunState(self.root,'run');loaded.load();loaded.scan_interrupted()
        self.assertEqual([(r.n,r.success) for r in loaded.stage('report').attempts],[(3,False)])
        reloaded=RunState(self.root,'run');reloaded.load()
        self.assertEqual(len(reloaded.stage('report').attempts),1)

    def test_manual_approval_updates_same_attempt_record(self):
        cfg=self.config({'name':'testing','kind':'test','test_cmd':[sys.executable,'-c','pass'],'approval':True})
        engine=Engine(cfg,{},run_id='approval',task='test')
        self.assertEqual(engine.run()['testing'],'awaiting_approval')
        rows=engine.state.stage('testing').attempts
        self.assertEqual(len(rows),1)
        self.assertTrue(engine.approve('testing'))
        self.assertTrue(rows[0].success)
        self.assertEqual(len(engine.state.stage('testing').attempts),1)
