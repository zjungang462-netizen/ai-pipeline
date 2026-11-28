"""提交前门禁：配置校验、增量凭据扫描、diff 检查与官方 quick 验收。"""
from __future__ import annotations

import argparse
import json
import os
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
HISTORY_BASELINE = Path(__file__).with_name('credential_history_baseline.json')
URL_CREDENTIAL = re.compile(r'''https?://[^\s/:'"]+:[^\s/@'"]+@''')
PRIVATE_KEY = re.compile(r'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----')
SECRET_ASSIGNMENT = re.compile(
    r'''(?i)\b(?:api_key|auth_token|access_token|password|client_secret)\s*[=:]\s*['"]([^'"\r\n]{8,})['"]''')
SECRET_ALLOWLIST = re.compile(r'#\s*pragma:\s*allowlist secret\b', re.IGNORECASE)


def git(repo, *args, check=True):
    return subprocess.run(['git', '-C', str(repo), *args], env=dict(os.environ),
                          check=check, capture_output=True, timeout=30)


def credential_findings(data, path, source):
    utf16 = data[:2] in (b'\xff\xfe', b'\xfe\xff')
    if not utf16 and b'\0' in data[:8192]:
        return []  # 只扫描文本；带 BOM 的 Windows UTF-16 脚本需继续检查。
    if len(data) > MAX_SCAN_BYTES:
        return [dict(path=path, source=source, kind='scan_size_limit', line=0)]
    try:
        text = data.decode('utf-16') if utf16 else data.decode('utf8', errors='replace')
    except UnicodeDecodeError:
        return [dict(path=path, source=source, kind='encoding_error', line=0)]
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        if SECRET_ALLOWLIST.search(line):
            continue
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


def check_repository(repo: Path, *, scan_all: bool = False) -> dict:
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
        if source == 'working_tree' and scan_all:
            candidates |= set(tracked) | set(untracked)
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
                scan_names = staged if source == 'index' else (set(tracked) | set(untracked) if scan_all else changed)
                if name in scan_names:
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
    if scan_all:
        issues.extend(committed_credential_findings(repo, None))
    return dict(ok=not issues, config_checks=config_checks, issues=issues)


def _history_baseline() -> set[tuple[str, str, str, int]]:
    if not HISTORY_BASELINE.is_file():
        return set()
    try:
        payload = json.loads(HISTORY_BASELINE.read_text(encoding='utf-8'))
        if payload.get('version') != 1 or not isinstance(payload.get('findings'), list):
            raise ValueError('invalid schema')
        findings = set()
        for item in payload['findings']:
            if (not isinstance(item, list) or len(item) != 4
                    or not all(isinstance(value, str) for value in item[:3])
                    or type(item[3]) is not int or item[3] < 1):
                raise ValueError('invalid finding')
            findings.add((item[0], item[1], item[2], item[3]))
        return findings
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise ValueError(f'历史凭据扫描基线损坏: {HISTORY_BASELINE.name}') from error


def committed_credential_findings(repo: Path, base_ref: str | None,
                                  head_ref: str = 'HEAD') -> list[dict]:
    """扫描提交区间内每个提交实际新增/修改的 blob，防止中途删除掩盖凭据。"""
    repo = Path(repo).absolute()
    head = git(repo, 'rev-parse', '--verify', head_ref + '^{commit}').stdout.decode('ascii').strip()
    revision = head
    if base_ref is not None:
        base = git(repo, 'rev-parse', '--verify', base_ref + '^{commit}').stdout.decode('ascii').strip()
        revision = f'{base}..{head}'
    rows = git(repo, 'rev-list', '--reverse', '--parents', revision).stdout.decode('ascii').splitlines()
    findings = []
    for row in rows:
        commit, *parents = row.split()
        if parents:
            changed = git(repo, 'diff', '--name-only', '--diff-filter=ACMR', '-z', parents[0], commit).stdout
        else:
            changed = git(repo, 'diff-tree', '--root', '--no-commit-id', '--name-only', '-r',
                          '--diff-filter=ACMR', '-z', commit).stdout
        for raw in changed.split(b'\0'):
            if not raw:
                continue
            name = raw.decode('utf8')
            data = git(repo, 'show', f'{commit}:{name}').stdout
            for issue in credential_findings(data, name, 'committed_diff'):
                findings.append({**issue, 'commit': commit})
    baseline = _history_baseline()
    return [issue for issue in findings
            if (issue['commit'], issue['path'], issue['kind'], issue['line']) not in baseline]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checks-only', action='store_true', help='只做静态检查；不替代完整提交前门禁')
    parser.add_argument('--base-ref', help='CI 检查该提交以来已经提交的改动，补足干净检出场景')
    parser.add_argument('--scan-all', action='store_true', help='扫描当前树与全部可达提交历史，用于无法确定增量基线的 CI')
    parser.add_argument('--output', help='quick 验收输出目录，须在项目外')
    parser.add_argument('--workers', type=int, choices=range(1, 17), default=1, metavar='1..16',
                        help='快速验收工作进程数')
    parser.add_argument('--timing-baseline', type=Path, help='并行验收使用的历史 test-durations.json')
    args = parser.parse_args(argv)
    if args.timing_baseline and args.workers < 2:
        parser.error('--timing-baseline 需要至少两个工作进程')
    repo = Path(git(ROOT, 'rev-parse', '--show-toplevel').stdout.decode('utf8').strip())
    report = check_repository(repo, scan_all=args.scan_all)
    if args.base_ref and not args.scan_all:
        report['issues'].extend(committed_credential_findings(repo, args.base_ref))
        report['ok'] = not report['issues']
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
    command = [sys.executable, str(ROOT/'scripts/verify_delivery.py'), '--quick', '--redact-logs',
               '--output', str(output), '--workers', str(args.workers)]
    if args.timing_baseline:
        command += ['--timing-baseline', str(args.timing_baseline.resolve())]
    run = subprocess.run(command, cwd=ROOT)
    return run.returncode


if __name__ == '__main__':
    raise SystemExit(main())
