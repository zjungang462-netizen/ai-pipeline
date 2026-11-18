"""固定引擎场景与可选真实模型小样本；离线结果不计入模型成功率。"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator.adapters.base import Adapter
from orchestrator.adapters.factory import build_all
from orchestrator.config import load_config
from orchestrator.contracts import PreflightReport, Result
from orchestrator.engine import Engine
from scripts.failure_evidence import Scrubber, environment_secrets

TASK = '只修改 app.py：normalize(value) 将任意字符串去除首尾空格，按空白分词后用单个连字符连接，并转小写；空字符串返回空字符串。保留其它文件。'
CHECK = "import sys;sys.path.insert(0,'.');from app import normalize;assert normalize(' A  B ')=='a-b';assert normalize('')=='';assert normalize('X\\tY')=='x-y'"


class Scripted(Adapter):
    channel = 'fake'

    def __init__(self, name, scenario):
        self.name, self.scenario, self.calls = name, scenario, 0

    def preflight(self):
        return PreflightReport(self.name, True)

    def run(self, prompt, workdir, ctx):
        self.calls += 1
        if ctx.kind == 'code':
            if self.scenario == 'interrupt_resume' and self.calls == 1:
                raise KeyboardInterrupt('离线故障注入：调用中断')
            correct = self.scenario != 'test_repair' or self.calls > 1
            body = "return '-'.join(value.lower().split())" if correct else 'return value'
            (workdir / 'app.py').write_text(f'def normalize(value):\n    {body}\n# attempt {self.calls}\n', encoding='utf8')
            return Result(True, changed_files=['app.py'], channel='fake')
        report = ctx.runs_dir / f'{ctx.stage}-report.md'
        report.write_text('离线脚本场景，非模型输出。', encoding='utf8')
        if ctx.kind == 'review':
            failed = self.scenario == 'exhausted' or (self.scenario == 'review_repair' and self.calls == 1)
            verdict = {'verdict': 'fail' if failed else 'pass', 'issues': [
                {'file': 'app.py', 'problem': '离线审查故障注入', 'expected': '进行有界返工'}] if failed else []}
            (ctx.runs_dir / 'review_verdict.json').write_text(json.dumps(verdict), encoding='utf8')
        return Result(True, report_files=[report], channel='fake')


def toml_value(value):
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(k) + '=' + toml_value(v) for k, v in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ','.join(toml_value(v) for v in value) + ']'
    return json.dumps(value, ensure_ascii=False)


def save_config(path, raw):
    # 本脚本配置仅包含无日期的 JSON 兼容类型，嵌套表使用 TOML inline table。
    path.write_text('\n'.join(k + '=' + toml_value(v) for k, v in raw.items()) + '\n', encoding='utf8')


def run_case(output, scenario, live_config=None):
    case = output / scenario
    case.mkdir()
    work = case / 'workspace'
    work.mkdir()
    (work / 'app.py').write_text('def normalize(value):\n    return value\n', encoding='utf8')
    check = case / 'acceptance.py'
    check.write_text(CHECK, encoding='utf8')
    expected_check_hash = hashlib.sha256(check.read_bytes()).hexdigest()
    prompts = {'design': '给出简短实施计划，不写代码。{task}',
               'coding': '实际修改工作区文件，仅允许 app.py。遵循引擎要求的输出协议，不要只描述，不改验收。\n{task}\n{upstream}\n{feedback}',
               'review': '独立检查变更与测试证据。只返回JSON对象verdict=pass/fail，issues=[]或file/problem/expected条目，不附散文。\n{task}\n{upstream}'}
    if live_config:
        base = tomllib.loads(Path(live_config).read_text(encoding='utf8'))
        adapters = base['adapters']
        roles = {stage['kind']: stage.get('adapter') for stage in base['stages'] if stage['kind'] != 'test'}
        if not all(roles.get(kind) for kind in ('report', 'code', 'review')):
            raise ValueError('真实配置必须含 report/code/review 三类角色')
        for name, options in adapters.items():
            if options.get('type') == 'openai_compat':
                options['review_json_mode'] = True
            options['timeout_s'] = min(options.get('timeout_s', 120), 120)
        selected = {'design': roles['report'], 'coding': roles['code'], 'review': roles['review']}
    else:
        adapters = {role: {'type': 'fake'} for role in prompts}
        selected = {role: role for role in prompts}
    stages = [dict(name=role, kind=kind, adapter=selected[role], prompt=prompts[role],
                   max_attempts=1, timeout_s=120, **({'feedback_to': 'coding', 'max_rounds': 2} if role == 'review' else {}))
              for role, kind in [('design', 'report'), ('coding', 'code'), ('review', 'review')]]
    stages.insert(2, dict(name='testing', kind='test', test_cmd=[sys.executable, '-X', 'utf8', str(check)],
                         timeout_s=30, feedback_to='coding', max_rounds=2))
    stages[1]['context_files'] = ['app.py']
    raw = dict(task_workdir=str(work), state_dir=str(case / 'state'), watch_root='off', adapters=adapters,
               run_budget=dict(unlimited_tokens=True, max_duration_s=240),
               autonomy=dict(enabled=True, allowed_files=['app.py'], baseline_paths=['app.py'],
                             max_repair_rounds=2, max_stagnant_rounds=2, repair_batch_size=1), stages=stages)
    config_path = case / 'pipeline.toml'
    save_config(config_path, raw)
    cfg = load_config(config_path)
    if live_config:
        kinds = {stage.adapter: stage.kind for stage in cfg.stages if stage.adapter}
        instances = build_all(cfg.adapters, kinds)
    else:
        instances = {name: Scripted(name, scenario) for name in adapters}
    started = time.monotonic()
    if live_config:
        checks = {}
        for name, adapter in instances.items():
            checked = adapter.preflight()
            known = not hasattr(adapter, '_version_known') or adapter._version_known()
            checks[name] = {'ok': checked.ok and known, 'tool_version': checked.tool_version,
                            'detail': checked.detail, 'version_known': known}
        (case / 'preflight.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding='utf8')
        if not all(item['ok'] for item in checks.values()):
            record = dict(scenario=scenario, mode='live', completed=False, expectation_met=False,
                          states={}, failure_category='preflight_blocked', preflight=checks,
                          wall_time_s=round(time.monotonic()-started, 3), usage={},
                          model_calls=0, protected_test_unchanged=True,
                          human_implementation_interventions=0, billing_cost=None)
            (case / 'result.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf8')
            return record
    engine = Engine(cfg, instances, 'sample-01', TASK)
    interrupted = False
    try:
        status = engine.run()
    except KeyboardInterrupt:
        if live_config:
            raise
        interrupted = True
        engine = Engine(cfg, instances, 'sample-01', TASK)
        status = engine.run()
    checked = subprocess.run([sys.executable, str(check)], cwd=work, capture_output=True, timeout=30)
    complete = bool(status) and all(v == 'done' for v in status.values()) and checked.returncode == 0
    unchanged = hashlib.sha256(check.read_bytes()).hexdigest() == expected_check_hash
    state = engine.state
    record = dict(scenario=scenario, mode='live' if live_config else 'offline_scripted',
                  completed=complete, expected_terminal='failed' if scenario == 'exhausted' else 'done',
                  states=status, interrupted=interrupted, wall_time_s=round(time.monotonic()-started, 3),
                  attempts={s.name: len(state.stage(s.name).attempts) for s in cfg.stages},
                  repair_events=[e['kind'] for e in state.events if e['kind'] in ('test_feedback', 'review_feedback')],
                  usage=state.usage_total, protected_test_unchanged=unchanged,
                  human_implementation_interventions=0, billing_cost=None)
    record['expectation_met'] = unchanged and (not complete and 'failed' in status.values() if scenario == 'exhausted' else complete)
    if scenario == 'test_repair':
        record['expectation_met'] &= 'test_feedback' in record['repair_events']
    if scenario == 'review_repair':
        record['expectation_met'] &= 'review_feedback' in record['repair_events']
    if scenario == 'interrupt_resume':
        record['expectation_met'] &= interrupted
    (case / 'result.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf8')
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--live-config', help='显式启用真实模型调用，沿用所选配置的角色适配器')
    args = parser.parse_args(argv)
    output = Path(args.output).resolve()
    if output.is_relative_to(ROOT):
        parser.error('输出必须位于产品目录之外')
    output.mkdir(parents=True, exist_ok=False)
    scenarios = ['live_normalize'] if args.live_config else ['first_pass', 'test_repair', 'review_repair', 'interrupt_resume', 'exhausted']
    scrub = Scrubber(environment_secrets())
    from contextlib import redirect_stdout, redirect_stderr
    from scripts.failure_evidence import RedactedLog
    results = []
    for scenario in scenarios:
        with (output / (scenario + '.log')).open('w', encoding='utf8') as raw:
            log = RedactedLog(raw)
            try:
                with redirect_stdout(log), redirect_stderr(log):
                    result = run_case(output, scenario, args.live_config)
            finally:
                log.finish()
        results.append(result)
        (output / 'results.json').write_text(scrub.text(json.dumps(results, ensure_ascii=False, indent=2)), encoding='utf8')
        print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0 if all(r['expectation_met'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
