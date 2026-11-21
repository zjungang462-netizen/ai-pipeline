"""Codex 0.169 工具协议到公共活动的映射；不执行或展示命令正文。"""
from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath


READ_TOOLS = frozenset(('read', 'read_file', 'view_file', 'sed_file'))
WRITE_TOOLS = frozenset(('edit', 'write', 'apply_patch', 'edit_file', 'write_file',
                         'write_to_file', 'replace_file_content', 'multi_replace_file_content'))
PATH_KEYS = ('Path', 'FilePath', 'TargetFile', 'path', 'file_path')
GET_CONTENT_LINE_OPTIONS = frozenset(('-totalcount', '-first', '-tail'))


@dataclass(frozen=True)
class ToolActivity:
    key: tuple
    name: str
    state: str
    parameters: dict = field(default_factory=dict)
    filename: str = ''
    read_signature: str = ''
    writes: bool = False
    counts_error: bool = True


def file_parameter(parameters):
    return next((parameters[k] for k in PATH_KEYS if isinstance(parameters.get(k), str)), '')


def _normalized_read_path(path):
    """读类活动只按文件路径去重：统一分隔符/冗余段，Windows 再折叠大小写。"""
    value = path.strip()
    if not value:
        return ''
    if os.name == 'nt':
        return os.path.normcase(os.path.normpath(value.replace('/', '\\')))
    return os.path.normpath(value)


def read_signature(name, parameters):
    """读类工具的去重签名 = 工具名 + 规范化文件路径。

    offset/limit/行范围/读取模式等参数不参与签名，因此同一文件被分多次、按不同
    偏移读取也能累计到同一个计数上；没有可识别路径参数时退回完整参数签名，
    避免把无法定位的读取错误合并。
    """
    parameters = parameters if isinstance(parameters, dict) else {}
    path = file_parameter(parameters)
    if path:
        return json.dumps({'name': name, 'path': _normalized_read_path(path)},
                          sort_keys=True, ensure_ascii=False)
    return json.dumps({'name': name, 'parameters': parameters}, sort_keys=True, ensure_ascii=False)


def _tokens(command):
    # posix=False 保留 Windows 路径中的反斜杠；仅解开整体配对引号。
    try:
        return [token[1:-1] if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'"
                else token for token in shlex.split(command, posix=False)]
    except ValueError:
        return []


def _plain_file(word):
    return (isinstance(word, str) and bool(word) and not word.startswith('-')
            and not any(c in word for c in '*?[]'))


def _read_filename(name, arguments):
    """仅在"单文件、确定只读"的保守形态下返回被读取文件名，否则返回空。"""
    if name in ('get-content', 'gc'):
        words = list(arguments)
        if words and words[0].lower() in ('-literalpath', '-path'):
            words = words[1:]
        # Get-Content [-LiteralPath|-Path] <file> [-TotalCount|-First|-Tail N]；
        # 行数限定只影响本次输出，被读文件相同，仍可累计重复读取。
        if len(words) == 1 and _plain_file(words[0]):
            return words[0]
        if (len(words) == 3 and _plain_file(words[0])
                and words[1].lower() in GET_CONTENT_LINE_OPTIONS and words[2].isdigit()):
            return words[0]
        return ''
    if name in ('cat', 'type'):
        words = list(arguments)
        if name == 'cat' and words[:1] == ['--']:
            words = words[1:]
        return words[0] if len(words) == 1 and _plain_file(words[0]) else ''
    if name in ('head', 'tail'):
        # head/tail [-n N] <file>
        words = list(arguments)
        if len(words) == 1 and _plain_file(words[0]):
            return words[0]
        if (len(words) == 3 and words[0] == '-n' and words[1].isdigit()
                and _plain_file(words[2])):
            return words[2]
        return ''
    if name == 'sed':
        # sed -n <脚本> <file>：脚本含空格时由 token 规则合并成单个词；只认 -n 只读形态。
        words = list(arguments)
        if len(words) == 3 and words[0] == '-n' and words[1] and _plain_file(words[2]):
            return words[2]
        return ''
    return ''


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
    filename = _read_filename(name, arguments)
    if not filename:
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
        qualified = 'mcp:' + server + '/' + tool
        return ToolActivity(key, qualified, state, parameters,
                            file_parameter(parameters),
                            read_signature(qualified, parameters) if tool in READ_TOOLS else '',
                            tool in WRITE_TOOLS)
    return None
