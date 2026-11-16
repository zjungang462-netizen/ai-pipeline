"""CLI 相对文件活动必须基于任务目录；不启动模型或实际工具。"""
import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.cli_telemetry import CLITrace
from orchestrator.contracts import RunContext


class CLIActivityPaths(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.workspace=self.root/'workspace';self.workspace.mkdir()
        self.ctx=RunContext('local activity fixture','activity-paths','coding',self.workspace,
                            self.root/'run',attempt_dir=self.root/'attempt')

    def activity(self,path,channel='cli_dsh',secrets=()):
        trace=CLITrace(self.ctx,secrets,channel=channel)
        if channel=='cli_dsh':
            event={'type':'tool_call','callId':'fixture','tool':'read','input':{'path':path}}
        else:
            event={'event':'step_update','step_update':{
                'conversation_id':'fixture','step_index':1,'step_type':'tool','state':'ACTIVE',
                'tool_name':'read_file','tool_info':{'parameters':{'Path':path}}}}
        try:
            trace.feed('stdout',json.dumps(event)+'\n')
            return trace.snapshot()
        finally:trace.finish('exited')

    def test_dsh_relative_path_uses_task_workspace(self):
        self.assertNotEqual(Path.cwd().resolve(),self.workspace.resolve())
        self.assertEqual(self.activity('src/check.py')['file'],'src/check.py')

    def test_agy_relative_unicode_path_uses_task_workspace(self):
        self.assertEqual(self.activity('src/中文.py','cli_agy')['file'],'src/中文.py')

    def test_absolute_path_and_dot_segments_remain_supported(self):
        absolute=str(self.workspace/'src/check.py')
        self.assertEqual(self.activity(absolute)['file'],'src/check.py')
        self.assertEqual(self.activity('./src/../check.py')['file'],'check.py')

    def test_escape_and_absolute_outside_path_remain_hidden(self):
        for path in ('../outside.py',str(self.root/'outside.py')):
            with self.subTest(path=path):
                self.assertEqual(self.activity(path)['file'],'[工作区外路径]')

    def test_known_secret_in_relative_file_name_is_redacted(self):
        secret='known-private-fixture-secret'
        snapshot=self.activity('src/'+secret+'.py',secrets=[secret])
        self.assertEqual(snapshot['file'],'src/[REDACTED].py')
        self.assertNotIn(secret,json.dumps(snapshot))


if __name__=='__main__':unittest.main()
