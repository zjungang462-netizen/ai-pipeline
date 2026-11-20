"""Qoder真实成功/工具协议，未知token不视为免费，模型与权限参数固定。"""
import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.adapters.cli_qoder import QoderAdapter
from orchestrator.adapters.cli_registry import build
from orchestrator.config import AdapterConfig,parse_config
from orchestrator.contracts import ErrorKind,RunContext,Result
from orchestrator.cli_telemetry import CLITrace
from orchestrator.engine import Engine
from orchestrator.collaboration.model_settings import validate_options


class Qoder1189(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        fixtures=Path(__file__).parent/'fixtures'
        self.final=json.loads((fixtures/'qoder_1.1.89_real.json').read_bytes())
        self.events=[json.loads(line) for line in (fixtures/'qoder_1.1.89_tools_real.jsonl').read_text(encoding='utf8').splitlines()]
        self.adapter=QoderAdapter('qoder');self.adapter._version='1.1.89'
        self.ctx=RunContext(task='test',run_id='run',stage='coding',workdir=self.root,runs_dir=self.root,
                            attempt_dir=self.root/'attempt',kind='code')

    def parse(self,events,code=0):
        raw=json.dumps(events) if isinstance(events,dict) else '\n'.join(json.dumps(e) for e in events)
        return self.adapter._parse(subprocess.CompletedProcess([],code,raw,''),self.root,self.ctx)

    def test_real_json_success_retains_credits_marks_zero_tokens_unknown(self):
        result=self.parse(self.final)
        self.assertTrue(result.ok);self.assertEqual(result.text,'QODER_TRIAL_OK_20261117')
        self.assertTrue(result.usage['usage_unknown']);self.assertEqual(result.usage['tokens'],0)
        self.assertAlmostEqual(result.usage['qoder_credits'],.03636524674285714)
        self.assertNotIn('cost',result.usage)

    def test_real_stream_validates_init_and_final(self):
        result=self.parse(self.events)
        self.assertTrue(result.ok);self.assertIn('QODER_FILE_EDIT_OK',result.text)
        self.assertGreater(result.usage['qoder_credits'],0)

    def test_missing_duplicate_trailing_wrong_version_error_never_complete(self):
        wrong=copy.deepcopy(self.events);wrong[0]['qodercli_version']='1.1.90'
        for events in (self.events[:-1],self.events+[self.events[-1]],self.events+[{'type':'assistant'}],wrong):
            with self.subTest(events=len(events)):
                result=self.parse(events);self.assertFalse(result.ok);self.assertTrue(result.parse_unknown)
        for changes in ({'is_error':True},{'stop_reason':'max_tokens'},
                        {'permission_denials':[{'tool_name':'Edit'}]},{'subtype':'error_max_turns'}):
            self.assertFalse(self.parse({**self.final,**changes}).ok)
        self.adapter._version='1.1.90';self.assertFalse(self.parse(self.final).ok)

    def test_nonzero_exit_preserves_reported_credits_and_fails(self):
        result=self.parse(self.final,7)
        self.assertFalse(result.ok);self.assertEqual(result.exit_code,7)
        self.assertGreater(result.usage['qoder_credits'],0)

    def test_invalid_counters_and_unstructured_success_rejected(self):
        for value in (-1,False,'0'):
            data=copy.deepcopy(self.final);data['usage']['input_tokens']=value
            self.assertFalse(self.parse(data).ok)
        for value in (-1,float('nan'),True):
            self.assertFalse(self.parse({**self.final,'total_credits':value}).ok)
        self.assertFalse(self.parse({**self.final,'permission_denials':{}}).ok)
        self.assertFalse(self.parse({'result':'claims success'}).ok)

    def test_preflight_requires_successful_exact_version_from_stdout(self):
        adapter=QoderAdapter('qoder')
        cases=[(0,'1.1.89\n','',True),(1,'1.1.89','',False),
               (0,'','1.1.89',False),(0,'qoder 1.1.90','',False),(0,'','',False)]
        with mock.patch('orchestrator.adapters.cli_qoder.resolve_exe',return_value='qoder'):
            for code,stdout,stderr,expected in cases:
                with self.subTest(code=code,stdout=stdout,stderr=stderr), mock.patch(
                    'orchestrator.adapters.cli_qoder.run_cli_process',
                    return_value=subprocess.CompletedProcess([],code,stdout,stderr),
                ) as probe:
                    report=adapter.preflight()
                    self.assertEqual(report.ok,expected)
                    self.assertEqual(report.tool_version,'1.1.89' if expected else None)
                    self.assertEqual(probe.call_args.args[0],['qoder','--version'])
                    self.assertEqual(probe.call_args.kwargs['timeout'],30)

    def test_timeout_does_not_fall_back_to_model_call(self):
        self.adapter._resolved=None
        self.ctx.allow_yolo=True
        with mock.patch('orchestrator.adapters.cli_qoder.resolve_exe',return_value='qoder'), mock.patch(
            'orchestrator.adapters.cli_qoder.run_cli_process',
            side_effect=subprocess.TimeoutExpired(['qoder','--version'],30),
        ) as probe, mock.patch.object(self.adapter,'_exec') as execute:
            result=self.adapter.run('model request',self.root,self.ctx)
        self.assertFalse(result.ok)
        self.assertTrue(result.parse_unknown)
        self.assertIsNone(result.tool_version)
        probe.assert_called_once()
        execute.assert_not_called()

    def test_arguments_bind_model_effort_and_only_allow_file_tools(self):
        adapter=QoderAdapter('q',model='Ultimate',reasoning_effort='high',thinking='adaptive')
        adapter._version='1.1.89';adapter._resolved='qoder'
        with mock.patch.object(adapter,'_exec',return_value=Result(ok=False)) as execute:
            adapter.run('PRIVATE PROMPT',self.root,self.ctx)
        args=execute.call_args.args[0]
        self.assertNotIn('PRIVATE PROMPT',args);self.assertNotIn('--yolo',args)
        self.assertEqual(args[args.index('--tools')+1],'Read,Glob,Grep,Edit,Write')
        self.assertEqual(args[args.index('--model')+1],'Ultimate')
        self.assertEqual(execute.call_args.kwargs['stdin_data'],'PRIVATE PROMPT')
        self.ctx.kind='review'
        with mock.patch.object(adapter,'_exec',return_value=Result(ok=False)) as execute:
            adapter.run('review',self.root,self.ctx)
        self.assertEqual(execute.call_args.args[0][execute.call_args.args[0].index('--tools')+1],'Read,Glob,Grep')

    def test_real_tool_events_map_read_edit_and_result_without_exposing_parameters(self):
        self.ctx.attempt_dir.mkdir()
        trace=CLITrace(self.ctx,channel='cli_qoder')
        for event in self.events:trace.feed('stdout',json.dumps(event)+'\n')
        trace.finish('exited')
        progress=json.loads((self.ctx.attempt_dir/'cli-progress.json').read_bytes())
        self.assertEqual(trace.tool_errors,0)
        self.assertTrue(trace.qoder_tools)
        self.assertNotIn('old_string',json.dumps(progress))

    def test_repeated_reads_and_failed_tool_events_use_existing_stop_guards(self):
        self.ctx.max_repeated_reads=1;self.ctx.max_tool_errors=1
        trace=CLITrace(self.ctx,channel='cli_qoder')
        for i in range(2):
            event={'type':'assistant','session_id':'s','message':{'content':[{'type':'tool_use','id':str(i),'name':'Read','input':{'file_path':str(self.root/'module.py')}}]}}
            trace.feed('stdout',json.dumps(event)+'\n')
        self.assertEqual(trace.tick(),'repeated_read_limit');trace.finish('stopped')
        trace=CLITrace(self.ctx,channel='cli_qoder')
        trace.feed('stdout',json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','id':'e','name':'Edit','input':{}}]}})+'\n')
        trace.feed('stdout',json.dumps({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'e','is_error':True,'content':'error'}]}})+'\n')
        self.assertEqual(trace.tick(),'tool_error_limit');trace.finish('stopped')

    def test_credits_account_once_and_console_supports_qoder(self):
        cfg=parse_config({'task_workdir':'ws','state_dir':'state','run_budget':{'unlimited_tokens':True,'max_duration_s':60},
              'adapters':{'q':{'type':'cli_qoder','model':'Ultimate','reasoning_effort':'max','thinking':'adaptive'}},
              'stages':[{'name':'report','kind':'report','adapter':'q','prompt':'read'}]},base_dir=self.root)
        adapter=build(cfg.adapters['q']);self.assertEqual(adapter.model,'Ultimate')
        clean=validate_options(cfg,{'models':{'q':{'model':'Ultimate','reasoning_effort':'max','thinking':'adaptive'}}})
        self.assertEqual(clean['models']['q']['thinking'],'adaptive')
        with self.assertRaises(ValueError):validate_options(cfg,{'models':{'q':{'thinking':'enabled'}}})
        engine=Engine(cfg,{'q':adapter},'credits','task')
        engine.budget.account_usage(Result(ok=True,usage={'tokens':0,'usage_unknown':True,'qoder_credits':2.5}),'report')
        self.assertEqual(engine.state.usage_total['qoder_credits'],2.5)
        self.assertEqual(engine.state.stage_usage['report']['qoder_credits'],2.5)
        self.assertEqual(engine.state.stage_usage['report']['unknown_calls'],1)
        from orchestrator.reporting import usage_summary,usage_csv
        summary=usage_summary('run',{'usage_total':{'tokens':0,'unknown_calls':1,'qoder_credits':2.5},
                   'stages':{'report':{'status':'done'}},'stage_usage':engine.state.stage_usage})
        self.assertEqual(summary['token_status'],'incomplete')
        self.assertEqual(summary['stages'][0]['unknown_calls'],1)
        self.assertIn('incomplete',usage_csv({'runs':[summary]}))
