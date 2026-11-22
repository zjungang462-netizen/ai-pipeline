"""固定快照的两角色会诊；关联分析运行独立记账，不改历史任务。"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..adapters.factory import build_all
from ..config import AdapterConfig, Config, RunBudget
from ..contracts import StageSpec
from ..engine import Engine
from ..safety import WriteRootViolation, relative_target
from ..state import RunState, StageLock, sha256_file
from .projection import directory, read_json

ROLE_LABELS = {
    'architect': '架构师',
    'developer': '开发者',
    'skeptic': '红队质疑者',
    'reviewer': '审查者',
    'consensus': '圆桌共识总结',
}
_ROLE_GUIDANCE = {
    'architect': '你是架构师：聚焦系统边界、模块职责、长期可维护性与最小改动方案。',
    'developer': '你是开发者：聚焦具体实现路径、关键函数逻辑、边界条件与落地可行性。',
    'skeptic': '你是红队质疑者：专门挑刺、反驳前面角色的盲区、幻觉假设与潜在回归风险，绝不附和客套。',
    'reviewer': '你是独立审查者：核查证据充分性、安全护栏、测试覆盖与结论是否经得起验证。',
}
_MENTION_FILE_RE = re.compile(r'@([A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,12})')


def _safe_read_workspace_files(workdir: Path, rel_paths: list[str], *, max_files: int = 4, max_chars: int = 3000) -> list[dict]:
    root = Path(workdir).resolve()
    out: list[dict] = []
    seen: set[str] = set()
    for raw in rel_paths:
        rel = str(raw or '').strip().replace('\\', '/').lstrip('./')
        if not rel or rel in seen:
            continue
        seen.add(rel)
        try:
            fp = relative_target(root, rel)
        except WriteRootViolation:
            continue
        if not fp.is_file() or fp.is_symlink():
            continue
        try:
            text = fp.read_text(encoding='utf-8', errors='replace')[:max_chars]
        except OSError:
            continue
        out.append({'path': rel, 'commit': 'workspace', 'body': text})
        if len(out) >= max_files:
            break
    return out


def capture(run, *, extra_files: list[str] | None = None, question: str = ''):
    root = directory(run)
    state = read_json(root / 'state.json')
    from ..config import load_config
    cfg = load_config(run['config'])
    record = {'run_id': run['run_id'], 'revision': state.get('revision', 0),
              'config_hash': cfg.source_hash, 'stages': {}, 'evidence': [], 'sources': []}
    remaining = 4000
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
                        body = stream.read(min(1800, remaining))
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
    # 支持显式附加文件以及问题中 @path/to/file.ext 精准引用工作区只读源码
    mentioned = _MENTION_FILE_RE.findall(question or '')
    requested_files = list(extra_files or []) + mentioned
    if requested_files:
        existing_paths = {s.get('path') for s in record['sources']}
        for src in _safe_read_workspace_files(cfg.task_workdir, requested_files):
            if src['path'] not in existing_paths:
                record['sources'].append(src)
                existing_paths.add(src['path'])
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


def _wrap_streaming_adapter(adapter, store, run_id: str, topic_id: str, role: str):
    from .discussion_stream import DiscussionEventSink, run_streaming_turn

    class _StreamingProxy:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, item):
            return getattr(self._inner, item)

        def run(self, prompt, workdir, ctx):
            sink = DiscussionEventSink(store, run_id, topic_id, role, turn_id=ctx.stage)
            return run_streaming_turn(self._inner, prompt, workdir, ctx, sink)

    return _StreamingProxy(adapter)


def execute(store, topic_id, *, stream_tokens: bool = False):
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
        rt = topic['snapshot'].get('roundtable') if isinstance(topic['snapshot'].get('roundtable'), dict) else {}
        rounds = rt.get('rounds', 1)
        if type(rounds) is not int or not 1 <= rounds <= 3:
            rounds = 1
        role_models = rt.get('role_models') if isinstance(rt.get('role_models'), dict) else {}
        want_consensus = bool(rt.get('consensus', False))

        # 构造每个席位的只读 API 适配器（支持不同席位指定不同模型）
        adapters = {}
        for role in list(topic['roles']) + (['consensus'] if want_consensus else []):
            kind = 'review' if role in ('reviewer', 'skeptic') else 'code'
            selected = next((original.adapters[s.adapter] for s in original.stages
                             if s.kind == kind and s.adapter in original.adapters
                             and original.adapters[s.adapter].type == 'openai_compat'), apis[0])
            opts = {**selected.options, 'max_tokens': 1000, 'timeout_s': 45, 'temperature': .15}
            custom_model = role_models.get(role)
            if isinstance(custom_model, str) and custom_model.strip():
                opts['model'] = custom_model.strip()
            adapters[role] = AdapterConfig(role, 'openai_compat', opts)

        snapshot = json.dumps(topic['snapshot'], ensure_ascii=False)
        question = json.dumps(topic['question'], ensure_ascii=False)
        for marker in ('{task}', '{upstream}', '{feedback}'):
            snapshot = snapshot.replace(marker, '\\u007b' + marker[1:])
            question = question.replace(marker, '\\u007b' + marker[1:])

        stages = []
        turn_meta: list[tuple[StageSpec, str, int]] = []
        for r_idx in range(1, rounds + 1):
            for role in topic['roles']:
                name = f'turn_{len(stages) + 1}_{role}' if rounds == 1 else f'r{r_idx}_{role}'
                prompt = base / (name + '.md')
                role_title = ROLE_LABELS.get(role, role)
                role_guide = _ROLE_GUIDANCE.get(role, f'你是只读议题会诊中的{role_title}。')
                round_guide = (
                    f'当前为第 {r_idx}/{rounds} 轮交叉辩论：请直接回应前面角色的观点，明确指出赞同之处、质疑漏洞或修订后的方案，禁止空泛客套。'
                    if r_idx > 1 else
                    (f'当前为第 1/{rounds} 轮首轮陈述：请基于固定证据给出清晰立场、具体依据与不确定点。' if rounds > 1 else '')
                )
                prompt.write_text(
                    f'{role_guide}\n'
                    '不要写文件或执行命令，不要声称已测试；只引用提供的固定证据。\n'
                    '回答不超过 350 中文字，包含核心结论、依据与不确定点。不能用讨论结论替代流水线审查。\n'
                    + (round_guide + '\n' if round_guide else '')
                    + '问题：' + question + '\n'
                    + '固定运行快照：\n' + snapshot + '\n'
                    + '此前角色发言记录：\n{upstream}\n',
                    encoding='utf8',
                )
                spec = StageSpec(name, role, str(prompt), kind='report', max_attempts=1, timeout_s=45)
                stages.append(spec)
                turn_meta.append((spec, role, r_idx))

        if want_consensus:
            cname = 'turn_consensus'
            cprompt = base / (cname + '.md')
            cprompt.write_text(
                '你是多模型圆桌会诊主持人。请综合上面全部轮次的各方发言，输出结构化共识总结（不超过 400 字），严格分为三节：\n'
                '### 已达成一致\n- ...\n### 残留分歧（需人工拍板）\n- ...\n### 建议执行步骤\n- ...\n'
                '不要编造未在讨论中出现的证据。\n'
                '原始问题：' + question + '\n'
                '完整讨论记录：\n{upstream}\n',
                encoding='utf8',
            )
            cspec = StageSpec(cname, 'consensus', str(cprompt), kind='report', max_attempts=1, timeout_s=45)
            stages.append(cspec)
            turn_meta.append((cspec, 'consensus', rounds + 1))

        from .config_copy import dump_config
        maximum_duration = min(240, max(90, len(stages) * 45))
        parent_job = read_json(directory(run) / 'job.json')
        if parent_job.get('status') in ('paused', 'waiting_approval'):
            maximum_duration = max(1, min(maximum_duration, parent_job['started_at'] + parent_job['max_duration_s'] - time.time()))
        config_path = base / 'analysis.toml'
        config_path.write_text(dump_config({
            'task_workdir': str(base / 'readonly'), 'state_dir': str(base / 'state'),
            'run_budget': {'max_tokens': budget, 'max_duration_s': maximum_duration},
            'autonomy': {'enabled': True, 'output_tokens': 1000},
            'stages': [{'name': s.name, 'adapter': s.adapter, 'prompt': s.prompt, 'kind': 'report',
                        'timeout_s': min(45, maximum_duration), 'max_attempts': 1} for s in stages],
            'adapters': {k: {'type': v.type, **v.options} for k, v in adapters.items()}}), encoding='utf8')
        if stream_tokens:
            config = load_config(config_path)
            built = build_all(config.adapters, {s.adapter: 'report' for s in stages})
            streamed = {role: _wrap_streaming_adapter(ad, store, run['id'], topic_id, role)
                        for role, ad in built.items()}
            engine = Engine(config, streamed, topic_id, topic['question'])
            engine.run()
        else:
            from ..supervisor import supervise
            supervise(config_path, topic_id, topic['question'], 0)
            config = load_config(config_path)
            engine = Engine(config, build_all(config.adapters, {s.adapter: 'report' for s in stages}), topic_id, topic['question'])
            with StageLock(engine.state.run_dir / '.run.lock'):
                engine.prepare()
        result = {s.name: engine.state.stage(s.name).status.value for s in stages}
        settle_parent(run, topic, engine.state.usage_total)
        consensus_text = ''
        for spec, role, r_idx in turn_meta:
            attempt = engine.state.current_attempt_dir(spec.name)
            if attempt and (attempt / 'final_text.txt').is_file():
                body = (attempt / 'final_text.txt').read_text(encoding='utf8')
                if role == 'consensus':
                    consensus_text = body[:4000]
                response = read_json(attempt / 'stdout.txt')
                msg_key = f'{topic_id}:{role}' if rounds == 1 and role != 'consensus' else f'{topic_id}:r{r_idx}:{role}'
                store.message(
                    run['id'], body[:31000], msg_key,
                    source='agent_response', role=role,
                    refs=[{
                        'snapshot_revision': topic['snapshot']['revision'],
                        'call_id': response.get('id'),
                        'returned_model': response.get('model'),
                        'turn': spec.name,
                        **({'round': r_idx, 'total_rounds': rounds} if rounds > 1 and role != 'consensus' else {}),
                        **({'consensus': True} if role == 'consensus' else {}),
                    }],
                    thread=topic_id,
                )
        completed = bool(result) and len(result) == len(stages) and all(v == 'done' for v in result.values())
        store.update_thread(topic_id, 'completed' if completed else 'failed', {
            'analysis_run': topic_id, 'results': result, 'usage': engine.state.usage_total,
            'rounds': rounds,
            **({'consensus': consensus_text} if consensus_text else {}),
            'kind': 'proposal_only', 'task_status_unchanged': True,
            'note': '角色建议仅供参考；可一键填入审批附注或基于共识创建后续任务，经真实测试与审查后生效。'})
    finally:
        lock.release()
