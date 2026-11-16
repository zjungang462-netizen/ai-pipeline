"""CLI 流式观测、部分计量、候选恢复和有界重试的行为验收。"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.adapters.cli_agy import AgyAdapter
from orchestrator.adapters.cli_codex import CodexAdapter
from orchestrator.adapters.factory import build_all
from orchestrator.cli_telemetry import CLITrace,StreamRedactor,UsageLedger,partial_usage
from orchestrator.config import parse_config
from orchestrator.contracts import RunContext,Result
from orchestrator.engine import Engine,BudgetExceeded
from orchestrator import artifacts,recovery
from orchestrator.cli_accounting import recover_pending_usage
from orchestrator.state import sha256_file


class CLIReliability(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.workspace=self.root/'workspace'
        self.workspace.mkdir()

    def ctx(self):
        return RunContext('task','run','coding',self.workspace,self.root/'run',kind='code',
                          attempt_dir=self.root/'attempt',timeout_s=3,call_id='coding/1')

    def fixture_config(self,mode='normal',attempts=2,budget=6000):
        fixture=Path(__file__).parent/'fixtures/cli_fault_fixture.py'
        if os.name=='nt':
            executable=self.root/'fake-agy.cmd'
            executable.write_text('@echo off\r\n"'+sys.executable+'" -u "'+str(fixture)+'" %*\r\n',encoding='utf8')
        else:
            executable=self.root/'fake-agy'
            executable.write_text('#!'+sys.executable+'\nexec(compile(open('+repr(str(fixture))+',encoding="utf8").read(),'+repr(str(fixture))+',"exec"))\n',encoding='utf8')
            executable.chmod(0o755)
        data=self.root/'fixture-data'
        data.mkdir(exist_ok=True)
        (self.workspace/'module.py').write_text('VALUE = -1\n',encoding='utf8')
        tests=self.workspace/'tests'
        tests.mkdir(exist_ok=True)
        (tests/'test_module.py').write_text('import unittest\nfrom module import VALUE\nclass Contract(unittest.TestCase):\n def test_value(self): self.assertEqual(VALUE,2)\n',encoding='utf8')
        cfg=parse_config({'task_workdir':str(self.workspace),'state_dir':str(self.root/'state'),
            'run_budget':{'max_tokens':budget,'max_duration_s':90},
            'autonomy':{'enabled':True,'allowed_files':['module.py'],'baseline_paths':['module.py','tests'],
                        'max_repair_rounds':1,'repair_batch_size':1,'output_tokens':128,
                        'cli_input_overhead_tokens':64,'cli_idle_timeout_s':30},
            'stages':[{'name':'coding','kind':'code','adapter':'agy','prompt':'修改module.py使VALUE=2。{upstream}\n{feedback}',
                       'context_files':['module.py'],'max_attempts':attempts,'timeout_s':4},
                      {'name':'testing','kind':'test','test_cmd':[sys.executable,'-X','utf8','-m','unittest','discover','-s','tests','-v'],
                       'feedback_to':'coding','max_rounds':2,'timeout_s':10}],
            'adapters':{'agy':{'type':'cli_agy','exe':str(executable),'extra_env':['CLI_FIXTURE_MODE','CLI_FIXTURE_DATA']}}},base_dir=self.root)
        return cfg,data,{'CLI_FIXTURE_MODE':mode,'CLI_FIXTURE_DATA':str(data)}

    def test_redaction_survives_every_chunk_boundary_and_overlapping_keys(self):
        keys=['abcdefghij','abcdefghij123456789','OTHER_SECRET_12345']
        text='prefix '+keys[1]+' middle '+keys[2]+' suffix'
        for chunk_size in range(1,25):
            redactor=StreamRedactor(keys)
            result=''.join(redactor.feed(text[i:i+chunk_size]) for i in range(0,len(text),chunk_size))+redactor.feed('',final=True)
            self.assertEqual(result,'prefix [REDACTED] middle [REDACTED] suffix')

    def test_partial_ledger_deduplicates_steps_and_final_total(self):
        ledger=UsageLedger()
        step={'event':'step_update','step_update':{'conversation_id':'c','step_index':1,'step_type':'agent_response','state':'DONE',
              'usage':{'total_tokens':20,'cache_read_tokens':30}}}
        ledger.observe(step);ledger.observe(step)
        self.assertEqual(ledger.snapshot()['tokens'],50)
        ledger.observe({'event':'result','result':{'usage':{'total_tokens':20,'cache_read_tokens':30}}})
        self.assertEqual(ledger.snapshot()['tokens'],50)
        self.assertTrue(ledger.snapshot()['usage_complete'])

    def test_codex_cache_is_a_subset_and_does_not_double_count(self):
        event={'type':'turn.completed','usage':{'input_tokens':100,'output_tokens':10,'cached_input_tokens':80}}
        usage=partial_usage(json.dumps(event)+'\n'+json.dumps(event))
        self.assertEqual(usage['known_tokens'],110)
        self.assertEqual(usage['cache_read_tokens'],80)

    def test_codex_final_replayed_turn_is_charged_once_by_adapter(self):
        event={'type':'turn.completed','usage':{'input_tokens':100,'output_tokens':10,'cached_input_tokens':80}}
        raw='\n'.join(json.dumps(e) for e in [
            {'type':'item.completed','item':{'type':'agent_message','text':'done'}},event,event])
        a=CodexAdapter('codex');a._resolved='codex';a._version='codex-cli 0.169.0'
        with mock.patch('orchestrator.adapters.cli_base.run_cli_process',return_value=subprocess.CompletedProcess([],0,raw,'')):
            result=a.run('fixture',self.workspace,self.ctx())
        self.assertEqual(result.usage['tokens'],110)
        self.assertEqual(result.usage['cached_input_tokens'],80)

    def test_tool_errors_stop_but_a_successful_write_resets_read_limit(self):
        ctx=self.ctx();ctx.max_tool_errors=2;ctx.max_repeated_reads=1
        trace=CLITrace(ctx)
        def tool(i,name,state):
            trace.feed('stdout',json.dumps({'event':'step_update','step_update':{'conversation_id':'c',
                'step_index':i,'step_type':'tool','tool_name':name,'state':state,
                'tool_info':{'parameters':{'Path':str(self.workspace/'module.py')}}}})+'\n')
        tool(1,'read_file','ACTIVE');tool(2,'write_file','DONE');tool(3,'read_file','ACTIVE')
        self.assertEqual(trace.tick(),'')
        tool(4,'read_file','ERROR');tool(5,'read_file','ERROR')
        self.assertEqual(trace.tick(),'tool_error_limit');trace.finish('stopped')

    def test_live_files_exist_before_cli_exit_and_never_contain_secret(self):
        cfg,data,env=self.fixture_config('stream',attempts=1)
        cfg.adapters['agy'].options['extra_env'].append('CLI_FIXTURE_API_KEY')
        adapter=build_all(cfg.adapters,{})['agy']
        ctx=self.ctx()
        # 覆盖进程启动和首条完整响应的等待，不能把活动必须可见偷换成 2.5 秒启动 SLA。
        ctx.timeout_s=20
        env['CLI_FIXTURE_API_KEY']='sk-live-test-private-key-1234567890'
        result=[]
        def run():
            with mock.patch.dict(os.environ,env):result.append(adapter.run('do fixture',self.workspace,ctx))
        thread=threading.Thread(target=run)
        thread.start()
        try:
            deadline=time.monotonic()+10
            progress=None
            while time.monotonic()<deadline:
                try:
                    progress=json.loads((ctx.attempt_dir/'cli-progress.json').read_text(encoding='utf8'))
                    if progress['usage']['known_tokens']==30:break
                except (OSError,ValueError):pass
                time.sleep(.03)
            self.assertIsNotNone(progress)
            self.assertTrue(thread.is_alive(),'must inspect during execution')
            self.assertEqual(progress['usage']['known_tokens'],30)
            raw=(ctx.attempt_dir/'live-stdout.log').read_text(encoding='utf8')
            self.assertIn('[REDACTED]',raw)
            self.assertNotIn(env['CLI_FIXTURE_API_KEY'],raw)
        finally:thread.join(timeout=25)
        self.assertFalse(thread.is_alive())
        self.assertTrue(result[0].usage['usage_unknown'])
        self.assertEqual(result[0].usage['known_tokens'],30)

    def test_repeated_tool_reads_stop_before_long_timeout(self):
        cfg,data,env=self.fixture_config('tool_loop')
        adapter=build_all(cfg.adapters,{})['agy']
        ctx=self.ctx();ctx.timeout_s=15;ctx.max_repeated_reads=2
        start=time.monotonic()
        with mock.patch.dict(os.environ,env):result=adapter.run('fixture',self.workspace,ctx)
        self.assertEqual(result.error_kind.value,'tool_limit')
        self.assertLess(time.monotonic()-start,10)
        self.assertEqual(result.usage['known_tokens'],30)

    def test_budget_stop_uses_known_usage_plus_unknown_reservation(self):
        ctx=self.ctx();ctx.remaining_tokens=40;ctx.token_reservation=20
        trace=CLITrace(ctx)
        event={'event':'step_update','step_update':{'conversation_id':'c','step_index':1,'step_type':'agent_response','state':'DONE','usage':{'total_tokens':25}}}
        trace.feed('stdout',json.dumps(event)+'\n')
        self.assertEqual(trace.tick(),'budget_limit')
        trace.finish('stopped')

    def test_normal_cli_fixture_finishes_with_real_tests(self):
        cfg,data,env=self.fixture_config()
        original=sha256_file(self.workspace/'tests/test_module.py')
        engine=Engine(cfg,build_all(cfg.adapters,{}),'normal','small fixture')
        with mock.patch.dict(os.environ,env):self.assertEqual(set(engine.run().values()),{'done'})
        self.assertEqual((data/'calls.txt').read_text(encoding='utf8'),'1')
        self.assertEqual(sha256_file(self.workspace/'tests/test_module.py'),original)
        self.assertEqual(engine.state.usage_total['tokens'],20)

    def test_real_test_failure_returns_to_cli_without_resetting_budget(self):
        cfg,data,env=self.fixture_config('repair')
        engine=Engine(cfg,build_all(cfg.adapters,{}),'repair','repair fixture')
        with mock.patch.dict(os.environ,env):self.assertEqual(set(engine.run().values()),{'done'})
        self.assertEqual((data/'calls.txt').read_text(encoding='utf8'),'2')
        self.assertEqual(engine.state.usage_total['tokens'],40)
        self.assertEqual(engine.state.autonomy['cli_attempts']['coding']['total'],2)
        self.assertEqual(sum(e['kind']=='test_feedback' for e in engine.state.events),1)

    def test_timeout_checkpoint_is_verified_and_restored_for_retry(self):
        cfg,data,env=self.fixture_config('timeout')
        engine=Engine(cfg,build_all(cfg.adapters,{}),'timeout','recover fixture')
        with mock.patch.dict(os.environ,env):self.assertEqual(set(engine.run().values()),{'done'})
        self.assertEqual((data/'calls.txt').read_text(encoding='utf8'),'2')
        self.assertEqual(engine.state.usage_total['reported_tokens'],50)
        self.assertGreater(engine.state.usage_total['reserved_tokens'],0)
        self.assertEqual(engine.state.usage_total['tokens'],50+engine.state.usage_total['reserved_tokens'])
        self.assertEqual(engine.state.usage_total['unknown_calls'],1)
        self.assertTrue((engine.state.attempts_dir('coding')/'1/recovery/checkpoint.json').is_file())
        self.assertTrue((engine.state.attempts_dir('coding')/'2/recovery-applied.json').is_file())
        prompt=(data/'prompt-2.txt').read_text(encoding='utf8')
        self.assertIn('VALUE = 1',prompt)
        self.assertIn('直接编辑',prompt)

    def test_retry_limit_survives_new_engine_and_authorization_failure_does_not_retry(self):
        cfg,data,env=self.fixture_config('auth',attempts=2)
        engine=Engine(cfg,build_all(cfg.adapters,{}),'auth','same task')
        with mock.patch.dict(os.environ,env):self.assertEqual(engine.run()['coding'],'failed')
        self.assertEqual((data/'calls.txt').read_text(encoding='utf8'),'1')
        # 显式续跑允许使用剩余一次，单次运行内鉴权错误绝不自动重试。
        second=Engine(cfg,build_all(cfg.adapters,{}),'auth','same task')
        with mock.patch.dict(os.environ,env):self.assertEqual(second.run()['coding'],'failed')
        self.assertEqual((data/'calls.txt').read_text(encoding='utf8'),'2')
        resumed=Engine(cfg,build_all(cfg.adapters,{}),'auth','same task')
        with mock.patch.dict(os.environ,env):self.assertEqual(resumed.run()['coding'],'failed')
        self.assertEqual((data/'calls.txt').read_text(encoding='utf8'),'2')
        self.assertIn('重试次数',resumed.state.stage('coding').error)

    def test_timeout_attempt_exhaustion_survives_restart_with_usage_and_deadline(self):
        cfg,data,env=self.fixture_config('timeout_always')
        engine=Engine(cfg,build_all(cfg.adapters,{}),'exhaust','same task')
        with mock.patch.dict(os.environ,env):self.assertEqual(engine.run()['coding'],'failed')
        original=dict(engine.state.usage_total);deadline=engine.state.control['cli_deadline']
        resumed=Engine(cfg,build_all(cfg.adapters,{}),'exhaust','same task')
        with mock.patch.dict(os.environ,env):self.assertEqual(resumed.run()['coding'],'failed')
        self.assertEqual((data/'calls.txt').read_text(encoding='utf8'),'2')
        self.assertEqual(resumed.state.usage_total,original)
        self.assertEqual(original['reported_tokens'],60)
        self.assertEqual(original['unknown_calls'],2)
        self.assertEqual(resumed.state.control['cli_deadline'],deadline)

    def recovery_setup(self):
        cfg,_,_=self.fixture_config()
        e=Engine(cfg,build_all(cfg.adapters,{}),'checkpoint','same task');e.prepare()
        spec=cfg.stage('coding');base=artifacts.head_sha(self.workspace)
        n,folder=e.state.new_attempt_dir('coding')
        tree=e.state.stage_dir('coding')/'worktree'/str(n)
        artifacts.add_worktree(self.workspace,base,'fixture-source',tree)
        return e,spec,base,n,folder,tree

    def recovery_target(self,e,base):
        n,folder=e.state.new_attempt_dir('coding')
        target=e.state.stage_dir('coding')/'worktree'/str(n)
        artifacts.add_worktree(self.workspace,base,'fixture-target',target)
        return n,folder,target

    def test_checkpoint_tampering_and_dirty_target_are_rejected(self):
        e,spec,base,n,folder,tree=self.recovery_setup()
        (tree/'module.py').write_text('VALUE = 1\n',encoding='utf8')
        recovery.capture(e,spec,tree,base,n,folder,'timeout')
        number,directory,target=self.recovery_target(e,base)
        (target/'module.py').write_text('local edit\n',encoding='utf8')
        with self.assertRaisesRegex(recovery.RecoveryError,'干净'):recovery.restore(e,spec,target,base,number,directory)
        self.assertEqual((target/'module.py').read_text(encoding='utf8'),'local edit\n')
        (target/'module.py').write_text('VALUE = -1\n',encoding='utf8')
        (folder/'recovery/file-0.bin').write_text('tampered\n',encoding='utf8')
        with self.assertRaisesRegex(recovery.RecoveryError,'完整性'):recovery.restore(e,spec,target,base,number,directory)
        self.assertEqual((target/'module.py').read_text(encoding='utf8'),'VALUE = -1\n')

    def test_checkpoint_is_bound_to_task_config_base_nonce_and_allowlist(self):
        e,spec,base,n,folder,tree=self.recovery_setup()
        (tree/'module.py').write_text('VALUE = 1\n',encoding='utf8')
        recovery.capture(e,spec,tree,base,n,folder,'timeout')
        number,directory,target=self.recovery_target(e,base)
        changes=[(e,'task','different task'),(e.config.autonomy,'output_tokens',256),
                 (e.config.autonomy,'allowed_files',['other.py'])]
        for obj,attr,value in changes:
            with self.subTest(attr=attr):
                old=getattr(obj,attr);setattr(obj,attr,value)
                try:
                    with self.assertRaisesRegex(recovery.RecoveryError,'变化'):recovery.restore(e,spec,target,base,number,directory)
                finally:setattr(obj,attr,old)
        e.state.invalidate_nonce['coding']=1
        with self.assertRaisesRegex(recovery.RecoveryError,'变化'):recovery.restore(e,spec,target,base,number,directory)
        e.state.invalidate_nonce.pop('coding')
        with self.assertRaisesRegex(recovery.RecoveryError,'变化'):recovery.restore(e,spec,target,'0'*40,number,directory)

    def test_scope_violation_cannot_be_captured_or_cleaned(self):
        e,spec,base,n,folder,tree=self.recovery_setup()
        (tree/'tests/test_module.py').write_text('unauthorized edit\n',encoding='utf8')
        self.assertFalse(e._finish_cli_worktree(spec,tree,base,n,folder,'timeout'))
        self.assertTrue(tree.is_dir())
        self.assertEqual(e.state.control['cli_worktrees'][f'coding/{n}']['status'],'retained')
        self.assertFalse((folder/'recovery/checkpoint.json').exists())

    def test_capture_io_failure_preserves_only_copy_and_stops_retry(self):
        cfg,data,env=self.fixture_config('timeout')
        e=Engine(cfg,build_all(cfg.adapters,{}),'save-failure','same task')
        with patch_env(env),mock.patch('orchestrator.recovery.capture',side_effect=OSError('disk full')):
            self.assertEqual(e.run()['coding'],'failed')
        tree=e.state.stage_dir('coding')/'worktree/1'
        self.assertEqual((tree/'module.py').read_text(encoding='utf8'),'VALUE = 1\n')
        self.assertEqual((data/'calls.txt').read_text(encoding='utf8'),'1')
        self.assertIn('已保留',e.state.stage('coding').error)

    def test_successful_code_is_captured_if_post_call_budget_rejects(self):
        cfg,data,env=self.fixture_config()
        e=Engine(cfg,build_all(cfg.adapters,{}),'budget-candidate','same task')
        with patch_env(env),mock.patch.object(e,'_check_budget',side_effect=BudgetExceeded('budget exhausted')):
            self.assertEqual(e.run()['coding'],'failed')
        self.assertTrue((e.state.attempts_dir('coding')/'1/recovery/checkpoint.json').is_file())
        self.assertEqual((e.state.attempts_dir('coding')/'1/recovery/file-0.bin').read_text(encoding='utf8'),'VALUE = 2\n')
        self.assertFalse((e.state.stage_dir('coding')/'worktree/1').exists())

    def test_restart_after_usage_settlement_still_finds_orphan_candidate(self):
        e,spec,base,n,folder,tree=self.recovery_setup()
        ctx=RunContext('same task',e.run_id,'coding',tree,e.state.run_dir,kind='code',
                       attempt_dir=folder,timeout_s=4,token_reservation=100,base_commit=base)
        call=e._reserve_call(spec,n,ctx)
        (tree/'module.py').write_text('VALUE = 1\n',encoding='utf8')
        e._account_usage(Result(ok=False,usage={'usage_unknown':True,'known_tokens':30}),'coding',call)
        resumed=Engine(e.config,build_all(e.config.adapters,{}),e.run_id,'same task');resumed.prepare()
        self.assertTrue((folder/'recovery/checkpoint.json').is_file())
        self.assertFalse(tree.exists())
        self.assertEqual(resumed.state.usage_total['reported_tokens'],30)
        self.assertEqual(resumed.state.usage_total['reserved_tokens'],100)
        resumed.prepare()
        self.assertEqual(resumed.state.usage_total['reported_tokens'],30)

    def test_pending_partial_usage_is_settled_once_after_cancellation_or_crash(self):
        cfg,_,_=self.fixture_config();e=Engine(cfg,build_all(cfg.adapters,{}),'partial','same task');e.prepare()
        spec=cfg.stage('coding');n,folder=e.state.new_attempt_dir('coding')
        ctx=self.ctx();ctx.token_reservation=100;ctx.timeout_s=4
        call=e._reserve_call(spec,n,ctx)
        (folder/'cli-progress.json').write_text(json.dumps({'schema_version':1,'run_id':e.run_id,
            'stage':'coding','call_id':call,'usage':{'known_tokens':30}}),encoding='utf8')
        self.assertEqual(len(recover_pending_usage(e.state)),1)
        self.assertEqual(recover_pending_usage(e.state),[])
        self.assertEqual(e.state.usage_total['tokens'],130)
        self.assertEqual(e.state.usage_total['reported_tokens'],30)
        self.assertEqual(e.state.usage_total['reserved_tokens'],100)
        self.assertEqual(e.state.usage_total['unknown_calls'],1)

    def test_expired_deadline_is_not_extended_by_resume(self):
        cfg,data,env=self.fixture_config()
        engine=Engine(cfg,build_all(cfg.adapters,{}),'deadline','same task')
        engine.prepare()
        engine.state.control['cli_deadline']=time.time()-1
        engine.state.save()
        resumed=Engine(cfg,build_all(cfg.adapters,{}),'deadline','same task')
        with mock.patch.dict(os.environ,env):self.assertEqual(resumed.run()['coding'],'failed')
        self.assertFalse((data/'calls.txt').exists())
        self.assertLess(resumed.state.control['cli_deadline'],time.time())


def patch_env(env):
    return mock.patch.dict(os.environ,env)

if __name__=='__main__':unittest.main()
