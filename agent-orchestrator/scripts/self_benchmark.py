"""在固定提交的隔离源码副本上运行真实维护任务，保留失败及保护测试证据。"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout, redirect_stderr
import json
from pathlib import Path
import subprocess
import sys
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator.adapters.factory import build_all
from orchestrator.config import load_config
from orchestrator.engine import Engine
from orchestrator.state import sha256_file
from scripts.benchmark import save_config
from scripts.failure_evidence import RedactedLog
from scripts.release import git, source_files


CASES = {
    'large_integer': {
        'task': '修复 orchestrator/reporting.py 的 number：任意非负 Python 整数（包括 10**400）必须原样保留，不能因 math.isfinite 的浮点转换溢出；bool、负数、NaN、Infinity、字符串仍返回 None。保留其他摘要与 CSV 行为。',
        'checks': """from orchestrator.reporting import number
assert number(10**400) == 10**400
assert number(0) == 0 and number(1.5) == 1.5
for value in (True, False, -1, -1.5, float('nan'), float('inf'), '10', None):
    assert number(value) is None, repr(value)
""",
    },
    'malformed_usage': {
        'task': '修复 orchestrator/reporting.py 的 usage_summary：现场 state.usage_total 为 None、list、str 等非对象时，按缺失计量处理，不抛异常、tokens/reported_tokens/duration_s 为 None、token_status=not_recorded；有效终态 metrics 仍优先，绝不用现场值覆盖。保留未知值与现有 CSV 行为。',
        'checks': """from orchestrator.reporting import usage_summary
for bad in (None, [], 'invalid', 7):
    row = usage_summary('case', {'usage_total': bad, 'stages': {}})
    assert row['tokens'] is None and row['reported_tokens'] is None
    assert row['duration_s'] is None and row['token_status'] == 'not_recorded'
row = usage_summary('case', {'usage_total': None}, {'tokens': 19, 'duration_s': 2, 'stages': {}})
assert row['tokens'] == 19 and row['source'] == 'immutable_metrics'
""",
    },
    'malformed_job': {
        'task': '修复 orchestrator/reporting.py 的 failure_categories：job 为非对象或 status 为 list/dict 等畸形值时忽略该字段，不抛异常；合法 timeout/cancelled 仍归类，已有 state 事件、metrics 失败原因和 unknown 回退保持。不要从错误正文推测原因。',
        'checks': """from orchestrator.reporting import failure_categories
state = {'stages': {'coding': {'status': 'failed'}}, 'events': [{'kind': 'test_fail'}]}
for job in (None, [], [1], 'invalid', 4, {'status': []}, {'status': {}}):
    assert failure_categories(state, job) == ['test_failure']
