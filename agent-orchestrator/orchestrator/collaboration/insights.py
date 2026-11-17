"""工作台只读对比、失败分页与具名模板；模板不能携带命令或修改权限。"""
from __future__ import annotations

from ..reporting import failure_categories, read_object, usage_summary
from .projection import directory, lifecycle
from .storage import Conflict


def run_summary(service, key):
    run = service.store.run(key)
    root = directory(run)
    state = read_object(root / 'state.json')
    metrics = read_object(root / 'metrics.json') if (root / 'metrics.json').exists() else None
    job = read_object(root / 'job.json') if (root / 'job.json').exists() else {}
    summary = usage_summary(run['run_id'], state, metrics)
    summary.update(id=key, project_id=run['project_id'], lifecycle=lifecycle(state, job),
                   config_hash=(metrics or {}).get('config_hash') or job.get('config_hash'),
                   failure_categories=failure_categories(state, job, metrics))
    return service.redact(summary)


def compare(service, left, right):
    if not left or not right or left == right:
        raise ValueError('请选择两个不同的运行')
    a, b = run_summary(service, left), run_summary(service, right)
    delta = {}
    for field in ('tokens', 'reported_tokens', 'reserved_tokens', 'duration_s'):
        av, bv = a[field], b[field]
        delta[field] = bv - av if av is not None and bv is not None else None
    return dict(left=a, right=b, delta=delta,
                same_config=bool(a['config_hash']) and a['config_hash'] == b['config_hash'],
                scope='usage_and_status_not_implementation_equivalence')


def failure_page(service, after=None, limit=50):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('limit 必须在 1..100')
    if after is not None and (not isinstance(after, str) or len(after) > 512):
        raise ValueError('after 无效')
    rows = service.store.sync_runs(after, limit + 1)
    selected = rows[:limit]
    counts, failures, errors = {}, [], []
    for run in selected:
        try:
            summary = run_summary(service, run['id'])
            categories = summary['failure_categories']
            if summary['lifecycle'] not in ('failed', 'timeout', 'cancelled'): continue
            categories = categories or ['unknown']
            failures.append(dict(id=run['id'], run_id=run['run_id'], lifecycle=summary['lifecycle'], categories=categories))
            for category in categories: counts[category] = counts.get(category, 0) + 1
        except (OSError, ValueError, TypeError, AttributeError):
            errors.append(dict(id=run['id'], reason='unreadable_metadata'))
    more = len(rows) > limit
    return dict(failures=failures, counts=counts, scanned=len(selected), errors=errors,
                has_more=more, next_after=selected[-1]['id'] if more else None,
                scope='this_page_only')


def save_template(service, project_id, name, task, config_hash):
    from ..config import load_config
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError('模板名称必须为 1..80 字符')
    if not isinstance(task, str) or not 1 <= len(task.strip()) <= 64000:
        raise ValueError('模板任务必须为 1..64000 字符')
    cfg = load_config(service.store.project(project_id)['config'])
    if not isinstance(config_hash, str) or config_hash != cfg.source_hash:
        raise Conflict('配置已变化，请刷新配置并核对执行范围后保存模板')
    # 名称按项目 upsert；总量限制由同一数据库事务实施。
    with service.store.connect() as db:
        old = db.execute('SELECT id FROM task_templates WHERE project_id=? AND name=?',
                         (project_id, name.strip())).fetchone()
        if not old and db.execute('SELECT COUNT(*) FROM task_templates').fetchone()[0] >= 100:
            raise ValueError('模板总量已到 100 项上限')
        from .storage import identifier
        key = old['id'] if old else identifier('template')
        db.execute('INSERT INTO task_templates(id,project_id,name,task,config_hash) VALUES(?,?,?,?,?) '
                   'ON CONFLICT(project_id,name) DO UPDATE SET task=excluded.task,config_hash=excluded.config_hash',
                   (key, project_id, name.strip(), task, config_hash))
    return dict(id=key, saved=True)


def templates(service):
    projects = {p['id']: p for p in service.list_projects()}
    with service.store.connect() as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM task_templates ORDER BY name,id LIMIT 100')]
    for row in rows:
        project = projects.get(row['project_id'])
        row['usable'] = bool(project and not project.get('unavailable') and row['config_hash'] == project['config_hash'])
    return service.redact(rows)
