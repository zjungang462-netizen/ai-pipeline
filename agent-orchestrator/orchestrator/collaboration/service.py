"""本地控制服务，不接受浏览器提供的任意磁盘路径或命令。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import subprocess
import sys
import time
from pathlib import Path

from ..config import load_config
from ..state import RunState, atomic_write_json
from .storage import Store, Conflict, identifier
from .projection import project, directory, lifecycle, artifact_list, read_json, stage_target, SummaryCache
from .execution_scope import execution_scope
from .read_cache import ReadCache, file_stamp
from . import run_listing


class Service:
    def __init__(self, database, configs, *, recover=True):
        self.store = Store(database)
        self.recover = recover
        self.operations = threading.RLock()
        self.active = {}
        self.active_topics = {}
        self._command_waiters = set()
        self._configs = {}
        self._projection_cache = {}
        self._summary_cache = SummaryCache()
        self._room_artifacts = ReadCache(64)
        self._room_json = ReadCache(256)
        self._published_activity = ReadCache(512)
        self.closed = threading.Event()
        self._sync_lock = threading.RLock()
        from .background_sync import BackgroundSync
        self._background_sync = BackgroundSync(self)
        self.last_sync_stats = {}
        self.last_sync_error = ''
        for path in configs:
            # 只登记路径，不在启动阶段解析配置：单个配置损坏/被删除时服务仍须可用。
            self.store.register_project(path)
        try:
            self.sync(recover=self.recover)
        except Exception as error:
            # 单个坏配置/畸形运行目录不应阻止工作台启动；房间仍可打开，错误留给诊断。
            self.last_sync_error = self.redact(str(error) or error.__class__.__name__)

    def config(self, path):
        key = str(path)
        signature = file_stamp(Path(path))
        cached = self._configs.get(key)
        if cached is None or cached[0] != signature:
            cached = (signature, load_config(path))
            self._configs[key] = cached
        return cached[1]

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

    def list_projects(self):
        """创建前展示真实工作目录和阶段能力，不实例化模型执行器。

        单个登记配置损坏/被删除时只把该条目标记为 unavailable，不影响其他项目。
        """
        result = []
        for entry in self.store.projects():
            try:
                cfg = self.config(entry['config'])
                from .model_settings import describe_models
                result.append({**entry, 'execution_scope': execution_scope(cfg),
                               'config_hash': cfg.source_hash, 'models': describe_models(cfg),
                               'unlimited_tokens': cfg.run_budget.unlimited_tokens})
            except Exception as error:
                result.append({**entry, 'execution_scope': None, 'config_hash': None,
                               'unavailable': (str(error) or error.__class__.__name__)[:200]})
        return self.redact(result)

    def sync(self, recover=False, incremental=False):
        with self._sync_lock:
            stats = self._background_sync.sync(recover, incremental)
            self.last_sync_stats = dict(stats)
            self.last_sync_error = ''
            return stats

    def sync_run_events(self, run):
        """只同步事件消费者所需的投影和当前 CLI 活动。"""
        state, job = project(self.store, run, self._projection_cache)
        from ..cli_activity import read_activities
        activities = read_activities(run, state, read_progress=self._room_json.read_json)
        key = run['id']
        fresh = [activity for activity in activities if self._published_activity.lookup(
            (key, activity['call_id']), activity['revision']) is None]
        if fresh:
            for activity, clean in zip(fresh, self.redact(fresh)):
                call_id, revision = activity['call_id'], activity['revision']
                self.store.event(key+':cli:'+call_id+':'+str(revision), key, 'cli_progress', clean)
                self._published_activity.remember((key, call_id), revision, True)
        return state, job, activities

    def _artifact_view(self, run, state, job):
        key = (run['id'], directory(run))
        version = (state, job)
        cached = self._room_artifacts.lookup(key, version)
        if cached is not None:
            return cached
        artifacts = artifact_list(run)
        # 封存可能早于状态提交，离线导入也会补文件，因此定期重扫展示目录。
        self._room_artifacts.remember(key, version, artifacts, ttl=2)
        return artifacts

    def snapshot(self, key):
        run = self.store.run(key)
        state, job, activities = self.sync_run_events(run)
        status = lifecycle(state, job)
        managed = bool(run['managed'])
        active = status in ('pending', 'running', 'recovering')
        paused = status in ('paused', 'waiting_external', 'waiting_owner')
        waiting = status == 'waiting_approval'
        data = {'id': key, 'run_id': run['run_id'], 'project_id': run['project_id'],
                'task': state.get('task') or job.get('task', ''), 'lifecycle': status,
                'revision': state.get('revision', 0), 'stages': state.get('stages', {}),
                'pending_approvals': state.get('pending_approvals', {}),
                'pending_waits': state.get('pending_waits', {}),
                'usage': state.get('usage_total', {}), 'rounds': state.get('review_rounds', {}),
                'autonomy': {'convergence': state.get('autonomy', {}).get('convergence', {})},
                'capabilities': {'pause': managed and active, 'resume': managed and paused,
                                 'cancel': managed and status in ('pending', 'running', 'recovering', 'paused',
                                                                  'waiting_approval', 'waiting_external', 'waiting_owner'),
                                 'approve': managed and waiting, 'derive': True,
                                 'rerun': managed and status in ('paused', 'waiting_external', 'waiting_owner', 'done', 'failed'),
                                 'discussion': bool(state.get('stages'))},
                'parent_id': run['parent_id'], 'managed': managed,
                'deadline': job.get('started_at', 0) + job.get('max_duration_s', 0) if job else state.get('control',{}).get('cli_deadline'),
                'artifacts': self._artifact_view(run, state, job), 'threads': self.store.threads(key),
                'cli_activity': activities}
        data['usage'] = dict(data['usage'])
        data['usage'].setdefault('reported_tokens', max(0, data['usage'].get('tokens', 0) - data['usage'].get('reserved_tokens', 0)))
        data['last_cursor']=self.store.last_cursor(key)
        cfg = self.config(run['config'])
        data['budget'] = {'max_tokens': cfg.run_budget.max_tokens, 'max_duration_s': cfg.run_budget.max_duration_s}
        data['budget']['unlimited_tokens'] = cfg.run_budget.unlimited_tokens
        from .model_settings import describe_models
        data['models'] = describe_models(cfg)
        data['code_stages'] = [s.name for s in cfg.stages if s.kind == 'code' and state.get('stages', {}).get(s.name, {}).get('commit_sha')]
        try:
            metrics = self._room_json.read_json(directory(run) / 'metrics.json')
        except (OSError, ValueError):
            metrics = None
        data['metrics'] = metrics if isinstance(metrics, dict) else {}
        observed_usage=(data['metrics'].get('stage_usage',{}) if data['metrics'] else state.get('stage_usage',{}))
        if isinstance(observed_usage,dict):
            for model in data['models']:
                rows=[observed_usage.get(spec.name,{}) for spec in cfg.stages if spec.adapter==model['name']]
                model['observed_models']=sorted({r['backend_model'] for r in rows if isinstance(r,dict) and isinstance(r.get('backend_model'),str)})
                model['observed_backend_endpoints']=sorted({r['backend_endpoint'] for r in rows if isinstance(r,dict) and isinstance(r.get('backend_endpoint'),str)})
        data['strategy'] = {'proposal_id': cfg.tune_proposal_id,
                            'repair_batch_size': cfg.autonomy.repair_batch_size,
                            'auto_apply': cfg.autotune.auto_apply}
        data['capabilities']['derive'] = any(s.get('commit_sha') for s in state.get('stages', {}).values())
        data['capabilities']['discussion'] = bool(state.get('stages')) and any(a.type == 'openai_compat' for a in cfg.adapters.values())
        data['config_integrity'] = ('not_recorded' if not job.get('config_hash') else
                                    'valid' if cfg.source_hash == job['config_hash'] else 'changed')
        from ..reporting import budget_alerts
        # 配置变化后不依据新限额给旧任务的运行预算作结论。
        data['budget_alerts'] = (budget_alerts(data['usage'], data['budget'], deadline=data['deadline'])
                                 if data['config_integrity'] != 'changed' else [])
        data['execution_scope'] = execution_scope(cfg)
        data['execution_scope']['matches_run_config'] = data['config_integrity'] == 'valid'
        if data['config_integrity'] == 'changed':
            for action in ('approve', 'resume', 'rerun', 'derive', 'discussion'):
                data['capabilities'][action] = False
        return self.redact(data)

    _CURSOR_VERSION = run_listing.CURSOR_VERSION
    _CURSOR_MAX_LEN = run_listing.CURSOR_MAX_LEN
    _STATUS_GROUPS = run_listing.STATUS_GROUPS

    def list_runs(self, limit=100, before=None):
        """列表只读摘要；产物、会诊和长记录在进入任务房间后加载。"""
        return run_listing.list_runs(self.store, self._summary_cache, self.redact, limit=limit, before=before)

    def _encode_cursor(self, created: float, run_id: str, before: float | None,
                       q: str = '', project_id: str = '', status: str = 'all',
                       sort: str = 'newest') -> str:
        return run_listing.encode_cursor(created, run_id, before, q, project_id, status, sort)

    def _decode_cursor(self, cursor: str) -> tuple[float, str, float | None, str, str, str, str]:
        return run_listing.decode_cursor(cursor)

    def list_runs_page(self, limit: int = 50, before: float | None = None, cursor: str | None = None,
                       q: str | None = None, project_id: str | None = None,
                       status: str | None = None, sort: str | None = None):
        """稳定游标分页，支持全历史筛选；返回 {runs, has_more, next_cursor}。"""
        return run_listing.list_runs_page(
            self.store, self._summary_cache, self.redact,
            limit=limit, before=before, cursor=cursor,
            q=q, project_id=project_id, status=status, sort=sort,
        )

    def create_run(self, project_id, task, idem, parent=None, expected_config_hash=None, execution_options=None):
        if not isinstance(task, str) or not task.strip() or len(task) > 64000:
            raise ValueError('任务必须为 1 到 64000 字符')
        if expected_config_hash is not None and not isinstance(expected_config_hash, str):
            raise ValueError('expected_config_hash 必须为字符串')
        entry = self.store.project(project_id)
        cfg = load_config(entry['config'])
        project_config_hash = cfg.source_hash
        from .model_settings import validate_options, prepare_config, config_copy
        overrides=validate_options(cfg,execution_options) if execution_options is not None else None
        if overrides is not None:prepare_config(cfg,overrides)
        # 页面确认过的执行范围必须与创建时配置一致，防止会话中改配置后按新范围执行。
        if expected_config_hash is not None and expected_config_hash != cfg.source_hash:
            raise Conflict('配置已变化，请刷新后重试')
        if not cfg.run_budget.max_duration_s or (not cfg.run_budget.max_tokens and
                not cfg.run_budget.unlimited_tokens and not (overrides and overrides['unlimited_tokens'])):
            raise ValueError('配置必须显式限制 token 和时长')
        if parent:
            original = self.store.run(parent)
            if original['project_id'] != project_id:
                raise ValueError('后续任务必须属于同一项目')
            if expected_config_hash is not None:
                original_cfg = load_config(original['config'])
                parent_job = read_json(directory(original) / 'job.json')
                bound_override = (parent_job.get('execution_options') is not None and
                                  parent_job.get('source_project_config_hash') == expected_config_hash and
                                  parent_job.get('config_hash') == original_cfg.source_hash)
                if expected_config_hash != original_cfg.source_hash and not bound_override:
                    raise Conflict('配置已变化，请刷新后重试')
            if self.snapshot(parent)['lifecycle'] in ('pending', 'running', 'recovering'):
                raise ValueError('请先暂停或结束旧运行，再从固定状态创建后续任务')
        # 确定性 run_id 使创建任务的网络重试不会创建另一份运行。
        run_id = 'web-' + hashlib.sha256((project_id + ':' + idem).encode()).hexdigest()[:16]
        with self.operations:
            root = cfg.state_dir / 'runs' / run_id
            new = not (root / 'job.json').is_file()
            if (root / 'job.json').is_file():
                job = read_json(root / 'job.json')
                if job.get('task') != task:
                    raise Conflict('同一创建标识不能用于不同任务')
                if job.get('execution_options') != overrides:
                    raise Conflict('同一创建标识不能更换模型或 token 选项')
            else:
                if any(r['id'] != parent and r['project_id'] == project_id and self.snapshot(r['id'])['lifecycle'] in
                       ('pending', 'running', 'recovering', 'paused', 'waiting_approval',
                        'waiting_external', 'waiting_owner') for r in self.store.runs()):
                    raise Conflict('此项目已有未结束任务；请先处理或取消')
                if parent:
                    from .config_copy import derive
                    cfg = load_config(derive(original, run_id))
                if overrides is not None:
                    cfg=config_copy(cfg,overrides,run_id)
                from ..state import sha256_text
                job = {'run_id': run_id, 'task': task, 'task_hash': sha256_text(task),
                       'requested_config': str(cfg.source_path), 'config': str(cfg.source_path),
                       'config_hash': cfg.source_hash, 'started_at': time.time(),
                       'max_duration_s': cfg.run_budget.max_duration_s, 'max_restarts': 2,
                       'restarts_used': 0, 'status': 'pending', 'control_db': str(self.store.path),
                       'run_key': project_id + ':' + run_id, 'execution_options': overrides,
                       'source_project_config_hash': project_config_hash}
                atomic_write_json(root / 'job.json', job)
            key = self.store.register_run(project_id, run_id, job['config'], cfg.state_dir,
                                          managed=True, parent=parent)
            self.store.event(key + ':created', key, 'run_created', {'task': task, 'parent_id': parent})
            if new or job.get('status') in ('pending', 'running', 'recovering'):
                self.launch(key)
            return self.snapshot(key)

    def launch(self, key):
        with self.operations:
            if key in self.active and self.active[key].poll() is None:
                return
            run = self.store.run(key)
            from ..state import _pid_alive
            lock = read_json(directory(run) / '.supervisor.lock')
            if lock and _pid_alive(lock.get('pid')):
                return
            cfg = load_config(run['config'])
            from ..safety import build_subprocess_env
            allow = [a.options['api_key_env'] for a in cfg.adapters.values() if a.type == 'openai_compat']
        if os.environ.get('PIPELINE_STREAM_DISCUSSION') == '1':
            allow.append('PIPELINE_STREAM_DISCUSSION')
            with (directory(run) / 'manager.log').open('ab') as log:
                self.active[key] = subprocess.Popen(
                    [sys.executable, '-X', 'utf8', '-m', 'orchestrator.collaboration.runner', str(self.store.path), key],
                    cwd=Path(__file__).resolve().parents[2], env=build_subprocess_env(allow),
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=os.name != 'nt',
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)

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
        if intent not in ('pause', 'resume', 'cancel', 'approve', 'rerun'):
            raise ValueError('不支持的界面操作；改变需求请创建后续任务')
        allowed = {'approve': {'stage', 'target'}, 'rerun': {'stage', 'revision'}}.get(intent, set())
        if not isinstance(payload, dict) or (
                set(payload) != allowed
                and not (intent == 'resume' and set(payload) == {'wait_target'})
                and not (intent == 'approve' and set(payload) == {'stage', 'target', 'note'})):
            raise ValueError('指令参数与当前操作不匹配')
        if intent == 'approve' and 'note' in payload:
            if not isinstance(payload['note'], str) or len(payload['note']) > 4000:
                raise ValueError('审批附注须为不超过 4000 字的文本')
        with self.operations:
            run = self.store.run(key)
            from .control import command_gate
            with command_gate(directory(run)):
                receipt=self._enqueue_command(run,intent,payload,idem)
            if receipt['status']=='queued':
                self.launch(key)
                if intent!='pause':self._wait_command_delivery(key)
            return receipt

    def _enqueue_command(self, run, intent, payload, idem):
        key=run['id']
        old = [c for c in self.store.commands(key, 'queued') + self.store.commands(key, 'applied') + self.store.commands(key, 'rejected')
               if c['idempotency'] == idem]
        if old:
            return self.store.enqueue(key, intent, payload, idem)
        current = self.snapshot(key)
        if any(t['status'] in ('queued', 'running') for t in self.store.threads(key)):
            raise Conflict('角色会诊正在使用固定快照，请等待会诊结束再操作运行')
        if not current['capabilities'].get(intent):
            raise Conflict('当前状态不允许此操作')
        if intent == 'resume' and current['lifecycle'] in ('waiting_external', 'waiting_owner'):
            targets = [[name, value.get('current_attempt'), value.get('outputs_hash')]
                       for name, value in current['stages'].items()
                       if value.get('status') == current['lifecycle'] and name in current['pending_waits']]
            if len(targets) != 1 or payload.get('wait_target') != targets[0]:
                raise Conflict('等待回执目标已过期，请刷新后继续')
        elif intent == 'resume' and payload:
            raise Conflict('运行已不再等待该回执')
        if intent == 'approve':
            stage = payload.get('stage')
            if stage not in current['pending_approvals'] or payload.get('target') != stage_target(current['stages'][stage]):
                raise Conflict('审批目标已过期')
        if intent == 'rerun':
            if payload.get('stage') not in current['stages'] or payload.get('revision') != current['revision']:
                raise Conflict('重跑目标已过期，请刷新后操作')
        return self.store.enqueue(key, intent, payload, idem)

    def _wait_command_delivery(self,key):
        """旧 manager 正退出时，等其释放 supervisor 锁后再交付尚未消费的指令。"""
        process=self.active.get(key)
        if process is None or process.poll() is not None or key in self._command_waiters:return
        self._command_waiters.add(key)
        def wait():
            try:
                while not self.closed.is_set():
                    try:
                        process.wait(timeout=.5)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                with self.operations:
                    if not self.closed.is_set() and self.store.commands(key):
                        self.launch(key)
            finally:
                with self.operations:
                    self._command_waiters.discard(key)
        threading.Thread(target=wait,daemon=True).start()

    def discuss(self, key, question, roles, budget, idem, *, rounds=1, role_models=None, context_files=None, consensus=False):
        run = self.store.run(key)
        if not isinstance(question, str) or not 1 <= len(question) <= 4000:
            raise ValueError('问题必须为 1 到 4000 字符')
        allowed_roles = {'architect', 'developer', 'skeptic', 'reviewer'}
        if not isinstance(roles, list) or not 1 <= len(roles) <= 4 or len(set(roles)) != len(roles) or not set(roles) <= allowed_roles:
            raise ValueError('会诊需选择 1 到 4 个不重复角色（architect / developer / skeptic / reviewer）')
        if type(budget) is not int or not 1000 <= budget <= 24000:
            raise ValueError('会诊额度必须在 1000 到 24000 tokens 之间')
        if type(rounds) is not int or not 1 <= rounds <= 3:
            raise ValueError('会诊轮次必须在 1 到 3 轮之间')
        if type(consensus) is not bool:
            raise ValueError('consensus 必须为布尔值')
        clean_models = {}
        if role_models is not None:
            if not isinstance(role_models, dict):
                raise ValueError('role_models 必须为对象')
            import re
            for r, m in role_models.items():
                if r not in allowed_roles and r != 'consensus':
                    raise ValueError(f'未知会诊角色：{r}')
                if not isinstance(m, str) or not m.strip():
                    continue
                mv = m.strip()
                if len(mv) > 160 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}(?:\[(?:1M|200K)\])?', mv):
                    raise ValueError(f'角色 {r} 的模型标识格式非法')
                clean_models[r] = mv
        clean_files = []
        if context_files is not None:
            if not isinstance(context_files, list) or len(context_files) > 4:
                raise ValueError('context_files 最多指定 4 个相对文件路径')
            clean_files = [str(f).strip() for f in context_files if isinstance(f, str) and f.strip()]
        status = self.snapshot(key)['lifecycle']
        if status in ('pending', 'running', 'recovering'):
            raise Conflict('请先在检查点暂停运行，再对固定快照发起会诊')
        if any(t['status'] in ('queued', 'running') and t['idempotency'] != idem for t in self.store.threads(key)):
            raise Conflict('该任务已有会诊，不能并行叠加模型调用')
        from .discussion import capture
        snap = capture(run, extra_files=clean_files, question=question)
        if rounds > 1 or clean_models or consensus:
            snap['roundtable'] = {'rounds': rounds, 'role_models': clean_models, 'consensus': consensus}
        topic = self.store.create_thread(key, question, roles, budget, snap, idem)
        if topic['status'] == 'queued':
            self.launch_discussion(topic['id'])
        return topic

    def launch_discussion(self, topic):
        if topic in self.active_topics and self.active_topics[topic].poll() is None:
            return
        thread = self.store.thread(topic)
        run = self.store.run(thread['run_id'])
        cfg = load_config(run['config'])
        from ..safety import build_subprocess_env
        allow = [a.options['api_key_env'] for a in cfg.adapters.values() if a.type == 'openai_compat']
        if os.environ.get('PIPELINE_STREAM_DISCUSSION') == '1':
            allow.append('PIPELINE_STREAM_DISCUSSION')
        base = Path(run['state_dir']) / 'discussions' / topic
        base.mkdir(parents=True, exist_ok=True)
        from ..state import _pid_alive
        lock = read_json(base / '.discussion.lock')
        if lock and _pid_alive(lock.get('pid')):
            return
        with (base / 'worker.log').open('ab') as log:
            self.active_topics[topic] = subprocess.Popen([sys.executable, '-X', 'utf8', '-m', 'orchestrator.collaboration.discussion_runner',
                              str(self.store.path), topic], cwd=Path(__file__).resolve().parents[2],
                             env=build_subprocess_env(allow), stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=os.name != 'nt',
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)

    def _discuss(self, topic):
        from .discussion import execute
        try:
            execute(self.store, topic)
        except Exception as error:
            self.store.update_thread(topic, 'failed', {'reason': self.redact(str(error))})
