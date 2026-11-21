"""基于不可变 metrics.json 的多模型自适应路由推荐。

安全与计量原则：
- 只读取终态不可变 metrics.json，绝不修改历史账本。
- 样本量低于 min_runs 时明确标记 insufficient_data，不盲目外推。
- 未知用量调用单独记账，绝不把 unknown_calls 当作 0 token 免费优势。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import statistics
from typing import Any

_MAX_METRICS_BYTES = 2 * 1024 * 1024


def recommend_adapters(
    state_dir: Path,
    *,
    stage_kind: str | None = None,
    min_runs: int = 2,
) -> dict[str, Any]:
    """按阶段类型聚合历史 (tool, model) 真实表现，输出可解释的推荐排序。"""
    if type(min_runs) is not int or min_runs < 1:
        raise ValueError('min_runs 必须为正整数')
    if stage_kind is not None and stage_kind not in ('code', 'report', 'review'):
        raise ValueError('stage_kind 必须为 code / report / review 之一')

    runs_dir = Path(state_dir).resolve() / 'runs'
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    scanned_metrics = 0

    if runs_dir.is_dir() and not runs_dir.is_symlink():
        for run_dir in sorted(runs_dir.iterdir()):
            if run_dir.is_symlink() or not run_dir.is_dir():
                continue
            metrics_path = run_dir / 'metrics.json'
            if not metrics_path.is_file() or metrics_path.is_symlink():
                continue
            try:
                if metrics_path.stat().st_size > _MAX_METRICS_BYTES:
                    continue
                data = json.loads(metrics_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or not isinstance(data.get('stages'), dict):
                continue
            scanned_metrics += 1
            run_done = data.get('status') == 'done'
            stage_usage = data.get('stage_usage') if isinstance(data.get('stage_usage'), dict) else {}
            for stage_name, info in data['stages'].items():
                if not isinstance(info, dict):
                    continue
                kind = info.get('kind')
                if kind not in ('code', 'report', 'review'):
                    continue
                if stage_kind is not None and kind != stage_kind:
                    continue
                # 跳过未实际执行的下游阶段（如上游失败导致 pending / attempts == 0），避免污染模型评分
                if info.get('status') not in ('done', 'failed'):
                    continue
                attempts = info.get('attempts')
                if type(attempts) is int and attempts <= 0:
                    continue
                tool = str(info.get('tool') or 'unknown')
                used = stage_usage.get(stage_name) if isinstance(stage_usage.get(stage_name), dict) else {}
                model = str(used.get('backend_model') or info.get('model') or 'default')
                key = (kind, tool, model)
                bucket = buckets.setdefault(key, {
                    'stage_kind': kind,
                    'tool': tool,
                    'model': model,
                    'runs': 0,
                    'stage_done': 0,
                    'run_done': 0,
                    'durations': [],
                    'known_tokens': [],
                    'unknown_calls': 0,
                })
                bucket['runs'] += 1
                if info.get('status') == 'done':
                    bucket['stage_done'] += 1
                if run_done:
                    bucket['run_done'] += 1
                dur = used.get('duration_s')
                if isinstance(dur, (int, float)) and not isinstance(dur, bool) and math.isfinite(dur) and dur >= 0:
                    bucket['durations'].append(float(dur))
                unknown = used.get('unknown_calls', 0)
                if type(unknown) is int and unknown > 0:
                    bucket['unknown_calls'] += unknown
                else:
                    tok = used.get('tokens')
                    if type(tok) is int and tok > 0:
                        bucket['known_tokens'].append(tok)

    by_kind: dict[str, list[dict[str, Any]]] = {}
    for bucket in buckets.values():
        runs = bucket['runs']
        stage_pass_rate = round(bucket['stage_done'] / runs, 4)
        run_pass_rate = round(bucket['run_done'] / runs, 4)
        median_dur = round(statistics.median(bucket['durations']), 3) if bucket['durations'] else None
        median_tok = int(statistics.median(bucket['known_tokens'])) if bucket['known_tokens'] else None
        eligible = runs >= min_runs
        entry = {
            'stage_kind': bucket['stage_kind'],
            'tool': bucket['tool'],
            'model': bucket['model'],
            'runs': runs,
            'eligible': eligible,
            'status': 'ranked' if eligible else 'insufficient_data',
            'run_pass_rate': run_pass_rate,
            'stage_pass_rate': stage_pass_rate,
            'median_duration_s': median_dur,
            'median_reported_tokens': median_tok,
            'unknown_calls': bucket['unknown_calls'],
            'usage_complete': bucket['unknown_calls'] == 0,
        }
        by_kind.setdefault(bucket['stage_kind'], []).append(entry)

    recommendations: dict[str, dict[str, Any]] = {}
    for kind, rows in sorted(by_kind.items()):
        # 排序规则：达标样本优先 > 整条运行完成率 > 阶段完成率 > 用量完整性 > 中位耗时更短 > 样本数更多
        rows.sort(key=lambda r: (
            not r['eligible'],
            -r['run_pass_rate'],
            -r['stage_pass_rate'],
            not r['usage_complete'],
            r['median_duration_s'] if r['median_duration_s'] is not None else float('inf'),
            -r['runs'],
            r['tool'],
            r['model'],
        ))
        top = next((r for r in rows if r['eligible']), None)
        recommendations[kind] = {
            'recommended': {'tool': top['tool'], 'model': top['model']} if top else None,
            'reason': (
                f"基于 {top['runs']} 条终态记录：整条完成率 {top['run_pass_rate']*100:.1f}%、"
                f"阶段完成率 {top['stage_pass_rate']*100:.1f}%"
                + (f"、中位耗时 {top['median_duration_s']}s" if top['median_duration_s'] is not None else '')
                if top else f'无满足最少 {min_runs} 条记录的候选，不自动推荐'
            ),
            'candidates': rows,
        }

    return {
        'schema_version': 1,
        'state_dir': str(Path(state_dir).resolve()),
        'scanned_metrics': scanned_metrics,
        'min_runs': min_runs,
        'recommendations': recommendations,
        'note': '仅基于历史已报告指标与终态结果推荐，不代表跨项目通用成功率或实际账单。',
    }
