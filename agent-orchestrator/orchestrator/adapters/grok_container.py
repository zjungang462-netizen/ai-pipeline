"""Grok 容器入口。以独立 Python 进程运行，只从 stdin 接收本轮资料。"""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys

WORKSPACE = Path('/workspace')
PRIVATE = Path('/tmp/pipeline-grok')
TOOL_FIELDS = {'read_file': 'target_file', 'search_replace': 'file_path'}


def grok_args(editable, prompt_path):
    """提示词只放在容器私有文件。Linux 单个命令行参数不能超过 128KiB。"""
    args = ['/opt/grok', '--cwd', str(WORKSPACE), '--disable-web-search',
            '--permission-mode', 'acceptEdits' if editable else 'plan',
            '--tools', 'read_file,search_replace' if editable else 'read_file',
            '--disallowed-tools', 'search_tool,use_tool', '--max-turns', '8',
            '--output-format', 'streaming-json',
            '--prompt-file', Path(prompt_path).as_posix()]
    if editable:
        args += ['--allow', 'Edit(/workspace/**)']
    return args


def check_tool():
    event = json.load(sys.stdin)
    allowed = False
    if isinstance(event, dict) and event.get('toolInputTruncated') is False:
        tool = event.get('toolName')
        field = TOOL_FIELDS.get(tool)
        tool_input = event.get('toolInput')
        value = tool_input.get(field) if field and isinstance(tool_input, dict) else None
        policy = json.loads((PRIVATE / 'policy.json').read_text(encoding='utf8'))
        if isinstance(value, str) and value:
            path = WORKSPACE / value
            allowed = (path.as_posix() in policy.get(tool, [])
                       and path.is_file() and path.resolve() == path)
    response = {'decision': 'allow'} if allowed else {
        'decision': 'deny', 'reason': 'Only declared workspace files are available'}
    print(json.dumps(response))


def main():
    request = json.load(sys.stdin)
    PRIVATE.mkdir(mode=0o700)
    home = PRIVATE / '.grok'
    home.mkdir(mode=0o700)
    auth = home / 'auth.json'
    auth.touch(mode=0o600)
    auth.write_text(request['auth'], encoding='utf8')
    sources = request['files']
    for name, value in sources.items():
        target = WORKSPACE / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(value, validate=True))
    editable = request['editable']
    policy = {
        'read_file': [str(WORKSPACE / name) for name in sources],
        'search_replace': [str(WORKSPACE / name) for name in editable],
    }
    (PRIVATE / 'policy.json').write_text(json.dumps(policy), encoding='utf8')
    hooks = home / 'hooks'
    hooks.mkdir()
    (hooks / 'pipeline.json').write_text(json.dumps({'hooks': {'PreToolUse': [{'hooks': [{
        'type': 'command', 'command': sys.executable, 'args': [__file__, 'check-tool'],
        'timeout': 5, 'failClosed': True,
    }]}]}}), encoding='utf8')
    prompt = PRIVATE / 'prompt.txt'
    prompt.write_text(request['prompt'], encoding='utf8')
    prompt.chmod(0o600)
    environment = {**os.environ, 'HOME': str(PRIVATE), 'GROK_HOME': str(home)}
    try:
        result = subprocess.run(grok_args(editable, prompt), cwd=WORKSPACE,
                                env=environment, stdin=subprocess.DEVNULL,
                                timeout=request['timeout'])
    except subprocess.TimeoutExpired:
        return 124
    if result.returncode:
        return result.returncode
    updates = {}
    remaining = request['max_bytes']
    for name in editable:
        target = WORKSPACE / name
        if not target.is_file() or target.resolve() != target or target.stat().st_size > remaining:
            return 122
        data = target.read_bytes()
        remaining -= len(data)
        encoded = base64.b64encode(data).decode('ascii')
        if encoded != sources[name]:
            updates[name] = encoded
    print(json.dumps({'type': 'pipeline_files', 'files': updates}), flush=True)
    return 0


if __name__ == '__main__':
    if sys.argv[1:] == ['check-tool']:
        check_tool()
    else:
        raise SystemExit(main())
