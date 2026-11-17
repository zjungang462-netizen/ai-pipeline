"""固定 HEAD 的本地发布：Git 原始字节→完整验收→ZIP→独立解压完整复验。"""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(raw): return hashlib.sha256(raw).hexdigest()
def encoded(value): return (json.dumps(value, ensure_ascii=False, indent=2)+'\n').encode('utf8')


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, timeout=60).stdout


def release_version(source):
    node = ast.parse(source.decode('utf8'))
    for item in node.body:
        if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__version__' for t in item.targets):
            value = ast.literal_eval(item.value)
            import re
            if isinstance(value, str) and re.fullmatch(r'\d+\.\d+\.\d+', value): return value
    raise ValueError('版本声明必须为三段数值字符串')


def changelog(repo, commit):
    raw = git(repo, 'log', '--format=%h%x09%s', '--no-merges', '-30', commit, '--', 'agent-orchestrator').decode('utf8')
    return '# 提交变更记录\n\n自动记录最近最多 30 个项目提交；功能说明与验证边界见 RELEASE_NOTES/DELIVERY。\n\n'+''.join('- `'+line.split('\t',1)[0]+'` '+line.split('\t',1)[1]+'\n' for line in raw.splitlines() if '\t' in line)


def source_files(repo, commit):
    raw = git(repo, '-c', 'core.autocrlf=false', '-c', 'core.eol=lf', 'archive', '--format=zip', commit, 'agent-orchestrator')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        files = {name: archive.read(name) for name in archive.namelist() if not name.endswith('/')}
    for name, body in files.items():
        if git(repo, 'cat-file', 'blob', commit+':'+name) != body:
            raise ValueError('导出与 Git blob 字节不一致：'+name)
    return files


def write_zip(path, files):
    with zipfile.ZipFile(path, 'x', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, raw in sorted(files.items()):
            # 原始路径必须位于项目或附带元数据中，拒绝任意绝对路径。
            if name.startswith('/') or ':' in name or '..' in Path(name).parts or '\\' in name:
                raise ValueError('包文件名非法')
            item = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o100644 << 16
            archive.writestr(item, raw)


def check_tree(root, expected):
    for name, raw in expected.items():
        if (root/name).read_bytes() != raw: raise ValueError('源码字节变化：'+name)


def verify(root, output):
    run = subprocess.run([sys.executable, '-X', 'utf8', 'scripts/verify_delivery.py',
                          '--redact-logs', '--output', str(output)], cwd=root/'agent-orchestrator')
    proof_path = output/'verification.json'
    if run.returncode or not proof_path.is_file(): raise ValueError('完整离线验收失败，保留证据：'+str(output))
    proof = json.loads(proof_path.read_bytes())
    if (proof.get('mode') != 'full' or proof.get('exit_code') != 0 or proof.get('tests_run', 0) <= 0
            or not proof.get('source_unchanged') or any(proof.get(k) for k in ('failures','errors','skipped'))):
        raise ValueError('验收结果不满足完整发布条件')
    return proof


def build_release(repo, output, verifier=verify):
    repo, output = Path(repo).resolve(), Path(output).absolute()
    if output.resolve().is_relative_to(repo): raise ValueError('发布输出必须位于仓库外')
    if output.exists(): raise ValueError('发布输出必须为全新目录，不覆盖已有交付')
    if git(repo, 'status', '--porcelain=v1'): raise ValueError('只发布干净工作树的 HEAD')
    commit = git(repo, 'rev-parse', 'HEAD').decode().strip()
    files = source_files(repo, commit)
    version = release_version(files['agent-orchestrator/orchestrator/_version.py'])
    output.mkdir(parents=True)
    first = output/'source-check';first.mkdir()
    for name, raw in files.items():
        path = first/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    first_proof = verifier(first, output/'verification-source')
    check_tree(first, files)
    metadata = {'delivery-metadata/CHANGELOG.md': changelog(repo, commit).encode('utf8')}
    evidence_names = ('verification.json','smoke.json','source-before.json','source-after.json',
                      'test-durations.json','slow-tests.json','test-ids.json')
    for name in evidence_names: metadata['delivery-metadata/'+name] = (output/'verification-source'/name).read_bytes()
    manifest = dict(schema_version=1, version=version, commit=commit,
                    files={name:sha(body) for name,body in sorted(files.items())},
                    metadata_files={name:sha(body) for name,body in sorted(metadata.items())},
                    verification=dict(tests_run=first_proof['tests_run'],exit_code=0,source_unchanged=True,
                                      file='delivery-metadata/verification.json'),
                    exclusions=['credentials','runtime_state','raw_logs','.git','worktrees'],
                    manifest_excludes_itself=True,
                    verification_scope='full_source_check; independent_package_check_sidecar')
    candidate = output/'candidate.zip'
    contents = {**files, **metadata, 'SNAPSHOT.json':encoded(manifest)}
    write_zip(candidate, contents)
    second = output/'package-check';second.mkdir()
    with zipfile.ZipFile(candidate) as archive:
        if archive.testzip() or len(archive.namelist()) != len(set(archive.namelist())):
            raise ValueError('包 CRC 或重名检查失败')
        if set(archive.namelist()) != set(contents): raise ValueError('包文件集合不匹配')
        if not all(archive.read(name) == body for name, body in contents.items()): raise ValueError('包内容不匹配')
        archive.extractall(second)
    check_tree(second, files)
    second_proof = verifier(second, output/'verification-package')
    check_tree(second, files)
    if first_proof['tests_run'] != second_proof['tests_run']:
        raise ValueError('源码与最终包测试数量不一致')
    if git(repo, 'rev-parse', 'HEAD').decode().strip() != commit or git(repo,'status','--porcelain=v1'):
        raise ValueError('发布期间 HEAD 或工作树改变，候选包不发布')
    package = output/f'agent-orchestrator-{version}-{commit[:8]}.zip'
    candidate.rename(package)
    digest = sha(package.read_bytes())
    (output/(package.name+'.sha256')).write_text(digest+'  '+package.name+'\n',encoding='ascii')
    result = dict(version=version, commit=commit, package=str(package), sha256=digest,
                  source_files=len(files), tests_run=second_proof['tests_run'], exit_code=0,
                  source_verification='verification-source/verification.json',
                  package_verification='verification-package/verification.json',
                  two_full_verifications=True, published_remote=False)
    (output/'release-result.json').write_bytes(encoded(result))
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    try:
        repo=Path(git(ROOT,'rev-parse','--show-toplevel').decode().strip())
        print(json.dumps(build_release(repo,args.output),ensure_ascii=True))
        return 0
    except (OSError,ValueError,subprocess.SubprocessError) as error:
        print('发布失败：'+str(error),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
