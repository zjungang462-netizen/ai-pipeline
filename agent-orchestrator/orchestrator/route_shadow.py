"""从真实配对评测收据重算质量排序，并对照当前编码运行历史。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


_MAX_REPORT_BYTES = 2 * 1024 * 1024
_SHA256 = re.compile(r'[0-9a-f]{64}')
_GIT_COMMIT = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?')
_OUTCOMES = {'completed', 'failed', 'blocked'}


def compare_routing(data):
    history = data.get('historical_routing') or {}
    ranking = {(row['tool'], row['model']): index for index, row in enumerate(history.get('candidates', ()))
               if row['eligible']}
    identities = {}
    for label, profile in data['candidates'].items():
        observed = {row['backend_model'] for row in data['results']
                    if row['candidate'] == label and row.get('backend_model')}
        if len(observed) > 1:
            return {'status': 'model_identity_changed'}
        model = next(iter(observed), profile['model'])
        if not model:
            return {'status': 'model_identity_unknown'}
        identities[label] = (profile['type'], model)
    if len(set(identities.values())) < len(identities) or not all(key in ranking for key in identities.values()):
        return {'status': 'insufficient_history'}
    if any(summary['blocked'] or not summary['scheduled'] for summary in data['summary'].values()):
        return {'status': 'incomplete_trials'}
    prior = sorted(identities, key=lambda label: ranking[identities[label]])
    summary = data['summary']

    def score(label):
        return summary[label]['completion_rate'], summary[label]['protected_pass_rate']

    paired = sorted(identities, key=score, reverse=True)
    if score(paired[0]) == score(paired[1]):
        return {'status': 'tie', 'historical_order': prior}
    return {'status': 'compared', 'historical_order': prior, 'paired_order': paired, 'agree': prior == paired}


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _load(path):
    with Path(path).open('rb') as source:
        raw = source.read(_MAX_REPORT_BYTES + 1)
    if len(raw) > _MAX_REPORT_BYTES:
        raise ValueError('配对评测结果超过 2 MiB')
    data = json.loads(raw)
    if (not isinstance(data, dict) or type(data.get('schema_version')) is not int
            or data['schema_version'] != 1 or data.get('mode') != 'live'):
        raise ValueError('影子对照需要 schema 1 的真实配对评测结果')
    return data, hashlib.sha256(raw).hexdigest()


def _validated_trials(data):
    candidates, tasks, repeats = data.get('candidates'), data.get('tasks'), data.get('repeats')
    if (not isinstance(candidates, dict) or len(candidates) != 2 or not isinstance(tasks, dict) or not tasks
            or type(repeats) is not int or not 1 <= repeats <= 10 or not isinstance(data.get('results'), list)):
        raise ValueError('配对评测候选、任务或轮次不符合收据格式')
    for profile in candidates.values():
        tool = profile.get('type') if isinstance(profile, dict) else None
        if (not isinstance(tool, str) or (tool != 'openai_compat' and not tool.startswith('cli_'))
                or 'model' not in profile or (profile['model'] is not None and not isinstance(profile['model'], str))):
            raise ValueError('配对评测候选缺少真实适配器身份')
    for task in tasks.values():
        if (not isinstance(task, dict) or not _matches(_GIT_COMMIT, task.get('baseline_commit'))
                or not _matches(_SHA256, task.get('protected_test_sha256'))
                or not _matches(_SHA256, task.get('task_sha256'))):
            raise ValueError('配对评测任务缺少基线或保护测试指纹')

    seen = set()
    per_candidate = {label: [] for label in candidates}
    for row in data['results']:
        if not isinstance(row, dict):
            raise ValueError('配对评测样本格式错误')
        case, repeat, label, outcome = (row.get(k) for k in ('project', 'repeat', 'candidate', 'outcome'))
        if (not isinstance(case, str) or case not in tasks or type(repeat) is not int or not 1 <= repeat <= repeats
                or not isinstance(label, str) or label not in candidates or not isinstance(outcome, str)
                or outcome not in _OUTCOMES):
            raise ValueError('配对评测样本未匹配已声明的任务、轮次和候选')
        key = (case, repeat, label)
        if key in seen:
            raise ValueError('配对评测包含重复样本')
        seen.add(key)
        for field in ('baseline_commit', 'protected_test_sha256'):
            if (outcome != 'blocked' or field in row) and row.get(field) != tasks[case][field]:
                raise ValueError('配对样本的基线或保护测试指纹不一致')
        if row.get('backend_model') is not None and not isinstance(row['backend_model'], str):
            raise ValueError('配对样本的实际模型身份格式错误')
        passed = (row.get('protected_unchanged') is True and type(row.get('acceptance_exit')) is int
                  and row['acceptance_exit'] == 0)
        if outcome == 'completed':
            statuses = row.get('statuses')
            required = ('design', 'coding', 'testing', 'review')
            if (not passed or row.get('completed') is not True or not isinstance(statuses, dict)
                    or any(statuses.get(stage) != 'done' for stage in required)):
                raise ValueError('标记完整通过的样本缺少阶段或保护测试证据')
        per_candidate[label].append(row)
    return per_candidate, len(tasks) * repeats


def review_paired_results(path, historical_report):
    data, digest = _load(path)
    trials, expected = _validated_trials(data)
    history = historical_report['recommendations'].get('code', {})
    historical = {(row['tool'], row['model']): row for row in history.get('candidates', ())}
    minimum = historical_report['min_runs']
    quality, coverage = {}, {}
    for label, rows in trials.items():
        observed = {row['backend_model'] for row in rows if row.get('backend_model')}
        profile = data['candidates'][label]
        model = next(iter(observed), profile['model']) if len(observed) <= 1 else None
        matched = historical.get((profile['type'], model), {})
        history_runs = matched.get('runs', 0)
        complete = sum(row['outcome'] == 'completed' for row in rows)
        passed = sum(row.get('protected_unchanged') is True and type(row.get('acceptance_exit')) is int
                     and row['acceptance_exit'] == 0 for row in rows)
        quality[label] = {'scheduled': expected, 'recorded': len(rows), 'completed': complete,
                          'protected_passed': passed, 'blocked': sum(row['outcome'] == 'blocked' for row in rows),
                          'completion_rate': complete / expected, 'protected_pass_rate': passed / expected}
        coverage[label] = {'tool': profile['type'], 'model': model, 'history_runs': history_runs,
                           'missing_runs': max(0, minimum - history_runs)}
    data['summary'] = quality
    data['historical_routing'] = history
    if any(row['recorded'] != expected or row['blocked'] for row in quality.values()):
        comparison = {'status': 'incomplete_trials'}
    else:
        comparison = compare_routing(data)
    return {'schema_version': 1, 'paired_results_sha256': digest, 'historical_min_runs': minimum,
            'paired_samples_per_candidate': expected, 'history_coverage': coverage,
            'paired_quality': quality, **comparison}
