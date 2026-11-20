"""固定快照的两角色会诊；关联分析运行独立记账，不改历史任务。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..adapters.factory import build_all
from ..config import AdapterConfig, Config, RunBudget
from ..contracts import StageSpec
from ..engine import Engine
from ..state import RunState, StageLock, sha256_file
from .projection import directory, read_json


def capture(run):
    root = directory(run)
    state = read_json(root / 'state.json')
    from ..config import load_config
    cfg = load_config(run['config'])
    record = {'run_id': run['run_id'], 'revision': state.get('revision', 0),
              'config_hash': cfg.source_hash, 'stages': {}, 'evidence': [], 'sources': []}
    remaining = 1200
    for name, stage in state.get('stages', {}).items():
        record['stages'][name] = {'attempt': stage.get('current_attempt'), 'commit': stage.get('commit_sha'),
                                 'inputs_hash': stage.get('inputs_hash'), 'outputs_hash': stage.get('outputs_hash')}
        if stage.get('current_attempt') is None:
            continue
        attempt = root / 'stages' / name / 'attempts' / str(stage['current_attempt'])
        manifest = read_json(attempt / 'manifest.json')
        files = manifest.get('files', {})
        if not isinstance(files, dict):
            continue
        for filename in files:
            if filename in ('final_text.txt', 'test_record.json', 'review_verdict.json') and remaining > 0:
                path = attempt / filename
                if path.is_file() and sha256_file(path) == files[filename]:
                    with path.open(encoding='utf8', errors='replace') as stream:
                        body = stream.read(min(1000, remaining))
                    remaining -= len(body)
                    record['evidence'].append({'stage': name, 'file': filename, 'hash': files[filename], 'body': body})
    commit = next((s['commit'] for s in reversed(list(record['stages'].values())) if s['commit']), None)
    if commit:
        from .. import artifacts
        git = artifacts.GitContext(cfg.git_exe)
        names = sorted({name for s in cfg.stages for name in s.context_files})[:4]
        for name in names:
            if remaining <= 0:
                break
            result = artifacts._git(['show', f'{commit}:{name}'], cfg.task_workdir, check=False, git=git)
            if result.returncode == 0:
                body = result.stdout[:min(1200, remaining)]
                remaining -= len(body)
                record['sources'].append({'path': name, 'commit': commit, 'body': body})
    return record


def reserve_parent(run, topic, config):
    job = read_json(directory(run) / 'job.json')
    if job.get('status') not in ('paused', 'waiting_approval'):
        return topic['budget']
    state = RunState(Path(run['state_dir']), run['run_id'])
    with StageLock(state.run_dir / '.run.lock'):
        state.load()
        state.usage_total.setdefault('reported_tokens', state.usage_total['tokens'] - state.usage_total.get('reserved_tokens', 0))
        if job.get('started_at', 0) + job.get('max_duration_s', 0) <= time.time():
            raise ValueError('父运行有效期已过，不能继续消耗其预算')
        holds = state.control.setdefault('discussion_holds', {})
        if topic['id'] in holds:
            return holds[topic['id']]['budget']
        budget = (topic['budget'] if config.run_budget.unlimited_tokens else
                  min(topic['budget'], config.run_budget.max_tokens - state.usage_total['tokens']))
        if budget < 1000:
            raise ValueError('父运行剩余预算不足以发起会诊，未发送请求')
        holds[topic['id']] = {'budget': budget, 'settled': False}
        state.usage_total['tokens'] += budget
        state.usage_total['reserved_tokens'] = state.usage_total.get('reserved_tokens', 0) + budget
        state.log_event('discussion_reserved', '', thread_id=topic['id'], tokens=budget)
        state.save()
        return budget


def settle_parent(run, topic, usage):
    state = RunState(Path(run['state_dir']), run['run_id'])
    with StageLock(state.run_dir / '.run.lock'):
        state.load()
        hold = state.control.get('discussion_holds', {}).get(topic['id'])
        if not hold or hold['settled']:
            return
        state.usage_total['tokens'] += usage['tokens'] - hold['budget']
        state.usage_total['reserved_tokens'] += usage.get('reserved_tokens', 0) - hold['budget']
        state.usage_total['reported_tokens'] = state.usage_total.get('reported_tokens', 0) + usage.get('reported_tokens', 0)
        state.usage_total['unknown_calls'] = state.usage_total.get('unknown_calls', 0) + usage.get('unknown_calls', 0)
        state.usage_total['duration_s'] += usage.get('duration_s', 0)
        hold.update(settled=True, usage=usage)
        state.log_event('discussion_settled', '', thread_id=topic['id'], tokens=usage['tokens'])
        state.save()


def execute(store, topic_id):
    topic = store.thread(topic_id)
    run = store.run(topic['run_id'])
    from ..config import load_config
    original = load_config(run['config'])
    if original.source_hash != topic['snapshot'].get('config_hash'):
        raise ValueError('会诊绑定的配置已变化，拒绝使用另一份配置')
    apis = [a for a in original.adapters.values() if a.type == 'openai_compat']
    if not apis:
        raise ValueError('会诊目前需要已配置的 API 适配器，不能把实验 CLI 当作只读讨论后端')
    # 独立分析空间：对运行中/历史任务提问不会改变其不可变 metrics。
    base = Path(run['state_dir']) / 'discussions' / topic_id
    base.mkdir(parents=True, exist_ok=True)
    lock = StageLock(base / '.discussion.lock')
    if not lock.acquire():
        return
    try:
        if store.thread(topic_id)['status'] not in ('queued', 'running'):
            return
        store.update_thread(topic_id, 'running', analysis_run=topic_id)
        budget = reserve_parent(run, topic, original)
        adapters = {}
        stages = []
        for index, role in enumerate(topic['roles']):
            kind = 'review' if role == 'reviewer' else 'code'
            selected = next((original.adapters[s.adapter] for s in original.stages
                             if s.kind == kind and s.adapter in original.adapters
                             and original.adapters[s.adapter].type == 'openai_compat'), apis[0])
            adapters[role] = AdapterConfig(role, 'openai_compat',
                                           {**selected.options, 'max_tokens': 600, 'timeout_s': 40, 'temperature': .1})
            name = f'turn_{index + 1}_{role}'
            prompt = base / (name + '.md')
            snapshot = json.dumps(topic['snapshot'], ensure_ascii=False)
            question = json.dumps(topic['question'], ensure_ascii=False)
            for marker in ('{task}', '{upstream}', '{feedback}'):
                snapshot = snapshot.replace(marker, '\\u007b' + marker[1:])
                question = question.replace(marker, '\\u007b' + marker[1:])
            prompt.write_text('你是只读议题会诊中的' + ('开发者' if role == 'developer' else '审查者') + '。\n'
                              '不要写文件或执行命令，不要声称已测试；只引用提供的固定证据。\n'
                              '回答不超过 300 中文字，包含建议、证据和不确定点。不能用讨论结论替代流水线审查。\n'
                              '问题：' + question + '\n'
                              '固定运行快照：\n' + snapshot + '\n'
                              '此前角色回复：\n{upstream}\n', encoding='utf8')
            stages.append(StageSpec(name, role, str(prompt), kind='report', max_attempts=1, timeout_s=40))
        from .config_copy import dump_config
        maximum_duration = 90
        parent_job = read_json(directory(run) / 'job.json')
        if parent_job.get('status') in ('paused', 'waiting_approval'):
            maximum_duration = max(1, min(90, parent_job['started_at'] + parent_job['max_duration_s'] - time.time()))
        config_path = base / 'analysis.toml'
        config_path.write_text(dump_config({
            'task_workdir': str(base / 'readonly'), 'state_dir': str(base / 'state'),
            'run_budget': {'max_tokens': budget, 'max_duration_s': maximum_duration},
            'autonomy': {'enabled': True, 'output_tokens': 600},
            'stages': [{'name': s.name, 'adapter': s.adapter, 'prompt': s.prompt, 'kind': 'report',
                        'timeout_s': min(40, maximum_duration), 'max_attempts': 1} for s in stages],
            'adapters': {k: {'type': v.type, **v.options} for k, v in adapters.items()}}), encoding='utf8')
        from ..supervisor import supervise
        supervise(config_path, topic_id, topic['question'], 0)
        config = load_config(config_path)
        engine = Engine(config, build_all(config.adapters, {s.adapter: 'report' for s in stages}), topic_id, topic['question'])
        with StageLock(engine.state.run_dir / '.run.lock'):
            engine.prepare()
        result = {s.name: engine.state.stage(s.name).status.value for s in stages}
        settle_parent(run, topic, engine.state.usage_total)
        for index, role in enumerate(topic['roles']):
            stage = stages[index]
            attempt = engine.state.current_attempt_dir(stage.name)
            if attempt and (attempt / 'final_text.txt').is_file():
                body = (attempt / 'final_text.txt').read_text(encoding='utf8')
                response = read_json(attempt / 'stdout.txt')
                store.message(run['id'], body[:31000], topic_id + ':' + role,
                              source='agent_response', role=role, refs=[{'snapshot_revision': topic['snapshot']['revision'],
                                  'call_id': response.get('id'), 'returned_model': response.get('model')}],
                              thread=topic_id)
        completed = bool(result) and len(result) == len(stages) and all(v == 'done' for v in result.values())
        store.update_thread(topic_id, 'completed' if completed else 'failed', {
            'analysis_run': topic_id, 'results': result, 'usage': engine.state.usage_total,
            'kind': 'proposal_only', 'task_status_unchanged': True,
            'note': '角色建议仅供参考；采用时必须创建后续任务并通过真实测试与审查。'})
    finally:
        lock.release()
