"""双模型并行 Best-of-N 竞技场：隔离 git worktree 并行编码、确定性测试对比与人工拍板合并。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import subprocess
import threading
import time
from typing import Any
import uuid

from .adapters.factory import build_adapter
from .artifacts import GitContext, _no_hooks_dir, resolve_git, unsafe_rel_paths
from .collaboration.project_manager import TEST_PRESETS
from .config import AdapterConfig, Config
from .contracts import RunContext
from .safety import build_subprocess_env
from .state import StageLock, atomic_write_json
from .testrun import run_test_cmd

_ALLOWED_ARENA_ADAPTERS = frozenset((
    'cli_agy', 'cli_codex', 'cli_claude', 'cli_qwen', 'cli_qoder',
))
_ARENA_ID_RE = re.compile(r'^[A-Za-z0-9_-]{6,64}$')
_LABEL_RE = re.compile(r'^[A-Za-z0-9_-]{1,16}$')
_SHA_RE = re.compile(r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$')
_DIGEST_RE = re.compile(r'^[0-9a-f]{64}$')
_MODEL_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}(?:\[(?:1M|200K)\])?$')


def _git(args: list[str], cwd: Path, git_exe: str | None = 'git', *,
         check: bool = True, binary: bool = False) -> subprocess.CompletedProcess:
    exe = resolve_git(GitContext(git_exe if git_exe and git_exe != 'git' else None)) or (git_exe or 'git')
    proc = subprocess.run(
        [exe, '--no-replace-objects', '-c', f'core.hooksPath={_no_hooks_dir()}', *args],
        cwd=str(cwd),
        env=build_subprocess_env(),
        capture_output=True,
        text=not binary,
        encoding=None if binary else 'utf-8',
        errors=None if binary else 'replace',
        timeout=60,
    )
    if check and proc.returncode != 0:
        output = proc.stderr or proc.stdout
        if isinstance(output, bytes):
            output = output.decode('utf-8', errors='replace')
        raise RuntimeError((output or f'git {args[0]} failed').strip()[:400])
    return proc


def _file_scope(raw: list[str]) -> list[str]:
    if not isinstance(raw, list) or len(raw) > 1024:
        raise ValueError('allowed_files 须为最多 1024 个相对文件路径的列表')
    paths = set()
    for item in raw:
        if not isinstance(item, str) or not item or len(item) > 4096 or '\0' in item:
            raise ValueError('allowed_files 包含无效的文件路径')
        name = item.replace('\\', '/')
        path = PurePosixPath(name)
        if path.is_absolute() or PureWindowsPath(name).drive or not path.parts or unsafe_rel_paths([name]):
            raise ValueError(f'文件不在有效的项目相对路径内：{item}')
        paths.add(path.as_posix())
    return sorted(paths)


def _changed_files(base_sha: str, candidate_sha: str, workdir: Path, git_exe: str | None) -> list[str]:
    output = _git(['diff', '--no-renames', '--name-only', '-z', base_sha, candidate_sha, '--'],
                  workdir, git_exe, binary=True).stdout
    return sorted(name.decode('utf-8') for name in output.split(b'\0') if name)


def _evidence_digest(evidence: dict[str, Any]) -> str:
    encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _remove_worktree(cfg: Config, arena_dir: Path, worktree: Path, git_lock: threading.Lock) -> None:
    # 清理前复核真实目录，不跟随被外部替换的链接或目录联接。
    if arena_dir.resolve() != arena_dir or worktree.resolve() != arena_dir / worktree.name:
        return
    with git_lock:
        _git(['worktree', 'remove', '--force', str(worktree)], cfg.task_workdir, cfg.git_exe, check=False)
    if (arena_dir.resolve() == arena_dir and worktree.is_dir()
            and worktree.resolve() == arena_dir / worktree.name):
        shutil.rmtree(worktree, ignore_errors=True)


def _build_candidate_adapter_cfg(
    cfg: Config,
    label: str,
    adapter_type: str,
    model: str,
) -> AdapterConfig:
    base = next((a for a in cfg.adapters.values() if a.type == adapter_type), None)
    opts: dict[str, Any] = dict(base.options) if base else {}
    if adapter_type == 'cli_agy':
        opts['mode'] = 'accept-edits'
        opts['sandbox'] = True
        opts.setdefault('allow_patch_probe', True)
        opts.setdefault('timeout_s', 240)
    elif adapter_type == 'cli_codex':
        opts['isolate_config'] = True
        opts.setdefault('timeout_s', 240)
    else:
        opts.setdefault('timeout_s', 240)
    if model:
        opts['model'] = model
    return AdapterConfig(f'arena_{label}', adapter_type, opts)


def _run_single_candidate(
    cfg: Config,
    arena_id: str,
    arena_dir: Path,
    base_sha: str,
    task: str,
    cand: dict[str, Any],
    test_preset: str,
    allowed_files: list[str],
    git_lock: threading.Lock,
    adapter_factory=None,
    cancel_requested=None,
    on_progress=None,
) -> dict[str, Any]:
    label = str(cand['label'])
    adapter_type = str(cand['adapter_type'])
    model = str(cand.get('model') or '').strip()
    wt_dir = arena_dir / f'wt_{label}'
    test_dir = arena_dir / f'test_{label}'
    attempt_dir = arena_dir / f'attempt_{label}'
    attempt_dir.mkdir(parents=True, exist_ok=True)
    ref_name = f'refs/arena/{arena_id}/{label}'
    t0 = time.monotonic()

    worktrees = []
    try:
        if cancel_requested and cancel_requested():
            raise RuntimeError('候选已取消')
        if on_progress:
            on_progress(label, 'running')
        with git_lock:
            _git(['worktree', 'add', '--detach', str(wt_dir), base_sha], cfg.task_workdir, cfg.git_exe)
            worktrees.append(wt_dir)
        if adapter_factory is not None:
            adapter = adapter_factory(label, adapter_type, model)
        else:
            ad_cfg = _build_candidate_adapter_cfg(cfg, label, adapter_type, model)
            adapter = build_adapter(ad_cfg, kind='code')

        prompt = (
            '你是双模型并行竞技场中的实现工程师。请直接用文件读写/编辑工具在当前隔离工作区完成以下任务，'
            '仅改动任务必要的文件，不要调用 shell/终端命令；完成后简述修改清单。\n\n'
            f'任务：{task}\n'
        )
        if allowed_files:
            prompt += '\n仅允许改动以下文件：' + json.dumps(allowed_files, ensure_ascii=False) + '\n'
        ctx = RunContext(task=task, run_id=arena_id, stage=f'arena_{label}',
                         workdir=wt_dir, runs_dir=arena_dir, kind='code', attempt_dir=attempt_dir,
                         editable_files=tuple(allowed_files), cancel_requested=cancel_requested)
        result = adapter.run(prompt, wt_dir, ctx)
        if cancel_requested and cancel_requested():
            raise RuntimeError('候选已取消')

        # 检查变更并封存到隔离 ref，确保后续可审计对比与安全 ff-only 合并
        commit_sha = ''
        changed_files: list[str] = []
        diff_text = ''
        diff_truncated = False
        _git(['add', '-A'], wt_dir, cfg.git_exe)
        if _git(['rev-parse', 'HEAD'], wt_dir, cfg.git_exe).stdout.strip() != base_sha:
            raise RuntimeError('候选更改了工作树的基线提交')
        staged = _git(['diff', '--cached', '--quiet', '--'], wt_dir, cfg.git_exe, check=False)
        if staged.returncode not in (0, 1):
            raise RuntimeError('无法读取候选暂存区')
        if staged.returncode == 1:
            _git([
                '-c', 'user.name=Pipeline Arena',
                '-c', 'user.email=arena@local',
                'commit', '--no-gpg-sign', '--no-verify', '-m', f'arena({arena_id}): candidate {label}',
            ], wt_dir, cfg.git_exe)
            commit_sha = _git(['rev-parse', 'HEAD'], wt_dir, cfg.git_exe).stdout.strip()
            with git_lock:
                _git(['update-ref', ref_name, commit_sha], cfg.task_workdir, cfg.git_exe)
            changed_files = _changed_files(base_sha, commit_sha, wt_dir, cfg.git_exe)
            raw_diff = _git(['diff', '--no-ext-diff', '--no-textconv', base_sha, commit_sha, '--'],
                            wt_dir, cfg.git_exe).stdout
            diff_text = raw_diff[:40000]
            diff_truncated = len(raw_diff) > 40000

        test_exit_code: int | None = None
        test_output = ''
        evidence_digest = ''
        if test_preset and result.ok and commit_sha:
            if on_progress:
                on_progress(label, 'testing')
            if cancel_requested and cancel_requested():
                raise RuntimeError('候选已取消')
            # 从封存提交创建干净工作树，测试不会读到候选留下的未提交或忽略文件。
            argv = list(TEST_PRESETS[test_preset])
            with git_lock:
                _git(['worktree', 'add', '--detach', str(test_dir), commit_sha], cfg.task_workdir, cfg.git_exe)
                worktrees.append(test_dir)
            options = {'cancel_requested': cancel_requested} if cancel_requested else {}
            trec = run_test_cmd(argv, test_dir, timeout_s=120, **options)
            if cancel_requested and cancel_requested():
                raise RuntimeError('候选已取消')
            verified_head = _git(['rev-parse', 'HEAD'], test_dir, cfg.git_exe).stdout.strip()
            tracked_changes = _git(['status', '--porcelain', '--untracked-files=no'],
                                   test_dir, cfg.git_exe).stdout
            test_exit_code = trec['exit_code']
            test_output = str(trec.get('output') or '')
            if trec.get('error'):
                test_output = f"{trec['error']}\n{test_output}"
            if verified_head != commit_sha or tracked_changes:
                test_exit_code = -1
                test_output = f'测试改变了验收工作树的提交或已跟踪文件\n{test_output}'
            if (type(test_exit_code) is int and test_exit_code == 0 and trec.get('ok') is True
                    and trec.get('cmd') == argv and not trec.get('error')):
                evidence = {
                    'arena_id': arena_id, 'label': label, 'base_sha': base_sha,
                    'candidate_sha': commit_sha, 'allowed_files': allowed_files,
                    'changed_files': changed_files, 'test_preset': test_preset, 'argv': argv,
                    'test_result': trec,
                }
                atomic_write_json(attempt_dir / 'test_evidence.json', evidence)
                evidence_digest = _evidence_digest(evidence)

        if not result.ok:
            apply_blocked = '候选生成失败'
        elif not changed_files:
            apply_blocked = '候选没有可采纳的改动'
        elif not allowed_files:
            apply_blocked = '本轮未声明允许改动的文件'
        elif not set(changed_files).issubset(allowed_files):
            apply_blocked = '候选改动超出允许的文件范围'
        elif not test_preset:
            apply_blocked = '本轮未选择验收测试'
        elif not evidence_digest:
            apply_blocked = '候选未通过验收测试'
        else:
            apply_blocked = ''

        usage = result.usage if isinstance(result.usage, dict) else {}
        err_msg = ''
        if not result.ok:
            err_kind = getattr(getattr(result, 'error_kind', None), 'value', '') or ''
            err_msg = (result.stderr or err_kind or '执行失败')[:500]
        return {
            'label': label,
            'adapter_type': adapter_type,
            'model': model or getattr(adapter, 'model', '') or '',
            'ok': bool(result.ok),
            'commit_sha': commit_sha,
            'changed_files': changed_files,
            'diff': diff_text,
            'diff_truncated': diff_truncated,
            'test_exit_code': test_exit_code,
            'test_output': test_output[:4000],
            'test_evidence_sha256': evidence_digest,
            'apply_eligible': not apply_blocked,
            'apply_blocked': apply_blocked,
            'duration_s': round(time.monotonic() - t0, 2),
            'tokens': int(usage.get('tokens') or 0),
            'usage_unknown': bool(usage.get('usage_unknown')),
            'summary': (result.text or '')[:2000],
            'error': err_msg,
        }
    except Exception as exc:
        return {
            'label': label,
            'adapter_type': adapter_type,
            'model': model,
            'ok': False,
            'commit_sha': '',
            'changed_files': [],
            'diff': '',
            'diff_truncated': False,
            'test_exit_code': None,
            'test_output': '',
            'test_evidence_sha256': '',
            'apply_eligible': False,
            'apply_blocked': '未能完成候选验收',
            'duration_s': round(time.monotonic() - t0, 2),
            'tokens': 0,
            'usage_unknown': True,
            'summary': '',
            'error': str(exc)[:500],
        }
    finally:
        for worktree in reversed(worktrees):
            _remove_worktree(cfg, arena_dir, worktree, git_lock)


def validate_arena_request(cfg: Config, task: str, candidates: list[dict[str, Any]], *,
                           test_preset: str = '', allowed_files: list[str] | None = None,
                           adapter_factory=None) -> dict[str, Any]:
    if not isinstance(task, str) or not 1 <= len(task.strip()) <= 16000:
        raise ValueError('竞技场任务描述须为 1..16000 字符')
    if not isinstance(candidates, list) or not 2 <= len(candidates) <= 3:
        raise ValueError('双模型竞技场需提供 2 到 3 个候选配置')
    if not isinstance(test_preset, str) or (test_preset and test_preset not in TEST_PRESETS):
        raise ValueError(f'测试预设不受支持：{test_preset}（仅允许 {", ".join(TEST_PRESETS)}）')

    configured_scope = _file_scope(cfg.autonomy.allowed_files)
    scope = _file_scope(allowed_files) if allowed_files is not None else configured_scope
    if configured_scope and not set(scope).issubset(configured_scope):
        raise ValueError('本轮允许改动的文件超出项目已经配置的范围')

    seen_labels: set[str] = set()
    clean_cands: list[dict[str, Any]] = []
    for idx, raw in enumerate(candidates):
        if not isinstance(raw, dict):
            raise ValueError('每个候选配置必须是对象')
        label = str(raw.get('label') or chr(ord('A') + idx)).strip()
        if not _LABEL_RE.fullmatch(label) or label.casefold() in seen_labels:
            raise ValueError(f'候选标签不合法或重复：{label!r}')
        seen_labels.add(label.casefold())
        atype = str(raw.get('adapter_type') or '').strip()
        if adapter_factory is None and atype not in _ALLOWED_ARENA_ADAPTERS:
            raise ValueError(f'候选 {label} 的适配器类型不受支持：{atype}')
        model = str(raw.get('model') or '').strip()
        if model and (len(model) > 160 or not _MODEL_ID_RE.fullmatch(model)):
            raise ValueError(f'候选 {label} 的模型标识格式非法')
        clean_cands.append({'label': label, 'adapter_type': atype, 'model': model})

    return {'task': task.strip(), 'candidates': clean_cands, 'test_preset': test_preset,
            'allowed_files': scope}


def arena_baseline(cfg: Config) -> str:
    try:
        return _git(['rev-parse', 'HEAD'], cfg.task_workdir, cfg.git_exe).stdout.strip()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f'项目目录不是有效 Git 仓库或尚无提交：{exc}') from exc


def run_arena(
    cfg: Config,
    task: str,
    candidates: list[dict[str, Any]],
    *,
    test_preset: str = '',
    allowed_files: list[str] | None = None,
    arena_id: str = '',
    adapter_factory=None,
    expected_base_sha: str | None = None,
    cancel_requested=None,
    on_progress=None,
) -> dict[str, Any]:
    """并行运行候选并封存结果，进度回调由工作台 worker 持久化。"""
    request = validate_arena_request(cfg, task, candidates, test_preset=test_preset,
                                     allowed_files=allowed_files, adapter_factory=adapter_factory)
    clean_cands = request['candidates']
    scope = request['allowed_files']
    base_sha = arena_baseline(cfg)
    if expected_base_sha is not None and base_sha != expected_base_sha:
        raise RuntimeError('工作区 HEAD 在竞技场启动前已变化')

    aid = arena_id.strip() if arena_id else f'arena-{uuid.uuid4().hex[:10]}'
    if not _ARENA_ID_RE.fullmatch(aid):
        raise ValueError('arena_id 格式不合法')
    arena_dir = (cfg.state_dir / 'arenas').resolve() / aid
    try:
        arena_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ValueError(f'竞技场记录已存在：{aid}') from exc

    git_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=len(clean_cands)) as pool:
        futures = {
            pool.submit(
                _run_single_candidate, cfg, aid, arena_dir, base_sha, request['task'], cand,
                test_preset, scope, git_lock, adapter_factory, cancel_requested, on_progress,
            ): cand['label']
            for cand in clean_cands
        }
        by_label = {}
        for future in as_completed(futures):
            label = futures[future]
            candidate = by_label[label] = future.result()
            if on_progress:
                status = 'done' if candidate['ok'] else 'failed'
                if cancel_requested and cancel_requested() and candidate.get('error') == '候选已取消':
                    status = 'cancelled'
                on_progress(label, status, candidate=candidate)
    results = [by_label[cand['label']] for cand in clean_cands]

    record = {
        'schema_version': 1,
        'arena_id': aid,
        'workdir': cfg.task_workdir.resolve().as_posix(),
        'allowed_files': scope,
        'base_sha': base_sha,
        'task': task.strip(),
        'test_preset': test_preset,
        'created': time.time(),
        'candidates': results,
    }
    atomic_write_json(arena_dir / 'result.json', record)
    return record


def _verified_candidate(
    cfg: Config, record: dict[str, Any], result_path: Path, arena_id: str, label: str,
    base_sha: str, expected_candidate_sha: str, expected_evidence_sha256: str,
) -> str:
    if record.get('schema_version') != 1 or record.get('workdir') != cfg.task_workdir.resolve().as_posix():
        raise RuntimeError('竞技场记录缺少当前工作区的验收证据，请重新运行竞技场')
    if record.get('arena_id') != arena_id or record.get('base_sha') != base_sha:
        raise ValueError('竞技场记录或基线提交发生变化')
    candidate = next((row for row in record['candidates'] if row.get('label') == label), None)
    if candidate is None:
        raise KeyError(f'竞技场 {arena_id} 中不存在候选 {label}')
    if candidate.get('apply_eligible') is not True or candidate.get('ok') is not True:
        raise RuntimeError(candidate.get('apply_blocked') or '候选缺少通过的验收证据')
    commit_sha = candidate.get('commit_sha')
    if commit_sha != expected_candidate_sha or candidate.get('test_evidence_sha256') != expected_evidence_sha256:
        raise RuntimeError('候选提交或验收证据与人工确认时不一致')

    scope = _file_scope(record['allowed_files'])
    current_scope = _file_scope(cfg.autonomy.allowed_files)
    test_preset = record.get('test_preset')
    if (not scope or (current_scope and not set(scope).issubset(current_scope))
            or not isinstance(test_preset, str) or test_preset not in TEST_PRESETS):
        raise RuntimeError('候选缺少当前配置认可的文件范围或验收预设')
    ref_sha = _git(['rev-parse', '--verify', f'refs/arena/{arena_id}/{label}'],
                   cfg.task_workdir, cfg.git_exe).stdout.strip()
    commit_and_parents = _git(['rev-list', '--parents', '-n', '1', commit_sha],
                              cfg.task_workdir, cfg.git_exe).stdout.split()
    if ref_sha != commit_sha or commit_and_parents != [commit_sha, base_sha]:
        raise RuntimeError('封存提交与受验提交或基线不一致')
    changed = _changed_files(base_sha, commit_sha, cfg.task_workdir, cfg.git_exe)
    if not changed or not set(changed).issubset(scope) or changed != candidate.get('changed_files'):
        raise RuntimeError('封存提交的变更与已确认的文件范围不一致')

    try:
        evidence = json.loads((result_path.parent / f'attempt_{label}' / 'test_evidence.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise RuntimeError('验收证据缺失或损坏') from error
    expected = {
        'arena_id': arena_id, 'label': label, 'base_sha': base_sha, 'candidate_sha': commit_sha,
        'allowed_files': scope, 'changed_files': changed, 'test_preset': test_preset,
        'argv': list(TEST_PRESETS[test_preset]),
    }
    if (not isinstance(evidence, dict) or _evidence_digest(evidence) != expected_evidence_sha256
            or any(evidence.get(key) != value for key, value in expected.items())):
        raise RuntimeError('验收证据与候选提交、文件范围或测试预设不一致')
    test = evidence.get('test_result')
    if (not isinstance(test, dict) or test.get('ok') is not True or type(test.get('exit_code')) is not int
            or test['exit_code'] != 0 or test.get('cmd') != expected['argv'] or test.get('error')):
        raise RuntimeError('候选未通过验收测试')
    return commit_sha


def apply_arena_winner(
    cfg: Config,
    arena_id: str,
    label: str,
    expected_base_sha: str,
    expected_candidate_sha: str,
    expected_evidence_sha256: str,
) -> dict[str, Any]:
    """人工确认提交及验收证据后快进合入，重试时核对合入意向。"""
    if not _ARENA_ID_RE.fullmatch(arena_id):
        raise ValueError('arena_id 格式不合法')
    if not _LABEL_RE.fullmatch(label):
        raise ValueError('label 格式不合法')
    if not _SHA_RE.fullmatch(expected_base_sha) or not _SHA_RE.fullmatch(expected_candidate_sha):
        raise ValueError('基线或候选提交 SHA 格式不合法')
    if not _DIGEST_RE.fullmatch(expected_evidence_sha256):
        raise ValueError('验收证据 SHA-256 格式不合法')

    result_path = cfg.state_dir / 'arenas' / arena_id / 'result.json'
    if not result_path.is_file():
        raise KeyError(f'未找到竞技场记录：{arena_id}')
    lock = StageLock(cfg.state_dir / 'arenas' / '.apply.lock')
    if not lock.acquire():
        raise RuntimeError('已有另一个竞技场合并操作正在执行，请稍后重试')
    try:
        record = json.loads(result_path.read_text(encoding='utf-8'))
        if record.get('applied_label'):
            raise RuntimeError(f'竞技场 {arena_id} 已采纳过方案 {record["applied_label"]}，禁止重复合并')
        commit_sha = _verified_candidate(cfg, record, result_path, arena_id, label, expected_base_sha,
                                         expected_candidate_sha, expected_evidence_sha256)
        branch = _git(['symbolic-ref', '-q', 'HEAD'], cfg.task_workdir, cfg.git_exe, check=False).stdout.strip()
        current_head = _git(['rev-parse', 'HEAD'], cfg.task_workdir, cfg.git_exe).stdout.strip()
        intent = {'label': label, 'base_sha': expected_base_sha, 'candidate_sha': commit_sha,
                  'test_evidence_sha256': expected_evidence_sha256, 'branch': branch}
        pending = record.get('apply_intent')
        if pending is not None and pending != intent:
            raise RuntimeError('还有未完成的采纳记录，请回到原分支重试原候选')
        recovered = pending == intent and current_head == commit_sha
        if not recovered:
            if current_head != expected_base_sha:
                raise RuntimeError(f'工作区 HEAD 已变化（当前 {current_head[:12]}，期望 {expected_base_sha[:12]}），拒绝自动合并')
            if _git(['status', '--porcelain'], cfg.task_workdir, cfg.git_exe).stdout:
                raise RuntimeError('工作区存在未提交改动，请先清理或提交后再采纳竞技场方案')
            # 先写意向。若合并后最终回执落盘失败，只在同一分支和候选提交上补写回执。
            if pending is None:
                record['apply_intent'] = intent
                atomic_write_json(result_path, record)
            _git(['merge', '--ff-only', commit_sha], cfg.task_workdir, cfg.git_exe)
            new_head = _git(['rev-parse', 'HEAD'], cfg.task_workdir, cfg.git_exe).stdout.strip()
            if new_head != commit_sha:
                raise RuntimeError('合并后的 HEAD 与受验提交不一致，请核对采纳记录')
        record.pop('apply_intent')
        record['applied_label'] = label
        record['applied_commit_sha'] = commit_sha
        record['applied_test_evidence_sha256'] = expected_evidence_sha256
        record['applied_at'] = time.time()
        record['applied_recovered'] = recovered
        atomic_write_json(result_path, record)
        return {'applied': True, 'arena_id': arena_id, 'label': label, 'commit_sha': commit_sha,
                'head_sha': commit_sha, 'recovered': recovered}
    finally:
        lock.release()
