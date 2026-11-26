"""同一基线、受保护测试和预算下的编码候选配对评测。"""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator.adapters.base import Adapter
from orchestrator.config import load_config
from orchestrator.contracts import PreflightReport, Result
from orchestrator.model_router import recommend_adapters
from orchestrator.route_shadow import compare_routing
from orchestrator.safety import build_subprocess_env
from orchestrator.state import atomic_write_json, sha256_file
from scripts.failure_evidence import RedactedLog, Scrubber, environment_secrets
from scripts.multi_project_benchmark import CASES, acceptance_source, run_case


BUDGET = {'max_tokens': 50_000, 'max_model_calls': 8, 'max_unknown_calls': 3,
          'stage_timeout_s': 120, 'max_duration_s': 360}
ROLES = {'report': 'design', 'code': 'coding', 'review': 'review'}

# 离线参考实现用于检查评测链路。次候选只通过 invoice，便于核对统计分母。
SOLUTIONS = {
    'invoice': {
        'pricing.py': '''def line_total(cents, quantity):
    if type(cents) is not int or cents < 0 or type(quantity) is not int or quantity <= 0:
        raise ValueError('invalid price or quantity')
    return cents * quantity
''',
        'invoice.py': '''from pricing import line_total


def total(items):
    amount = 0
    for item in items:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError('invalid line')
        amount += line_total(*item)
    return amount
''',
    },
    'reservations': {
        'slots.py': '''def overlaps(a, b):
    if a[0] >= a[1] or b[0] >= b[1]:
        raise ValueError('invalid range')
    return a[0] < b[1] and b[0] < a[1]
''',
        'booking.py': '''from slots import overlaps


def can_book(existing, requested, capacity):
    if type(capacity) is not int or capacity <= 0 or requested[0] >= requested[1]:
        raise ValueError('invalid capacity or request')
    events = []
    for slot in existing:
        if overlaps(slot, requested):
            events.append((max(slot[0], requested[0]), 1))
            events.append((min(slot[1], requested[1]), -1))
    count = 0
    fits = True
    for _, delta in sorted(events):
        count += delta
        fits = fits and count < capacity
    return fits
''',
    },
    'ledger': {
        'ledger.py': '''def apply_refund(paid_cents, refunded_cents, amount_cents):
    if (any(type(n) is not int for n in (paid_cents, refunded_cents, amount_cents))
            or not 0 <= refunded_cents <= paid_cents or amount_cents <= 0
            or refunded_cents + amount_cents > paid_cents):
        raise ValueError('invalid amounts')
    return refunded_cents + amount_cents
''',
        'reconcile.py': '''from ledger import apply_refund


def settle(paid_cents, events):
    if type(paid_cents) is not int or paid_cents < 0:
        raise ValueError('invalid paid amount')
    seen = {}
    refunded = 0
    for event_id, amount in events:
        if not isinstance(event_id, str) or not event_id or type(amount) is not int or amount <= 0:
            raise ValueError('invalid event')
        if event_id in seen:
            if seen[event_id] != amount:
                raise ValueError('conflicting event')
            continue
        seen[event_id] = amount
        refunded = apply_refund(paid_cents, refunded, amount)
    return paid_cents - refunded
''',
    },
}


class FixtureAdapter(Adapter):
    channel = 'fake'

    def __init__(self, name, case, candidate):
        self.name, self.case, self.candidate = name, case, candidate
        self.attempt = 0

    def preflight(self):
        return PreflightReport(self.name, True, tool_version='fixture-v1')

    def run(self, prompt, workdir, ctx):
        if ctx.kind == 'code':
            self.attempt += 1
            files = SOLUTIONS[self.case]
            if self.candidate == 'fixture-partial' and self.case != 'invoice':
                files = {name: source + f'\n# fixture attempt {self.attempt}\n'
                         for name, source in CASES[self.case][0].items()}
            for name, source in files.items():
                (workdir / name).write_text(source, encoding='utf8')
            return Result(True, changed_files=list(files))
        report = ctx.runs_dir / f'{ctx.stage}-report.md'
        report.write_text('配对评测离线夹具。', encoding='utf8')
        if ctx.kind == 'review':
            atomic_write_json(ctx.runs_dir / 'review_verdict.json', {'verdict': 'pass', 'issues': []})
        return Result(True, report_files=[report])


