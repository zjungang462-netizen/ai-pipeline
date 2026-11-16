"""当前任务的有限进程恢复，不定时创建新任务，不接受失败结果为成功。"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import ConfigError, load_config
from .safety import build_subprocess_env
from .state import RunState, StageLock, atomic_write_json, sha256_text
from .tuning import select_policy_config


def supervise(config_path: Path, run_id: str, task: str, max_restarts: int = 2,
              control_db=None, run_key=None) -> dict:
    if type(max_restarts) is not int or not 0 <= max_restarts <= 5:
        raise ConfigError('max_restarts 必须在 0 到 5 之间')
    source = config_path.resolve()
    config = load_config(source)
    if not config.autonomy.enabled and not control_db:
        raise ConfigError('unattended 必须显式启用 autonomy')
    if control_db and (not config.run_budget.max_duration_s or not config.run_budget.max_tokens):
        raise ConfigError('工作台启动任务必须有正数 token 与时长预算')
    state = RunState(config.state_dir, run_id)
    directory = state.run_dir
    lock = StageLock(directory / '.supervisor.lock')
    if not lock.acquire():
        return {'run_id': run_id, 'status': 'running', 'reason': 'supervisor_busy'}
    try:
        jobfile = directory / 'job.json'
        if jobfile.is_file():
            job = json.loads(jobfile.read_text(encoding='utf8'))
            if job['task_hash'] != sha256_text(task) or Path(job['requested_config']) != source:
                raise ConfigError('同一 run_id 不可更换任务或配置')
        else:
            selected = select_policy_config(source)
            config = load_config(selected)
            job = {'run_id': run_id, 'task': task, 'task_hash': sha256_text(task),
                   'requested_config': str(source), 'config': str(selected),
                   'config_hash': config.source_hash, 'started_at': time.time(),
                   'max_duration_s': config.run_budget.max_duration_s,
                   'max_restarts': max_restarts, 'restarts_used': 0, 'status': 'pending'}
            if control_db:
                job.update(control_db=str(Path(control_db).resolve()), run_key=run_key)
            atomic_write_json(jobfile, job)
        store = None
        if job.get('control_db'):
            from .collaboration.storage import Store
            store = Store(job['control_db'])
            for command_id, receipt in job.get('control_receipts', {}).items():
                store.finish(command_id, receipt['status'], receipt['result'])
            queued = store.commands(job['run_key'])
            cancel = next((c for c in queued if c['kind'] == 'cancel'), None)
            if cancel:
                _cancel_job(job, jobfile, state, store, cancel)
                return job
            if job.get('status') in ('failed', 'done') and any(c['kind'] == 'rerun' for c in queued):
                job['status'] = 'pending'
            elif job.get('status') in ('failed', 'done', 'timeout', 'cancelled'):
                for command in queued:
                    store.finish(command['id'], 'rejected', {'reason': '运行已经结束，此操作未生效'})
        config = load_config(job['config'])
        if config.source_hash != job['config_hash']:
            raise ConfigError('被固定的运行配置发生变化，停止自动恢复')
        if job.get('status') in ('failed', 'timeout', 'cancelled'):
            return job
        if job.get('status') == 'done':
            from .engine import Engine
            from .adapters.factory import build_all
            validator = Engine(config, build_all(config.adapters, {s.adapter: s.kind for s in config.stages}),
                               run_id, task)
            valid = validator.state.load() and all(
                validator.state.is_complete(spec.name, validator._inputs_hash(spec, index))
                for index, spec in enumerate(config.stages))
            if valid:
                return job
            job['status'] = 'pending'
            atomic_write_json(jobfile, job)
        allow = [a.options['api_key_env'] for a in config.adapters.values()
                 if a.type == 'openai_compat']
        environment = build_subprocess_env(allow)
        while True:
            remaining = job['max_duration_s'] - (time.time() - job['started_at'])
            if remaining <= 0:
                job.update(status='timeout', reason='supervisor_wall_budget')
                break
            with (directory / 'worker.log').open('ab') as log:
                process = subprocess.Popen([sys.executable, '-X', 'utf8', '-m',
                                            'orchestrator.worker', str(jobfile)],
                                           cwd=Path(__file__).resolve().parents[1],
                                           env=environment, stdout=log, stderr=subprocess.STDOUT,
                                           start_new_session=os.name != 'nt',
                                           creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
                if store:
                    from .process_tree import attach
                    try:
                        attach(process)
                    except OSError as error:
                        _terminate(process)
                        job.update(status='failed', reason='无法建立进程树归属: ' + str(error))
                        atomic_write_json(jobfile, job)
                        return job
                job.update(status='running', worker_pid=process.pid if isinstance(process.pid, int) else None)
                atomic_write_json(jobfile, job)
                try:
                    if not store:
                        code = process.wait(timeout=remaining)
                    else:
                        while True:
                            cancel = next((c for c in store.commands(job['run_key']) if c['kind'] == 'cancel'), None)
                            if cancel:
                                _terminate(process)
                                _cancel_job(job, jobfile, state, store, cancel)
                                return job
                            remaining = job['max_duration_s'] - (time.time() - job['started_at'])
                            if remaining <= 0:
                                raise subprocess.TimeoutExpired(process.args, 0)
                            try:
                                code = process.wait(timeout=min(.3, remaining))
                                break
                            except subprocess.TimeoutExpired:
                                continue
                except subprocess.TimeoutExpired:
                    _terminate(process)
                    job.update(status='timeout', reason='supervisor_wall_budget')
                    break
                finally:
                    if store:
                        from .process_tree import close
                        close(process)
            job['exit_code'] = code
            if code == 0:
                job['status'] = 'done'
                break
            if store and code in (10, 11, 12):
                job['status'] = {10: 'paused', 11: 'waiting_approval', 12: 'cancelled'}[code]
                break
            if code in (1, 2) or job['restarts_used'] >= job['max_restarts']:
                job.update(status='failed', reason='terminal_failure' if code in (1, 2) else 'restart_exhausted')
                break
            job['restarts_used'] += 1
            job['status'] = 'recovering'
            atomic_write_json(jobfile, job)
            time.sleep(min(1.0, max(0, remaining)))
        if store:
            from .collaboration.control import command_gate
            with command_gate(directory):
                _finish_controlled_job(job,jobfile,state,store)
        else:
            job['finished_at'] = time.time()
            atomic_write_json(jobfile, job)
        return job
    finally:
        lock.release()


def _finish_controlled_job(job,jobfile,state,store):
    """终态发布前补查已准入指令；不得为处理暂停重新执行阶段。"""
    queued=store.commands(job['run_key'])
    cancel=next((c for c in queued if c['kind']=='cancel'),None)
    if cancel:
        _cancel_job(job,jobfile,state,store,cancel)
        if job.get('status')=='cancelled':
            for command in store.commands(job['run_key']):
                store.finish(command['id'],'rejected',{'reason':'运行已取消，此操作未生效'})
        return
    if job['status']=='done' and any(c['kind']=='pause' for c in queued):
        from .collaboration.control import Control
        with StageLock(state.run_dir/'.run.lock'):
            if not state.load():
                raise RuntimeError('缺少 worker 结束状态，不能确认暂停')
            if Control(store.path,job['run_key']).pause_after_worker(state):
                job['status']='paused'
    job['finished_at']=time.time()
    atomic_write_json(jobfile,job)
    if job['status'] in ('done','failed','timeout','cancelled'):
        for command in store.commands(job['run_key']):
            store.finish(command['id'],'rejected',{'reason':'运行已经结束，此操作未生效'})


def _terminate(process):
    """只操作此 supervisor 创建的进程对象及其后代。"""
    from .process_tree import terminate
    grouped = terminate(process)
    if os.name == 'nt' and not grouped:
        try:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           env=build_subprocess_env(), stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif os.name != 'nt':
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def _cancel_job(job, jobfile, state, store, command):
    lock = StageLock(state.run_dir / '.run.lock')
    if not lock.acquire():
        store.finish(command['id'], 'rejected', {'reason': '运行写锁仍被占用，取消尚未确认'})
        return
    try:
        state.load()
        # 发送前预扣的未知用量保留，不能因为取消而冲掉预算。
        from .cli_accounting import recover_pending_usage
        recover_pending_usage(state)
        state.control['cancelled'] = True
        from .contracts import StageStatus
        for stage in state.stages.values():
            if stage.status == StageStatus.RUNNING:
                stage.status = StageStatus.INTERRUPTED
        state.log_event('cancelled', '', command_id=command['id'])
        state.save()
        result = {'lifecycle': 'cancelled', 'existing_commits_preserved': True}
        job.setdefault('control_receipts', {})[command['id']] = {'status': 'applied', 'result': result}
        job.update(status='cancelled', finished_at=time.time())
        atomic_write_json(jobfile, job)
        store.finish(command['id'], 'applied', result)
    finally:
        lock.release()
