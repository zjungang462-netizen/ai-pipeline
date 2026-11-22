"""双模型并行 Best-of-N 竞技场：隔离 git worktree 并行编码、确定性测试对比与人工拍板合并。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Any
import uuid

from .adapters.factory import build_adapter
from .collaboration.project_manager import TEST_PRESETS
from .config import AdapterConfig, Config
from .contracts import RunContext
from .safety import build_subprocess_env
from .state import atomic_write_json

_ALLOWED_ARENA_ADAPTERS = frozenset((
    'cli_agy', 'openai_compat', 'cli_codex', 'cli_claude', 'cli_qwen', 'cli_qoder',
))
_ARENA_ID_RE = re.compile(r'^[A-Za-z0-9_-]{6,64}$')
_LABEL_RE = re.compile(r'^[A-Za-z0-9_-]{1,16}$')
_SHA_RE = re.compile(r'^[0-9a-f]{7,40}$')


def _git(args: list[str], cwd: Path, git_exe: str | None = 'git', *, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [git_exe or 'git', *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=60,
    )
    if check and proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or f'git {args[0]} failed').strip()
        raise RuntimeError(msg[:400])
    return proc


def _build_candidate_adapter_cfg(
    cfg: Config,
    label: str,
    adapter_type: str,
    model: str,
) -> AdapterConfig:
    base = next((a for a in cfg.adapters.values() if a.type == adapter_type), None)
    opts: dict[str, Any] = dict(base.options) if base else {}
    if adapter_type == 'openai_compat':
        if not base:
            raise ValueError(f'候选 {label} 选择了 openai_compat，但项目配置中未配置兼容 API 适配器')
        opts.setdefault('timeout_s', 120)
    elif adapter_type == 'cli_agy':
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
    git_lock: threading.Lock,
    adapter_factory=None,
) -> dict[str, Any]:
    label = str(cand['label'])
    adapter_type = str(cand['adapter_type'])
    model = str(cand.get('model') or '').strip()
    wt_dir = arena_dir / f'wt_{label}'
    attempt_dir = arena_dir / f'attempt_{label}'
    attempt_dir.mkdir(parents=True, exist_ok=True)
    ref_name = f'refs/arena/{arena_id}/{label}'
    t0 = time.monotonic()

    if wt_dir.exists():
        shutil.rmtree(wt_dir, ignore_errors=True)

    with git_lock:
        _git(['worktree', 'add', '--detach', str(wt_dir), base_sha], cfg.task_workdir, cfg.git_exe)
    try:
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
        ctx = RunContext(task=task, run_id=arena_id, stage=f'arena_{label}',
                         workdir=wt_dir, runs_dir=arena_dir, kind='code', attempt_dir=attempt_dir)
        result = adapter.run(prompt, wt_dir, ctx)

        # 检查变更并封存到隔离 ref，确保后续可审计对比与安全 ff-only 合并
        status_out = _git(['status', '--porcelain'], wt_dir, cfg.git_exe).stdout.strip()
        commit_sha = ''
        changed_files: list[str] = []
        diff_text = ''
        diff_truncated = False
        if status_out:
            _git(['add', '-A'], wt_dir, cfg.git_exe)
            _git([
                '-c', 'user.name=Pipeline Arena',
                '-c', 'user.email=arena@local',
                'commit', '-m', f'arena({arena_id}): candidate {label}',
            ], wt_dir, cfg.git_exe)
            commit_sha = _git(['rev-parse', 'HEAD'], wt_dir, cfg.git_exe).stdout.strip()
            with git_lock:
                _git(['update-ref', ref_name, commit_sha], cfg.task_workdir, cfg.git_exe)
            names_out = _git(['diff', '--name-only', base_sha, commit_sha], wt_dir, cfg.git_exe).stdout
            changed_files = [line.strip() for line in names_out.splitlines() if line.strip()]
            raw_diff = _git(['diff', base_sha, commit_sha], wt_dir, cfg.git_exe).stdout
            if len(raw_diff) > 40000:
                diff_text = raw_diff[:40000]
                diff_truncated = True
            else:
                diff_text = raw_diff

        test_exit_code: int | None = None
        test_output = ''
        if test_preset:
            cmd = list(TEST_PRESETS[test_preset])
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(wt_dir),
                    env=build_subprocess_env(),
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=120,
                )
                test_exit_code = proc.returncode
                combined = (proc.stdout or '') + ('\n' + proc.stderr if proc.stderr else '')
                test_output = combined.strip()[:4000]
            except (OSError, subprocess.TimeoutExpired) as exc:
                test_exit_code = -1
                test_output = f'测试执行异常：{exc}'[:400]

        usage = result.usage if isinstance(result.usage, dict) else {}
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
            'test_output': test_output,
            'duration_s': round(time.monotonic() - t0, 2),
            'tokens': int(usage.get('tokens') or 0),
            'usage_unknown': bool(usage.get('usage_unknown')),
            'summary': (result.text or '')[:2000],
            'error': (result.error or '')[:500] if not result.ok else '',
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
            'duration_s': round(time.monotonic() - t0, 2),
            'tokens': 0,
            'usage_unknown': True,
            'summary': '',
            'error': str(exc)[:500],
        }
    finally:
        with git_lock:
            _git(['worktree', 'remove', '--force', str(wt_dir)], cfg.task_workdir, cfg.git_exe, check=False)
        if wt_dir.exists():
            shutil.rmtree(wt_dir, ignore_errors=True)


def run_arena(
    cfg: Config,
    task: str,
    candidates: list[dict[str, Any]],
    *,
    test_preset: str = '',
    arena_id: str = '',
    adapter_factory=None,
) -> dict[str, Any]:
    """在独立 git worktree 中并行运行 2..3 个候选模型并产出可并排对比的 Diff 与测试结果。"""
    if not isinstance(task, str) or not 1 <= len(task.strip()) <= 16000:
        raise ValueError('竞技场任务描述须为 1..16000 字符')
    if not isinstance(candidates, list) or not 2 <= len(candidates) <= 3:
        raise ValueError('双模型竞技场需提供 2 到 3 个候选配置')
    if test_preset and test_preset not in TEST_PRESETS:
        raise ValueError(f'测试预设不受支持：{test_preset}（仅允许 {", ".join(TEST_PRESETS)}）')

    seen_labels: set[str] = set()
    clean_cands: list[dict[str, Any]] = []
    for idx, raw in enumerate(candidates):
        if not isinstance(raw, dict):
            raise ValueError('每个候选配置必须是对象')
        label = str(raw.get('label') or chr(ord('A') + idx)).strip()
        if not _LABEL_RE.fullmatch(label) or label in seen_labels:
            raise ValueError(f'候选标签不合法或重复：{label!r}')
        seen_labels.add(label)
        atype = str(raw.get('adapter_type') or '').strip()
        if adapter_factory is None and atype not in _ALLOWED_ARENA_ADAPTERS:
            raise ValueError(f'候选 {label} 的适配器类型不受支持：{atype}')
        model = str(raw.get('model') or '').strip()
        if len(model) > 160:
            raise ValueError(f'候选 {label} 的模型名过长')
        clean_cands.append({'label': label, 'adapter_type': atype, 'model': model})

    try:
        base_sha = _git(['rev-parse', 'HEAD'], cfg.task_workdir, cfg.git_exe).stdout.strip()
    except RuntimeError as exc:
        raise ValueError(f'项目目录不是有效 Git 仓库或尚无提交，无法启动 worktree 竞技场：{exc}') from exc

    aid = arena_id.strip() if arena_id else f'arena-{uuid.uuid4().hex[:10]}'
    if not _ARENA_ID_RE.fullmatch(aid):
        raise ValueError('arena_id 格式不合法')
    arena_dir = cfg.state_dir / 'arenas' / aid
    arena_dir.mkdir(parents=True, exist_ok=True)

    git_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=len(clean_cands)) as pool:
        futures = [
            pool.submit(
                _run_single_candidate,
                cfg, aid, arena_dir, base_sha, task.strip(), cand, test_preset, git_lock, adapter_factory,
            )
            for cand in clean_cands
        ]
        results = [f.result() for f in futures]

    record = {
        'arena_id': aid,
        'base_sha': base_sha,
        'task': task.strip(),
        'test_preset': test_preset,
        'created': time.time(),
        'candidates': results,
    }
    atomic_write_json(arena_dir / 'result.json', record)
    return record


def apply_arena_winner(
    cfg: Config,
    arena_id: str,
    label: str,
    expected_base_sha: str,
) -> dict[str, Any]:
    """经人工明确选择后，将获胜候选的隔离提交以 fast-forward 方式安全合入项目工作区。"""
    if not _ARENA_ID_RE.fullmatch(str(arena_id or '')):
        raise ValueError('arena_id 格式不合法')
    if not _LABEL_RE.fullmatch(str(label or '')):
        raise ValueError('label 格式不合法')
    if not _SHA_RE.fullmatch(str(expected_base_sha or '')):
        raise ValueError('expected_base_sha 格式不合法')

    result_path = cfg.state_dir / 'arenas' / arena_id / 'result.json'
    if not result_path.is_file():
        raise KeyError(f'未找到竞技场记录：{arena_id}')
    record = json.loads(result_path.read_text(encoding='utf-8'))
    if record.get('base_sha') != expected_base_sha:
        raise ValueError('expected_base_sha 与竞技场基线提交不一致')

    cand = next((c for c in record.get('candidates') or [] if c.get('label') == label), None)
    if cand is None:
        raise KeyError(f'竞技场 {arena_id} 中不存在候选 {label}')
    commit_sha = str(cand.get('commit_sha') or '')
    if not _SHA_RE.fullmatch(commit_sha):
        raise ValueError(f'候选 {label} 没有产生可合并的代码提交')

    current_head = _git(['rev-parse', 'HEAD'], cfg.task_workdir, cfg.git_exe).stdout.strip()
    if current_head != expected_base_sha:
        raise RuntimeError(f'工作区 HEAD 已变化（当前 {current_head[:12]}，期望 {expected_base_sha[:12]}），拒绝自动合并')
    dirty = _git(['status', '--porcelain'], cfg.task_workdir, cfg.git_exe).stdout.strip()
    if dirty:
        raise RuntimeError('工作区存在未提交改动，请先清理或提交后再采纳竞技场方案')

    anc = _git(['merge-base', '--is-ancestor', expected_base_sha, commit_sha],
               cfg.task_workdir, cfg.git_exe, check=False)
    if anc.returncode != 0:
        raise RuntimeError('获胜提交不是基线提交的直接后代，拒绝合并')

    _git(['merge', '--ff-only', commit_sha], cfg.task_workdir, cfg.git_exe)
    new_head = _git(['rev-parse', 'HEAD'], cfg.task_workdir, cfg.git_exe).stdout.strip()
    return {
        'applied': True,
        'arena_id': arena_id,
        'label': label,
        'commit_sha': commit_sha,
        'head_sha': new_head,
    }