def make_baseline(path, case):
    path.mkdir(parents=True)
    for name, source in CASES[case][0].items():
        (path / name).write_bytes(source.encode('utf8'))
    env = build_subprocess_env()
    env.update(GIT_AUTHOR_DATE='2000-01-01T00:00:00Z', GIT_COMMITTER_DATE='2000-01-01T00:00:00Z',
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=str(path.parent / 'empty.gitconfig'))
    git_exe = shutil.which('git')
    if not git_exe:
        raise RuntimeError('配对评测需要 Git')

    def git(*args):
        return subprocess.run([git_exe, *args], cwd=path, env=env, check=True, capture_output=True,
                              encoding='utf8', timeout=30).stdout.strip()

    git('init', '-q', '--template=', '--object-format=sha1')
    for key, value in {'user.name': 'Pipeline Benchmark', 'user.email': 'benchmark@local',
                       'core.autocrlf': 'false'}.items():
        git('config', key, value)
    git('add', '--', *sorted(CASES[case][0]))
    git('commit', '-q', '--no-gpg-sign', '-m', f'Benchmark baseline: {case}')
    return git('rev-parse', 'HEAD')


def summarize(rows):
    count = len(rows)
    completed = sum(row['outcome'] == 'completed' for row in rows)
    protected_pass = sum(row.get('protected_unchanged') is True and row.get('acceptance_exit') == 0 for row in rows)
    durations = [row['elapsed_s'] for row in rows if row.get('elapsed_s') is not None]
    usage = [row['usage'] for row in rows if row.get('model_calls') and 'usage' in row]
    unknown = sum(value.get('unknown_calls', 0) for value in usage)
    known = [value.get('reported_tokens', value.get('tokens')) for value in usage if not value.get('unknown_calls')]
    known = [value for value in known if type(value) is int]
    return {'scheduled': count, 'completed': completed, 'protected_passed': protected_pass,
            'failed': sum(row['outcome'] == 'failed' for row in rows),
            'blocked': sum(row['outcome'] == 'blocked' for row in rows),
            'completion_rate': completed / count if count else None,
            'protected_pass_rate': protected_pass / count if count else None,
            'wall_time_samples': len(durations), 'median_wall_time_s': statistics.median(durations) if durations else None,
            'known_usage_samples': len(known), 'median_reported_tokens': statistics.median(known) if known else None,
            'unknown_calls': unknown, 'model_calls': sum(row.get('model_calls', 0) for row in rows)}


def compare_pairs(tasks, repeats, names, rows):
    index = {(row['project'], row['repeat'], row['candidate']): row for row in rows}
    pairs = []
    for case in tasks:
        for repeat in range(1, repeats + 1):
            found = [index.get((case, repeat, label)) for label in names]
            if any(row is None for row in found):
                continue
            a, b = found
            timings = [row.get('elapsed_s') for row in found]
            pairs.append({'case': case, 'repeat': repeat, 'outcomes': [a['outcome'], b['outcome']],
                          'wall_time_delta_s': round(timings[1] - timings[0], 3) if None not in timings else None})
    return pairs


def member(adapter):
    return {'type': adapter.type, 'model': adapter.options.get('model'),
            'reasoning_effort': adapter.options.get('reasoning_effort'), 'runtime_version': adapter.options.get('runtime_version')}


