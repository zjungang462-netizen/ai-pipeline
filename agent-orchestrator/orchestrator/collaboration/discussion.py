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


def _collect_human_interjections(
    store,
    run_id: str,
    since_ts: float,
    *,
    max_items: int = 5,
    max_chars: int = 1200,
) -> str:
    """读取圆桌会诊启动后，人工主持人在该任务房间新发的留言（仅 source == 'human'）。"""
    try:
        events = store.events(run_id, 0, 500)
    except Exception:
        return ''
    notes: list[str] = []
    for ev in events:
        if ev.get('kind') != 'message':
            continue
        if float(ev.get('created', 0)) <= since_ts:
            continue
        payload = ev.get('payload') or {}
        if payload.get('source') != 'human':
            continue
        body = str(payload.get('body') or '').strip()
        if body:
            notes.append(body)
    if not notes:
        return ''
    recent = notes[-max_items:]
    joined = '\n'.join(f'- {item[:400]}' for item in recent)
    return joined[:max_chars]


def _wrap_roundtable_adapter(
    adapter,
    store,
    run_id: str,
    topic_id: str,
    role: str,
    *,
    stream: bool,
    since_ts: float,
):
    from .discussion_stream import DiscussionEventSink, run_streaming_turn

    class _RoundtableProxy:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, item):
            return getattr(self._inner, item)

        def run(self, prompt, workdir, ctx):
            # 第 2 轮及之后（含最终共识）自动注入会诊期间人工主持人的新留言引导
            stage_name = getattr(ctx, 'stage', '') or ''
            if stage_name.startswith(('r2_', 'r3_')) or stage_name == 'turn_consensus':
                interjections = _collect_human_interjections(store, run_id, since_ts)
                if interjections:
                    prompt = (
                        prompt
                        + '\n### 人工主持人最新插话引导（本轮请优先按此方向收敛，不可忽略）\n'
                        + interjections
                        + '\n'
                    )
            if stream:
                sink = DiscussionEventSink(store, run_id, topic_id, role, turn_id=stage_name)
                if hasattr(self._inner, 'stream_tokens'):
                    return run_streaming_turn(self._inner, prompt, workdir, ctx, sink)
                # 只读 CLI 席位无逐 token HTTP 流：推送起止事件以便前端展示实时思考状态
                sink.start()
                result = self._inner.run(prompt, workdir, ctx)
                if result.ok and result.text:
                    sink.on_token(result.text[:1600])
                sink.finish(status='done' if result.ok else 'failed')
                return result
            return self._inner.run(prompt, workdir, ctx)

    return _RoundtableProxy(adapter)


def _build_seat_adapter(
    role: str,
    adapter_type: str,
    custom_model: str,
    original: Config,
    apis: list[AdapterConfig],
    agys: list[AdapterConfig],
) -> AdapterConfig:
    if adapter_type == 'cli_agy':
        agy_base = agys[0] if agys else None
        opts: dict = {}
        if agy_base:
            for k in ('executable', 'proxy_url', 'model', 'allow_patch_probe'):
                if k in agy_base.options:
                    opts[k] = agy_base.options[k]
        # 严格强制只读 plan + sandbox，绝不允许圆桌讨论改写代码
        opts['mode'] = 'plan'
        opts['sandbox'] = True
        opts.setdefault('allow_patch_probe', True)
        opts['timeout_s'] = 90
        if custom_model:
            opts['model'] = custom_model
        return AdapterConfig(role, 'cli_agy', opts)
    if not apis:
        raise ValueError(f'席位 {role} 选择了 openai_compat，但项目配置中没有可用的 openai_compat API 适配器')
    kind = 'review' if role in ('reviewer', 'skeptic') else 'code'
    selected = next(
        (
            original.adapters[s.adapter]
            for s in original.stages
            if s.kind == kind
            and s.adapter in original.adapters
            and original.adapters[s.adapter].type == 'openai_compat'
        ),
        apis[0],
    )
    opts = {**selected.options, 'max_tokens': 1000, 'timeout_s': 45, 'temperature': .15}
    if custom_model:
        opts['model'] = custom_model
    return AdapterConfig(role, 'openai_compat', opts)


