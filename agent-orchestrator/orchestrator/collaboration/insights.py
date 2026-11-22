"""工作台只读对比、失败分页与具名模板；模板不能携带命令或修改权限。"""
from __future__ import annotations

from ..reporting import failure_categories, read_object, usage_summary
from ..event_journal import read_events
from .projection import directory, lifecycle
from .storage import Conflict


def run_summary(service, key):
    run = service.store.run(key)
    root = directory(run)
    state = read_object(root / 'state.json')
    if state.get('schema_version') == 3:
        state['events'] = read_events(root, state)
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


def export_run_report(service, key) -> dict:
    """将单次运行的状态、阶段结论、累计代码差异与多维用量汇总为可归档的 Markdown 交付报告。"""
    import re
    import time
    from .projection import code_diff, read_artifact

    run = service.store.run(key)
    snap = service.snapshot(key)
    usage = run_summary(service, key)
    safe_id = re.sub(r'[^A-Za-z0-9._-]+', '_', str(run['run_id']))[:80] or 'run'
    created_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(run.get('created') or 0))
    lines = [
        f'# 任务交付报告 · {run["run_id"]}',
        '',
        f'- **运行标识**：`{run["run_id"]}`',
        f'- **生命周期状态**：`{snap.get("lifecycle")}`',
        f'- **创建时间**：{created_str}',
        f'- **累计耗时**：{round(float(usage.get("duration_s") or 0), 2)}s',
        f'- **已报告 Tokens**：{usage.get("reported_tokens", 0)}（预留：{usage.get("reserved_tokens", 0)}，未知用量调用：{usage.get("unknown_calls", 0)}）',
        '',
        '## 任务说明',
        '',
        (snap.get('task') or '（无任务说明）').strip(),
        '',
        '## 阶段执行汇总',
        '',
        '| 阶段 | 状态 | 尝试次数 | 代码提交 | 说明 |',
        '| --- | --- | --- | --- | --- |',
    ]
    stages_map = snap.get('stages') or {}
    last_commit_stage = None
    for sname, sdata in stages_map.items():
        commit = (sdata.get('commit_sha') or '')[:12]
        if sdata.get('commit_sha'):
            last_commit_stage = sname
        err = str(sdata.get('last_error') or '').replace('\n', ' ').replace('|', '\\|')[:120]
        lines.append(
            f'| `{sname}` | `{sdata.get("status")}` | {sdata.get("current_attempt") or 0} | '
            f'{("`" + commit + "`") if commit else "—"} | {err or "—"} |'
        )
    lines.append('')

    # 附带每个阶段的最终归档正文（经 manifest 哈希校验）
    lines.append('## 阶段产物摘要')
    lines.append('')
    artifact_count = 0
    for sname, sdata in stages_map.items():
        att = sdata.get('current_attempt')
        if not att:
            continue
        for fname in ('final_text.txt', 'review_verdict.json', 'test_record.json'):
            try:
                art = read_artifact(run, f'{sname}/{att}/{fname}')
            except Exception:
                continue
            body = (art.get('body') or '').strip()
            if not body:
                continue
            artifact_count += 1
            lines.append(f'### `{sname}` · Attempt {att} · `{fname}` ({art.get("integrity")})')
            lines.append('')
            lines.append(body[:3000])
            if len(body) > 3000:
                lines.append('\n…（已截断，完整内容请见本地产物目录）')
            lines.append('')
            break
    if artifact_count == 0:
        lines.append('（当前尚无归档文本产物）')
        lines.append('')

    # 累计代码差异（如有代码提交）
    if last_commit_stage:
        try:
            diff = code_diff(run, last_commit_stage, 'start')
            diff_body = (diff.get('body') or '').strip()
            if diff_body:
                lines.append(f'## 累计代码差异（基线 → `{last_commit_stage}`）')
                lines.append('')
                lines.append('````diff')
                lines.append(diff_body[:12000])
                if len(diff_body) > 12000:
                    lines.append('# …（差异过长已截断）')
                lines.append('````')
                lines.append('')
        except Exception:
            pass

    # 圆桌会诊共识摘要（如有）
    threads = [t for t in service.store.threads(key) if t.get('status') == 'completed']
    if threads:
        lines.append('## 关联圆桌会诊记录')
        lines.append('')
        for t in threads:
            res = t.get('result') or {}
            lines.append(f'### 议题：{t.get("question", "")[:120]}')
            lines.append('')
            consensus = res.get('consensus')
            if consensus:
                lines.append(consensus[:2000])
            else:
                lines.append(f'- 参与角色：{", ".join(t.get("roles") or [])}（状态：{t.get("status")}）')
            lines.append('')

    markdown = '\n'.join(lines).rstrip() + '\n'
    return {
        'filename': f'{safe_id}-report.md',
        'markdown': service.redact(markdown),
    }
