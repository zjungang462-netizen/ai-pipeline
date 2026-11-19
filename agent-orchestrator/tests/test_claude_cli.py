"""Claude Code/DeepSeek真机夹具、连接隔离与保守终态/计量校验。"""
import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.adapters.cli_claude import ClaudeAdapter
from orchestrator.config import parse_config
from orchestrator.contracts import RunContext,Result
from orchestrator.cli_telemetry import CLITrace,UsageLedger
from orchestrator.collaboration.model_settings import validate_options,describe_models


class ClaudeCLI(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        fixtures=Path(__file__).parent/'fixtures'
        self.result=[json.loads(line) for line in (fixtures/'claude_2.1.325_result_real.jsonl').read_text(encoding='utf8').splitlines()]
        self.tools=[json.loads(line) for line in (fixtures/'claude_2.1.325_tools_real.jsonl').read_text(encoding='utf8').splitlines()]
        self.adapter=ClaudeAdapter('claude');self.adapter._version='2.1.325 (Claude Code)'
        self.ctx=RunContext(task='test',run_id='test',stage='coding',kind='code',workdir=self.root,runs_dir=self.root,attempt_dir=self.root/'attempt')

    def parse(self,events,exit_code=0):
        proc=subprocess.CompletedProcess([],exit_code,'\n'.join(json.dumps(e) for e in events),'')
        return self.adapter._parse(proc,self.root,self.ctx)

    def profile(self,env):
        folder=self.root/'.claude';folder.mkdir(exist_ok=True)
        path=folder/'settings.json'
        path.write_text(json.dumps({'env':env,'effortLevel':'max','hooks':{'not_inherited':'PRIVATE'},'mcpServers':{'not_inherited':{}}}),encoding='utf8')
        return path

    def test_real_result_identifies_tool_and_actual_backend(self):
        result=self.parse(self.result)
        self.assertTrue(result.ok);self.assertEqual(result.text,'CLAUDE_DEEPSEEK_PRO_MAX_OK')
        self.assertEqual(result.usage['tokens'],209)
        self.assertIn('deepseek-v4-pro',result.usage['backend_model'])
        self.assertEqual(result.usage['tool'],'Claude Code');self.assertFalse(result.usage['usage_unknown'])
        self.assertNotIn('cost',result.usage)

    def test_tool_result_usage_does_not_double_count_cache_or_thinking(self):
        result=self.parse(self.tools)
        self.assertTrue(result.ok);self.assertEqual(result.usage['tokens'],3592)
        self.assertEqual(result.usage['cache_read_tokens'],2048)
        ledger=UsageLedger()
        for event in self.tools:
            ledger.observe_claude(event);ledger.observe_claude(event)
        self.assertEqual(ledger.snapshot()['tokens'],3592)
        self.assertTrue(ledger.snapshot()['usage_complete'])

    def test_missing_duplicate_wrong_version_denied_or_nonzero_never_complete(self):
        wrong=copy.deepcopy(self.result)
        next(e for e in wrong if e.get('subtype')=='init')['claude_code_version']='2.1.326'
        for events in (self.result[:-1],self.result+[self.result[-1]],wrong,self.result+[{'type':'assistant'}]):
            self.assertFalse(self.parse(events).ok)
        for key,value in (('is_error',True),('terminal_reason','interrupted'),('stop_reason','max_tokens'),('permission_denials',[{}]),('safety_stops',1)):
            events=copy.deepcopy(self.result);events[-1][key]=value
            self.assertFalse(self.parse(events).ok)
        self.assertFalse(self.parse(self.result,7).ok)
        self.adapter._version='2.1.326 (Claude Code)';self.assertFalse(self.parse(self.result).ok)

    def test_partial_and_invalid_usage_never_becomes_free_or_complete(self):
        for value in (-1,True,'171'):
            events=copy.deepcopy(self.result);events[-1]['usage']['input_tokens']=value
            self.assertFalse(self.parse(events).ok)
        events=copy.deepcopy(self.result);events[-1]['usage'].pop('input_tokens')
        result=self.parse(events);self.assertTrue(result.usage['usage_unknown'])
        ledger=UsageLedger()
        for e in self.tools[:-1]:ledger.observe_claude(e)
        self.assertGreater(ledger.snapshot()['known_tokens'],0);self.assertFalse(ledger.snapshot()['usage_complete'])

    def test_profile_inherits_only_connection_fields_no_credentials_in_fingerprint(self):
        secret='private-connection-value'
        path=self.profile({'ANTHROPIC_API_KEY':secret,'ANTHROPIC_BASE_URL':'https://api.deepseek.com/anthropic',
                           'CLAUDE_CODE_EFFORT_LEVEL':'max',
                           'ANTHROPIC_DEFAULT_OPUS_MODEL':'deepseek-v4-pro[1M]','ARBITRARY_SECRET':'not_inherited'})
        before=path.read_bytes()
        with mock.patch('pathlib.Path.home',return_value=self.root):
            env,model,effort=self.adapter._profile();fp=self.adapter.input_fingerprint()
        self.assertEqual(model,'deepseek-v4-pro[1M]');self.assertEqual(effort,'max')
        self.assertNotIn('ARBITRARY_SECRET',env);self.assertNotIn(secret,json.dumps(fp))
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(fp['backend_endpoint'],'https://api.deepseek.com/anthropic')
        settings=json.loads(path.read_bytes());settings['effortLevel']='low';path.write_text(json.dumps(settings),encoding='utf8')
        with mock.patch('pathlib.Path.home',return_value=self.root):
            self.assertEqual(self.adapter._profile()[2],'max')
            alias=ClaudeAdapter('c',model='opus').input_fingerprint()
        settings['env']['ANTHROPIC_DEFAULT_OPUS_MODEL']='different-backend-model'
        path.write_text(json.dumps(settings),encoding='utf8')
        with mock.patch('pathlib.Path.home',return_value=self.root):
            self.assertNotEqual(alias,ClaudeAdapter('c',model='opus').input_fingerprint())

    def test_profile_rejects_bad_endpoints_and_can_disable_inheritance(self):
        for endpoint in ('https://' 'user:pass@example.invalid','file:///secret','https://example.invalid/?key=private'):
            self.profile({'ANTHROPIC_BASE_URL':endpoint})
            with mock.patch('pathlib.Path.home',return_value=self.root),self.assertRaises(ValueError):self.adapter._profile()
        with mock.patch('pathlib.Path.home',side_effect=AssertionError('inherit disabled')):
            env,model,effort=ClaudeAdapter('c',inherit_connection=False)._profile()
        self.assertEqual(env,{})
        with mock.patch.dict('os.environ',{'ANTHROPIC_BASE_URL':'https://api.deepseek.com/anthropic','ANTHROPIC_API_KEY':'process-private'}):
            adapter=ClaudeAdapter('c',inherit_connection=False,extra_env=['ANTHROPIC_BASE_URL','ANTHROPIC_API_KEY'])
            fp=adapter.input_fingerprint()
            self.assertEqual(fp['backend_endpoint'],'https://api.deepseek.com/anthropic')
            self.assertNotIn('process-private',json.dumps(fp))

    def test_command_sends_secret_only_in_environment_and_restricts_tools(self):
        secret='private-connection-value'
        self.profile({'ANTHROPIC_API_KEY':secret,'ANTHROPIC_BASE_URL':'https://api.deepseek.com/anthropic','ANTHROPIC_DEFAULT_OPUS_MODEL':'deepseek-v4-pro[1M]'})
        adapter=ClaudeAdapter('c',reasoning_effort='max')
        adapter._resolved='fixture-claude'
        adapter._version='2.1.325 (Claude Code)'
        with mock.patch('pathlib.Path.home',return_value=self.root),mock.patch.object(adapter,'_exec',return_value=Result(ok=False)) as execute:
            adapter.run('PRIVATE_PROMPT',self.root,self.ctx)
        args=execute.call_args.args[0];kwargs=execute.call_args.kwargs
        self.assertNotIn(secret,args);self.assertNotIn('PRIVATE_PROMPT',args)
        self.assertEqual(kwargs['env_overrides']['ANTHROPIC_API_KEY'],secret)
        self.assertEqual(kwargs['stdin_data'],'PRIVATE_PROMPT')
        self.assertIn('--restricted',args);self.assertIn('--bare',args)
        self.assertEqual(args[args.index('--tools')+1],'Read,Glob,Grep,Edit,Write')
        self.assertNotIn('--dangerously-skip-permissions',args)
        self.assertEqual(kwargs['env_overrides']['ANTHROPIC_BASE_URL'],'https://api.deepseek.com/anthropic')

    def test_unknown_version_is_rejected_before_model_call(self):
        adapter = ClaudeAdapter('claude')
        with mock.patch('orchestrator.adapters.cli_claude.resolve_exe', return_value='claude'), \
                mock.patch('orchestrator.adapters.cli_claude.run_cli_process',
                           return_value=subprocess.CompletedProcess([], 0, '2.1.999 (Claude Code)', '')), \
                mock.patch.object(adapter, '_exec') as execute:
            self.assertFalse(adapter.run('request', self.root, self.ctx).ok)
        execute.assert_not_called()

    def test_preflight_requires_stdout_version_and_successful_exit(self):
        for code, stdout, stderr, expected in (
            (0, '2.1.325 (Claude Code)', '', True),
            (0, '2.1.328 (Claude Code)', '', True),
            (1, '2.1.328 (Claude Code)', '', False),
            (0, '', '2.1.328 (Claude Code)', False),
            (0, '2.1.329 (Claude Code)', '', False),
        ):
            with self.subTest(code=code, stdout=stdout, stderr=stderr), \
                    mock.patch('orchestrator.adapters.cli_claude.resolve_exe', return_value='claude'), \
                    mock.patch('orchestrator.adapters.cli_claude.run_cli_process',
                               return_value=subprocess.CompletedProcess([], code, stdout, stderr)):
                self.assertEqual(ClaudeAdapter('claude').preflight().ok, expected)

    def test_real_21328_file_tools_and_usage(self):
        path = Path(__file__).parent / 'fixtures/claude_2.1.328_tools_real.jsonl'
        events = [json.loads(line) for line in path.read_text(encoding='utf8').splitlines()]
        self.adapter._version = '2.1.328 (Claude Code)'
        result = self.parse(events)
        self.assertTrue(result.ok)
        self.assertEqual(result.usage['tokens'], 23673)
        names = {block.get('name') for event in events
                 for block in event.get('message', {}).get('content', []) if isinstance(block, dict)}
        self.assertTrue({'Read', 'Edit'} <= names)

    def test_trace_uses_real_file_events_and_complete_ledger(self):
        trace=CLITrace(self.ctx,channel='cli_claude')
        for event in self.tools:trace.feed('stdout',json.dumps(event)+'\n')
        trace.finish('exited')
        self.assertTrue(trace.qoder_tools)
        self.assertEqual(trace.ledger.snapshot()['tokens'],3592)
        self.assertEqual(trace.tool_errors,0)

    def test_console_accepts_context_model_suffix_and_max_without_api_thinking_mode(self):
        cfg=parse_config({'task_workdir':'ws','state_dir':'state','adapters':{'c':{'type':'cli_claude','model':'deepseek-v4-pro[1M]','reasoning_effort':'max'}},
                          'stages':[{'name':'code','adapter':'c','prompt':'edit'}]},base_dir=self.root)
        options=validate_options(cfg,{'models':{'c':{'model':'deepseek-v4-pro[1M]','reasoning_effort':'max'}}})
        self.assertEqual(options['models']['c']['reasoning_effort'],'max')
        self.assertTrue(describe_models(cfg)[0]['supports_effort']);self.assertFalse(describe_models(cfg)[0]['supports_thinking'])
        with self.assertRaises(ValueError):validate_options(cfg,{'models':{'c':{'thinking':'enabled'}}})
        from orchestrator.engine import Engine
        from orchestrator.reporting import usage_summary
        engine=Engine(cfg,{'c':self.adapter},'metadata','test')
        result=self.parse(self.result);result.usage['backend_endpoint']='https://api.deepseek.com/anthropic'
        engine._account_usage(result,'code')
        summary=usage_summary('run',{'stages':{'code':{'status':'done'}},'stage_usage':engine.state.stage_usage,'usage_total':engine.state.usage_total})
        self.assertIn('deepseek-v4-pro',summary['stages'][0]['backend_model'])
        self.assertEqual(summary['stages'][0]['backend_endpoint'],'https://api.deepseek.com/anthropic')
