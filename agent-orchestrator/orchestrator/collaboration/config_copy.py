"""从固定 commit 创建后续任务工作区，原任务与原配置保持不变。"""
import json
import stat
import tomllib
import zipfile
import re
from pathlib import Path

from .. import artifacts
from ..config import load_config
from ..safety import relative_target
from .projection import directory, read_json


def scalar(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if type(value) is bool:
        return 'true' if value else 'false'
    if type(value) in (int, float):
        return repr(value)
    if isinstance(value, list) and all(not isinstance(v, dict) for v in value):
        return '[' + ', '.join(scalar(v) for v in value) + ']'
    raise ValueError('配置副本包含不支持的值')


def dump_config(raw):
    lines = []
    def emit(data, prefix=()):
        for key, value in data.items():
            if not isinstance(value, dict) and not (isinstance(value, list) and value and isinstance(value[0], dict)):
                lines.append(json.dumps(key) + ' = ' + scalar(value))
        for key, value in data.items():
            table = prefix + (key,)
            label = '.'.join(json.dumps(v) for v in table)
            if isinstance(value, dict):
                lines.append('\n[' + label + ']')
                emit(value, table)
            elif isinstance(value, list) and value and isinstance(value[0], dict):
                for item in value:
                    lines.append('\n[[' + label + ']]')
                    emit(item, table)
    emit(raw)
    return '\n'.join(lines) + '\n'


def derive(run, new_id):
    cfg = load_config(run['config'])
    state = read_json(directory(run) / 'state.json')
    commit = next((s.get('commit_sha') for s in reversed(list(state.get('stages', {}).values()))
                   if s.get('commit_sha')), None)
    if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
        raise ValueError('该任务没有固定代码 commit，不能推断后续任务的源码基线')
    base = cfg.state_dir / 'workbench' / 'derived' / new_id
    workspace = base / 'workspace'
    archive = base / 'source.zip'
    workspace.mkdir(parents=True, exist_ok=True)
    artifacts._git(['archive', '--format=zip', '-o', str(archive), commit], cfg.task_workdir)
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if sum(e.file_size for e in entries) > 50 * 1024 * 1024:
            raise ValueError('快照超过本地工作台 50 MiB 复制上限')
        for entry in entries:
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError('后续任务快照含符号链接，需要先人工检查')
            target = relative_target(workspace, entry.filename)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read(entry))
    artifacts.ensure_repo(workspace)
    inputs = [p.relative_to(workspace).as_posix() for p in workspace.rglob('*')
              if p.is_file() and '.git' not in p.relative_to(workspace).parts]
    artifacts.commit_files(workspace, inputs, 'baseline: fixed parent snapshot')
    raw = tomllib.loads(Path(run['config']).read_text(encoding='utf8'))
    raw['task_workdir'] = str(workspace.resolve())
    raw['state_dir'] = str(cfg.state_dir)
    raw['watch_root'] = 'off'
    raw.pop('tune_proposal_id', None)
    for stage in raw['stages']:
        prompt = stage.get('prompt')
        if prompt:
            candidate = cfg.base_dir / prompt
            if candidate.is_file():
                stage['prompt'] = str(candidate.resolve())
    output = base / 'config.toml'
    output.write_text(dump_config(raw), encoding='utf8')
    load_config(output)
    return output
