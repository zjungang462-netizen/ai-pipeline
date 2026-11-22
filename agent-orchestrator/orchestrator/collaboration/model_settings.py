"""本次运行的模型与思考选项；仅复制配置，不接受浏览器传来的路径、命令或凭据。"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from ..config import load_config, parse_config
from .config_copy import dump_config

MODEL_TYPES = {'openai_compat', 'cli_codex', 'cli_qwen', 'cli_agy', 'cli_qoder', 'cli_claude'}
EFFORTS = ('none', 'low', 'medium', 'high', 'xhigh')


def describe_models(cfg):
    result=[]
    for name, adapter in cfg.adapters.items():
        if adapter.type not in MODEL_TYPES: continue
        options=adapter.options
        effective_model=options.get('model') or ''
        effective_effort=options.get('reasoning_effort') or ''
        endpoint=None
        if adapter.type=='cli_claude':
            from ..adapters.cli_claude import ClaudeAdapter
            profile=ClaudeAdapter(name,model=options.get('model'),reasoning_effort=options.get('reasoning_effort'),
                                  inherit_connection=options.get('inherit_connection',True)).input_fingerprint()
            effective_model=profile['model'] or '';effective_effort=profile['reasoning_effort'] or ''
            endpoint=profile['backend_endpoint']
        result.append(dict(name=name, type=adapter.type, model=options.get('model') or '',
                           stages=[s.name for s in cfg.stages if s.adapter == name],
                           reasoning_effort=effective_effort, effective_model=effective_model,
                           backend_endpoint=endpoint,
                           thinking=options.get('thinking') or '',
                           review_json_mode=options.get('review_json_mode', False),
                           supports_effort=adapter.type in ('openai_compat','cli_codex','cli_qoder','cli_claude'),
                           supports_thinking=adapter.type in ('openai_compat','cli_qoder')))
    return result


def validate_options(cfg, options):
    if not isinstance(options, dict) or set(options)-{'models','unlimited_tokens'}:
        raise ValueError('本次执行选项只支持 models/unlimited_tokens')
    unlimited=options.get('unlimited_tokens',False)
    if type(unlimited) is not bool: raise ValueError('unlimited_tokens 必须为布尔值')
    models=options.get('models',{})
    if not isinstance(models,dict):raise ValueError('models 必须为对象')
    clean={}
    for name, values in models.items():
        adapter=cfg.adapters.get(name)
        if not adapter or adapter.type not in MODEL_TYPES:raise ValueError('该适配器不支持工作台模型覆盖')
        if not isinstance(values,dict) or set(values)-{'model','reasoning_effort','thinking','review_json_mode'}:
            raise ValueError('仅可覆盖模型及受支持的思考选项')
        row={}
        for key,value in values.items():
            if key=='review_json_mode':
                if adapter.type!='openai_compat' or type(value) is not bool:
                    raise ValueError('结构化审查模式仅支持兼容 API 的布尔选项')
                row[key]=value;continue
            if not isinstance(value,str):raise ValueError('模型选项必须为字符串')
            if key=='model':
                if len(value)>160 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}(?:\[(?:1M|200K)\])?',value):
                    raise ValueError('模型名称必须为 1..160 字符的模型标识')
            elif key=='reasoning_effort':
                allowed=('low','medium','high','xhigh','max') if adapter.type in ('cli_qoder','cli_claude') else EFFORTS
                if adapter.type not in ('openai_compat','cli_codex','cli_qoder','cli_claude') or value not in allowed:
                    raise ValueError('该工具不支持此思考强度')
            elif (adapter.type=='cli_qoder' and value not in ('auto','adaptive','disabled')) or (
                    adapter.type!='cli_qoder' and (adapter.type!='openai_compat' or value not in ('enabled','disabled'))):
                raise ValueError('thinking 必须为工具支持的模式')
            row[key]=value
        if row:clean[name]=row
    return {'models':clean,'unlimited_tokens':unlimited}


def prepare_config(cfg, options):
    options=validate_options(cfg,options)
    raw=tomllib.loads(cfg.source_path.read_text(encoding='utf8'))
    raw['task_workdir']=cfg.task_workdir.as_posix();raw['state_dir']=cfg.state_dir.as_posix()
    if cfg.watch_root and cfg.watch_root.lower()!='off':raw['watch_root']=(cfg.base_dir/cfg.watch_root).resolve().as_posix()
    raw.pop('tune_proposal_id',None)
    if options['unlimited_tokens']:
        budget=raw.setdefault('run_budget',{});budget.pop('max_tokens',None);budget['unlimited_tokens']=True
        for stage in raw['stages']:stage.pop('max_tokens',None)
        for adapter in raw.get('adapters',{}).values():adapter.pop('max_tokens',None)
    for name,values in options['models'].items():raw['adapters'][name].update(values)
    for stage in raw['stages']:
        prompt=stage.get('prompt')
        if prompt:
            try:
                candidate=cfg.base_dir/prompt
                if candidate.is_file():stage['prompt']=candidate.resolve().as_posix()
            except OSError:pass  # 长内联提示词不作为文件路径处理。
    # 重写前后都做严格校验；保留真实任务目录、命令、权限和时长/轮次。
    parse_config(raw,base_dir=cfg.base_dir)
    return dump_config(raw)


def config_copy(cfg, options, run_id):
    text=prepare_config(cfg,options)
    target=cfg.state_dir/'workbench/overrides'/run_id/'config.toml'
    target.parent.mkdir(parents=True,exist_ok=True)
    try:
        with target.open('x',encoding='utf8') as stream:stream.write(text)
    except FileExistsError:
        if target.read_text(encoding='utf8')!=text:
            raise ValueError('本次运行配置副本已存在且内容不同')
    return load_config(target)
