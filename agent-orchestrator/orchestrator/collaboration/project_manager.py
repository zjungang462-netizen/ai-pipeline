"""工作台项目可视化登记与项目级自优化查询/审批辅助。"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

from ..config import load_config
from ..lessons import APPROVED_LESSONS_FILE, approve_lessons, propose_lessons
from ..model_router import recommend_adapters
from .execution_scope import execution_scope
from .model_settings import describe_models

_PROJECT_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$')
_PROMPTS_ROOT = Path(__file__).resolve().parents[2] / 'prompts'
_ALLOWED_STAGE_ADAPTER_TYPES = frozenset((
    'cli_agy', 'openai_compat', 'cli_codex', 'cli_claude', 'cli_qwen', 'cli_qoder', 'cli_grok', 'cli_doubao',
))
_PRESET_DEFAULT_STAGES = {
    'review_only': (
        ('code_review', 'cli_agy'),
    ),
    'review_plan_fix': (
        ('code_review', 'cli_agy'),
        ('repair_plan', 'cli_agy'),
        ('coding', 'cli_agy'),
        ('final_review', 'openai_compat'),
    ),
    'standard_dev': (
        ('design', 'openai_compat'),
        ('coding', 'cli_agy'),
        ('review', 'openai_compat'),
    ),
}


def _toml_str(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _find_project_config(service, project_id: str):
    entry = next((p for p in service.store.projects() if p['id'] == project_id), None)
    if entry is None:
        raise ValueError(f'未找到已登记项目：{project_id}')
    return entry, service.config(entry['config'])


def project_lessons(service, project_id: str, *, min_occurrences: int = 2) -> dict[str, Any]:
    entry, cfg = _find_project_config(service, project_id)
    proposal = propose_lessons(cfg.state_dir, min_occurrences=min_occurrences)
    approved_path = cfg.state_dir / APPROVED_LESSONS_FILE
    approved = None
    if approved_path.is_file() and not approved_path.is_symlink():
        try:
            approved = json.loads(approved_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            approved = None
    return {
        'project_id': entry['id'],
        'project_name': entry['name'],
        'proposal': proposal,
        'approved': approved,
    }


def approve_project_lessons(service, project_id: str, proposal: dict[str, Any], *, max_items: int = 12) -> dict[str, Any]:
    entry, cfg = _find_project_config(service, project_id)
    target = approve_lessons(cfg.state_dir, proposal, max_items=max_items)
    saved = json.loads(target.read_text(encoding='utf-8'))
    return {
        'project_id': entry['id'],
        'approved_file': str(target),
        'items': len(saved.get('items') or []),
        'proposal_hash': saved.get('proposal_hash'),
        'approved': saved,
    }


def project_route_recommend(service, project_id: str, *, min_runs: int = 2) -> dict[str, Any]:
    entry, cfg = _find_project_config(service, project_id)
    report = recommend_adapters(cfg.state_dir, min_runs=min_runs)
    return {
        'project_id': entry['id'],
        'project_name': entry['name'],
        **report,
    }


def register_managed_project(service, payload: dict[str, Any]) -> dict[str, Any]:
    """根据白名单预设生成并校验本地项目 TOML，热登记到当前工作台。"""
    name = str(payload.get('name') or '').strip()
    if not _PROJECT_NAME_RE.fullmatch(name):
        raise ValueError('项目标识须为 1..48 位字母/数字/点/下划线/短横线，且以字母或数字开头')

    raw_workdir = str(payload.get('workdir') or '').strip()
    if not raw_workdir:
        raise ValueError('必须提供已存在的本地工作目录路径')
    workdir = Path(raw_workdir).expanduser()
    if workdir.is_symlink() or not workdir.is_dir():
        raise ValueError(f'工作目录不存在或不是普通目录：{raw_workdir}')
    workdir = workdir.resolve()

    preset = str(payload.get('preset') or 'review_plan_fix').strip()
    if preset not in ('review_only', 'review_plan_fix', 'standard_dev'):
        raise ValueError('preset 必须为 review_only / review_plan_fix / standard_dev 之一')

    agy_model = str(payload.get('agy_model') or 'gemini-3.8-flash-high').strip()
    api_model = str(payload.get('api_model') or 'deepseek-chat').strip()
    api_base_url = str(payload.get('api_base_url') or 'https://api.deepseek.com/v1').strip()
    api_key_env = str(payload.get('api_key_env') or 'DEEPSEEK_API_KEY').strip()
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', api_key_env):
        raise ValueError('api_key_env 必须是合法环境变量名')
    parsed_api = urlsplit(api_base_url)
    if parsed_api.scheme not in ('http', 'https') or not parsed_api.hostname or parsed_api.username or parsed_api.password:
        raise ValueError('api_base_url 必须是不含凭据的 HTTP(S) 地址')

    proxy_url = str(payload.get('proxy_url') or '').strip()
    if proxy_url:
        parsed_proxy = urlsplit(proxy_url)
        if parsed_proxy.scheme not in ('http', 'https') or not parsed_proxy.hostname or parsed_proxy.username or parsed_proxy.password:
            raise ValueError('proxy_url 必须是不含凭据的 HTTP(S) 代理地址')

    allow_patch_probe = bool(payload.get('allow_patch_probe', True))
    max_duration_s = int(payload.get('max_duration_s', 900))
    max_model_calls = int(payload.get('max_model_calls', 15))
    if not 60 <= max_duration_s <= 7200:
        raise ValueError('max_duration_s 须在 60..7200 秒之间')
    if not 1 <= max_model_calls <= 100:
        raise ValueError('max_model_calls 须在 1..100 之间')

    raw_stage_adapters = payload.get('stage_adapters')
    if raw_stage_adapters is not None and not isinstance(raw_stage_adapters, dict):
        raise ValueError('stage_adapters 必须是按阶段名索引的对象')
    stage_adapters = raw_stage_adapters or {}

    managed_dir = Path(service.store.path).resolve().parent / 'managed_projects'
    managed_dir.mkdir(parents=True, exist_ok=True)
    state_dir = managed_dir / f'state-{name}'
    state_dir.mkdir(parents=True, exist_ok=True)
    config_path = managed_dir / f'{name}.toml'

    evidence_review = (_PROMPTS_ROOT / 'evidence_review.md').as_posix()
    verified_plan = (_PROMPTS_ROOT / 'verified_repair_plan.md').as_posix()

    lines = [
        f'# 由工作台生成的本地项目配置（preset = {preset}，按阶段独立适配器）',
        f'state_dir = {_toml_str(state_dir.as_posix())}',
        f'task_workdir = {_toml_str(workdir.as_posix())}',
        '',
        '[run_budget]',
        'max_tokens = 350000',
        f'max_duration_s = {max_duration_s}',
        f'max_model_calls = {max_model_calls}',
        'max_unknown_calls = 2',
        '',
    ]

    stage_adapter_names: dict[str, str] = {}
    for stage_name, default_type in _PRESET_DEFAULT_STAGES[preset]:
        override = stage_adapters.get(stage_name) if isinstance(stage_adapters.get(stage_name), dict) else {}
        atype = str(override.get('type') or default_type).strip()
        if atype not in _ALLOWED_STAGE_ADAPTER_TYPES:
            raise ValueError(f'阶段 {stage_name} 的适配器类型不受支持：{atype}')
        default_model = api_model if atype == 'openai_compat' else (agy_model if atype == 'cli_agy' else '')
        model = str(override.get('model') if override.get('model') is not None else default_model).strip()
        adapter_name = f'{stage_name}_agent'
        stage_adapter_names[stage_name] = adapter_name
        lines.append(f'[adapters.{adapter_name}]')
        lines.append(f'type = {_toml_str(atype)}')
        if atype == 'openai_compat':
            lines.append(f'base_url = {_toml_str(api_base_url)}')
            lines.append(f'api_key_env = {_toml_str(api_key_env)}')
            lines.append(f'model = {_toml_str(model or api_model)}')
            lines.append('review_json_mode = true')
        elif atype == 'cli_agy':
            if model:
                lines.append(f'model = {_toml_str(model)}')
            lines.append(f'allow_patch_probe = {"true" if allow_patch_probe else "false"}')
            if proxy_url:
                lines.append(f'proxy_url = {_toml_str(proxy_url)}')
        else:
            if model:
                lines.append(f'model = {_toml_str(model)}')
            if atype in ('cli_grok', 'cli_doubao') and proxy_url:
                lines.append(f'proxy_url = {_toml_str(proxy_url)}')
        lines.append('')

    if preset == 'review_only':
        lines += [
            '[[stages]]',
            'name = "code_review"',
            'kind = "report"',
            f'adapter = {_toml_str(stage_adapter_names["code_review"])}',
            f'prompt = {_toml_str(evidence_review)}',
            'timeout_s = 240',
            'max_attempts = 1',
        ]
    elif preset == 'review_plan_fix':
        lines += [
            '[[stages]]',
            'name = "code_review"',
            'kind = "report"',
            f'adapter = {_toml_str(stage_adapter_names["code_review"])}',
            f'prompt = {_toml_str(evidence_review)}',
            'timeout_s = 240',
            'max_attempts = 1',
            '',
            '[[stages]]',
            'name = "repair_plan"',
            'kind = "report"',
            f'adapter = {_toml_str(stage_adapter_names["repair_plan"])}',
            'approval = true',
            f'prompt = {_toml_str(verified_plan)}',
            'timeout_s = 240',
            'max_attempts = 1',
            '',
            '[[stages]]',
            'name = "coding"',
            'kind = "code"',
            f'adapter = {_toml_str(stage_adapter_names["coding"])}',
            'prompt = "你是实现工程师。请严格按照已批准的修复方案与人工审批附注修改当前工作目录中的相关文件。直接用文件读写/编辑工具修改（不要调用 shell/终端命令），仅改动方案涉及的文件；完成后简述实际修改清单。\\n\\n任务：{task}\\n\\n上游审查与修复方案：{upstream}\\n\\n终审返工反馈：{feedback}"',
            'timeout_s = 240',
            'max_attempts = 1',
            '',
            '[[stages]]',
            'name = "final_review"',
            'kind = "review"',
            f'adapter = {_toml_str(stage_adapter_names["final_review"])}',
            'prompt = "你是独立代码复核员。请对照任务与已批准的修复方案核查本次代码改动（累积 diff），确认采纳项已彻底修复且未引入新缺陷，输出唯一合法的 review_verdict.json。\\n\\n任务：{task}\\n\\n上游产物与代码改动：{upstream}"',
            'feedback_to = "coding"',
            'max_rounds = 2',
            'timeout_s = 120',
            'max_attempts = 1',
        ]
    else:
        lines += [
            '[[stages]]',
            'name = "design"',
            'kind = "report"',
            f'adapter = {_toml_str(stage_adapter_names["design"])}',
            'approval = true',
            'prompt = "你是技术架构师。请针对任务输出简洁、可执行的实现方案与修改清单，供负责人审批后进入编码。\\n\\n任务：{task}\\n\\n上游产物：{upstream}"',
            'timeout_s = 120',
            'max_attempts = 1',
            '',
            '[[stages]]',
            'name = "coding"',
            'kind = "code"',
            f'adapter = {_toml_str(stage_adapter_names["coding"])}',
            'prompt = "你是实现工程师。请严格按照已批准的方案与人工附注修改工作区文件，直接用文件编辑工具修改（不要调用 shell/终端命令）。\\n\\n任务：{task}\\n\\n上游方案：{upstream}\\n\\n审查反馈：{feedback}"',
            'timeout_s = 240',
            'max_attempts = 1',
            '',
            '[[stages]]',
            'name = "review"',
            'kind = "review"',
            f'adapter = {_toml_str(stage_adapter_names["review"])}',
            'prompt = "你是代码审查员。请核对任务需求与代码变更，输出唯一合法的 review_verdict.json。\\n\\n任务：{task}\\n\\n上游产物：{upstream}"',
            'feedback_to = "coding"',
            'max_rounds = 2',
            'timeout_s = 120',
            'max_attempts = 1',
        ]

    tmp_path = config_path.with_suffix('.toml.tmp')
    tmp_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    try:
        load_config(tmp_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    tmp_path.replace(config_path)
    project_id = service.store.register_project(config_path)
    cfg = service.config(config_path)
    return {
        'id': project_id,
        'name': name,
        'config': str(config_path),
        'execution_scope': execution_scope(cfg),
        'config_hash': cfg.source_hash,
        'models': describe_models(cfg),
        'unlimited_tokens': cfg.run_budget.unlimited_tokens,
    }
