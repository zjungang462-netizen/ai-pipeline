"""Codex 0.169 工具协议到公共活动的映射；不执行或展示命令正文。"""
from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath


READ_TOOLS = frozenset(('read', 'read_file', 'view_file', 'sed_file'))
WRITE_TOOLS = frozenset(('edit', 'write', 'apply_patch', 'edit_file', 'write_file',
                         'write_to_file', 'replace_file_content', 'multi_replace_file_content'))
PATH_KEYS = ('Path', 'FilePath', 'TargetFile', 'path', 'file_path')


@dataclass(frozen=True)
class ToolActivity:
    key: tuple
    name: str
    state: str
    parameters: dict = field(default_factory=dict)
    filename: str = ''
    read_signature: str = ''
    writes: bool = False


def file_parameter(parameters):
    return next((parameters[k] for k in PATH_KEYS if isinstance(parameters.get(k), str)), '')


def read_signature(name, parameters):
    return json.dumps({'name': name, 'parameters': parameters}, sort_keys=True, ensure_ascii=False)


def _tokens(command):
    # posix=False 保留 Windows 路径中的反斜杠；仅解开整体配对引号。
    try:
        return [token[1:-1] if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'"
                else token for token in shlex.split(command, posix=False)]
    except ValueError:
        return []


def simple_read(command):
    """只识别单文件、无插值/管道/重定向的明确读取；复杂命令不作推断。"""
    if not isinstance(command, str):
        return '', ''
    words = _tokens(command)
    if not words:
        return '', ''
    executable = words[0].replace('\\', '/').rsplit('/', 1)[-1].lower()
    shells = {'pwsh', 'pwsh.exe', 'powershell', 'powershell.exe', 'bash', 'sh', 'bash.exe', 'sh.exe'}
    if executable in shells:
        index = next((i for i, word in enumerate(words[1:], 1)
                      if word.lower() in ('-command', '-c', '-lc')), None)
        allowed = {'-noprofile', '-noninteractive', '-nologo', '-l'}
        if index is None or len(words) != index + 2 or any(w.lower() not in allowed for w in words[1:index]):
            return '', ''
        command = words[-1]
        words = _tokens(command)
    # 连引号内的特殊符号也保守拒绝，宁可少识别，不把写入当成读取。
    if not words or any(c in command for c in (';', '|', '&', '>', '<', '$', '`', '\n', '\r',
                                               '(', ')', '{', '}', '@', '#')):
        return '', ''
    name, arguments = words[0].lower(), words[1:]
    if name in ('get-content', 'gc'):
        if arguments and arguments[0].lower() in ('-literalpath', '-path'):
            arguments = arguments[1:]
    elif name == 'cat':
        if arguments[:1] == ['--']:
            arguments = arguments[1:]
    else:
        return '', ''
    if len(arguments) != 1 or not arguments[0] or arguments[0].startswith('-'):
        return '', ''
    filename = arguments[0]
    if any(c in filename for c in ('*', '?', '[', ']')):
        return '', ''
    return filename, read_signature(name, {'path': filename})


def codex_activity(event, thread=''):
    """字段按 rust-v0.169.0/exec/src/exec_events.rs 校验；未知形状忽略。"""
    phase = event.get('type')
    if phase not in ('item.started', 'item.updated', 'item.completed'):
        return None
    item = event.get('item')
    if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id']:
        return None
    kind, status = item.get('type'), item.get('status')
    if not isinstance(kind, str) or not isinstance(status, str):
        return None
    if phase == 'item.completed':
        if status not in ('completed', 'failed', 'declined'):
            return None
        state = 'DONE' if status == 'completed' else 'ERROR'
    elif status == 'in_progress':
        state = 'ACTIVE'
    else:
        return None
    key = ('codex', thread, item['id'])
    if kind == 'command_execution':
        command = item.get('command')
        if not isinstance(command, str):
            return None
        if state == 'DONE':
            if type(item.get('exit_code')) is not int:
                return None
            if item['exit_code'] != 0:
                state = 'ERROR'
        filename, signature = simple_read(command)
        # 此版本的 command_execution 不含 cwd；相对路径不能可靠定位，
        # 否则不同工作目录中的同名读取会被误计为重复，也会显示错误路径。
        if not (PureWindowsPath(filename).is_absolute() or PurePosixPath(filename).is_absolute()):
            filename, signature = '', ''
        return ToolActivity(key, kind, state, filename=filename, read_signature=signature)
    if kind == 'file_change':
        changes = item.get('changes')
        if status == 'declined' or not isinstance(changes, list) or not changes:
            return None
        if any(not isinstance(c, dict) or not isinstance(c.get('path'), str) or not c['path']
               or c.get('kind') not in ('add', 'update', 'delete') for c in changes):
            return None
        return ToolActivity(key, kind, state, filename=changes[0]['path'], writes=True)
    if kind == 'mcp_tool_call':
        server, tool = item.get('server'), item.get('tool')
        if (status == 'declined' or not isinstance(server, str) or not server
                or not isinstance(tool, str) or not tool):
            return None
        parameters = item.get('arguments')
        if not isinstance(parameters, dict):
            parameters = {}
        if state == 'DONE' and item.get('error') is not None:
            state = 'ERROR'
        return ToolActivity(key, 'mcp:' + server + '/' + tool, state, parameters,
                            file_parameter(parameters),
                            read_signature(tool, {'server': server, 'arguments': parameters}) if tool in READ_TOOLS else '',
                            tool in WRITE_TOOLS)
    return None
