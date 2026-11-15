"""无人值守的真实文件、真实测试、失败重试与停止条件。"""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.adapters.base import Adapter
from orchestrator.adapters.openai_compat import OpenAICompatAdapter
from orchestrator.config import ConfigError, load_config, parse_config
from orchestrator.contracts import ErrorKind, Result
from orchestrator.engine import Engine, BudgetExceeded
from orchestrator.state import RunState, sha256_file
from orchestrator.tuning import policy_tune, select_policy_config


class Writer(Adapter):
    channel = 'api'
    name = 'api'
    max_tokens = 512
    def __init__(self, failures=0, wrong=False):
        self.calls=0;self.prompts=[];self.failures=failures;self.wrong=wrong
    def preflight(self):
        from orchestrator.contracts import PreflightReport
        return PreflightReport(self.name,True)
    def run(self,prompt,workdir,ctx):
        self.calls+=1;self.prompts.append(prompt)
        if self.calls<=self.failures:
            return Result(ok=False,error_kind=ErrorKind.TIMEOUT,stderr='temporary',usage={'usage_unknown':True})
        if ctx.kind=='review':
            path=ctx.attempt_dir/'review_verdict.json';path.write_text('{"verdict":"pass","issues":[]}',encoding='utf8')
            report=ctx.attempt_dir/'review.md';report.write_text('pass')
            return Result(ok=True,text='pass',report_files=[report],verdict_file=path,usage={'tokens':10})
        name='tests/test_app.py' if self.wrong else ('a.py' if self.calls%2 else 'b.py')
        path=workdir/name;path.parent.mkdir(exist_ok=True)
        path.write_text('VALUE = 1\n',encoding='utf8')
        return Result(ok=True,text='fixed',changed_files=[str(path)],usage={'tokens':10})


class AutonomyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.ws=self.root/'ws';self.ws.mkdir()
        (self.ws/'a.py').write_text('VALUE = 0\n');(self.ws/'b.py').write_text('VALUE = 0\n')
        (self.ws/'tests').mkdir()
        (self.ws/'tests/test_app.py').write_text('''import unittest
from a import VALUE as A
from b import VALUE as B
class Check(unittest.TestCase):
 def test_a(self): self.assertEqual(A,1)
 def test_b(self): self.assertEqual(B,1)
''',encoding='utf8')
    def config(self, **policy):
        raw={'task_workdir':'ws','state_dir':'.s',
             'autonomy':{'enabled':True,'allowed_files':['a.py','b.py'],
                         'baseline_paths':['a.py','b.py','tests'],'max_repair_rounds':3,
                         'repair_batch_size':1,**policy},
             'run_budget':{'max_tokens':20000,'max_duration_s':60},
             'adapters':{'api':{'type':'fake'}},'stages':[
                 {'name':'coding','adapter':'api','kind':'code','prompt':'{task}\n{upstream}\n{feedback}',
                  'context_files':['a.py','b.py'],'max_attempts':2,
                  'precheck_cmd':[sys.executable,'-m','unittest','discover','-s','tests','-v']},
                 {'name':'testing','kind':'test','test_cmd':[sys.executable,'-m','unittest','discover','-s','tests','-v'],
                  'feedback_to':'coding','max_rounds':3},
                 {'name':'review','adapter':'api','kind':'review','prompt':'{upstream}',
                  'feedback_to':'coding','max_rounds':2}]}
        return parse_config(raw,base_dir=self.root)
    def test_real_two_cycle_convergence_and_resume_without_model_calls(self):
        cfg=self.config();ad=Writer();engine=Engine(cfg,{'api':ad},'r','fix both')
        result=engine.run()
        self.assertEqual(set(result.values()),{'done'})
        self.assertEqual(ad.calls,3)
        self.assertIn('VALUE = 1',ad.prompts[1])
        self.assertTrue(any(e['kind']=='test_feedback' for e in engine.state.events))
        self.assertEqual(engine.state.usage_total['tokens'],30)
        second=Writer();resumed=Engine(cfg,{'api':second},'r','')
        self.assertEqual(set(resumed.run().values()),{'done'})
        self.assertEqual(second.calls,0)
    def test_scope_violation_stops_and_preserves_test(self):
        cfg=self.config();before=(self.ws/'tests/test_app.py').read_bytes()
        ad=Writer(wrong=True);engine=Engine(cfg,{'api':ad},'r','fix')
        self.assertEqual(engine.run()['coding'],'failed')
        self.assertEqual(ad.calls,1);self.assertEqual((self.ws/'tests/test_app.py').read_bytes(),before)
    def test_unknown_usage_reserved_and_retry_gets_recovery_hint(self):
        cfg=self.config();ad=Writer(failures=1);engine=Engine(cfg,{'api':ad},'r','fix')
        engine.run()
        self.assertGreater(engine.state.usage_total['reserved_tokens'],0)
        self.assertEqual(engine.state.usage_total['unknown_calls'],1)
        self.assertIn('temporary',ad.prompts[1])
        self.assertTrue((engine.state.attempts_dir('coding')/'1/execution.json').is_file())
    def test_total_repair_limit_stops_without_infinite_loop(self):
        cfg=self.config(max_repair_rounds=1)
        class Broken(Writer):
            def run(self,prompt,workdir,ctx):
                self.calls+=1
                path=workdir/'a.py';path.write_text(f'VALUE = {self.calls+2}\n')
                return Result(ok=True,changed_files=[str(path)],usage={'tokens':10})
        engine=Engine(cfg,{'api':Broken()},'r','fix')
        self.assertEqual(engine.run()['testing'],'failed')
        self.assertIn('总返工',engine.state.stage('testing').error)
    def test_explicit_approval_policy_and_rejected_implicit_bypass(self):
        cfg=self.config(auto_approve=['coding']);cfg.stages[0].approval=True
        engine=Engine(cfg,{'api':Writer()},'r','fix')
        self.assertEqual(set(engine.run().values()),{'done'})
        self.assertTrue(any(e['kind']=='policy_approved' for e in engine.state.events))
        with self.assertRaises(ConfigError):self.config(allowed_files=[])
    def test_literal_template_markers_in_task_are_not_reinterpreted(self):
        cfg=self.config();engine=Engine(cfg,{'api':Writer()},'r','literal {feedback}')
        prompt=engine._render_prompt(cfg.stages[0],0)
        self.assertIn('literal {feedback}',prompt)
    def test_crash_keeps_pending_reservation(self):
        cfg=self.config();engine=Engine(cfg,{'api':Writer()},'r','fix');engine.prepare()
        engine.state.usage_total['tokens']=500
        engine.state.autonomy['pending_calls']={'coding/1':{'stage':'coding','tokens':500,'started_at':0,'timeout_s':1}}
        engine.state.save();resumed=Engine(cfg,{'api':Writer()},'r','');resumed.prepare()
        self.assertEqual(resumed.state.usage_total['tokens'],500)
        self.assertEqual(resumed.state.usage_total['unknown_calls'],1)
        self.assertFalse(resumed.state.autonomy.get('pending_calls'))
    def test_review_plain_json_and_length_rejected(self):
        ad=OpenAICompatAdapter('api','https://example.invalid','m','AUTONOMY_TEST_KEY')
        from orchestrator.contracts import RunContext
        ctx=RunContext('t','r','review',self.ws,self.root/'run',kind='review',output_tokens=128)
        for reason,expected in [('stop',True),('length',False)]:
            body=io.BytesIO(json.dumps({'choices':[{'message':{'content':'{"verdict":"pass","issues":[]}'},'finish_reason':reason}],'usage':{'total_tokens':10}}).encode())
            with mock.patch.dict('os.environ',{'AUTONOMY_TEST_KEY':'dummy'}),mock.patch('urllib.request.urlopen',return_value=body):
                result=ad.run('p',self.ws,ctx)
            self.assertEqual(result.ok,expected)
            if expected:self.assertTrue(result.verdict_file.is_file())
    def test_unparsed_review_retries_then_remains_failed_on_resume(self):
        class Unparsed(Writer):
            def run(self,prompt,workdir,ctx):
                if ctx.kind=='review':
                    self.calls+=1
                    path=ctx.attempt_dir/'report.md';path.write_text('no conclusion')
                    return Result(ok=True,report_files=[path],text='no conclusion',usage={'tokens':10})
                return super().run(prompt,workdir,ctx)
        cfg=self.config();ad=Unparsed();engine=Engine(cfg,{'api':ad},'r','fix')
        self.assertEqual(engine.run()['review'],'failed')
        self.assertEqual(ad.calls,4)
        second=Unparsed();resumed=Engine(cfg,{'api':second},'r','')
        self.assertEqual(resumed.run()['review'],'failed')
        self.assertEqual(second.calls,0)
        self.assertFalse(resumed.state.pending_approvals)


if __name__=='__main__':unittest.main()
