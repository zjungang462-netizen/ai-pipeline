"""本地子进程验证登录提示早停，不调用任何模型或触发浏览器。"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from orchestrator.adapters.cli_agy import AgyAdapter
from orchestrator.cli_telemetry import CLITrace
from orchestrator.contracts import RunContext,ErrorKind
from scripts.failure_evidence import EvidenceTestCase

PROMPT='Authentication required. Please visit the URL to log in:'

class CLIAuthStop(EvidenceTestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
    def ctx(self):
        return RunContext('local auth fixture','auth-stop','coding',self.root,self.root,
                          kind='code',attempt_dir=self.root/'attempt',call_id='coding/1',timeout_s=12)
    def test_split_stderr_login_prompt_stops_real_child_and_retains_usage(self):
        script=('import sys,time,json\n'
                'print(json.dumps({"event":"step_update","step_update":{"conversation_id":"fixture","step_index":1,"state":"DONE","step_type":"agent_response","usage":{"total_tokens":10,"cache_read_tokens":20}}}),flush=True)\n'
                'sys.stderr.write('+repr(PROMPT[:20])+');sys.stderr.flush();time.sleep(.1)\n'
                'sys.stderr.write('+repr(PROMPT[20:]+'\n')+');sys.stderr.flush();time.sleep(30)\n')
        adapter=AgyAdapter('fixture');adapter._resolved=sys.executable;adapter._version='1.5.9'
        start=time.monotonic();result=adapter._exec(['-u','-c',script],self.root,self.ctx())
        self.assertEqual(result.error_kind,ErrorKind.AUTH)
        self.assertLess(time.monotonic()-start,5)
        self.assertEqual(result.usage['known_tokens'],30)
        self.assertTrue(result.usage['usage_unknown'])
        progress=json.loads((self.root/'attempt/cli-progress.json').read_text(encoding='utf8'))
        self.assertEqual(progress['reason'],'authentication_required')
    def test_login_marker_survives_every_chunk_boundary(self):
        for index in range(1,len(PROMPT)):
            with self.subTest(index=index):
                trace=CLITrace(self.ctx(),channel='cli_agy')
                trace.feed('stderr',PROMPT[:index]);trace.feed('stderr',PROMPT[index:])
                self.assertEqual(trace.tick(),'authentication_required');trace.finish('stopped')
    def test_stdout_other_tools_and_quoted_errors_do_not_request_login(self):
        for channel,stream,value in (('cli_agy','stdout',PROMPT+'\n'),
                 ('cli_dsh','stderr',PROMPT+'\n'),('cli_agy','stderr','Example: '+PROMPT+'\n')):
            with self.subTest(channel=channel,stream=stream,value=value):
                trace=CLITrace(self.ctx(),channel=channel);trace.feed(stream,value)
                self.assertEqual(trace.tick(),'');trace.finish('exited')

if __name__=='__main__':unittest.main()
