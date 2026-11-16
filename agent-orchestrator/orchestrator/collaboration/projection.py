"""已提交状态的可重建投影与受限产物读取。"""
from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from threading import RLock
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


def _signature(root):
    stamps = []
    for path in (root / 'state.json', root / 'job.json'):
        try:
            stat = path.stat()
            stamps.append((stat.st_size, stat.st_mtime_ns))
        except OSError:
            stamps.append(None)
    return repr(stamps)


def _state_pair(root, signature):
    """摘要与完整投影共用双文件一致性检查，读摘要不提交投影游标。"""
    for _ in range(4):
        state, job = read_json(root / 'state.json'), read_json(root / 'job.json')
        after = _signature(root)
        if after == signature:
            return signature, state, job
        signature = after
    raise OSError('运行状态正在连续提交，请重试快照')


class SummaryCache:
    """仅缓存标题和状态；有界、按文件签名失效，不含正文或投影进度。"""
    def __init__(self, limit=2048):
        self.limit = limit
        self.rows = OrderedDict()
        self.lock = RLock()

    def read(self, run):
        root = directory(run)
        key = (run['id'], str(root))
        signature = _signature(root)
        with self.lock:
            saved = self.rows.get(key)
            if saved is not None and saved[0] == signature:
                self.rows.move_to_end(key)
                return saved[1], saved[2]
        signature, state, job = _state_pair(root, signature)
        task = state.get('task') or job.get('task', '')
        status = lifecycle(state, job)
        with self.lock:
            self.rows[key] = (signature, task, status)
            self.rows.move_to_end(key)
            while len(self.rows) > self.limit:
                self.rows.popitem(last=False)
        return task, status


def project(store, run, cache=None):
    root = directory(run)
    signature = _signature(root)

    # 不能把旧 state 与随后提交的终态 job 拼成一次看似已完成的快照。
    if cache is not None and run['id'] in cache:
        saved_signature, state, job = cache[run['id']]
        if saved_signature == signature:
            return state, job
    signature, state, job = _state_pair(root, signature)
    with store.connect() as db:
        old = db.execute('SELECT signature FROM projection_offsets WHERE run_id=?', (run['id'],)).fetchone()
        if old and old[0] == signature:
            if cache is not None:
                cache[run['id']] = (signature, state, job)
            return state, job
        for index, event in enumerate(state.get('events', [])):
            event_id = run['id'] + ':engine:' + str(event.get('event_id') or index)
            store._event(db, event_id, run['id'], 'engine_event', event, event.get('ts'))
        store._event(db, run['id'] + ':lifecycle:' + hashlib.sha256(signature.encode()).hexdigest(),
                     run['id'], 'snapshot_changed', {'lifecycle': lifecycle(state, job),
                                                     'revision': state.get('revision', 0)})
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
                execution = read_json(attempt / 'execution.json')
                raw = read_json(attempt / 'stdout.txt')
                text_path = attempt / 'final_text.txt'
                if text_path.is_file():
                    body = text_path.read_text(encoding='utf8', errors='replace')
                else:
                    raw = read_json(attempt / 'stdout.txt')
                    try:
                        body = raw['choices'][0]['message']['content']
                    except (KeyError, IndexError, TypeError):
                        body = ''
                if not isinstance(body, str) or not body:
                    continue
                source, role = 'agent_response', stage_name
                if str(execution.get('tool_version', '')).startswith('fake'):
                    source, role = 'system_summary', '离线示例'
                reference[0].update(call_id=raw.get('id'), returned_model=raw.get('model'))
                usage = execution.get('usage', {})
                reference[0].update(adapter_channel=execution.get('channel'), tool_version=execution.get('tool_version'))
                if isinstance(usage, dict):
                    reference[0].update(cli_model=usage.get('model'), requested_model=usage.get('requested_model'))
            key = run['id'] + ':output:' + stage_name + ':' + attempt.name
            store.message(run['id'], body[:31000] + ('\n（显示已截断，查看原始产物）' if len(body) > 31000 else ''),
                          key, source=source, role=role, refs=reference,
                          key='msg-' + hashlib.sha256(key.encode()).hexdigest()[:24])
    with store.connect() as db:
        db.execute('INSERT INTO projection_offsets VALUES(?,?,?) ON CONFLICT(run_id) DO UPDATE SET signature=excluded.signature,updated=excluded.updated',
                   (run['id'], signature, time.time()))
    if cache is not None:
        cache[run['id']] = (signature, state, job)
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


def code_diff(run, stage_name, mode='previous'):
    import re
    from ..config import load_config
    from .. import artifacts
    from ..evidence import validate
    cfg = load_config(run['config'])
    spec = cfg.stage(stage_name)
    if spec.kind != 'code' or mode not in ('previous', 'project'):
        raise ValueError('请选择代码阶段及支持的比较范围')
    state = read_json(directory(run) / 'state.json')
    stage = state.get('stages', {}).get(stage_name, {})
    head = stage.get('commit_sha')
    if not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', head):
        raise ValueError('没有可信的代码 commit 指针')
    attempt = relative_target(directory(run), f'stages/{stage_name}/attempts/{stage.get("current_attempt")}')
    record = read_json(attempt / 'success.json')
    manifest = read_json(attempt / 'manifest.json')
    repository = manifest.get('code', {}).get('repository') if isinstance(manifest.get('code'), dict) else None
    if repository is None or Path(repository).resolve() != cfg.task_workdir.resolve() or not validate(attempt, record):
        raise ValueError('代码产物完整性未通过，不能作为已验证差异展示')
    base = None
    if mode == 'previous':
        prior = [a.get('commit_sha') for a in stage.get('attempts', [])
                 if a.get('n', 0) < stage.get('current_attempt', 0) and a.get('commit_sha')]
        if prior:
            base = prior[-1]
        else:
            value = artifacts._git(['rev-parse', head + '^'], cfg.task_workdir, check=False)
            if value.returncode == 0:
                base = value.stdout.strip()
    else:
        base = state.get('control', {}).get('project_baseline') or artifacts.baseline_sha(cfg.task_workdir)
    if base and not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', base):
        raise ValueError('代码比较基线非法')
    args = ['diff', '--no-ext-diff', '--no-textconv', base, head, '--'] if base else ['show', '--no-ext-diff', '--no-textconv', '--format=', head, '--']
    text = artifacts._git(args, cfg.task_workdir, timeout=15).stdout
    return {'stage': stage_name, 'mode': mode, 'base': base, 'head': head,
            'body': text[:200000], 'truncated': len(text) > 200000, 'integrity': 'valid'}
