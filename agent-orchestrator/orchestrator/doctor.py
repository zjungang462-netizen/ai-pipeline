"""只读本机诊断：复用严格配置校验，不执行模型、登录或配置中的命令。"""
from __future__ import annotations

import copy
import difflib
import math
import os
import re
import shutil
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from .adapters.factory import build_adapter
from .config import ConfigError, config_key_sets, parse_config


def _nearest_directory(path: Path) -> Path:
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def diagnose(path: str | Path) -> dict:
    path = Path(path).absolute()
    checks = []

    def add(level, field, message, hint=""):
        checks.append(dict(level=level, field=field, message=message, hint=hint))

    def result():
        return {"config": str(path), "ok": not any(c['level'] == 'error' for c in checks),
                "checks": checks, "scope": "本机静态检查；不探测登录态或模型连通性"}

    try:
        raw = tomllib.loads(path.read_text(encoding='utf8'))
    except (OSError, ValueError) as error:
        # TOML 错误可能带原始行内容，不回显配置正文。
        add('error', 'config', f'无法读取 UTF-8 TOML（{type(error).__name__}）', '检查文件路径、编码和 TOML 语法')
        return result()
    schema = config_key_sets()
    sanitized = copy.deepcopy(raw)

    def unknown(table, allowed, field):
        if not isinstance(table, dict):
            return
        for key in sorted(set(table)-allowed):
            suggestion = difflib.get_close_matches(key, sorted(allowed), n=1)
            add('error', f'{field}.{key}', '未知配置字段',
                '改为 '+suggestion[0] if suggestion else '删除未知字段或对照同包示例确认位置')
            del table[key]

    unknown(sanitized, schema['root'], 'root')
    for section in ('autonomy', 'autotune', 'run_budget'):
        unknown(sanitized.get(section), schema[section], section)
    stages = sanitized.get('stages', [])
    if isinstance(stages, list):
        for i, stage in enumerate(stages):
            unknown(stage, schema['stages'], f'stages[{i}]')
    for key in ('task_workdir', 'state_dir', 'stages'):
        if not sanitized.get(key):
            add('error', key, '缺少必需配置', '对照示例填写该字段')
    try:
        cfg = parse_config(sanitized, base_dir=path.parent)
    except (ConfigError, ValueError, TypeError, OSError, OverflowError) as error:
        # 校验器会在少数报错中回显非法值；屏蔽引号中的原值，保留字段与原因。
        detail = re.sub(r"(['\"]).*?\1", '[值已省略]', str(error))[:1000]
        add('error', 'validation', detail, '修正后再次运行 doctor；使用与运行命令相同的严格校验')
        return result()
    add('ok', 'validation', '字段、类型、阶段引用、预算与无人值守约束校验通过')

    for key, target in (('task_workdir', cfg.task_workdir), ('state_dir', cfg.state_dir)):
        if target.exists() and not target.is_dir():
            add('error', key, '目标存在但不是目录', str(target))
        else:
            parent = _nearest_directory(target)
            if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
                add('error', key, '目标或最近存在的父目录不可写', '修正目录权限；doctor 不创建目录')
            elif not target.exists():
                add('warning', key, '目录尚不存在，运行时将创建', str(target))
            else:
                add('ok', key, '目录存在且访问权限检查通过', str(target))

    def command_exists(command, cwd):
        if not isinstance(command, str) or not command:
            return False
        if '/' in command or '\\' in command or Path(command).is_absolute():
            target = Path(command) if Path(command).is_absolute() else cwd/command
            return target.is_file() and (os.name == 'nt' or os.access(target, os.X_OK))
        return bool(shutil.which(command))

    used = {s.adapter for s in cfg.stages if s.kind != 'test'}
    for name, adapter_cfg in cfg.adapters.items():
        label = f'adapters.{name}'
        try:
            adapter = build_adapter(adapter_cfg)
            opts = adapter_cfg.options
            if adapter_cfg.type == 'openai_compat':
                endpoint = urlsplit(opts['base_url'])
                if endpoint.scheme not in ('http', 'https') or not endpoint.hostname or endpoint.username or endpoint.password:
                    add('error', label+'.base_url', '需要不含凭据的 HTTP(S) 端点')
                if not os.environ.get(opts['api_key_env']):
                    add('error' if name in used else 'warning', label+'.api_key_env',
                        '凭据环境变量未设置', '设置配置指定的环境变量；不要将凭据写入 TOML')
                for key in ('temperature', 'max_tokens', 'timeout_s'):
                    value = opts.get(key)
                    if value is not None and (type(value) not in (int, float) or not math.isfinite(value)
                                              or value < 0 or (key != 'temperature' and value == 0)):
                        add('error', label+'.'+key, '需要有效的非负数（token/超时必须为正数）')
            elif adapter_cfg.type.startswith('cli_'):
                from .safety import resolve_exe
                exe = opts.get('exe')
                if exe and not command_exists(exe, cfg.base_dir):
                    add('error', label+'.exe', '显式配置的可执行文件不存在或不可执行', '使用可执行文件绝对路径或 PATH 命令名')
                elif not resolve_exe(adapter.exe_name, exe):
                    add('error' if name in used else 'warning', label+'.exe', '本机未找到 CLI 工具', '安装工具或配置 exe 绝对路径')
                extra_env = opts.get('extra_env', [])
                if not isinstance(extra_env, list) or not all(isinstance(v, str) and v for v in extra_env):
                    add('error', label+'.extra_env', '需要环境变量名字符串数组')
                if getattr(adapter, 'timeout_s', 0) <= 0:
                    add('error', label+'.timeout_s', '超时必须为正数')
            add('ok', label, '适配器配置可构造；未执行版本、登录或网络探测')
        except (ValueError, TypeError, KeyError, OSError, OverflowError):
            add('error', label, '适配器参数无效', '检查 type、exe、timeout_s、模型和代理参数的类型及范围')

    if any(s.kind == 'code' for s in cfg.stages) and not command_exists(cfg.git_exe or 'git', cfg.base_dir):
        add('error', 'git_exe', '编码阶段需要 Git', '安装 Git 或修正 git_exe')
    for stage in cfg.stages:
        label = f'stages.{stage.name}'
        for key in ('max_tokens', 'max_duration_s'):
            limit = getattr(cfg.run_budget, key)
            value = getattr(stage, key)
            if limit is not None and value is not None and value > limit:
                add('warning', label+'.'+key, '阶段额度大于运行总额度，实际仍受运行总额度限制')
        if stage.prompt:
            prompt = cfg.base_dir/stage.prompt
            # 引擎允许内联文本，仅将显式路径式提示词诊断为缺失文件。
            if (stage.prompt.startswith(('prompts/', './', '../')) or Path(stage.prompt).suffix in ('.md', '.txt')) and not prompt.is_file():
                add('error', label+'.prompt', '提示词文件不存在', str(prompt))
        for key, command in (('test_cmd', stage.test_cmd), ('precheck_cmd', stage.precheck_cmd)):
            if command and not command_exists(command[0], cfg.task_workdir):
                add('error', label+'.'+key, '命令入口不存在或不可执行', '检查工作目录、命令路径与 PATH；未执行该命令')
        for name in stage.context_files:
            if not (cfg.task_workdir/name).is_file():
                add('warning', label+'.context_files', '上下文文件不存在', name)
    if cfg.watch_root and cfg.watch_root.strip().lower() != 'off':
        watch = (cfg.base_dir/cfg.watch_root).resolve()
        if not watch.is_dir():
            add('error', 'watch_root', '监视目录不存在', str(watch))
        elif watch.is_relative_to(cfg.task_workdir):
            add('error', 'watch_root', '监视域落在可写根内，运行时会禁用该域', '选择可写根外的有限监视目录')
        elif watch.is_relative_to(cfg.state_dir):
            add('warning', 'watch_root', '监视域位于状态目录内，需确认未落入本次 run 的可写根')
    elif cfg.watch_enforce:
        add('error', 'watch_enforce', '开启强制监视但未设置 watch_root', '指定有效的监视域')
    if cfg.watch_enforce and not any(s.kind == 'code' for s in cfg.stages):
        add('warning', 'watch_enforce', '当前没有 code 阶段；测试命令不受此越界快照约束')
    return result()
