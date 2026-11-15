import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from orchestrator.supervisor import supervise

class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.file=self.root/'config.toml'
        self.file.write_text('''task_workdir="ws"
state_dir=".s"
[run_budget]
max_tokens=1000
max_duration_s=30
[autonomy]
enabled=true
[[stages]]
name="verify"
kind="test"
test_cmd=['''+json.dumps(sys.executable.replace('\\','/'))+''',"-c","assert 1+1==2"]
''',encoding='utf8')
    def test_real_worker_runs_once_and_terminal_job_is_not_restarted(self):
        result=supervise(self.file,'r','verify')
        self.assertEqual(result['status'],'done')
        with mock.patch('orchestrator.engine.artifacts.git_available',return_value=False), \
                mock.patch('orchestrator.supervisor.subprocess.Popen',side_effect=AssertionError('must not restart')):
            self.assertEqual(supervise(self.file,'r','verify')['status'],'done')
        with self.assertRaises(ValueError):supervise(self.file,'r','changed task')
    def test_unexpected_exit_restarts_with_limit(self):
        first=mock.Mock();first.wait.return_value=70
        second=mock.Mock();second.wait.return_value=0
        with mock.patch('orchestrator.supervisor.subprocess.Popen',side_effect=[first,second]) as popen:
            result=supervise(self.file,'r','verify',1)
        self.assertEqual(popen.call_count,2);self.assertEqual(result['restarts_used'],1)
        self.assertEqual(result['status'],'done')
    def test_expected_failure_is_not_retried(self):
        failed=mock.Mock();failed.wait.return_value=1
        with mock.patch('orchestrator.supervisor.subprocess.Popen',return_value=failed) as popen:
            result=supervise(self.file,'r','verify',2)
        self.assertEqual(result['status'],'failed');self.assertEqual(popen.call_count,1)
    def test_done_job_still_validates_its_artifacts(self):
        self.assertEqual(supervise(self.file,'r','verify')['status'],'done')
        directory=self.root/'.s/runs/r/stages/verify/attempts/1'
        (directory/'manifest.json').write_text('[]')
        self.assertEqual(supervise(self.file,'r','verify')['status'],'done')
        state=json.loads((self.root/'.s/runs/r/state.json').read_text())
        self.assertEqual(state['stages']['verify']['current_attempt'],2)
    def test_restart_exhaustion_is_terminal(self):
        failed=mock.Mock();failed.wait.return_value=70
        with mock.patch('orchestrator.supervisor.subprocess.Popen',return_value=failed) as popen:
            result=supervise(self.file,'r','verify',1)
        self.assertEqual(result['status'],'failed');self.assertEqual(popen.call_count,2)
        self.assertEqual(result['reason'],'restart_exhausted')

if __name__=='__main__':unittest.main()