def execute(store, topic_id, *, stream_tokens: bool = False):
    topic = store.thread(topic_id)
    run = store.run(topic['run_id'])
    from ..config import load_config
    original = load_config(run['config'])
    if original.source_hash != topic['snapshot'].get('config_hash'):
        raise ValueError('会诊绑定的配置已变化，拒绝使用另一份配置')
    apis = [a for a in original.adapters.values() if a.type == 'openai_compat']
    agys = [a for a in original.adapters.values() if a.type == 'cli_agy']
    rt = topic['snapshot'].get('roundtable') if isinstance(topic['snapshot'].get('roundtable'), dict) else {}
    role_adapters = rt.get('role_adapters') if isinstance(rt.get('role_adapters'), dict) else {}
    if not apis and not agys and 'cli_agy' not in role_adapters.values():
        raise ValueError('会诊需要已配置的 openai_compat API 或 cli_agy 只读适配器')
    # 独立分析空间：对运行中/历史任务提问不会改变其不可变 metrics。
    base = Path(run['state_dir']) / 'discussions' / topic_id
    base.mkdir(parents=True, exist_ok=True)
    lock = StageLock(base / '.discussion.lock')
    if not lock.acquire():
        return
    try:
        if store.thread(topic_id)['status'] not in ('queued', 'running'):
            return
        topic_started_at = float(topic.get('created') or time.time())
        store.update_thread(topic_id, 'running', analysis_run=topic_id)
        budget = reserve_parent(run, topic, original)
        rounds = rt.get('rounds', 1)
        if type(rounds) is not int or not 1 <= rounds <= 3:
            rounds = 1
        role_models = rt.get('role_models') if isinstance(rt.get('role_models'), dict) else {}
        want_consensus = bool(rt.get('consensus', False))
        default_backend = 'openai_compat' if apis else 'cli_agy'

        # 构造每个席位的只读适配器（支持 openai_compat 与 cli_agy 只读 plan+sandbox 混编）
        adapters: dict[str, AdapterConfig] = {}
        for role in list(topic['roles']) + (['consensus'] if want_consensus else []):
            chosen_backend = str(role_adapters.get(role) or default_backend).strip()
            if chosen_backend not in ('openai_compat', 'cli_agy'):
                chosen_backend = default_backend
            custom_model = role_models.get(role)
            custom_model_str = custom_model.strip() if isinstance(custom_model, str) else ''
            adapters[role] = _build_seat_adapter(role, chosen_backend, custom_model_str, original, apis, agys)

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
                turn_timeout = 90 if adapters[role].type == 'cli_agy' else 45
                spec = StageSpec(name, role, str(prompt), kind='report', max_attempts=1, timeout_s=turn_timeout)
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
            c_timeout = 90 if adapters['consensus'].type == 'cli_agy' else 45
            cspec = StageSpec(cname, 'consensus', str(cprompt), kind='report', max_attempts=1, timeout_s=c_timeout)
            stages.append(cspec)
            turn_meta.append((cspec, 'consensus', rounds + 1))

        from .config_copy import dump_config
        maximum_duration = min(360, max(90, sum(s.timeout_s for s in stages)))
        parent_job = read_json(directory(run) / 'job.json')
        if parent_job.get('status') in ('paused', 'waiting_approval'):
            maximum_duration = max(1, min(maximum_duration, parent_job['started_at'] + parent_job['max_duration_s'] - time.time()))
        config_path = base / 'analysis.toml'
        config_path.write_text(dump_config({
            'task_workdir': str(base / 'readonly'), 'state_dir': str(base / 'state'),
            'run_budget': {'max_tokens': budget, 'max_duration_s': maximum_duration},
            'autonomy': {'enabled': True, 'output_tokens': 1000},
            'stages': [{'name': s.name, 'adapter': s.adapter, 'prompt': s.prompt, 'kind': 'report',
                        'timeout_s': min(s.timeout_s, maximum_duration), 'max_attempts': 1} for s in stages],
            'adapters': {k: {'type': v.type, **v.options} for k, v in adapters.items()}}), encoding='utf8')
        use_inline_engine = stream_tokens or rounds > 1 or want_consensus or any(v.type == 'cli_agy' for v in adapters.values())
        if use_inline_engine:
            config = load_config(config_path)
            built = build_all(config.adapters, {s.adapter: 'report' for s in stages})
            wrapped = {
                role: _wrap_roundtable_adapter(
                    ad, store, run['id'], topic_id, role, stream=stream_tokens, since_ts=topic_started_at
                )
                for role, ad in built.items()
            }
            engine = Engine(config, wrapped, topic_id, topic['question'])
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
                        'returned_model': response.get('model') or adapters[role].options.get('model'),
                        'adapter_type': adapters[role].type,
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
