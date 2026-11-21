"""在隔离的小型多模块项目中验证真实维护闭环。"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator.adapters.factory import build_all
from orchestrator.config import load_config, parse_config
from orchestrator.engine import Engine
from orchestrator.state import atomic_write_json, sha256_file


CASES = {
    'invoice': (
        {'pricing.py': 'def line_total(cents, quantity):\n    return cents * quantity\n',
         'invoice.py': 'from pricing import line_total\n\ndef total(items):\n    return sum(line_total(p, q) for p, q in items)\n'},
        '只修改 pricing.py 和 invoice.py。价格须为非负整数分、数量须为正整数，bool 不算整数；'
        '非法值抛 ValueError。total 接收由二元组组成的 iterable，单项不是长度为 2 的元组时抛 ValueError。'
        '空 iterable 返回 0，支持任意大的整数精确计算。',
        '''from invoice import total
from pricing import line_total
assert total(iter([(167, 2), (20, 3)])) == 394
assert total([]) == 0
assert total([(10**30, 3)]) == 3 * 10**30
for price, qty in [(True, 2), (-1, 1), (3, False), (3, 0), (3, -2), (1.5, 2)]:
    for fn in (lambda: line_total(price, qty), lambda: total([(price, qty)])):
        try:
            fn()
        except ValueError:
            pass
        else:
            raise AssertionError((price, qty))
for item in (None, [20, 3], (20,), (20, 3, 1)):
    try:
        total([item])
    except ValueError:
        pass
    else:
        raise AssertionError(item)
'''),
    'reservations': (
        {'slots.py': 'def overlaps(a, b):\n    return a[0] <= b[1] and b[0] <= a[1]\n',
         'booking.py': 'from slots import overlaps\n\ndef can_book(existing, requested, capacity):\n'
                       '    return sum(overlaps(slot, requested) for slot in existing) < capacity\n'},
        '只修改 slots.py 和 booking.py。预约是半开区间 [start,end)，相邻不重叠。'
        'can_book 仅在新预约范围内任一时刻加入它后都不超过正整数容量时返回 True。'
        '空或逆序区间和非法容量抛 ValueError，existing 可为一次性迭代器。'
        '调用方按 can_book(existing, requested, capacity) 传入三个参数，requested 是二元组；'
        '公开签名必须保持为 can_book(existing, requested, capacity) 和 overlaps(a, b)。',
        '''from slots import overlaps
from booking import can_book
assert not overlaps((1, 3), (3, 5))
assert can_book(iter([(1, 3), (3, 5)]), (1, 5), 2)
assert not can_book([(1, 4), (3, 5)], (2, 5), 2)
assert can_book([(0, 1), (5, 6)], (1, 5), 1)
assert not can_book([(2, 4)], (1, 5), 1)
for existing, requested, cap in [([], (2, 2), 1), ([(4, 2)], (1, 3), 1), ([], (1, 2), 0), ([], (1, 2), True)]:
    try:
        can_book(existing, requested, cap)
    except ValueError:
        pass
    else:
        raise AssertionError((existing, requested, cap))
'''),
}


def run_case(root, name, adapters, roles, stage_timeout_s=120, max_tokens=50000, *,
             unlimited_tokens=False, max_model_calls=None, max_unknown_calls=None):
    files, task, assertions = CASES[name]
    case = root / name
    workspace = case / 'workspace'
    workspace.mkdir(parents=True)
    for path, body in files.items():
        (workspace / path).write_text(body, encoding='utf8')
    check = case / 'acceptance.py'
    check.write_text('import sys\nsys.path.insert(0, ".")\n' + assertions, encoding='utf8')
    frozen = sha256_file(check)
    command = [sys.executable, '-I', '-B', '-X', 'utf8', str(check)]

    def check_exit(filename):
        process = subprocess.run(command, cwd=workspace, capture_output=True, timeout=20)
        (case / filename).write_bytes(process.stdout + process.stderr)
        return process.returncode

    before = check_exit('before.log')
    if before == 0:
        raise ValueError('初始保护断言未复现缺陷')
    coding_prompt = '解决任务并按引擎指定的方式交付。\n{task}\n{upstream}\n{feedback}'
    if roles['code'] == 'api':
        coding_prompt += ('\n每个文件单独一段。第一行写 # patch: 文件名，第二行写单行 JSON，'
                          '形如 {"edits":[{"old":"唯一匹配的原文","new":"替换后的文本"}]}。'
                          '换行在 JSON 字符串内编码为 \\n。')
    stages = [
        dict(name='design', kind='report', prompt='根据任务给出三条实施步骤，保留所有公开函数签名。\n{task}\n{upstream}'),
        dict(name='coding', kind='code', prompt=coding_prompt),
        dict(name='testing', kind='test', test_cmd=command, timeout_s=20, feedback_to='coding', max_rounds=2),
        dict(name='review', kind='review', prompt='核对任务、改动与测试证据。只返回 verdict/ issues 的 JSON 对象。\n{task}\n{upstream}',
             feedback_to='coding', max_rounds=2),
    ]
    for stage in stages:
        if stage['kind'] != 'test':
            timeout = stage_timeout_s if stage['kind'] == 'code' else 120
            stage.update(adapter=roles[stage['kind']], timeout_s=timeout, max_attempts=1, context_files=list(files))
    run_budget = dict(max_duration_s=max(360, stage_timeout_s + 240))
    if unlimited_tokens:
        run_budget['unlimited_tokens'] = True
    else:
        run_budget['max_tokens'] = max_tokens
    if max_model_calls is not None:
        run_budget['max_model_calls'] = max_model_calls
    if max_unknown_calls is not None:
        run_budget['max_unknown_calls'] = max_unknown_calls
    cfg = parse_config(dict(task_workdir=str(workspace), state_dir=str(case / 'state'),
                            run_budget=run_budget,
                            autonomy=dict(enabled=True, allowed_files=list(files), repair_batch_size=2,
                                          max_repair_rounds=2, output_tokens=4096),
                            adapters=adapters, stages=stages), base_dir=case)
    instances = build_all(cfg.adapters, {s.adapter: s.kind for s in cfg.stages if s.adapter})
    preflights = {name: adapter.preflight() for name, adapter in instances.items()}
    if not all(probe.ok for probe in preflights.values()):
        return dict(project=name, completed=False, reason='adapter_preflight', model_calls=0,
                    unavailable={role: probe.detail for role, probe in preflights.items() if not probe.ok})
    engine = Engine(cfg, instances, 'maintenance', task)
    started = time.monotonic()
    statuses = engine.run()
    protected = sha256_file(check) == frozen
    after = check_exit('after.log') if protected else None
    protected = protected and sha256_file(check) == frozen
    return dict(project=name, editable_files=list(files), baseline_exit=before,
                acceptance_exit=after, protected_unchanged=protected, statuses=statuses,
                completed=all(statuses.get(stage['name']) == 'done' for stage in stages)
                and after == 0 and protected, elapsed_s=round(time.monotonic() - started, 3),
                versions={role: probe.tool_version for role, probe in preflights.items()},
                usage=engine.state.usage_total, model_calls=sum(e['kind'] == 'model_call_started'
                                                               for e in engine.state.events))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--live-config', required=True, type=Path, help='显式使用其中的真实 API 与编码角色')
    parser.add_argument('--editor-exe', type=Path, help='为配置中的 CLI 编码角色指定固定版本的可执行文件')
    parser.add_argument('--case', action='append', choices=tuple(CASES), help='只运行指定样例，可重复')
    parser.add_argument('--stage-timeout', type=int, default=120, help='编码阶段单次调用秒数，默认 120')
    parser.add_argument('--max-tokens', type=int, default=50000, help='单次样例的运行 token 上限，默认 50000')
    parser.add_argument('--unlimited-tokens', action='store_true',
                        help='关闭 token 上限，改由时长与调用次数预算约束（适用于豆包等无 token 上报的 CLI）')
    parser.add_argument('--max-model-calls', type=int, help='单次样例的模型调用次数上限')
    parser.add_argument('--max-unknown-calls', type=int, help='单次样例的未知 token 用量调用次数上限')
    args = parser.parse_args()
    if args.stage_timeout < 1 or args.max_tokens < 1:
        parser.error('--stage-timeout 和 --max-tokens 必须为正数')
    if args.max_model_calls is not None and args.max_model_calls < 1:
        parser.error('--max-model-calls 必须为正数')
    if args.max_unknown_calls is not None and args.max_unknown_calls < 1:
        parser.error('--max-unknown-calls 必须为正数')
    config = load_config(args.live_config)
    source = next((item for item in config.adapters.values() if item.type == 'openai_compat'), None)
    if source is None:
        parser.error('需要已配置的 openai_compat 适配器')
    options = dict(source.options, type='openai_compat', review_json_mode=True, thinking='disabled')
    options.pop('reasoning_effort', None)
    options.pop('max_tokens', None)
    adapters = {'api': options}
    roles = dict(report='api', code='api', review='api')
    code = next((config.adapters[stage.adapter] for stage in config.stages if stage.kind == 'code'), None)
    if code and code.type.startswith('cli_'):
        adapters['editor'] = {**code.options, 'type': code.type}
        roles['code'] = 'editor'
        if args.editor_exe:
            adapters['editor']['exe'] = str(args.editor_exe.resolve(strict=True))
    elif args.editor_exe:
        parser.error('--editor-exe 需要 CLI 编码角色')
    output = args.output.resolve()
    if output.is_relative_to(ROOT.parent):
        parser.error('证据须位于仓库外')
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for name in (tuple(dict.fromkeys(args.case)) if args.case else tuple(CASES)):
        try:
            row = run_case(output, name, adapters, roles, stage_timeout_s=args.stage_timeout,
                           max_tokens=args.max_tokens, unlimited_tokens=args.unlimited_tokens,
                           max_model_calls=args.max_model_calls, max_unknown_calls=args.max_unknown_calls)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            row = dict(project=name, completed=False, error_type=type(error).__name__)
        rows.append(row)
        atomic_write_json(output / 'results.json', dict(scope='two_synthetic_projects_live_model',
                                                       billing_cost=None, roles=roles, results=rows))
        print(json.dumps(row, ensure_ascii=True), flush=True)
    return 0 if all(row['completed'] for row in rows) else 1


if __name__ == '__main__':
    raise SystemExit(main())
