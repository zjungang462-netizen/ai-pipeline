"""提交前门禁：配置校验、增量凭据扫描、diff 检查与官方 quick 验收。"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator.config import parse_config
from scripts.failure_evidence import TOKEN

MAX_SCAN_BYTES = 2 * 1024 * 1024
URL_CREDENTIAL = re.compile(r'''https?://[^\s/:'"]+:[^\s/@'"]+@''')
PRIVATE_KEY = re.compile(r'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----')
SECRET_ASSIGNMENT = re.compile(
    r'''(?i)\b(?:api_key|auth_token|access_token|password|client_secret)\s*[=:]\s*['"]([^'"\r\n]{8,})['"]''')


def git(repo, *args, check=True):
    return subprocess.run(['git', '-C', str(repo), *args], check=check,
                          capture_output=True, timeout=30)


def credential_findings(data, path, source):
    if b'\0' in data[:8192]:
        return []  # 只扫描文本；不把二进制作为配置或源码读取。
    if len(data) > MAX_SCAN_BYTES:
        return [dict(path=path, source=source, kind='scan_size_limit', line=0)]
    text = data.decode('utf8', errors='replace')
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        kinds = []
        if TOKEN.search(line): kinds.append('token_pattern')
        if URL_CREDENTIAL.search(line): kinds.append('url_credentials')
        if PRIVATE_KEY.search(line): kinds.append('private_key')
        match = SECRET_ASSIGNMENT.search(line)
        if match and match[1].lower() not in ('[redacted]', 'your_api_key', 'your_token', 'changeme'):
            kinds.append('literal_credential')
        for kind in kinds:
            # 只输出位置和类别，绝不回显命中的凭据正文。
            findings.append(dict(path=path, source=source, kind=kind, line=number))
    return findings


def check_repository(repo: Path) -> dict:
    repo = repo.absolute()
    issues, config_checks = [], 0
    for args in (('diff', '--check'), ('diff', '--cached', '--check')):
        run = git(repo, *args, check=False)
        if run.returncode:
            issues.append(dict(kind='diff_check', source=' '.join(args),
                               message='diff 检查失败；运行对应 git diff --check 查看位置'))
    names = lambda data: [s.decode('utf8') for s in data.split(b'\0') if s]
    tracked = names(git(repo, 'ls-files', '-z').stdout)
    untracked = names(git(repo, 'ls-files', '--others', '--exclude-standard', '-z').stdout)
    staged = set(names(git(repo, 'diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z').stdout))
    changed = set(names(git(repo, 'diff', '--name-only', '--diff-filter=ACMR', '-z').stdout)) | staged | set(untracked)
    configs = {p for p in tracked+untracked if p.endswith('.toml')}
    for source in ('index', 'working_tree'):
        candidates = (staged | (configs & set(tracked))) if source == 'index' else changed | configs
        for name in sorted(candidates):
            try:
                if source == 'index':
                    run = git(repo, 'show', ':'+name, check=False)
                    if run.returncode:
                        continue
                    data = run.stdout
                else:
                    path = repo/name
                    if not path.is_file():
                        continue
                    from orchestrator.maintenance import boundary
                    boundary(path, repo)
                    if path.stat().st_size > MAX_SCAN_BYTES:
                        issues.append(dict(path=name, source=source, kind='scan_size_limit'))
                        continue
                    data = path.read_bytes()
                if name in (staged if source == 'index' else changed):
                    issues.extend(credential_findings(data, name, source))
                if name.endswith('.toml'):
                    if len(data) > MAX_SCAN_BYTES:
                        raise ValueError('配置超过扫描上限')
                    raw = tomllib.loads(data.decode('utf8'))
                    if name.endswith('.example.toml') or {'task_workdir', 'state_dir', 'stages'} & set(raw):
                        parse_config(raw, base_dir=(repo/name).parent)
                        config_checks += 1
            except (OSError, ValueError, TypeError) as error:
                issues.append(dict(path=name, source=source, kind='config_or_file',
                                   message=type(error).__name__+'：配置或文件检查失败；运行 doctor 查看诊断'))
    return dict(ok=not issues, config_checks=config_checks, issues=issues)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checks-only', action='store_true', help='只做静态检查；不替代完整提交前门禁')
    parser.add_argument('--output', help='quick 验收输出目录，须在项目外')
    args = parser.parse_args(argv)
    repo = Path(git(ROOT, 'rev-parse', '--show-toplevel').stdout.decode('utf8').strip())
    report = check_repository(repo)
    from scripts.generate_config_docs import check_generated
    try:
        for name in check_generated(ROOT):
            report['issues'].append(dict(path=name,kind='generated_docs',message='运行 generate_config_docs.py 同步'))
    except (OSError,ValueError):
        report['issues'].append(dict(kind='generated_docs',message='无法检查配置表与示例注释'))
    report['ok']=not report['issues']
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if not report['ok']:
        return 1
    if args.checks_only:
        return 0
    output = Path(args.output).absolute() if args.output else Path(tempfile.mkdtemp(prefix='orchestrator-precommit-'))
    print('快速验收证据：'+str(output), flush=True)
    run = subprocess.run([sys.executable, str(ROOT/'scripts/verify_delivery.py'),
                          '--quick', '--redact-logs', '--output', str(output)], cwd=ROOT)
    return run.returncode


if __name__ == '__main__':
    raise SystemExit(main())
