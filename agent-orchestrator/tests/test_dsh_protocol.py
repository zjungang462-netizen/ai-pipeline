"""dsh 真机 JSON 事件重放、用量和实际完成约束。"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from orchestrator.adapters.cli_dsh import DshAdapter
from orchestrator.adapters.factory import build_adapter
from orchestrator.config import AdapterConfig
from orchestrator.contracts import RunContext,ErrorKind
from orchestrator.cli_telemetry import UsageLedger,CLITrace

class DshProtocol(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name)
        self.ctx=RunContext('fixture','r','tester',self.root,self.root,kind='report',attempt_dir=self.root/'a')
        self.a=DshAdapter('dsh');self.a._resolved='dsh';self.a._version='0.2.2-rc.3'
        self.raw=(Path(__file__).parent/'fixtures/dsh_0.2.2-rc.3_real.jsonl').read_text(encoding='utf8')
    def parse(self,raw=None,code=0):
        return self.a._parse(subprocess.CompletedProcess([],code,self.raw if raw is None else raw,''),self.root,self.ctx)
    def test_real_final_and_usage_without_cache_double_count(self):
        r=self.parse();self.assertTrue(r.ok);self.assertEqual(r.text,'DSH_PROBE_OK_20261116')
        self.assertEqual(r.usage['tokens'],6545);self.assertFalse(r.usage['usage_unknown'])
    def test_step_replay_is_deduplicated_and_cache_already_in_total(self):
        e={'type':'status','phase':'step_end','turn':1,'step':1,
           'usage':{'inputTokens':187,'outputTokens':2504,'cacheReadTokens':10496,'totalTokens':13187}}
        ledger=UsageLedger();ledger.observe(e);ledger.observe(e)
        self.assertEqual(ledger.snapshot()['tokens'],13187)
        self.assertEqual(ledger.snapshot()['cache_read_tokens'],10496)
    def test_missing_final_and_error_final_are_never_success(self):
        events=[json.loads(s) for s in self.raw.splitlines()]
        missing='\n'.join(json.dumps(e) for e in events if e['type']!='final')
        self.assertFalse(self.parse(missing).ok)
        error=json.dumps({'type':'final','reason':{'kind':'error','error':{'code':'MISSING_CREDENTIAL'}},'text':''})
        self.assertFalse(self.parse(error).ok);self.assertEqual(self.parse(error,1).error_kind,ErrorKind.AUTH)
    def test_unknown_version_and_duplicate_final_are_rejected(self):
        self.a._version='9.9';self.assertTrue(self.parse().parse_unknown)
        self.a._version='0.2.2-rc.3';self.assertFalse(self.parse(self.raw+self.raw.splitlines()[-1]).ok)
    def test_empty_output_is_never_success_even_after_workspace_edits(self):
        for version in ('0.2.2-rc.3','unverified legacy'):
            self.a._version=version
            result=self.parse(' \n\t')
            self.assertFalse(result.ok)
            self.assertTrue(result.parse_unknown)
            self.assertEqual(result.error_kind,ErrorKind.PARSE_FAILED)
    def test_unknown_version_stops_before_model_call(self):
        with mock.patch('orchestrator.adapters.cli_base.resolve_exe', return_value='dsh'), \
                mock.patch('orchestrator.adapters.cli_base.run_cli_process', return_value=
                           subprocess.CompletedProcess([], 0, '9.9', '')) as execute:
            self.a._version = None
            result = self.a.run('private prompt', self.root, self.ctx)
        self.assertTrue(result.parse_unknown)
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[0], ['dsh', '--version'])
    def test_stdin_flags_and_explicit_environment_remain_separate(self):
        a=build_adapter(AdapterConfig('dsh','cli_dsh',{'permission_mode':'workspace-write',
               'proxy_url':'http://127.0.0.1:7897','extra_env':['DEEPSEEK_API_KEY']}))
        a._resolved='dsh';a._version='0.2.2-rc.3'
        with mock.patch('orchestrator.adapters.cli_base.run_cli_process',return_value=subprocess.CompletedProcess([],0,self.raw,'')) as execute:
            r=a.run('private prompt',self.root,self.ctx)
        args=execute.call_args.args[0];self.assertEqual(args[-2:],['--','-'])
        self.assertNotIn('private prompt',args);self.assertIn('workspace-write',args)
        self.assertEqual(execute.call_args.kwargs['input'],'private prompt');self.assertTrue(r.ok)
    def test_no_full_access_or_proxy_embedded_credentials(self):
        with self.assertRaises(ValueError):DshAdapter('dsh',permission_mode='full-access')
        with self.assertRaises(ValueError):DshAdapter('dsh',proxy_url='http://'+'user:secret@localhost')

    def test_legacy_plaintext_can_be_archived_but_never_completes_stage(self):
        self.a._version='unverified legacy'
        result=self.parse('legacy diagnostics')
        result=self.a._finalize(result,self.ctx)
        self.assertTrue(result.report_files)
        self.assertTrue(result.parse_unknown)
        self.assertFalse(result.is_meaningful('report'))
    def test_tool_progress_exposes_summary_and_not_thinking(self):
        trace=CLITrace(self.ctx)
        trace.feed('stdout',json.dumps({'type':'thinking','text':'private test sentinel'})+'\n')
        trace.feed('stdout',json.dumps({'type':'tool_call','callId':'x','tool':'pwsh','input':{'command':'python tests'}})+'\n')
        self.assertEqual(trace.snapshot()['tool'],'pwsh')
        self.assertNotIn('private test sentinel',json.dumps(trace.snapshot()))
        trace.finish('exited')

    def test_dsh_successful_edit_resets_repeated_reads_by_call_identity(self):
        self.ctx.max_repeated_reads=1;trace=CLITrace(self.ctx)
        def call(identifier,tool):
            trace.feed('stdout',json.dumps({'type':'tool_call','callId':identifier,'tool':tool,'input':{'path':str(self.root/'a.py')}})+'\n')
        call('read-1','read');call('edit-1','edit')
        trace.feed('stdout',json.dumps({'type':'tool_result','callId':'edit-1','status':'completed','result':'changed'})+'\n')
        call('read-2','read');self.assertEqual(trace.tick(),'')
        call('read-3','read');self.assertEqual(trace.tick(),'repeated_read_limit');trace.finish('stopped')

if __name__=='__main__':unittest.main()
