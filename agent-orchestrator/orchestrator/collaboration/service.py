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


class Service:
    def __init__(self, database, configs):
        self.store = Store(database)
        self.operations = threading.RLock()
        self.active = {}
        self.active_topics = {}
        self._command_waiters = set()
        self._configs = {}
        self._projection_cache = {}
        self._summary_cache = SummaryCache()
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
            self.sync(recover=True)
        except Exception as error:
            # 单个坏配置/畸形运行目录不应阻止工作台启动；房间仍可打开，错误留给诊断。
            self.last_sync_error = self.redact(str(error) or error.__class__.__name__)

    def config(self, path):
        key = str(path)
        stat = Path(path).stat()
        signature = (stat.st_size, stat.st_mtime_ns)
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

    def snapshot(self, key):
        run = self.store.run(key)
        state, job = project(self.store, run, self._projection_cache)
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
                                 'rerun': managed and status in ('paused', 'done', 'failed'),
                                 'discussion': bool(state.get('stages'))},
                'parent_id': run['parent_id'], 'managed': managed,
                'last_cursor': self.store.last_cursor(key),
                'deadline': job.get('started_at', 0) + job.get('max_duration_s', 0) if job else state.get('control',{}).get('cli_deadline'),
                'artifacts': artifact_list(run), 'threads': self.store.threads(key)}
        data['usage'] = dict(data['usage'])
        data['usage'].setdefault('reported_tokens', max(0, data['usage'].get('tokens', 0) - data['usage'].get('reserved_tokens', 0)))
        from ..cli_activity import read_activities
        data['cli_activity']=self.redact(read_activities(run,state))
        for activity in data['cli_activity']:
            self.store.event(key+':cli:'+activity['call_id']+':'+str(activity['revision']),key,'cli_progress',activity)
        data['last_cursor']=self.store.last_cursor(key)
        cfg = self.config(run['config'])
        data['budget'] = {'max_tokens': cfg.run_budget.max_tokens, 'max_duration_s': cfg.run_budget.max_duration_s}
        data['budget']['unlimited_tokens'] = cfg.run_budget.unlimited_tokens
        from .model_settings import describe_models
        data['models'] = describe_models(cfg)
        data['code_stages'] = [s.name for s in cfg.stages if s.kind == 'code' and state.get('stages', {}).get(s.name, {}).get('commit_sha')]
        data['metrics'] = read_json(directory(run) / 'metrics.json')
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

    def list_runs(self, limit=100, before=None):
        """列表只读摘要；产物、会诊和长记录在进入任务房间后加载。"""
        result = []
        with self.store.session():
            for run in self.store.runs(limit, before):
                task, status = self._summary_cache.read(run)
                result.append({'id': run['id'], 'run_id': run['run_id'],
                               'project_id': run['project_id'], 'created': run['created'],
                               'task': task,
                               'lifecycle': status, 'managed': bool(run['managed'])})
        return self.redact(result)

    # ── 游标格式：URL-safe Base64(JSON {"v":1,"ch":"<created.hex()>","id":"<id>","bh":"<before.hex()|null>",
    #                                       "qh":"<q_normalized|null>","pi":"<project_id|null>","st":"<status>"})
    _CURSOR_VERSION = 3
    _CURSOR_MAX_LEN = 8192  # 容纳 200 个 Unicode 字符编码后的合法查询

    # 合法 status 值及其对应的底层 lifecycle 集合
    _STATUS_GROUPS: dict[str, set[str] | None] = {
        'all': None,
        'running': {'pending', 'running', 'recovering'},
        'paused': {'paused'},
        'waiting_approval': {'waiting_approval'},
        'done': {'done'},
        'failed': {'failed', 'timeout', 'cancelled'},
    }

    def _encode_cursor(self, created: float, run_id: str, before: float | None,
                       q: str = '', project_id: str = '', status: str = 'all',
                       sort: str = 'newest') -> str:
        payload = json.dumps({
            'v': self._CURSOR_VERSION,
            'ch': float(created).hex(),
            'id': run_id,
            'bh': float(before).hex() if before is not None else None,
            'qh': q if q else None,
            'pi': project_id if project_id else None,
            'st': status,
            'sort': sort,
        }, separators=(',', ':')).encode('utf-8')
        return base64.urlsafe_b64encode(payload).decode('ascii')

    def _decode_cursor(self, cursor: str) -> tuple[float, str, float | None, str, str, str, str]:
        """游标绑定筛选和时间方向；旧版本保持最新在前。"""
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= self._CURSOR_MAX_LEN:
            raise ValueError('游标格式无效')
        try:
            payload = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
            obj = json.loads(payload.decode('utf-8'))
        except (ValueError, UnicodeError):
            raise ValueError('游标格式无效')
        if not isinstance(obj, dict) or type(obj.get('v')) is not int:
            raise ValueError('游标版本不支持')
        # 兼容旧版 v=1 游标（无筛选字段），视为无筛选
        if obj['v'] == 1:
            if set(obj) != {'v', 'ch', 'id', 'bh'}:
                raise ValueError('游标版本不支持')
            try:
                if not isinstance(obj['ch'], str) or not isinstance(obj['id'], str):
                    raise ValueError('游标字段类型错误')
                created = float.fromhex(obj['ch'])
                run_id = obj['id']
                bh = obj.get('bh')
                if bh is not None and not isinstance(bh, str):
                    raise ValueError('游标边界类型错误')
                before = float.fromhex(bh) if bh is not None else None
            except (KeyError, ValueError, TypeError, OverflowError):
                raise ValueError('游标字段无效')
            if not isinstance(run_id, str) or not run_id:
                raise ValueError('游标 id 无效')
            import math
            if not math.isfinite(created) or (before is not None and not math.isfinite(before)):
                raise ValueError('游标时间戳无效')
            return created, run_id, before, '', '', 'all', 'newest'
        elif obj['v'] in (2, self._CURSOR_VERSION):
            fields = {'v', 'ch', 'id', 'bh', 'qh', 'pi', 'st'}
            if obj['v'] == self._CURSOR_VERSION:
                fields.add('sort')
            if set(obj) != fields:
                raise ValueError('游标版本不支持')
            try:
                if not isinstance(obj['ch'], str) or not isinstance(obj['id'], str):
                    raise ValueError('游标字段类型错误')
                created = float.fromhex(obj['ch'])
                run_id = obj['id']
                bh = obj.get('bh')
                if bh is not None and not isinstance(bh, str):
                    raise ValueError('游标边界类型错误')
                before = float.fromhex(bh) if bh is not None else None
                qh = obj.get('qh')
                if qh is not None and (not isinstance(qh,str) or len(qh)>600):
                    raise ValueError('游标关键词类型或长度错误')
                q = qh if isinstance(qh, str) else ''
                pi = obj.get('pi')
                if pi is not None and (not isinstance(pi,str) or len(pi)>512):
                    raise ValueError('游标项目类型或长度错误')
                project_id = pi if isinstance(pi, str) else ''
                st = obj.get('st', 'all')
                if not isinstance(st, str) or st not in self._STATUS_GROUPS:
                    raise ValueError('游标状态无效')
                sort = obj.get('sort', 'newest')
                if not isinstance(sort, str) or sort not in ('newest', 'oldest'):
                    raise ValueError('游标排序无效')
            except (KeyError, ValueError, TypeError, OverflowError):
                raise ValueError('游标字段无效')
            if not isinstance(run_id, str) or not run_id:
                raise ValueError('游标 id 无效')
            import math
            if not math.isfinite(created) or (before is not None and not math.isfinite(before)):
                raise ValueError('游标时间戳无效')
            return created, run_id, before, q, project_id, st, sort
        else:
            raise ValueError('游标版本不支持')

    def list_runs_page(self, limit: int = 50, before: float | None = None, cursor: str | None = None,
                       q: str | None = None, project_id: str | None = None,
                       status: str | None = None, sort: str | None = None):
        """稳定游标分页，支持全历史筛选；返回 {runs, has_more, next_cursor}。"""
        import math

        # None 表示遗漏；显式空关键词、空项目不能被游标中的旧条件覆盖。
        supplied_q, supplied_project, supplied_status = q is not None, project_id is not None, status is not None
        q = '' if q is None else q
        project_id = '' if project_id is None else project_id
        status = 'all' if status is None else status
        supplied_sort = sort is not None
        sort = 'newest' if sort is None else sort
        if not isinstance(sort, str) or sort not in ('newest', 'oldest'):
            raise ValueError('sort 必须为 newest/oldest 之一')

        # 验证筛选参数
        if not isinstance(q, str):
            raise ValueError('q 必须为字符串')
        q_norm = q.strip().casefold()  # 规范化关键词
        if len(q) > 200:
            raise ValueError('q 最多 200 字符')
        if not isinstance(project_id, str):
            raise ValueError('project_id 必须为字符串')
        if len(project_id) > 512:
            raise ValueError('project_id 过长')
        if not isinstance(status, str) or status not in self._STATUS_GROUPS:
            raise ValueError(f'status 必须为 {"/".join(self._STATUS_GROUPS)} 之一')

        # 夹取 limit
        limit = max(1, min(100, limit))
        if before is not None and not math.isfinite(before):
            raise ValueError('分页时间边界必须为有限数')

        # 解码游标
        cur_created: float | None = None
        cur_id: str | None = None
        cursor_before: float | None = None
        cursor_q: str = ''
        cursor_project_id: str = ''
        cursor_status: str = 'all'
        # 判断调用方是否显式提供了筛选参数（URL 中有 q/project_id/status）
        # 若只传 cursor 不传筛选参数，则从游标中恢复筛选条件；
        # 若显式传了筛选参数但与游标不一致，则返回400。
        caller_has_filters = supplied_q or supplied_project or supplied_status
        if cursor is not None:
            cur_created, cur_id, cursor_before, cursor_q, cursor_project_id, cursor_status, cursor_sort = self._decode_cursor(cursor)
            # 遗漏范围参数时继承游标；显式改变任何已绑定参数都必须重置分页。
            if before is not None and before != cursor_before:
                raise ValueError('游标与当前时间范围不一致，请重置分页')
            before = cursor_before
            cursor_q_norm = cursor_q.strip().casefold() if cursor_q else ''
            if ((supplied_q and q_norm != cursor_q_norm)
                    or (supplied_project and project_id != cursor_project_id)
                    or (supplied_status and status != cursor_status)
                    or (supplied_sort and sort != cursor_sort)):
                raise ValueError('游标与当前筛选条件不一致，请重置分页')
            q_norm = cursor_q_norm
            project_id = cursor_project_id
            status = cursor_status
            sort = cursor_sort

        # 排序方向仅来自已验证枚举，禁止把请求值直接拼进 SQL。
        direction, comparison = ('ASC', '>') if sort == 'oldest' else ('DESC', '<')

        result = []
        # 构建基础 SQL：按 created DESC, id DESC 遍历全部历史
        # 续页条件：(created < cur_created) OR (created = cur_created AND id < cur_id)
        params: list = []
        where_parts: list[str] = []

        if before is not None:
            where_parts.append('created < ?')
            params.append(before)

        if cur_created is not None and cur_id is not None:
            where_parts.append(
                f'(created {comparison} ? OR (created = ? AND id {comparison} ?))'
            )
            params.extend([cur_created, cur_created, cur_id])

        # project_id 精确匹配可在 SQL 层过滤（无需逐行跳过）
        if project_id:
            where_parts.append('project_id = ?')
            params.append(project_id)

        where_sql = ('WHERE ' + ' AND '.join(where_parts)) if where_parts else ''

        # 状态组和关键词需要在 Python 层过滤（读取 state/job），因此需要全量遍历
        # 每次批量取 BATCH 条，积累足够 limit+1 条匹配记录后停止
        BATCH = 200
        status_set = self._STATUS_GROUPS.get(status)  # None 表示不过滤状态
        need_filter = bool(q_norm) or status_set is not None

        with self.store.session():
            if not need_filter:
                # 无关键词/状态过滤：一次取 limit+1 条
                fetch_sql = f"""
                    SELECT *
                    FROM runs
                    {where_sql}
                    ORDER BY created {direction}, id {direction}
                    LIMIT ?
                """
                fetch_params = params + [limit + 1]
                with self.store.connect() as db:
                    rows = db.execute(fetch_sql, fetch_params).fetchall()
                has_more = len(rows) > limit
                for row in rows[:limit]:
                    full_run = dict(row)
                    task_text, lc = self._summary_cache.read(full_run)
                    result.append({
                        'id': full_run['id'], 'run_id': full_run['run_id'],
                        'project_id': full_run['project_id'], 'created': full_run['created'],
                        'task': task_text,
                        'lifecycle': lc, 'managed': bool(full_run['managed']),
                    })
            else:
                # 有关键词或状态过滤：批量遍历全部历史直到收集足够结果
                # 使用游标记录已扫描到的位置，实现分批遍历
                scan_created: float | None = cur_created
                scan_id: str | None = cur_id
                exhausted = False
                found_count = 0  # 已匹配并放入 result
                has_more = False

                while not exhausted and found_count <= limit:
                    # 构建批次 WHERE
                    batch_where_parts = list(where_parts)  # 包含 before / project_id 条件
                    batch_params = list(params)

                    if scan_created is not None and scan_id is not None:
                        # 续批：从上一批最后一条之后继续
                        batch_where_parts.append(
                            f'(created {comparison} ? OR (created = ? AND id {comparison} ?))'
                        )
                        batch_params.extend([scan_created, scan_created, scan_id])
                    # 移除已被 cur_created/cur_id 替代的游标续页条件（避免重复）
                    # 注：where_parts 中若有游标条件，scan_* 初始值已是 cur_*，会重复添加
                    # 重建 batch_where_parts 以避免重复
                    batch_where_parts2: list[str] = []
                    batch_params2: list = []
                    if before is not None:
                        batch_where_parts2.append('created < ?')
                        batch_params2.append(before)
                    if project_id:
                        batch_where_parts2.append('project_id = ?')
                        batch_params2.append(project_id)
                    if scan_created is not None and scan_id is not None:
                        batch_where_parts2.append(
                            f'(created {comparison} ? OR (created = ? AND id {comparison} ?))'
                        )
                        batch_params2.extend([scan_created, scan_created, scan_id])
                    batch_where_sql = ('WHERE ' + ' AND '.join(batch_where_parts2)) if batch_where_parts2 else ''

                    fetch_sql = f"""
                        SELECT *
                        FROM runs
                        {batch_where_sql}
                        ORDER BY created {direction}, id {direction}
                        LIMIT ?
                    """
                    with self.store.connect() as db:
                        batch_rows = db.execute(fetch_sql, batch_params2 + [BATCH]).fetchall()

                    if len(batch_rows) < BATCH:
                        exhausted = True

                    for row in batch_rows:
                        full_run = dict(row)
                        task_text, lc = self._summary_cache.read(full_run)

                        # 状态匹配
                        if status_set is not None and lc not in status_set:
                            continue

                        # 关键词匹配（casefold 子串，%/_/' 不作通配符）
                        if q_norm:
                            task_cf = task_text.casefold()
                            run_id_cf = full_run['run_id'].casefold()
                            if q_norm not in task_cf and q_norm not in run_id_cf:
                                continue

                        # 匹配成功
                        if found_count < limit:
                            result.append({
                                'id': full_run['id'], 'run_id': full_run['run_id'],
                                'project_id': full_run['project_id'], 'created': full_run['created'],
                                'task': task_text,
                                'lifecycle': lc, 'managed': bool(full_run['managed']),
                            })
                        found_count += 1
                        if found_count > limit:
                            has_more = True
                            break

                    if found_count > limit:
                        break

                    if batch_rows:
                        last_row = dict(batch_rows[-1])
                        scan_created = last_row['created']
                        scan_id = last_row['id']

        next_cursor: str | None = None
        if has_more and result:
            last = result[-1]
            next_cursor = self._encode_cursor(last['created'], last['id'], before,
                                              q_norm, project_id, status, sort)

        return self.redact({'runs': result, 'has_more': has_more, 'next_cursor': next_cursor})

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
                       ('pending', 'running', 'recovering', 'paused', 'waiting_approval') for r in self.store.runs()):
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
        if not isinstance(payload, dict) or set(payload) != allowed:
            raise ValueError('指令参数与当前操作不匹配')
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
        if any(t['status'] in ('queued', 'running') and t['idempotency'] != idem for t in self.store.threads(key)):
            raise Conflict('该任务已有会诊，不能并行叠加模型调用')
        from .discussion import capture
        topic = self.store.create_thread(key, question, roles, budget, capture(run), idem)
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
