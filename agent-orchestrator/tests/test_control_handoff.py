"""固定 worker 退出到 supervisor 提交终态间的控制指令窗口。"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.collaboration.storage import Store,Conflict
from orchestrator.collaboration.control import command_gate
from orchestrator.collaboration.service import Service
from orchestrator.config import load_config
from orchestrator.contracts import StageStatus
from orchestrator.state import RunState
from orchestrator.supervisor import supervise,_finish_controlled_job
from orchestrator.state import atomic_write_json


class ControlHandoff(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.config=self.root/'workflow.toml'
        self.config.write_text('task_workdir="ws"\nstate_dir="state"\n[run_budget]\n'
            'max_tokens=1000\nmax_duration_s=90\n[[stages]]\nname="verify"\nkind="test"\n'
            'test_cmd='+json.dumps([sys.executable.replace('\\','/'),'-c','print("done")'])+'\n',encoding='utf8')
        self.cfg=load_config(self.config)
        self.store=Store(self.root/'db.sqlite3')
        self.project=self.store.register_project(self.config)
        self.key=self.store.register_run(self.project,'r',self.config,self.cfg.state_dir,managed=True)
        state=RunState(self.cfg.state_dir,'r')
        state.task='handoff'
        state.stage('verify').status=StageStatus.DONE
        state.stage('verify').current_attempt=1
        state.usage_total['tokens']=123
        state.save()

    def test_pause_accepted_after_last_worker_checkpoint_is_applied_at_completion(self):
        command=[]
        process=mock.Mock(pid=1234)
        def wait(*args,**kwargs):
            if not command:
                command.append(self.store.enqueue(self.key,'pause',{},'late-pause-001'))
            return 0
        process.wait.side_effect=wait
        with mock.patch('orchestrator.supervisor.subprocess.Popen',return_value=process) as start, \
             mock.patch('orchestrator.process_tree.attach'),mock.patch('orchestrator.process_tree.close'):
            job=supervise(self.config,'r','handoff',control_db=self.store.path,run_key=self.key)
        self.assertEqual(job['status'],'paused')
        self.assertEqual(self.store.command(command[0]['id'])['status'],'applied')
        state=RunState(self.cfg.state_dir,'r');state.load()
        self.assertTrue(state.control['paused'])
        self.assertEqual(state.stage('verify').current_attempt,1)
        self.assertEqual(state.usage_total['tokens'],123)
        self.assertEqual(start.call_count,1,'补结算不得重跑模型或测试')

    def final_job(self,status='done'):
        return {'run_key':self.key,'status':status,'finished_at':0}

    def test_failed_worker_rejects_late_pause_instead_of_masking_failure(self):
        command=self.store.enqueue(self.key,'pause',{},'late-pause-001')
        job=self.final_job('failed')
        state=RunState(self.cfg.state_dir,'r')
        _finish_controlled_job(job,state.run_dir/'job.json',state,self.store)
        self.assertEqual(job['status'],'failed')
        self.assertEqual(self.store.command(command['id'])['status'],'rejected')
        state.load()
        self.assertFalse(state.control.get('paused'))

    def test_cancel_at_completion_takes_priority_and_preserves_usage(self):
        pause=self.store.enqueue(self.key,'pause',{},'late-pause-001')
        cancel=self.store.enqueue(self.key,'cancel',{},'late-cancel-001')
        job=self.final_job()
        state=RunState(self.cfg.state_dir,'r')
        _finish_controlled_job(job,state.run_dir/'job.json',state,self.store)
        self.assertEqual(job['status'],'cancelled')
        self.assertEqual(self.store.command(cancel['id'])['status'],'applied')
        self.assertEqual(self.store.command(pause['id'])['status'],'rejected')
        state.load()
        self.assertEqual(state.usage_total['tokens'],123)

    def test_pause_receipt_recovers_after_state_commit_before_sql_finish(self):
        command=self.store.enqueue(self.key,'pause',{},'late-pause-001')
        job=self.final_job()
        state=RunState(self.cfg.state_dir,'r')
        original=Store.finish
        def fail(store,*args,**kwargs):
            if args[0]==command['id']:raise OSError('injected receipt failure')
            return original(store,*args,**kwargs)
        with mock.patch.object(Store,'finish',fail):
            with self.assertRaisesRegex(OSError,'injected'):
                _finish_controlled_job(job,state.run_dir/'job.json',state,self.store)
        self.assertEqual(self.store.command(command['id'])['status'],'queued')
        _finish_controlled_job(job,state.run_dir/'job.json',state,self.store)
        self.assertEqual(job['status'],'paused')
        state.load()
        self.assertEqual(sum(e['kind']=='control_receipt' for e in state.events),1)
        self.assertEqual(self.store.command(command['id'])['status'],'applied')

    def test_terminal_publication_prevents_new_pause_admission(self):
        # Service 初始化前没有 job，随后显式设置为仍在运行的终态交接窗口。
        service=Service(self.root/'service.sqlite3',[self.config])
        key=service.store.register_run(self.project,'r',self.config,self.cfg.state_dir,managed=True)
        # 导入记录可能已登记为历史；测试直接标记为受管，不调用模型。
        with service.store.connect() as db:db.execute('UPDATE runs SET managed=1 WHERE id=?',(key,))
        folder=self.cfg.state_dir/'runs/r'
        atomic_write_json(folder/'job.json',{'status':'running'})
        attempting=threading.Event();finished=threading.Event();errors=[]
        def request():
            attempting.set()
            try:service.command(key,'pause',{},'after-done-pause')
            except Exception as error:errors.append(error)
            finally:finished.set()
        with mock.patch.object(service,'launch',side_effect=AssertionError('终态不得启动 worker')):
            with command_gate(folder):
                worker=threading.Thread(target=request);worker.start()
                self.assertTrue(attempting.wait(5))
                self.assertFalse(finished.wait(.1))
                atomic_write_json(folder/'job.json',{'status':'done'})
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors),1)
        self.assertIsInstance(errors[0],Conflict)
        self.assertEqual(service.store.commands(key),[])

    def test_resume_delivery_waits_until_old_manager_exits(self):
        service=Service(self.root/'service.sqlite3',[])
        old_exits=threading.Event();launched=threading.Event()
        old=mock.Mock();old.poll.return_value=None
        def wait(timeout=None):
            if not old_exits.wait(timeout):
                import subprocess
                raise subprocess.TimeoutExpired('old manager',timeout)
            return 0
        old.wait.side_effect=wait
        service.active[self.key]=old
        self.addCleanup(service.closed.set)
        with mock.patch.object(service.store,'commands',return_value=[{'kind':'resume'}]), \
                mock.patch.object(service,'launch',side_effect=lambda key:launched.set()) as launch:
            with service.operations:
                service._wait_command_delivery(self.key)
                service._wait_command_delivery(self.key)
            self.assertFalse(launched.wait(.1))
            old_exits.set()
            self.assertTrue(launched.wait(5))
            deadline=time.monotonic()+5
            while service._command_waiters and time.monotonic()<deadline:time.sleep(.01)
            self.assertFalse(service._command_waiters)
            self.assertEqual(launch.call_count,1)

    def test_real_worker_exit_pause_then_resume_reuses_completed_attempt(self):
        import subprocess
        service=Service(self.root/'live.sqlite3',[])
        project=service.store.register_project(self.config)
        key=service.store.register_run(project,'real',self.config,self.cfg.state_dir,managed=True)
        real_start=subprocess.Popen
        received=[]
        def spawn(*args,**kwargs):
            process=real_start(*args,**kwargs)
            real_wait=process.wait
            def wait(*wargs,**wkwargs):
                code=real_wait(*wargs,**wkwargs)
                if code==0 and not received:
                    # worker 已实际完成并退出，仍未发布 done；只注入请求时间。
                    received.append(service.command(key,'pause',{},'pause-after-worker-exit'))
                return code
            process.wait=wait
            return process
        with mock.patch('orchestrator.supervisor.subprocess.Popen',side_effect=spawn):
            job=supervise(self.config,'real','real handoff',control_db=service.store.path,run_key=key)
        self.assertEqual(job['status'],'paused')
        self.assertEqual(service.store.command(received[0]['id'])['status'],'applied')
        state=RunState(self.cfg.state_dir,'real');state.load()
        self.assertEqual(state.stage('verify').current_attempt,1)
        usage=dict(state.usage_total)
        with mock.patch.object(service,'launch'):
            resumed=service.command(key,'resume',{},'resume-after-worker-exit')
        job=supervise(self.config,'real','real handoff',control_db=service.store.path,run_key=key)
        self.assertEqual(job['status'],'done')
        self.assertEqual(service.store.command(resumed['id'])['status'],'applied')
        state.load()
        self.assertEqual(state.stage('verify').current_attempt,1)
        self.assertEqual(state.usage_total,usage)


if __name__=='__main__':unittest.main()
