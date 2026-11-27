"""Windows 后代必须在父进程归入 Job 之后才可创建，恢复失败须安全停止。"""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from orchestrator import process_tree
from orchestrator.adapters.cli_base import run_cli_process
from orchestrator.cli_telemetry import CLITrace
from orchestrator.contracts import RunContext
from orchestrator.safety import build_subprocess_env
from orchestrator.state import _pid_alive
from orchestrator.supervisor import supervise
from cmd_shim import write_cmd_shim
from scripts.failure_evidence import EvidenceTestCase


def spawn_suspended(args, root):
    """按生产路径创建"挂起、尚未归入 Job"的进程。"""
    return subprocess.Popen(args, cwd=str(root), env=build_subprocess_env(),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=process_tree.creation_flags())


def dispose(process):
    process_tree.close(process)
    if process.poll() is None:
        process.kill()
        process.wait(timeout=10)
    for stream in (process.stdin, process.stdout, process.stderr):
        try:
            stream.close()
        except (AttributeError, OSError):
            pass


@unittest.skipUnless(os.name=='nt','Windows Job 启动顺序回归')
class ProcessStart(EvidenceTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def test_cmd_child_cannot_escape_before_delayed_job_attachment(self):
        pidfile=self.root/'child.pid'
        script=self.root/'child.py'
        script.write_text('import os,sys,time\nfrom pathlib import Path\nPath('+repr(str(pidfile))+')'
                          '.write_text(str(os.getpid()))\nsys.stdin.buffer.read()\n'
                          'print("CHILD_READY",flush=True)\ntime.sleep(60)\n',encoding='utf8')
        wrapper=self.root/'child.cmd'
        write_cmd_shim(wrapper,sys.executable,'-u',str(script))
        original=process_tree.attach
        observed=[]
        def attach(process):
            time.sleep(.5)
            observed.append(pidfile.exists())
            return original(process)
        try:
            with mock.patch.object(process_tree,'attach',side_effect=attach):
                with self.assertRaises(subprocess.TimeoutExpired) as raised:
                    # 本用例针对绑定顺序，不把繁忙机器的 .cmd/Python 启动耗时限定为 3 秒。
                    run_cli_process([str(wrapper)],cwd=str(self.root),env=build_subprocess_env(),input='',timeout=10)
            self.assertEqual(observed,[False],'绑定 Job 前不应产生 Python 后代')
            self.assertIn('CHILD_READY',raised.exception.stdout)
            self.assertEqual(raised.exception.stop_reason,'call_timeout')
            self.assertFalse(_pid_alive(int(pidfile.read_text())))
        finally:
            if pidfile.exists() and _pid_alive(int(pidfile.read_text())):
                subprocess.run(['taskkill','/PID',pidfile.read_text().strip(),'/T','/F'],
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)

    def test_fast_resume_path_does_not_enumerate_system_threads(self):
        # P2-4：句柄级恢复成功后不得再走全系统线程表扫描。
        process=self.root/'fast.pid'
        script='from pathlib import Path;Path('+repr(str(process))+').write_text("ran")'
        handle=spawn_suspended([sys.executable,'-c',script],self.root)
        handle.__dict__['_pipeline_start_suspended']=True
        fallback=mock.Mock(side_effect=AssertionError('不应回退到全系统线程扫描'))
        try:
            with mock.patch.object(process_tree,'_resume_initial_thread',fallback):
                process_tree.attach(handle)
            out,err=handle.communicate(input=b'',timeout=30)
            self.assertEqual(handle.returncode,0,err)
            fallback.assert_not_called()
            self.assertEqual(process.read_text(encoding='utf8'),'ran')
        finally:
            dispose(handle)

    def test_fast_resume_falls_back_to_thread_scan_when_unavailable(self):
        # P2-4：OpenProcess/NtResumeProcess 不可用时仍须能恢复，不能把启动判死。
        marker=self.root/'fallback.pid'
        script='from pathlib import Path;Path('+repr(str(marker))+').write_text("ran")'
        handle=spawn_suspended([sys.executable,'-c',script],self.root)
        handle.__dict__['_pipeline_start_suspended']=True
        fallback=mock.Mock(side_effect=process_tree._resume_initial_thread)
        try:
            with mock.patch.object(process_tree,'_resume_process_handle',return_value=False), \
                    mock.patch.object(process_tree,'_resume_initial_thread',fallback):
                process_tree.attach(handle)
            out,err=handle.communicate(input=b'',timeout=30)
            self.assertEqual(handle.returncode,0,err)
            fallback.assert_called_once_with(handle)
            self.assertEqual(marker.read_text(encoding='utf8'),'ran')
        finally:
            dispose(handle)

    def test_resume_failure_terminates_owned_process_and_leaves_no_job_handle(self):
        marker=self.root/'must-not-run'
        script='from pathlib import Path;Path('+repr(str(marker))+').write_text("bad")'
        captured=[]
        real_start=subprocess.Popen
        def start(*args,**kwargs):
            process=real_start(*args,**kwargs)
            captured.append(process)
            self.addCleanup(dispose,process)
            return process
        with mock.patch('orchestrator.adapters.cli_base.subprocess.Popen',side_effect=start), \
                mock.patch.object(process_tree,'_resume_process_handle',return_value=False), \
                mock.patch.object(process_tree,'_resume_initial_thread',side_effect=OSError('injected resume error')):
            with self.assertRaisesRegex(OSError,'injected resume error'):
                run_cli_process([sys.executable,'-c',script],cwd=str(self.root),env=build_subprocess_env(),input='',timeout=10)
        self.assertFalse(marker.exists())
        self.assertEqual(len(captured),1)
        self.assertIsNotNone(captured[0].poll())
        self.assertNotIn('_workbench_job',captured[0].__dict__)
        self.assertTrue(all(pipe.closed for pipe in
                            (captured[0].stdin,captured[0].stdout,captured[0].stderr)))

    def test_failed_resume_closes_job_and_kills_suspended_process(self):
        # P3-5：两条恢复路径都失败时，只能靠关闭 Job（KILL_ON_JOB_CLOSE）杀死挂起进程；
        # 本用例直接调用 attach，不依赖 cli_base 的兜底 kill，能识别 Job 清理被跳过。
        marker=self.root/'must-not-run'
        script='from pathlib import Path;Path('+repr(str(marker))+').write_text("bad")'
        handle=spawn_suspended([sys.executable,'-c',script],self.root)
        handle.__dict__['_pipeline_start_suspended']=True
        try:
            with mock.patch.object(process_tree,'_resume_process_handle',return_value=False), \
                    mock.patch.object(process_tree,'_resume_initial_thread',side_effect=OSError('injected resume error')):
                with self.assertRaisesRegex(OSError,'injected resume error'):
                    process_tree.attach(handle)
            self.assertNotIn('_workbench_job',handle.__dict__)
            self.assertIsNotNone(handle.wait(timeout=10),'关闭 Job 必须杀死仍挂起的进程')
            self.assertFalse(marker.exists())
        finally:
            dispose(handle)

    def test_short_command_executes_after_job_attachment(self):
        result=run_cli_process([sys.executable,'-c','print("SHORT_OK")'],cwd=str(self.root),
                               env=build_subprocess_env(),input='',timeout=15)
        self.assertEqual(result.returncode,0)
        self.assertIn('SHORT_OK',result.stdout)


class CLIStartWindowAndTimeout(EvidenceTestCase):
    """启动窗口/超时预算/清理加固回归；不依赖 Windows Job，POSIX 同样覆盖。"""

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def context(self):
        return RunContext('fixture','run','coding',self.root,self.root,
                          attempt_dir=self.root/'attempt',timeout_s=15)

    def test_attach_time_is_not_charged_to_call_timeout(self):
        # 只在 CLI 时钟上模拟慢绑定，实际启动和进程树绑定仍走真实路径。
        timeout = 5
        attach_elapsed = 0
        clock = SimpleNamespace(monotonic=lambda: time.monotonic() + attach_elapsed,
                                sleep=time.sleep)
        real_attach = process_tree.attach

        def slow_attach(process):
            nonlocal attach_elapsed
            real_attach(process)
            attach_elapsed = timeout + 1

        with (mock.patch('orchestrator.adapters.cli_base.time', clock),
              mock.patch.object(process_tree, 'attach', side_effect=slow_attach)):
            result = run_cli_process([sys.executable, '-c', 'print("BUDGET_OK")'],
                                     cwd=str(self.root), env=build_subprocess_env(), input='', timeout=timeout)
        self.assertEqual(result.returncode,0)
        self.assertIn('BUDGET_OK',result.stdout)

    def test_exception_between_popen_and_attach_kills_suspended_process(self):
        # 绑定失败时没有 I/O 线程接管，调用方必须关闭全部管道。
        captured=[]
        real_start=subprocess.Popen
        def start(*args,**kwargs):
            process=real_start(*args,**kwargs)
            captured.append(process)
            self.addCleanup(dispose,process)
            return process
        with mock.patch('orchestrator.adapters.cli_base.subprocess.Popen',side_effect=start), \
                mock.patch.object(process_tree,'attach',side_effect=RuntimeError('injected attach error')):
            with self.assertRaisesRegex(RuntimeError,'injected attach error'):
                run_cli_process([sys.executable,'-c','print("MUST_NOT_RUN")'],
                                cwd=str(self.root),env=build_subprocess_env(),input='',timeout=10)
        self.assertEqual(len(captured),1)
        self.assertIsNotNone(captured[0].poll(),'创建窗口异常后进程必须已终止')
        self.assert_process_pipes_closed(captured[0])

    def test_io_thread_start_failure_closes_unclaimed_pipes(self):
        captured=[]
        real_popen=subprocess.Popen
        real_thread_start=threading.Thread.start
        started=[]

        def popen(*args,**kwargs):
            process=real_popen(*args,**kwargs)
            captured.append(process)
            self.addCleanup(dispose,process)
            return process

        def start_thread(thread):
            if started:
                raise RuntimeError('injected thread start error')
            real_thread_start(thread)
            started.append(thread)

        # 第一个读取线程已启动，其他管道尚未移交。
        script='import time; time.sleep(60)'
        with mock.patch('orchestrator.adapters.cli_base.subprocess.Popen',side_effect=popen), \
                mock.patch.object(threading.Thread,'start',new=start_thread):
            with self.assertRaisesRegex(RuntimeError,'injected thread start error'):
                run_cli_process([sys.executable,'-c',script],cwd=str(self.root),
                                env=build_subprocess_env(),input='',timeout=10)
        self.assertEqual(len(started),1)
        self.assertFalse(started[0].is_alive())
        self.assertEqual(len(captured),1)
        self.assertIsNotNone(captured[0].poll())
        self.assert_process_pipes_closed(captured[0])

    def assert_process_pipes_closed(self,process):
        for name in ('stdin','stdout','stderr'):
            with self.subTest(pipe=name):
                self.assertTrue(getattr(process,name).closed)

    def test_pipe_close_failure_keeps_attach_error_and_closes_other_pipes(self):
        captured=[]
        real_popen=subprocess.Popen
        attach_error=RuntimeError('injected attach error')

        def popen(*args,**kwargs):
            process=real_popen(*args,**kwargs)
            captured.append(process)
            self.addCleanup(dispose,process)
            return process

        def fail_attach(process):
            close_stdin=process.stdin.close

            def close_then_fail():
                close_stdin()
                raise OSError('injected pipe close error')

            patcher=mock.patch.object(process.stdin,'close',side_effect=close_then_fail)
            self.addCleanup(patcher.stop)
            patcher.start()
            raise attach_error

        with mock.patch('orchestrator.adapters.cli_base.subprocess.Popen',side_effect=popen), \
                mock.patch.object(process_tree,'attach',side_effect=fail_attach):
            with self.assertRaises(RuntimeError) as raised:
                run_cli_process([sys.executable,'-c','print("MUST_NOT_RUN")'],
                                cwd=str(self.root),env=build_subprocess_env(),input='',timeout=10)
        self.assertIs(raised.exception,attach_error)
        self.assertEqual(len(captured),1)
        self.assertIsNotNone(captured[0].poll())
        self.assert_process_pipes_closed(captured[0])

    def test_successful_exit_does_not_hide_incomplete_stdin_write(self):
        # 子进程完全不读 stdin；输入超过管道容量，必然无法完整写入。
        with mock.patch.object(threading, 'excepthook') as thread_error:
            with self.assertRaisesRegex(OSError, 'CLI 输入写入失败'):
                run_cli_process([sys.executable,'-c','print("OK")'],cwd=str(self.root),
                                env=build_subprocess_env(),input=b'x'*(1024*1024),timeout=15)
        thread_error.assert_not_called()

    def test_nonzero_exit_keeps_diagnostics_when_stdin_write_is_incomplete(self):
        script='import sys; print("CLI_REJECTED", file=sys.stderr); sys.exit(3)'
        with mock.patch.object(threading, 'excepthook') as thread_error:
            result=run_cli_process([sys.executable,'-c',script],cwd=str(self.root),
                                   env=build_subprocess_env(),input=b'x'*(1024*1024),timeout=15)
        self.assertEqual(result.returncode,3)
        self.assertIn('CLI_REJECTED',result.stderr)
        thread_error.assert_not_called()

    def test_timeout_keeps_stop_reason_when_stdin_is_blocked(self):
        with mock.patch.object(threading, 'excepthook') as thread_error:
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                run_cli_process([sys.executable,'-c','import time; time.sleep(60)'],
                                cwd=str(self.root),env=build_subprocess_env(),
                                input=b'x'*(1024*1024),timeout=1)
        self.assertEqual(raised.exception.stop_reason,'call_timeout')
        thread_error.assert_not_called()

    def test_stdin_close_error_is_reported_instead_of_escaping_writer(self):
        real_popen=subprocess.Popen

        def popen(*args,**kwargs):
            process=real_popen(*args,**kwargs)
            self.addCleanup(dispose,process)
            close_stdin=process.stdin.close

            def close_then_fail():
                close_stdin()
                raise OSError('injected stdin close error')

            patcher=mock.patch.object(process.stdin,'close',side_effect=close_then_fail)
            self.addCleanup(patcher.stop)
            patcher.start()
            return process

        script='import sys; assert sys.stdin.buffer.read() == b"prompt"'
        with mock.patch('orchestrator.adapters.cli_base.subprocess.Popen',side_effect=popen), \
                mock.patch.object(threading, 'excepthook') as thread_error:
            with self.assertRaisesRegex(OSError, 'injected stdin close error'):
                run_cli_process([sys.executable,'-c',script],cwd=str(self.root),
                                env=build_subprocess_env(),input=b'prompt',timeout=15)
        thread_error.assert_not_called()

    def test_cleanup_failure_keeps_primary_error_and_finishes_trace(self):
        # P3-7：terminate 抛异常时，close(process) 与 trace.finish 仍须执行，
        # 且真正的主异常不能被清理异常掩盖。
        trace=CLITrace(self.context())
        captured=[]
        real_start=subprocess.Popen
        def start(*args,**kwargs):
            process=real_start(*args,**kwargs);captured.append(process);return process
        script='import sys,time\nsys.stdin.buffer.read()\ntime.sleep(30)\n'
        with mock.patch('orchestrator.adapters.cli_base.subprocess.Popen',side_effect=start), \
                mock.patch('orchestrator.process_tree.terminate',side_effect=OSError('injected terminate error')), \
                mock.patch('orchestrator.process_tree.close',wraps=process_tree.close) as close_mock:
            with self.assertRaises(OSError) as raised:
                run_cli_process([sys.executable,'-u','-c',script],cwd=str(self.root),
                                env=build_subprocess_env(),input='',timeout=0.3,trace=trace)
        self.assertIn('injected terminate error',str(raised.exception))
        self.assertTrue(close_mock.called,'清理异常不得跳过 close(process)')
        self.assertTrue(trace.closed,'清理异常不得跳过 trace.finish')
        self.assertIsNotNone(captured[0].poll(),'清理异常后仍必须杀死子进程')
        notes='\n'.join(getattr(raised.exception,'__notes__',[]))
        self.assertIn('清理阶段异常',notes)
        for stream in (captured[0].stdin,captured[0].stdout,captured[0].stderr):
            try:
                stream.close()
            except (AttributeError,OSError):
                pass

    def test_close_failure_still_finishes_trace(self):
        # P3-7：close(process) 抛异常时 trace.finish 仍须执行（finally 嵌套）。
        trace=CLITrace(self.context())
        with mock.patch('orchestrator.process_tree.close',side_effect=OSError('injected close error')):
            with self.assertRaisesRegex(OSError,'injected close error'):
                run_cli_process([sys.executable,'-c','print("OK")'],cwd=str(self.root),
                                env=build_subprocess_env(),input='',timeout=15,trace=trace)
        self.assertTrue(trace.closed)


class SupervisorCreationWindow(EvidenceTestCase):
    """P3-6：supervisor 的 Popen→attach 窗口同样不能留下挂起的 worker。"""

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.file=self.root/'config.toml'
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
test_cmd=['''+repr(sys.executable.replace('\\','/'))+''',"-c","assert 1+1==2"]
''',encoding='utf8')

    class FakeStore:
        def __init__(self,path):
            self.path=path
        def commands(self,run_key):
            return []
        def finish(self,*args,**kwargs):
            pass

    def test_worker_creation_window_failure_kills_suspended_worker(self):
        captured=[]
        real_start=subprocess.Popen
        def start(*args,**kwargs):
            process=real_start(*args,**kwargs);captured.append(process);return process
        with mock.patch('orchestrator.supervisor.subprocess.Popen',side_effect=start), \
                mock.patch('orchestrator.collaboration.storage.Store',self.FakeStore), \
                mock.patch.object(process_tree,'attach',side_effect=RuntimeError('injected attach error')):
            with self.assertRaisesRegex(RuntimeError,'injected attach error'):
                supervise(self.file,'window','verify',control_db=str(self.root/'control.db'),
                          run_key='window-key')
        self.assertEqual(len(captured),1)
        self.assertIsNotNone(captured[0].poll(),'创建窗口异常后 worker 必须已终止')
        for stream in (captured[0].stdin,captured[0].stdout,captured[0].stderr):
            try:
                stream.close()
            except (AttributeError,OSError):
                pass


if __name__=='__main__':unittest.main()
