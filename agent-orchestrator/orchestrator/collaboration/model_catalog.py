"""本机已安装 CLI 工具与真实可用模型目录探测（只读、不触发推理、不泄漏凭据）。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Any
import urllib.error
import urllib.request

from ..adapters.cli_base import resolve_exe

_CACHE_LOCK = threading.Lock()
_CACHE_TTL_S = 300.0
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

_FALLBACK_MODELS: dict[str, list[dict[str, str]]] = {
    'openai_compat': [
        {'id': 'deepseek-chat', 'label': 'DeepSeek Chat'},
        {'id': 'deepseek-reasoner', 'label': 'DeepSeek Reasoner'},
        {'id': 'gpt-4.1', 'label': 'GPT-4.1'},
        {'id': 'gpt-4o', 'label': 'GPT-4o'},
    ],
    'cli_agy': [
        {'id': 'gemini-3.8-flash-high', 'label': 'Gemini 3.8 Flash (High)'},
        {'id': 'gemini-3.8-flash-medium', 'label': 'Gemini 3.8 Flash (Medium)'},
    ],
    'cli_codex': [
        {'id': 'gpt-5.4', 'label': 'GPT-5.4'},
        {'id': 'gpt-5.3-codex', 'label': 'GPT-5.3 Codex'},
        {'id': 'o3', 'label': 'o3'},
    ],
    'cli_claude': [
        {'id': 'claude-sonnet-4-6', 'label': 'Claude Sonnet 4.6'},
        {'id': 'claude-opus-4-6', 'label': 'Claude Opus 4.6'},
        {'id': 'deepseek-v4-pro[1M]', 'label': 'DeepSeek V4 Pro [1M]'},
    ],
    'cli_qwen': [
        {'id': 'qwen3-coder-plus', 'label': 'Qwen3 Coder Plus'},
        {'id': 'qwen-plus', 'label': 'Qwen Plus'},
    ],
    'cli_qoder': [
        {'id': 'Ultimate', 'label': 'Qoder Ultimate'},
        {'id': 'Performance', 'label': 'Qoder Performance'},
        {'id': 'Efficient', 'label': 'Qoder Efficient'},
    ],
    'cli_grok': [
        {'id': 'grok-code-fast-1', 'label': 'Grok Code Fast 1'},
        {'id': 'grok-4', 'label': 'Grok 4'},
    ],
    'cli_doubao': [
        {'id': 'doubao-seed-code', 'label': 'Doubao Seed Code'},
        {'id': 'doubao-pro-32k', 'label': 'Doubao Pro 32K'},
    ],
}


def _parse_agy_models(stdout: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_line in (stdout or '').splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith('fetching') or line.startswith('Usage:'):
            continue
        if '\t' in line:
            mid, label = line.split('\t', 1)
        else:
            parts = line.split(None, 1)
            mid = parts[0]
            label = parts[1] if len(parts) > 1 else parts[0]
        mid = mid.strip()
        label = label.strip() or mid
        if not mid or mid in seen or ' ' in mid:
            continue
        seen.add(mid)
        items.append({'id': mid, 'label': label})
    return items


def _probe_agy_models(exe: str, proxy_url: str = '') -> list[dict[str, str]]:
    env = dict(os.environ)
    if proxy_url:
        env.update(HTTP_PROXY=proxy_url, HTTPS_PROXY=proxy_url, ALL_PROXY=proxy_url,
                   NO_PROXY='localhost,127.0.0.1,::1')
    try:
        proc = subprocess.run(
            [exe, 'models'],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            env=env, timeout=15,
        )
        if proc.returncode == 0:
            parsed = _parse_agy_models(proc.stdout)
            if parsed:
                return parsed
    except (OSError, subprocess.SubprocessError):
        pass
    return []


def _probe_openai_compat_models(base_url: str, api_key_env: str) -> tuple[bool, list[dict[str, str]]]:
    key = os.environ.get(api_key_env, '').strip()
    if not key:
        return False, []
    url = base_url.rstrip('/') + '/models'
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {key}', 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            payload = json.loads(resp.read().decode('utf-8', errors='replace'))
        rows = payload.get('data') if isinstance(payload, dict) else None
        if isinstance(rows, list):
            items = []
            seen: set[str] = set()
            for row in rows:
                mid = row.get('id') if isinstance(row, dict) else None
                if isinstance(mid, str) and mid.strip() and mid not in seen:
                    seen.add(mid)
                    items.append({'id': mid.strip(), 'label': mid.strip()})
            if items:
                return True, items
    except (OSError, ValueError, urllib.error.URLError):
        pass
    return True, []


def discover_local_catalog(
    *,
    proxy_url: str = '',
    api_base_url: str = 'https://api.deepseek.com/v1',
    api_key_env: str = 'DEEPSEEK_API_KEY',
    force: bool = False,
) -> dict[str, Any]:
    """探测本机已安装的 CLI 与真实可用模型列表，带短期内存缓存。"""
    cache_key = f'{proxy_url}|{api_base_url}|{api_key_env}'
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if not force and cached and (now - cached[0]) < _CACHE_TTL_S:
            return cached[1]

    adapters: dict[str, dict[str, Any]] = {}

    # 1. OpenAI 兼容 API（如 DeepSeek）
    api_configured, api_live = _probe_openai_compat_models(api_base_url, api_key_env)
    adapters['openai_compat'] = {
        'installed': api_configured,
        'source': 'live_api' if api_live else 'preset',
        'models': api_live or list(_FALLBACK_MODELS['openai_compat']),
    }

    # 2. 反重力 CLI（真机 agy models）
    agy_exe = resolve_exe('agy')
    agy_live = _probe_agy_models(agy_exe, proxy_url=proxy_url) if agy_exe else []
    adapters['cli_agy'] = {
        'installed': bool(agy_exe),
        'source': 'live_cli' if agy_live else 'preset',
        'models': agy_live or list(_FALLBACK_MODELS['cli_agy']),
    }

    # 3. 其它本地 CLI 工具（仅探测可执行文件是否存在，不触发网络或推理）
    for adapter_type, exe_name in (
        ('cli_codex', 'codex'),
        ('cli_claude', 'claude'),
        ('cli_qwen', 'qwen'),
        ('cli_qoder', 'qoder'),
    ):
        found = resolve_exe(exe_name) or shutil.which(exe_name)
        adapters[adapter_type] = {
            'installed': bool(found),
            'source': 'preset',
            'models': list(_FALLBACK_MODELS[adapter_type]),
        }

    # 4. 容器隔离 CLI（检查 docker 可用性）
    docker_found = shutil.which('docker')
    for adapter_type in ('cli_grok', 'cli_doubao'):
        adapters[adapter_type] = {
            'installed': bool(docker_found),
            'source': 'preset',
            'models': list(_FALLBACK_MODELS[adapter_type]),
        }

    result = {
        'refreshed_at': round(now, 3),
        'adapters': adapters,
    }
    with _CACHE_LOCK:
        _CACHE[cache_key] = (now, result)
    return result