assert failure_categories({}, {'status': 'cancelled'}) == ['cancelled']
assert failure_categories({}, {'status': 'timeout'}) == ['timeout']
assert failure_categories({'stages': {'x': {'status': 'failed'}}}, []) == ['unknown']
""",
    },
}


def acceptance_text(checks):
    return ("import sys, unittest\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path.cwd()))\n" + checks +
            "\nsuite = unittest.defaultTestLoader.discover('tests', pattern='test_phase5_reporting.py')\n"
            "result = unittest.TextTestRunner(verbosity=2).run(suite)\n"
            "assert result.testsRun > 0\nsys.exit(not result.wasSuccessful())\n")


def run_case(output, name, spec, source, files, commit):
    case = output / name
    workspace = case / 'workspace'
    workspace.mkdir(parents=True)
    for relative, raw in files.items():
        path = workspace / Path(relative).relative_to('agent-orchestrator')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    check = case / 'acceptance.py'
    check.write_text(acceptance_text(spec['checks']), encoding='utf8')
    protected = {p.relative_to(workspace).as_posix(): sha256_file(p)
                 for p in (workspace / 'tests').rglob('*') if p.is_file()}
    protected_hash = sha256_file(check)
    initial = subprocess.run([sys.executable, '-X', 'utf8', str(check)], cwd=workspace,
                             capture_output=True, timeout=60)
    (case / 'baseline-test.log').write_bytes(initial.stdout + initial.stderr)
    if initial.returncode == 0:
        raise ValueError('维护缺陷未被初始保护测试复现，不能计入修复基准')
    raw = tomllib.loads(Path(source).read_text(encoding='utf8'))
    roles = {s['kind']: s['adapter'] for s in raw['stages'] if s['kind'] != 'test'}
    if not all(roles.get(kind) for kind in ('report', 'code', 'review')):
        raise ValueError('配置必须有 report/code/review 角色')
    adapters = {name: dict(raw['adapters'][name]) for name in set(roles.values())}
    for options in adapters.values():
        options.pop('max_tokens', None)
        options['timeout_s'] = 180
        if options['type'] == 'openai_compat':
            options['review_json_mode'] = True
    task = spec['task'] + '\n只允许修改该文件；实际测试由引擎执行，不能修改验收或其它源码。'
    coding_prompt = '实际修改文件，不要只提出建议。\n{task}\n{upstream}\n{feedback}'
    design_prompt = ('根据下列真实源码给出最多三条修改方案，不需要扩展需求。\n{task}\n'
                     + (workspace / 'orchestrator/reporting.py').read_text(encoding='utf8'))
    config = dict(task_workdir=str(workspace), state_dir=str(case / 'state'), watch_root='off',
                  run_budget=dict(max_tokens=120000, max_duration_s=600), adapters=adapters,
                  autonomy=dict(enabled=True, allowed_files=['orchestrator/reporting.py'],
                                baseline_paths=[p.name for p in workspace.iterdir()],
                                max_repair_rounds=2, repair_batch_size=1, output_tokens=16384),
                  stages=[
                      dict(name='design', kind='report', adapter=roles['report'],
                           prompt=design_prompt,
                           context_files=['orchestrator/reporting.py'], max_attempts=1, timeout_s=180),
                      dict(name='coding', kind='code', adapter=roles['code'], prompt=coding_prompt,
                           context_files=['orchestrator/reporting.py'], max_attempts=1, timeout_s=180),
                      dict(name='testing', kind='test', test_cmd=[sys.executable, '-X', 'utf8', str(check)],
                           feedback_to='coding', max_rounds=2, timeout_s=60),
                      dict(name='review', kind='review', adapter=roles['review'],
                           prompt='独立检查需求、源码差异及真实测试证据。返回严格 JSON 对象 verdict=pass/fail，issues=[] 或 file/problem/expected。\n{task}\n{upstream}',
                           feedback_to='coding', max_rounds=2, max_attempts=1, timeout_s=180)])
    path = case / 'pipeline.toml'
    save_config(path, config)
    cfg = load_config(path)
    instances = build_all(cfg.adapters, {s.adapter: s.kind for s in cfg.stages if s.adapter})
    checks = {}
    for name, adapter in instances.items():
        report = adapter.preflight()
        known = not hasattr(adapter, '_version_known') or adapter._version_known()
        checks[name] = dict(ok=report.ok and known, version=report.tool_version, version_known=known)
    started = time.monotonic()
    record = dict(scenario=case.name, source_commit=commit, mode='live_self_maintenance',
                  baseline_reproduced=True, protected_test_sha256=protected_hash,
                  preflight=checks, human_implementation_interventions=0, completed=False)
    if all(check['ok'] for check in checks.values()):
        engine = Engine(cfg, instances, 'self-' + case.name, task)
        statuses = engine.run()
        final = subprocess.run([sys.executable, '-X', 'utf8', str(check)], cwd=workspace,
                               capture_output=True, timeout=60)
        (case / 'final-test.log').write_bytes(final.stdout + final.stderr)
        unchanged = (sha256_file(check) == protected_hash and all(
            (workspace / p).is_file() and sha256_file(workspace / p) == digest
            for p, digest in protected.items()))
        record.update(states=statuses, protected_tests_unchanged=unchanged,
                      final_test_exit=final.returncode,
                      completed=bool(statuses) and all(s == 'done' for s in statuses.values())
                      and final.returncode == 0 and unchanged,
                      usage=engine.state.usage_total,
                      attempts={s.name: len(engine.state.stage(s.name).attempts) for s in cfg.stages},
                      repair_events=[e['kind'] for e in engine.state.events if e['kind'] in ('test_feedback', 'review_feedback')])
    else:
        record.update(failure_category='preflight_blocked', model_calls=0)
    record['wall_time_s'] = round(time.monotonic() - started, 3)
    (case / 'result.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf8')
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--live-config', required=True, help='显式启用真实模型调用')
    parser.add_argument('--ref', default='HEAD', help='用于复现维护缺陷的固定源码提交')
    parser.add_argument('--case', action='append', choices=tuple(CASES))
    args = parser.parse_args(argv)
    output = Path(args.output).resolve()
    repo = Path(git(ROOT, 'rev-parse', '--show-toplevel').decode().strip())
    if output.is_relative_to(repo):
        parser.error('证据必须位于仓库外的全新目录')
    output.mkdir(parents=True, exist_ok=False)
    commit = git(repo, 'rev-parse', '--verify', args.ref + '^{commit}').decode().strip()
    files = source_files(repo, commit)
    records = []
    runner = {name: sha256_file(ROOT / name) for name in (
        'orchestrator/engine.py', 'orchestrator/adapters/cli_claude.py', 'scripts/self_benchmark.py')}
    for name in args.case or CASES:
        with (output / (name + '.log')).open('w', encoding='utf8') as raw:
            log = RedactedLog(raw)
            try:
                with redirect_stdout(log), redirect_stderr(log):
                    try:
                        record = run_case(output, name, CASES[name], args.live_config, files, commit)
                    except Exception as error:
                        record = dict(scenario=name, completed=False, source_commit=commit,
                                      failure_category='runner_error', error_type=type(error).__name__,
                                      wall_time_s=0)
                        case = output / name
                        case.mkdir(exist_ok=True)
                        (case / 'result.json').write_text(json.dumps(record, indent=2), encoding='utf8')
            finally:
                log.finish()
        records.append(record)
        summary = dict(source_commit=commit, scope='single_project_small_sample',
                       runner_source_sha256=runner,
                       results=records, completed=sum(r['completed'] for r in records), total=len(records),
                       billing_cost=None)
        (output / 'results.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf8')
        print(json.dumps(dict(scenario=name, completed=record['completed'], wall_time_s=record['wall_time_s'])), flush=True)
    return 0 if all(r['completed'] for r in records) else 1


if __name__ == '__main__':
    raise SystemExit(main())
