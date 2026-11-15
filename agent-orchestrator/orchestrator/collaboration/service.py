"""本地控制服务，不接受浏览器提供的任意磁盘路径或命令。"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from ..config import load_config
from ..state import RunState, atomic_write_json
from .storage import Store, Conflict, identifier
from .projection import project, directory, lifecycle, artifact_list, read_json, stage_target


class Service:
    def __init__(self, database, configs):
        self.store = Store(database)
        self.operations = threading.RLock()
        self.active = {}
        self.closed = threading.Event()
        for path in configs:
            cfg = load_config(path)
            self.store.register_project(cfg.source_path)
        self.sync()

    def redact(self, value):
        secrets = []
        for key, secret in os.environ.items():
            if len(secret) >= 8 and any(s in key.upper() for s in ('API_KEY', 'AUTH_TOKEN', 'ACCESS_TOKEN')):
                secrets.append(secret)
        def scrub(item):
            if isinstance(item, str):
                for secret in secrets:
                    item = item.replace(secret, '[REDACTED]')
                return item
            if isinstance(item, list):
                return [scrub(v) for v in item]
            if isinstance(item, dict):
                return {k: scrub(v) for k, v in item.items()}
            return item
        return scrub(value)

    def sync(self):
        for entry in self.store.projects():
            cfg = load_config(entry['config'])
            root = cfg.state_dir / 'runs'
            if root.is_dir():
                for path in root.iterdir():
                    if path.is_dir() and (path / 'state.json').is_file():
                        try:
                            RunState(cfg.state_dir, path.name)
                        except ValueError:
                            continue
                        self.store.register_run(entry['id'], path.name, entry['config'], cfg.state_dir)
        for run in self.store.runs():
            state, job = project(self.store, run)
            if run['managed'] and lifecycle(state, job) in ('running', 'pending', 'recovering'):
                self.launch(run['id'])
            # state 先提交、收据尚未投影时，补齐命令而不重复执行。
            for command_id, receipt in state.get('control', {}).get('receipts', {}).items():
                try:
                    self.store.finish(command_id, receipt['status'], receipt['result'])
                except KeyError:
                    pass

    def snapshot(self, key):
        run = self.store.run(key)
        state, job = project(self.store, run)
        status = lifecycle(state, job)
        managed = bool(run['managed'])
        active = status in ('pending', 'running', 'recovering')
        paused = status == 'paused'
        waiting = status == 'waiting_approval'
        data = {'id': key, 'run_id': run['run_id'], 'project_id': run['project_id'],
                'task': state.get('task') or job.get('task', ''), 'lifecycle': status,
                'revision': state.get('revision', 0), 'stages': state.get('stages', {}),
                'pending_approvals': state.get('pending_approvals', {}),
                'usage': state.get('usage_total', {}), 'rounds': state.get('review_rounds', {}),
                'autonomy': {'convergence': state.get('autonomy', {}).get('convergence', {})},
                'capabilities': {'pause': managed and active, 'resume': managed and paused,
                                 'cancel': managed and status in ('pending', 'running', 'recovering', 'paused', 'waiting_approval'),
                                 'approve': managed and waiting, 'derive': True,
                                 'discussion': bool(state.get('stages'))},
                'parent_id': run['parent_id'], 'managed': managed,
                'last_cursor': self.store.last_cursor(key),
                'deadline': job.get('started_at', 0) + job.get('max_duration_s', 0) if job else None,
                'artifacts': artifact_list(run), 'threads': self.store.threads(key)}
        return self.redact(data)

    def create_run(self, project_id, task, idem, parent=None):
        if not isinstance(task, str) or not task.strip() or len(task) > 64000:
            raise ValueError('任务必须为 1 到 64000 字符')
        entry = self.store.project(project_id)
        cfg = load_config(entry['config'])
        if not cfg.run_budget.max_tokens or not cfg.run_budget.max_duration_s:
            raise ValueError('配置必须显式限制 token 和时长')
        if parent:
            original = self.store.run(parent)
            if original['project_id'] != project_id:
                raise ValueError('后续任务必须属于同一项目')
            if self.snapshot(parent)['lifecycle'] in ('pending', 'running', 'recovering'):
                raise ValueError('请先暂停或结束旧运行，再从固定状态创建后续任务')
        # 确定性 run_id 使创建任务的网络重试不会创建另一份运行。
        run_id = 'web-' + hashlib.sha256((project_id + ':' + idem).encode()).hexdigest()[:16]
        with self.operations:
            root = cfg.state_dir / 'runs' / run_id
            if (root / 'job.json').is_file():
                job = read_json(root / 'job.json')
                if job.get('task') != task:
                    raise Conflict('同一创建标识不能用于不同任务')
            else:
                if any(r['project_id'] == project_id and self.snapshot(r['id'])['lifecycle'] in
                       ('pending', 'running', 'recovering', 'paused', 'waiting_approval') for r in self.store.runs()):
                    raise Conflict('此项目已有未结束任务；请先处理或取消')
                from ..state import sha256_text
                job = {'run_id': run_id, 'task': task, 'task_hash': sha256_text(task),
                       'requested_config': str(cfg.source_path), 'config': str(cfg.source_path),
                       'config_hash': cfg.source_hash, 'started_at': time.time(),
                       'max_duration_s': cfg.run_budget.max_duration_s, 'max_restarts': 2,
                       'restarts_used': 0, 'status': 'pending', 'control_db': str(self.store.path),
                       'run_key': project_id + ':' + run_id}
                atomic_write_json(root / 'job.json', job)
            key = self.store.register_run(project_id, run_id, cfg.source_path, cfg.state_dir,
                                          managed=True, parent=parent)
            self.store.event(key + ':created', key, 'run_created', {'task': task, 'parent_id': parent})
            self.launch(key)
            return self.snapshot(key)

    def launch(self, key):
        with self.operations:
            if key in self.active and self.active[key].is_alive():
                return
            thread = threading.Thread(target=self._run, args=(key,), daemon=True)
            self.active[key] = thread
            thread.start()

    def _run(self, key):
        run = self.store.run(key)
        jobfile = directory(run) / 'job.json'
        job = read_json(jobfile)
        try:
            from ..supervisor import supervise
            supervise(Path(job['config']), run['run_id'], job['task'],
                      control_db=self.store.path, run_key=key)
        except Exception as error:
            self.store.event(identifier('error'), key, 'service_error', {'reason': self.redact(str(error))})
        finally:
            project(self.store, run)

    def command(self, key, intent, payload, idem):
        if intent not in ('pause', 'resume', 'cancel', 'approve'):
            raise ValueError('不支持的界面操作；改变需求请创建后续任务')
        with self.operations:
            run = self.store.run(key)
            current = self.snapshot(key)
            if not current['capabilities'].get(intent):
                raise Conflict('当前状态不允许此操作')
            if intent == 'approve':
                stage = payload.get('stage')
                if stage not in current['pending_approvals'] or payload.get('target') != stage_target(current['stages'][stage]):
                    raise Conflict('审批目标已过期')
            receipt = self.store.enqueue(key, intent, payload, idem)
            self.launch(key)
            return receipt

    def discuss(self, key, question, roles, budget, idem):
        run = self.store.run(key)
        if not isinstance(question, str) or not 1 <= len(question) <= 4000:
            raise ValueError('问题必须为 1 到 4000 字符')
        if not isinstance(roles, list) or not 1 <= len(roles) <= 2 or len(set(roles)) != len(roles) or not set(roles) <= {'developer', 'reviewer'}:
            raise ValueError('会诊最多选择开发者和审查者两个角色')
        if type(budget) is not int or not 1000 <= budget <= 6000:
            raise ValueError('会诊额度必须在 1000 到 6000 tokens 之间')
        status = self.snapshot(key)['lifecycle']
        if status in ('pending', 'running', 'recovering'):
            raise Conflict('请先在检查点暂停运行，再对固定快照发起会诊')
        from .discussion import capture
        topic = self.store.create_thread(key, question, roles, budget, capture(run), idem)
        if topic['status'] == 'queued':
            threading.Thread(target=self._discuss, args=(topic['id'],), daemon=True).start()
        return topic

    def _discuss(self, topic):
        from .discussion import execute
        try:
            execute(self.store, topic)
        except Exception as error:
            self.store.update_thread(topic, 'failed', {'reason': self.redact(str(error))})
