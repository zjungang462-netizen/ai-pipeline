"""固定快照的两角色会诊；关联分析运行独立记账，不改历史任务。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..adapters.factory import build_all
from ..config import AdapterConfig, Config, RunBudget
from ..contracts import StageSpec
from ..engine import Engine
from ..state import StageLock, sha256_file
from .projection import directory, read_json


def capture(run):
    root = directory(run)
    state = read_json(root / 'state.json')
    record = {'run_id': run['run_id'], 'revision': state.get('revision', 0), 'stages': {}, 'evidence': []}
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
            if filename in ('final_text.txt', 'test_record.json', 'review_verdict.json'):
                path = attempt / filename
                if path.is_file() and sha256_file(path) == files[filename]:
                    with path.open(encoding='utf8', errors='replace') as stream:
                        body = stream.read(4500)
                    record['evidence'].append({'stage': name, 'file': filename, 'hash': files[filename], 'body': body})
    return record


def execute(store, topic_id):
    topic = store.thread(topic_id)
    run = store.run(topic['run_id'])
    from ..config import load_config
    original = load_config(run['config'])
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
        first = apis[0]
        options = {**first.options, 'max_tokens': 600, 'timeout_s': 40, 'temperature': 0.1}
        adapters = {'discussion': AdapterConfig('discussion', 'openai_compat', options)}
        stages = []
        for index, role in enumerate(topic['roles']):
            name = f'turn_{index + 1}_{role}'
            prompt = base / (name + '.md')
            prompt.write_text('你是只读议题会诊中的' + ('开发者' if role == 'developer' else '审查者') + '。\n'
                              '不要写文件或执行命令，不要声称已测试；只引用提供的固定证据。\n'
                              '回答不超过 300 中文字，包含建议、证据和不确定点。不能用讨论结论替代流水线审查。\n'
                              '问题：' + topic['question'] + '\n'
                              '固定运行快照：\n' + json.dumps(topic['snapshot'], ensure_ascii=False) + '\n'
                              '此前角色回复：\n{upstream}\n', encoding='utf8')
            stages.append(StageSpec(name, 'discussion', str(prompt), kind='report', max_attempts=1, timeout_s=40))
        config = Config(base / 'readonly', base / 'state', stages, adapters,
                        run_budget=RunBudget(topic['budget'], 90), base_dir=base)
        engine = Engine(config, build_all(adapters, {'discussion': 'report'}), topic_id, topic['question'])
        # 分析独立额度必须在适配器调用之前计算输出上限；复用预留/未知用量机制。
        config.autonomy.enabled = True
        config.autonomy.output_tokens = 600
        result = engine.run()
        for index, role in enumerate(topic['roles']):
            stage = stages[index]
            attempt = engine.state.current_attempt_dir(stage.name)
            if attempt and (attempt / 'final_text.txt').is_file():
                body = (attempt / 'final_text.txt').read_text(encoding='utf8')
                store.message(run['id'], body[:31000], topic_id + ':' + role,
                              source='agent_response', role=role, refs=[{'snapshot_revision': topic['snapshot']['revision']}],
                              thread=topic_id)
        completed = bool(result) and len(result) == len(stages) and all(v == 'done' for v in result.values())
        store.update_thread(topic_id, 'completed' if completed else 'failed', {
            'analysis_run': topic_id, 'results': result, 'usage': engine.state.usage_total,
            'kind': 'proposal_only', 'task_status_unchanged': True,
            'note': '角色建议仅供参考；采用时必须创建后续任务并通过真实测试与审查。'})
    finally:
        lock.release()
