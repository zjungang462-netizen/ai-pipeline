"""已提交状态的可重建投影与受限产物读取。"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from ..state import sha256_file
from ..safety import relative_target


def read_json(path, default=None):
    try:
        result = json.loads(Path(path).read_text(encoding='utf8'))
        return result if isinstance(result, dict) else (default or {})
    except (OSError, ValueError):
        return default or {}


def directory(run):
    return Path(run['state_dir']) / 'runs' / run['run_id']


def stage_target(stage):
    return {'attempt': stage.get('current_attempt'), 'inputs_hash': stage.get('inputs_hash'),
            'outputs_hash': stage.get('outputs_hash')}


def lifecycle(state, job):
    if job.get('status'):
        return job['status']
    stages = list(state.get('stages', {}).values())
    statuses = {s.get('status') for s in stages}
    if 'awaiting_approval' in statuses:
        return 'waiting_approval'
    if 'running' in statuses:
        return 'running'
    if 'failed' in statuses:
        return 'failed'
    if stages and statuses == {'done'}:
        return 'done'
    return 'pending'


def project(store, run):
    root = directory(run)
    state_path, job_path = root / 'state.json', root / 'job.json'
    state, job = read_json(state_path), read_json(job_path)
    stamps = []
    for path in (state_path, job_path):
        try:
            stat = path.stat()
            stamps.append((stat.st_size, stat.st_mtime_ns))
        except OSError:
            stamps.append(None)
    signature = repr(stamps)
    with store.connect() as db:
        old = db.execute('SELECT signature FROM projection_offsets WHERE run_id=?', (run['id'],)).fetchone()
        if old and old[0] == signature:
            return state, job
        for index, event in enumerate(state.get('events', [])):
            event_id = run['id'] + ':engine:' + str(event.get('event_id') or index)
            store._event(db, event_id, run['id'], 'engine_event', event, event.get('ts'))
        store._event(db, run['id'] + ':lifecycle:' + hashlib.sha256(signature.encode()).hexdigest(),
                     run['id'], 'snapshot_changed', {'lifecycle': lifecycle(state, job),
                                                     'revision': state.get('revision', 0)})
        db.execute('INSERT INTO projection_offsets VALUES(?,?,?) ON CONFLICT(run_id) DO UPDATE SET signature=excluded.signature,updated=excluded.updated',
                   (run['id'], signature, time.time()))
    # 只有已归档成功、或已记录结束事件的 attempt 才能显示模型/工具结果。
    closed = {(e.get('stage'), e.get('n')) for e in state.get('events', [])
              if e.get('kind') in ('attempt_done', 'attempt_fail', 'test_pass', 'test_fail')}
    for stage_name, stage in state.get('stages', {}).items():
        attempts = root / 'stages' / stage_name / 'attempts'
        if not attempts.is_dir():
            continue
        for attempt in sorted(attempts.iterdir()):
            if not attempt.is_dir() or not attempt.name.isdigit():
                continue
            number = int(attempt.name)
            if not (attempt / 'success.json').is_file() and (stage_name, number) not in closed:
                continue
            reference = [{'stage': stage_name, 'attempt': number, 'artifact_id': f'{stage_name}/{number}/stdout.txt'}]
            if (attempt / 'test_record.json').is_file():
                record = read_json(attempt / 'test_record.json')
                body = f"命令：{' '.join(record.get('cmd', []))}\n退出码：{record.get('exit_code')}\n耗时：{record.get('duration_s', 0):.2f}s\n\n{record.get('output', '')}"
                source, role = 'tool_result', '测试器'
            else:
                text_path = attempt / 'final_text.txt'
                if text_path.is_file():
                    body = text_path.read_text(encoding='utf8', errors='replace')
                else:
                    raw = read_json(attempt / 'stdout.txt')
                    try:
                        body = raw['choices'][0]['message']['content']
                    except (KeyError, IndexError, TypeError):
                        body = ''
                if not body:
                    continue
                source, role = 'agent_response', stage_name
            key = run['id'] + ':output:' + stage_name + ':' + attempt.name
            store.message(run['id'], body[:31000] + ('\n（显示已截断，查看原始产物）' if len(body) > 31000 else ''),
                          key, source=source, role=role, refs=reference,
                          key='msg-' + hashlib.sha256(key.encode()).hexdigest()[:24])
    return state, job


def artifact_list(run):
    root = directory(run)
    result = []
    for success in root.glob('stages/*/attempts/*/success.json'):
        attempt = success.parent
        manifest = read_json(attempt / 'manifest.json')
        files = manifest.get('files', {})
        if not isinstance(files, dict):
            continue
        record = read_json(success)
        for name, expected in files.items():
            if not isinstance(name, str) or Path(name).name != name:
                continue
            result.append({'id': f'{attempt.parent.parent.name}/{attempt.name}/{name}',
                           'stage': attempt.parent.parent.name, 'attempt': int(attempt.name),
                           'name': name, 'expected_hash': expected, 'commit': record.get('commit_sha'),
                           'integrity': 'not_checked'})
    return result


def read_artifact(run, artifact_id):
    parts = artifact_id.split('/')
    if len(parts) != 3 or not parts[1].isdigit() or any(not p or p in ('.', '..') for p in parts):
        raise ValueError('非法产物标识')
    entry = next((a for a in artifact_list(run) if a['id'] == artifact_id), None)
    if not entry:
        raise KeyError('该文件不在已封存产物清单中')
    target = relative_target(directory(run), f'stages/{parts[0]}/attempts/{parts[1]}/{parts[2]}')
    if not target.is_file():
        return {**entry, 'integrity': 'missing', 'body': ''}
    integrity = 'valid' if sha256_file(target) == entry['expected_hash'] else 'changed'
    with target.open(encoding='utf8', errors='replace') as stream:
        text = stream.read(200001)
    return {**entry, 'integrity': integrity, 'body': text[:200000], 'truncated': len(text) > 200000}
