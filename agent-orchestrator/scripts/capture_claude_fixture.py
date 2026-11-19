"""从已归档的真实输出机械脱敏生成夹具，不调用模型。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.failure_evidence import Scrubber, environment_secrets


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args(argv)
    raw = Path(args.source).read_bytes()
    events = [json.loads(line) for line in raw.decode('utf8').splitlines() if line.strip()]
    original_init = next(event for event in events if event.get('subtype') == 'init')
    private_paths = {str(Path.home())}
    private_paths.update(original_init.get(key) for key in ('cwd', 'original_cwd', 'powershell_path')
                         if isinstance(original_init.get(key), str))
    identity = {}
    scrub = Scrubber(environment_secrets())

    def clean(value, key=''):
        if key in ('thinking', 'signature'):
            return '[OMITTED]'
        if isinstance(value, dict):
            return {name: clean(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [clean(item, key) for item in value]
        if isinstance(value, str):
            if key in ('id', 'uuid', 'session_id', 'tool_use_id', 'parent_tool_use_id', 'request_id'):
                if value not in identity:
                    identity[value] = 'fixture-' + str(len(identity) + 1)
                return identity[value]
            if key in ('cwd', 'original_cwd', 'powershell_path', 'file_path', 'path'):
                return 'C:/fixture'
            for path in sorted(private_paths, key=len, reverse=True):
                value = value.replace(path, 'C:/fixture').replace(path.replace('\\', '/'), 'C:/fixture')
            return scrub.text(value)
        return value

    kept = [clean(event) for event in events if event.get('type') in ('assistant', 'user', 'result')
            or (event.get('type') == 'system' and event.get('subtype') == 'init')]
    init = next(event for event in kept if event.get('subtype') == 'init')
    if init.get('claude_code_version') != args.version:
        raise ValueError('真实版本与请求版本不一致')
    output = ROOT / 'tests/fixtures' / ('claude_' + args.version + '_tools_real.jsonl')
    if output.parent != ROOT / 'tests/fixtures':
        raise ValueError('版本不能包含路径分隔符')
    body = ('\n'.join(json.dumps(event, ensure_ascii=False) for event in kept) + '\n').encode('utf8')
    with output.open('xb') as stream:
        stream.write(body)
    path = ROOT / 'tests/fixtures/cli-capture-provenance.json'
    provenance = json.loads(path.read_text(encoding='utf8'))
    provenance[output.name] = dict(source='真实自身维护任务的 Claude Code Read/Edit 输出；原任务因未知版本被拒绝',
                                   version=args.version, captured_at=datetime.now(timezone.utc).isoformat(),
                                   backend='DeepSeek Anthropic-compatible endpoint',
                                   source_sha256=hashlib.sha256(raw).hexdigest(),
                                   fixture_sha256=hashlib.sha256(body).hexdigest(),
                                   redacted_fields=['IDs', 'paths', 'thinking/signature body omitted',
                                                    'system incremental thinking counters omitted'])
    path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    print(json.dumps(dict(fixture=output.name, events=len(kept))))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