def configured_candidates(cfg, names):
    if len(names) != 2 or len(set(names)) != 2:
        raise ValueError('真实评测需要两个不同的 --candidate 名称')
    shared = {stage.kind: cfg.adapters[stage.adapter] for stage in reversed(cfg.stages)
              if stage.kind in ('report', 'review') and stage.adapter}
    if set(shared) != {'report', 'review'} or any(adapter.type == 'fake' for adapter in shared.values()):
        raise ValueError('真实评测配置必须包含可用的 report 和 review 角色')
    resolved = {}
    for label in names:
        adapter = cfg.adapters.get(label)
        if adapter is None or (adapter.type != 'openai_compat' and not adapter.type.startswith('cli_')):
            raise ValueError(f'候选须是配置中的 API 或 CLI 编码适配器，名称为 {label}')
        resolved[label] = ({ROLES[kind]: {**item.options, 'type': item.type}
                            for kind, item in {**shared, 'code': adapter}.items()}, member(adapter))
    return resolved, {kind: member(adapter) for kind, adapter in shared.items()}


def write_report(path, data):
    def shown(value):
        return '—' if value is None else f'{value:.3g}'

    rows = ['# 配对评测', '', f"执行模式 `{data['mode']}`，每个任务重复 {data['repeats']} 次。", '',
            '| 候选 | 全流程完成 | 保护测试通过 | 失败 | 阻塞 | 中位耗时（秒） | 中位已报告 tokens | 未知用量调用 |',
            '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for name, summary in data['summary'].items():
        count = summary['scheduled']
        rows.append(f"| `{name}` | {summary['completed']}/{count} | {summary['protected_passed']}/{count} | "
                    f"{summary['failed']} | {summary['blocked']} | {shown(summary['median_wall_time_s'])} | "
                    f"{shown(summary['median_reported_tokens'])} | {summary['unknown_calls']} |")
    rows.extend(['', '每个样本的基线、测试指纹及配对结果见 `results.json`。', ''])
    if 'routing_comparison' in data:
        comparison = data['routing_comparison']
        if comparison['status'] == 'compared':
            verdict = '一致' if comparison['agree'] else '不同'
            rows.extend([f'历史路由排序与本轮配对结果{verdict}。', ''])
        else:
            rows.extend([f"路由对照状态 `{comparison['status']}`。", ''])
    path.write_text('\n'.join(rows), encoding='utf8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--case', choices=tuple(CASES), action='append')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--max-tokens', type=int, default=BUDGET['max_tokens'], help='每个样本使用相同的本地 token 预算')
    parser.add_argument('--live-config', type=Path, help='显式启用真实调用并提供公共报告和审查角色')
    parser.add_argument('--candidate', action='append', help='真实配置中的编码候选名称，指定两次')
    parser.add_argument('--routing-state', type=Path, help='真实评测时可选，用于附加现有历史路由排序')
    args = parser.parse_args(argv)
    if not 1 <= args.repeats <= 10:
        parser.error('--repeats 必须介于 1 和 10')
    if args.max_tokens <= 0:
        parser.error('--max-tokens 必须为正整数')
    budget = {**BUDGET, 'max_tokens': args.max_tokens}
    if not args.live_config and (args.candidate or args.routing_state):
        parser.error('--candidate 和 --routing-state 需要 --live-config')
    output = args.output.resolve()
    if output.is_relative_to(ROOT.parent):
        parser.error('评测输出须位于仓库外')
    config_hash = None
    if args.live_config:
        try:
            cfg = load_config(args.live_config)
            candidates, shared = configured_candidates(cfg, args.candidate or [])
            config_hash = cfg.source_hash
        except (ValueError, OSError) as error:
            parser.error(str(error))
    else:
        candidates = {label: ({role: {'type': 'fake'} for role in ROLES.values()}, {'type': 'fake', 'model': label})
                      for label in ('fixture-reference', 'fixture-partial')}
        shared = {kind: {'type': 'fake'} for kind in ('report', 'review')}
    tasks = tuple(dict.fromkeys(args.case or CASES))
    names = tuple(candidates)
    output.mkdir(parents=True, exist_ok=False)
    specimens = {}
    for case in tasks:
        repo = output / 'baselines' / case
        specimens[case] = {'baseline_commit': make_baseline(repo, case),
                           'task_sha256': hashlib.sha256(CASES[case][1].encode('utf8')).hexdigest(),
                           'protected_test_sha256': hashlib.sha256(acceptance_source(case)).hexdigest()}
    scrubber = Scrubber(environment_secrets())
    data = {'schema_version': 1, 'mode': 'live' if args.live_config else 'offline_fixture',
            'repeats': args.repeats, 'budget': budget, 'config_sha256': config_hash, 'shared_roles': shared,
            'candidates': {key: profile for key, (_, profile) in candidates.items()}, 'tasks': specimens,
            'runner_sha256': {file.relative_to(ROOT).as_posix(): sha256_file(file) for file in (
                Path(__file__).resolve(), ROOT / 'scripts/multi_project_benchmark.py',
                ROOT / 'orchestrator/engine.py', ROOT / 'prompts/cli_review.md')},
            'results': []}
    if args.routing_state:
        route = recommend_adapters(args.routing_state, stage_kind='code')['recommendations'].get('code')
        data['historical_routing'] = route
    atomic_write_json(output / 'results.json', data)
    for index, case in enumerate(tasks):
        specimen = specimens[case]
        for repeat in range(1, args.repeats + 1):
            order = names if (index + repeat) % 2 else names[::-1]
            for label in order:
                path = output / 'trials' / case / str(repeat) / str(names.index(label) + 1)
                path.mkdir(parents=True)
                adapters = candidates[label][0]
                instance_factory = None
                if not args.live_config:
                    def instance_factory(config):
                        return {name: FixtureAdapter(name, case, label) for name in config.adapters}
                with (path / 'run.log').open('w', encoding='utf8') as sink:
                    log = RedactedLog(sink)
                    try:
                        with redirect_stdout(log), redirect_stderr(log):
                            row = run_case(path, case, adapters, ROLES, max_tokens=budget['max_tokens'],
                                           stage_timeout_s=budget['stage_timeout_s'], max_model_calls=budget['max_model_calls'],
                                           max_unknown_calls=budget['max_unknown_calls'], baseline_repo=output / 'baselines' / case,
                                           expected_baseline=specimen['baseline_commit'], instance_factory=instance_factory,
                                           verify_versions=bool(args.live_config))
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                        row = {'project': case, 'completed': False, 'reason': 'runner_error',
                               'detail': scrubber.text(str(exc))}
                    finally:
                        log.finish()
                if row.get('unavailable'):
                    row['unavailable'] = {role: scrubber.text(str(reason)) for role, reason in row['unavailable'].items()}
                if row.get('protected_test_sha256') not in (None, specimen['protected_test_sha256']):
                    row.update(completed=False, protected_unchanged=False, reason='fixture_mismatch')
                outcome = 'blocked' if row.get('reason') else 'completed' if row['completed'] else 'failed'
                if not args.live_config:
                    row['fixture_calls'] = row.get('model_calls', 0)
                    row['model_calls'] = 0
                row.update(candidate=label, repeat=repeat, outcome=outcome)
                data['results'].append(row)
                atomic_write_json(output / 'results.json', data)
    data['summary'] = {label: summarize([row for row in data['results'] if row['candidate'] == label]) for label in names}
    data['pairs'] = compare_pairs(tasks, args.repeats, names, data['results'])
    data['pair_order'] = names
    if args.routing_state:
        data['routing_comparison'] = compare_routing(data)
    atomic_write_json(output / 'results.json', data)
    write_report(output / 'report.md', data)
    print(json.dumps(data['summary'], ensure_ascii=True), flush=True)
    return int(any(row['outcome'] == 'blocked' for row in data['results']))


if __name__ == '__main__':
    raise SystemExit(main())
