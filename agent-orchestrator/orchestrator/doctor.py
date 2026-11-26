"""只读本机诊断：复用严格配置校验，不执行模型、登录或配置中的命令。"""
from __future__ import annotations

import copy
import difflib
import os
import re
import shutil
import tomllib
from pathlib import Path

from .adapters import registry as adapter_registry
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
    for section in ('autonomy', 'autotune', 'run_budget', 'review_coverage'):
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
    if cfg.review_coverage.incomplete == 'allow':
        add('warning', 'review_coverage.incomplete', '显式允许未完整覆盖的审查 pass；结果仍标记缺失清单')

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
    for issue in cfg.adapter_warnings:
        add(issue.level, issue.path, issue.message, issue.hint)
    for name, adapter_cfg in cfg.adapters.items():
        label = f'adapters.{name}'
        try:
            adapter = build_adapter(adapter_cfg)
            opts = adapter_cfg.options
            # 参数类型、范围与端点格式已由 parse_config 按 SPEC 校验；这里只做环境相关检查。
            spec = adapter_registry.get(adapter_cfg.type)
            for option in spec.options:
                if option.secret_ref and not os.environ.get(opts.get(option.key) or ''):
                    add('error' if name in used else 'warning', label+'.'+option.key,
                        '凭据环境变量未设置', '设置配置指定的环境变量；不要将凭据写入 TOML')
            if spec.verified_versions:
                add('ok', label+'.verified_versions', '已验证版本：' + '、'.join(spec.verified_versions),
                    '未列出的版本由适配器在调用模型前拦截')
            if adapter_cfg.type.startswith('cli_'):
                from .safety import resolve_exe
                exe = opts.get('exe')
                if exe and not command_exists(exe, cfg.base_dir):
                    add('error', label+'.exe', '显式配置的可执行文件不存在或不可执行', '使用可执行文件绝对路径或 PATH 命令名')
                elif not resolve_exe(adapter.exe_name, exe):
                    add('error' if name in used else 'warning', label+'.exe', '本机未找到 CLI 工具', '安装工具或配置 exe 绝对路径')
                if adapter_cfg.type in ('cli_grok', 'cli_doubao'):
                    from .adapters.verified_runtimes import get_verified_runtime
                    from .state import sha256_file
                    runtime = getattr(adapter, 'runtime', None) or get_verified_runtime(adapter_cfg.type)
                    binary = getattr(adapter, 'binary', None)
                    if binary is None:
                        add('warning' if name not in used else 'error', label+'.binary',
                            '未配置已验证的容器 Linux 原生程序路径', '参考 OPERATIONS.md 配置容器适配器参数')
                    elif not binary.is_file():
                        add('error' if name in used else 'warning', label+'.binary',
                            '配置的容器 Linux 原生程序文件不存在', '检查 binary 绝对路径')
                    elif sha256_file(binary) != runtime.binary_sha256:
                        add('error' if name in used else 'warning', label+'.binary',
                            '容器 Linux 原生程序 SHA-256 与已验证版本不匹配',
                            f'需要已验证版本 {runtime.version}')
                    if adapter_cfg.type == 'cli_grok':
                        auth_file = getattr(adapter, 'auth_file', None)
                        if auth_file and not auth_file.is_file():
                            add('warning', label+'.auth_file',
                                '未找到 Grok 会话文件，运行前需先登录', '先执行 grok login 或检查 auth_file 路径')
                    elif adapter_cfg.type == 'cli_doubao':
                        machine_id = getattr(adapter, 'machine_id', None)
                        if machine_id is not None and not machine_id.is_file():
                            add('error' if name in used else 'warning', label+'.machine_id',
                                '配置的豆包 machine_id 文件不存在', '准备持久化的 Linux machine-id 文件')
                        if (name in used and not cfg.run_budget.unlimited_tokens
                                and cfg.run_budget.max_tokens is not None
                                and cfg.run_budget.max_tokens < 80000):
                            add('warning', 'run_budget.max_tokens',
                                '豆包终态不报告 token，有限 token 预算下多轮返工易被未知用量预扣拦截',
                                '建议启用 unlimited_tokens=true 并配置 max_duration_s 与 max_model_calls/max_unknown_calls')
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
            # 含空白的自然语言不因末尾提及 README.md 被误认为文件路径。
            text = stage.prompt
            explicit = text.startswith(('prompts/', './', '../'))
            path_like = not any(ch.isspace() for ch in text) and Path(text).suffix in ('.md', '.txt')
            if (explicit or path_like) and not prompt.is_file():
                add('error', label+'.prompt', '提示词文件不存在', str(prompt))
        for key, command in (('test_cmd', stage.test_cmd), ('precheck_cmd', stage.precheck_cmd)):
            if command and not command_exists(command[0], cfg.task_workdir):
                add('error', label+'.'+key, '命令入口不存在或不可执行', '检查工作目录、命令路径与 PATH；未执行该命令')
        for name in stage.context_files:
            if not (cfg.task_workdir/name).is_file():
                add('warning', label+'.context_files', '上下文文件不存在', name)
    for stage_name, candidates in cfg.autotune.test_command_candidates.items():
        for index, command in enumerate(candidates):
            if not command_exists(command[0], cfg.task_workdir):
                add('warning', f'autotune.test_command_candidates.{stage_name}[{index}]',
                    '候选命令入口当前不可用；doctor 不会执行候选命令',
                    '确认工具已安装，或从候选清单删除该命令')
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
