"""只读运维摘要：计量与账单分开，未知值不填零，不读取模型正文。"""
from __future__ import annotations

import csv
import io
import json
import math
from pathlib import Path

MAX_JSON_BYTES = 8 * 1024 * 1024


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def read_object(path):
    path = Path(path)
    from .maintenance import boundary
    boundary(path, path.parent)
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError('元数据超过摘要读取上限')
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError('元数据必须为 JSON 对象')
    return value


def budget_alerts(usage, budget, *, deadline=None, now=None, threshold=.8):
    """只给显示提示，不改变限额。tokens 已含预留，不重复相加。"""
    import time
    now = time.time() if now is None else now
    alerts = []
    for dimension, used, limit in (
        ('tokens', number(usage.get('tokens')), number(budget.get('max_tokens'))),
        ('duration_s', number(usage.get('duration_s')), number(budget.get('max_duration_s')))):
        if used is not None and limit is not None and limit > 0 and used / limit >= threshold:
            alerts.append(dict(dimension=dimension, used=used, limit=limit,
                               ratio=used / limit, level='exhausted' if used >= limit else 'warning'))
    wall_limit = number(budget.get('max_duration_s'))
    if number(deadline) is not None and deadline > 0 and wall_limit and deadline - now <= wall_limit * (1 - threshold):
        alerts.append(dict(dimension='wall_clock', remaining_s=max(0, deadline - now),
                           level='exhausted' if now >= deadline else 'warning'))
    return alerts


def failure_categories(state, job=None, metrics=None):
    """只依据明确状态/事件类别；不把错误正文的关键词猜成原因。"""
    from .contracts import ErrorKind
    known = {kind.value for kind in ErrorKind} - {'none'}
    categories = set()
    mapping = {'test_fail': 'test_failure', 'review_exhausted': 'review_failure',
               'review_incomplete': 'review_incomplete', 'watch_enforce': 'write_violation',
               'test_inputs_modified': 'test_inputs_modified', 'worktree_error': 'worktree_error',
               'cli_retry_exhausted': 'retry_exhausted'}
    events = state.get('events')
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict): continue
        error = event.get('error_kind')
        if isinstance(error, str) and error in known: categories.add(error)
        kind = event.get('kind')
        if isinstance(kind, str) and kind in mapping: categories.add(mapping[kind])
    reasons = (metrics or {}).get('fail_reasons')
    for reason in reasons if isinstance(reasons, list) else []:
        if isinstance(reason, str) and reason in {'budget_exhausted', 'verify_nonzero', 'review_exhausted',
                                                  'review_fail', 'execution_failed'}:
            categories.add(reason)
    if (job or {}).get('status') in ('timeout', 'cancelled'):
        categories.add(job['status'])
    failed = any(isinstance(s, dict) and s.get('status') == 'failed' for s in state.get('stages', {}).values())
    if failed and not categories: categories.add('unknown')
    return sorted(categories)


def usage_summary(run_id, state, metrics=None):
    metrics = metrics or {}
    # 终态 metrics 优先，防止续跑现场覆盖历史计量；旧 metrics 无字段时明确未知。
    frozen = bool(metrics)
    usage = metrics if frozen else state.get('usage_total', {})
    tokens = number(usage.get('tokens'))
    reserved = number(usage.get('reserved_tokens', 0))
    reported = number(usage.get('reported_tokens'))
    if reported is None and tokens is not None and reserved is not None:
        reported = max(0, tokens - reserved)
    rows = []
    stages = metrics.get('stages', {}) if frozen else state.get('stages', {})
    stage_usage = metrics.get('stage_usage', {}) if frozen else state.get('stage_usage', {})
    for name, stage in stages.items():
        if not isinstance(stage, dict): continue
        used = stage_usage.get(name, {})
        rows.append(dict(run_id=run_id, stage=name, tool=stage.get('tool') or 'unknown',
                         adapter=stage.get('adapter') or 'unknown', status=stage.get('status', 'unknown'),
                         model=stage.get('model'), reasoning_effort=stage.get('reasoning_effort'),
                         thinking=stage.get('thinking'),
                         tokens=number(used.get('tokens')), duration_s=number(used.get('duration_s')),
                         qoder_credits=number(used.get('qoder_credits')),
                         unknown_calls=number(used.get('unknown_calls')),
                         token_status='incomplete' if used.get('unknown_calls') else 'not_recorded' if not used else 'reported',
                         billing_cost=None, currency='', cost_status='not_reported'))
    return dict(run_id=run_id, source='immutable_metrics' if frozen else 'live_state',
                tokens=tokens, reported_tokens=reported, reserved_tokens=reserved,
                duration_s=number(usage.get('duration_s')), unknown_calls=number(usage.get('unknown_calls', 0)),
                qoder_credits=number(usage.get('qoder_credits')),
                token_status='incomplete' if usage.get('unknown_calls') else 'not_recorded' if tokens is None else 'reported',
                billing_cost=None, cost_status='not_reported', stages=rows,
                failure_categories=failure_categories(state, metrics=metrics))


def export_usage(state_dir, run_id=None):
    from .maintenance import boundary
    from .state import RunState
    root = Path(state_dir).absolute()
    if run_id:
        RunState(root, run_id)  # 同运行入口的 ID 校验。
    runs = root / 'runs'
    boundary(runs, root)
    if not runs.is_dir(): raise ValueError('状态目录下没有 runs 目录')
    result, errors = [], []
    for directory in ([runs / run_id] if run_id else sorted(runs.iterdir())):
        try:
            boundary(directory, root)
            if not directory.is_dir(): continue
            state = read_object(directory / 'state.json')
            metrics = read_object(directory / 'metrics.json') if (directory / 'metrics.json').exists() else None
            result.append(usage_summary(directory.name, state, metrics))
        except (OSError, ValueError, TypeError, AttributeError):
            errors.append(dict(run_id=directory.name, reason='invalid_or_unreadable_metadata'))
    return dict(runs=result, errors=errors, scope='reported_usage_not_a_bill')


CSV_FIELDS = ('scope', 'run_id', 'stage', 'tool', 'adapter', 'model', 'reasoning_effort', 'thinking', 'status', 'tokens',
              'reported_tokens', 'reserved_tokens', 'unknown_calls', 'token_status', 'duration_s', 'qoder_credits', 'billing_cost', 'currency', 'cost_status')


def csv_cell(value):
    if value is None: return ''
    # CSV 可能在电子表格打开：包括空格前缀的公式也转义，不执行单元格表达式。
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


def usage_csv(report):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction='ignore')
    writer.writeheader()
    for run in report['runs']:
        writer.writerow({k: csv_cell(v) for k, v in dict(run, scope='run_total', stage='', tool='', adapter='', currency='').items()})
        for row in run['stages']:
            writer.writerow({k: csv_cell(v) for k, v in dict(row, scope='stage').items()})
    return stream.getvalue()
