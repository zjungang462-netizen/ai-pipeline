"""竞技场后台任务。工作台和 worker 共享落盘状态，worker 独立于 HTTP 连接。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

from ..arena import arena_baseline, run_arena, validate_arena_request
from ..config import load_config
from ..safety import adapter_env_allow, build_subprocess_env
from ..state_io import StageLock, atomic_write_json
from .storage import Conflict


TERMINAL = frozenset({'done', 'failed', 'cancelled', 'interrupted'})
_JOB_ID = re.compile(r'arena-[0-9a-f]{32}')
_QUEUE_GRACE_S = 60


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def _read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _root(cfg, project_id):
    return cfg.state_dir / 'arena_jobs' / _digest(project_id)


def is_job_id(job_id):
    return _JOB_ID.fullmatch(job_id) is not None


def _directory(cfg, project_id, job_id):
    if not is_job_id(job_id):
        raise ValueError('竞技场任务 ID 格式不合法')
    path = _root(cfg, project_id) / job_id
    if not (path / 'job.json').is_file():
        raise KeyError('竞技场任务不存在')
    return path


def _event(path, state, status, *, label=None, reason=''):
    if label is None:
        state['status'] = status
        if reason:
            state['error'] = reason
        if status in TERMINAL and status != 'done':
            for candidate in state['candidates']:
                if candidate['status'] not in TERMINAL:
                    candidate['status'] = status
    else:
        next(c for c in state['candidates'] if c['label'] == label)['status'] = status
    state['events'].append({'cursor': len(state['events']) + 1, 'status': status,
                            'label': label, 'time': time.time()})
    atomic_write_json(path / 'state.json', state)


def _state(path):
    state = _read(path / 'state.json')
    if state['status'] in TERMINAL:
        return state
    if state['status'] == 'queued' and time.time() - state['created'] < _QUEUE_GRACE_S:
        return state
    # 只在没有 worker 持有锁时补记中断，避免查询线程与活跃 worker 同时写状态。
    lock = StageLock(path / '.worker.lock')
    if lock.acquire():
        try:
            state = _read(path / 'state.json')
            if state['status'] not in TERMINAL:
                _event(path, state, 'interrupted', reason='竞技场后台进程已退出')
        finally:
            lock.release()
    return state


def _snapshot(path, cfg, *, include_result=False):
    state = _state(path)
    result = {k: v for k, v in state.items() if k != 'events'}
    result['cursor'] = len(state['events'])
    result['cancel_requested'] = state['status'] not in TERMINAL and (path / 'cancel.json').exists()
    if include_result:
        if state['status'] in TERMINAL:
            result_path = cfg.state_dir / 'arenas' / state['arena_id'] / 'result.json'
            try:
                result['result'] = _read(result_path)
            except FileNotFoundError:
                pass
        if 'result' not in result:
            completed = []
            for candidate in state['candidates']:
                if candidate['status'] in TERMINAL:
                    try:
                        completed.append(_read(path / f"candidate_{candidate['label']}.json"))
                    except FileNotFoundError:
                        continue
            if completed:
                result['result'] = {'arena_id': state['arena_id'], 'base_sha': state['base_sha'],
                                    'candidates': completed}
    return result


def _job_paths(root):
    if not root.is_dir():
        return
    for path in root.iterdir():
        if _JOB_ID.fullmatch(path.name) and (path / 'state.json').is_file():
            yield path


def _jobs(cfg, project_id):
    return _job_paths(_root(cfg, project_id))


def has_active_job(cfg, project_id):
    return has_active_job_in(cfg.state_dir, project_id)


def has_active_job_in(state_dir, project_id):
    root = Path(state_dir) / 'arena_jobs' / _digest(project_id)
    return any(_state(path)['status'] not in TERMINAL for path in _job_paths(root))


def has_idempotent_job(cfg, project_id, idem):
    job_id = 'arena-' + _digest([project_id, idem])[:32]
    return (_root(cfg, project_id) / job_id / 'state.json').is_file()


def list_jobs(cfg, project_id):
    paths = sorted(_jobs(cfg, project_id), key=lambda path: (path / 'job.json').stat().st_mtime, reverse=True)
    return [_snapshot(path, cfg) for path in paths[:30]]


def get_job(cfg, project_id, job_id):
    return _snapshot(_directory(cfg, project_id, job_id), cfg, include_result=True)


def read_events(cfg, project_id, job_id, after):
    state = _state(_directory(cfg, project_id, job_id))
    return [event for event in state['events'] if event['cursor'] > after], state['status'] in TERMINAL


def cancel_job(cfg, project_id, job_id):
    path = _directory(cfg, project_id, job_id)
    if _state(path)['status'] not in TERMINAL and not (path / 'cancel.json').exists():
        atomic_write_json(path / 'cancel.json', {'requested_at': time.time()})
    return _snapshot(path, cfg, include_result=True)


def _launch(path, cfg):
    allow = adapter_env_allow(cfg.adapters)
    with (path / 'worker.log').open('ab') as log:
        proc = subprocess.Popen(
            [sys.executable, '-X', 'utf8', '-m', 'orchestrator.collaboration.arena_jobs', str(path)],
            cwd=Path(__file__).resolve().parents[2], env=build_subprocess_env(allow),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=os.name != 'nt',
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0,
        )
    threading.Thread(target=proc.wait, daemon=True).start()


def start_job(cfg, config_path, project_id, idem, body):
    request = validate_arena_request(cfg, body.get('task', ''), body.get('candidates', []),
                                     test_preset=body.get('test_preset', ''), allowed_files=body.get('allowed_files'))
    fingerprint = _digest(request)
    job_id = 'arena-' + _digest([project_id, idem])[:32]
    root = _root(cfg, project_id)
    path = root / job_id
    lock = StageLock(root / '.start.lock')
    if not lock.acquire():
        raise Conflict('竞技场正在登记，请重试')
    try:
        if (path / 'state.json').is_file():
            if _read(path / 'job.json')['fingerprint'] != fingerprint:
                raise Conflict('同一幂等标识已用于不同的竞技场请求')
            return _snapshot(path, cfg)
        if has_active_job(cfg, project_id):
            raise Conflict('该项目已有竞技场任务正在执行')
        config_path = Path(config_path).resolve()
        if cfg.source_path != config_path or not cfg.source_hash:
            raise Conflict('无法确认竞技场使用的项目配置')
        payload = {'request': request, 'fingerprint': fingerprint, 'config': str(config_path),
                   'config_sha256': cfg.source_hash,
                   'base_sha': arena_baseline(cfg)}
        path.mkdir(exist_ok=True)
        atomic_write_json(path / 'job.json', payload)
        state = {'arena_id': job_id, 'created': time.time(), 'task': request['task'], 'events': [],
                 'base_sha': payload['base_sha'], 'candidates': [dict(c, status='queued') for c in request['candidates']]}
        _event(path, state, 'queued')
        try:
            _launch(path, cfg)
        except OSError as exc:
            _event(path, state, 'failed', reason=f'无法启动竞技场后台进程。{exc}')
        return _snapshot(path, cfg)
    finally:
        lock.release()


def run_worker(path):
    lock = StageLock(path / '.worker.lock')
    if not lock.acquire():
        return
    try:
        state = _read(path / 'state.json')
        if state['status'] != 'queued':
            return
        cancelled = (path / 'cancel.json').is_file
        if cancelled():
            _event(path, state, 'cancelled')
            return
        _event(path, state, 'running')
        updates = threading.Lock()

        def progress(label, status, *, candidate=None):
            with updates:
                if candidate is not None:
                    atomic_write_json(path / f'candidate_{label}.json', candidate)
                _event(path, state, status, label=label)

        try:
            payload = _read(path / 'job.json')
            cfg = load_config(payload['config'])
            if cfg.source_hash != payload['config_sha256']:
                raise RuntimeError('竞技场排队期间项目配置已变化')
            if path.resolve().parents[2] != cfg.state_dir.resolve():
                raise RuntimeError('竞技场状态目录与项目配置不一致')
            result = run_arena(cfg, **payload['request'], arena_id=state['arena_id'],
                               expected_base_sha=payload['base_sha'], cancel_requested=cancelled,
                               on_progress=progress)
            if cancelled():
                _event(path, state, 'cancelled')
            elif any(candidate['ok'] for candidate in result['candidates']):
                _event(path, state, 'done')
            else:
                _event(path, state, 'failed', reason='全部候选执行失败')
        except Exception as exc:
            if cancelled():
                _event(path, state, 'cancelled')
            else:
                _event(path, state, 'failed', reason=str(exc)[:500])
    finally:
        lock.release()


if __name__ == '__main__':
    run_worker(Path(sys.argv[1]))
