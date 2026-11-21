"""干净目录离线验收：示例、HTTP/CLI 冒烟、样本指纹与原有全部测试。"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import hashlib
import http.client
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.failure_evidence import (EvidenceResultMixin, Scrubber, RedactedLog,
                                      environment_secrets)

# 固定、可审计的快速集合；完整入口继续发现全部测试。
QUICK_TESTS = ('test_stage1.TestConfig', 'test_stage1.TestState',
               'test_stage1.TestEngine.test_resume_idempotent', 'test_stage1.TestEngine.test_approval_hash_check',
               'test_budget', 'test_api_adapter', 'test_cli_adapters',
               'test_cli_entry.TestCLIEntry.test_missing_run', 'test_execution_scope', 'test_fix_ninth.ConfigValidation',
               'test_phase2_operations', 'test_phase2_gates', 'test_schema_docs', 'test_test_layers',
               'test_phase5_reporting', 'test_phase5_http', 'test_phase5_ui',
               'test_model_settings', 'test_qoder_1189','test_claude_cli',
               'test_state_backup', 'test_policy_tuning', 'test_cli_current', 'test_docker_base',
               'test_grok_container', 'test_doubao_container',
               'test_failure_evidence.FailureEvidence.test_unreadable_directory_is_recorded_and_other_roots_continue')


def test_ids(suite):
    ids=[]
    def visit(node):
        if isinstance(node,unittest.TestSuite):
            for item in node:visit(item)
        else:ids.append(node.id())
    visit(suite)
    return sorted(ids)


def group_tests_by_module(ids):
    """按顶层测试模块分组，并按用例数降序排列以减少多进程尾延迟。"""
    groups = {}
    for identifier in ids:
        module = identifier.split('.', 1)[0]
        groups.setdefault(module, []).append(identifier)
    return sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))


def _run_suite_in_process(suite, log_path, evidence_dir, *, redact_logs=False):
    durations = []

    class Result(EvidenceResultMixin, unittest.TextTestResult):
        evidence_output = evidence_dir

        def startTest(self, test):
            self.started = time.monotonic()
            super().startTest(test)
            self.stream.flush()

        def stopTest(self, test):
            durations.append({'test': test.id(), 'duration_s': round(time.monotonic() - self.started, 3)})
            super().stopTest(test)
            self.stream.flush()

    with log_path.open('w', encoding='utf8') as raw_log:
        log = RedactedLog(raw_log) if redact_logs else raw_log
        try:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                result = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=Result).run(suite)
        finally:
            if redact_logs:
                log.finish()
    return {
        'tests_run': result.testsRun,
        'durations': durations,
        'failures': [{'test': t.id(), 'traceback': s} for t, s in result.failures],
        'errors': [{'test': t.id(), 'traceback': s} for t, s in result.errors],
        'skipped': [{'test': t.id(), 'reason': s} for t, s in result.skipped],
        'failure_evidence': getattr(result, '_test_evidence_failures', []),
    }


def _worker_main(spec_path, out_path, evidence_dir, *, redact_logs=False):
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / 'tests'))
    spec = json.loads(Path(spec_path).read_text(encoding='utf8'))
    suite = unittest.defaultTestLoader.loadTestsFromNames(spec['tests'])
    log_path = Path(spec['log_path'])
    data = _run_suite_in_process(suite, log_path, Path(evidence_dir), redact_logs=redact_logs)
    data['module'] = spec['module']
    Path(out_path).write_text(json.dumps(data, ensure_ascii=False), encoding='utf8')
    return 0


def _run_parallel_workers(grouped, output, log_path, evidence_dir, workers, *, redact_logs=False):
    work_dir = output / '_workers'
    work_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)

    def run_one(index, module, names):
        spec_file = work_dir / f'{index:03d}-{module}.in.json'
        out_file = work_dir / f'{index:03d}-{module}.out.json'
        worker_log = work_dir / f'{index:03d}-{module}.log'
        spec_file.write_text(
            json.dumps({'module': module, 'tests': names, 'log_path': str(worker_log)}, ensure_ascii=False),
            encoding='utf8',
        )
        cmd = [
            sys.executable, '-X', 'utf8', str(Path(__file__).resolve()),
            '--worker-input', str(spec_file),
            '--worker-output', str(out_file),
            '--worker-evidence', str(evidence_dir),
        ]
        if redact_logs:
            cmd.append('--redact-logs')
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                              encoding='utf8', errors='replace')
        if proc.returncode != 0 or not out_file.is_file():
            detail = (proc.stderr or proc.stdout or f'worker 退出码 {proc.returncode}').strip()
            return index, {
                'module': module,
                'tests_run': 0,
                'durations': [],
                'failures': [],
                'errors': [{'test': f'{module}.<worker>', 'traceback': detail}],
                'skipped': [],
                'failure_evidence': [],
                'log_path': worker_log,
            }
        payload = json.loads(out_file.read_text(encoding='utf8'))
        payload['log_path'] = worker_log
        return index, payload

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_one, idx, mod, names) for idx, (mod, names) in enumerate(grouped)]
        for future in concurrent.futures.as_completed(futures):
            idx, payload = future.result()
            results[idx] = payload

    merged = {'tests_run': 0, 'durations': [], 'failures': [], 'errors': [], 'skipped': [], 'failure_evidence': []}
    with log_path.open('w', encoding='utf8') as combined:
        for idx in range(len(grouped)):
            item = results[idx]
            merged['tests_run'] += item['tests_run']
            merged['durations'].extend(item['durations'])
            merged['failures'].extend(item['failures'])
            merged['errors'].extend(item['errors'])
            merged['skipped'].extend(item['skipped'])
            merged['failure_evidence'].extend(item['failure_evidence'])
            worker_log = item['log_path']
            if worker_log.is_file():
                combined.write(f'=== module: {item["module"]} ===\n')
                combined.write(worker_log.read_text(encoding='utf8', errors='replace'))
                combined.write('\n')
    return merged


def collect_tests(quick=False, suite=None):
    mode='fast' if quick else suite or 'full'
    sys.path.insert(0, str(ROOT/'tests'))
    if mode=='fast':
        return unittest.defaultTestLoader.loadTestsFromNames(QUICK_TESTS)
    full=unittest.defaultTestLoader.discover(str(ROOT/'tests'))
    if mode=='full':return full
    if mode!='slow':raise ValueError('未知测试分层')
    fast=set(test_ids(unittest.defaultTestLoader.loadTestsFromNames(QUICK_TESTS)))
    if not fast <= set(test_ids(full)):
        raise ValueError('快速集合含完整发现范围之外的测试')
    selected=[]
    def visit(node):
        if isinstance(node,unittest.TestSuite):
            for item in node:visit(item)
        elif node.id() not in fast:selected.append(node)
    visit(full)
    return unittest.TestSuite(selected)


def digest_files():
    # demo/ 属于交付声明范围，源码指纹必须覆盖，否则演示入口改动无法被前后比对发现。
    files=[p for directory in ('orchestrator','tests','prompts','scripts','demo')
           for p in (ROOT/directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    files+=list(ROOT.glob('*.example.toml'))
    files+=[p for p in (ROOT/'README.md',ROOT/'OPERATIONS.md',ROOT/'HISTORY.md',ROOT/'RELEASE_NOTES.md',ROOT/'DELIVERY.md',ROOT/'.gitattributes') if p.exists()]
    return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


def redact_verification(proof, secrets=()):
    """verification.json 中失败/错误正文按已知环境凭据与可识别令牌/URL 规则脱敏。

    只替换最终报告里的 traceback/reason 文本，不触碰测试结果、跳过标记或退出码；
    主回归日志由 unittest stream 直接写入，保持原始内容（脱敏落差由 DELIVERY 说明）。
    """
    scrub=Scrubber([*environment_secrets(),*secrets])
    for key in ('failures','errors'):
        for item in proof.get(key) or []:
            if isinstance(item,dict) and isinstance(item.get('traceback'),str):
                item['traceback']=scrub.text(item['traceback'])
    for item in proof.get('skipped') or []:
        if isinstance(item,dict) and isinstance(item.get('reason'),str):
            item['reason']=scrub.text(item['reason'])
    return proof


def smoke(output, redact_logs=False):
    from orchestrator.config import load_config
    from orchestrator.dashboard.server import create_server
    result={'python':platform.python_version(),'utf8_mode':sys.flags.utf8_mode,
            'filesystem_encoding':sys.getfilesystemencoding(),'git_available':bool(shutil.which('git')),
            'node_available':bool(shutil.which('node')),'examples':[],'cli':[]}
    if sys.version_info<(3,11) or not result['git_available'] or not result['node_available']:
        raise RuntimeError('完整离线验收需要 Python 3.11+、Git 和 Node.js')
    env=dict(os.environ)
    env.pop('PYTHONPATH',None)
    for tool in ('git','node'):
        run=subprocess.run([tool,'--version'],cwd=ROOT,env=env,capture_output=True,text=True,
                           encoding='utf8',errors='replace',timeout=15)
        if run.returncode:raise RuntimeError(tool+' 版本探测失败')
        result[tool+'_version']=run.stdout.strip()
    for path in sorted(ROOT.glob('*.example.toml')):
        config=load_config(path)
        for stage in config.stages:
            # 只核对实际提示词文件；内联提示词由配置加载器校验。
            if stage.prompt.startswith('prompts/') and not (ROOT/stage.prompt).is_file():
                raise RuntimeError('示例提示词缺失: '+stage.prompt)
        result['examples'].append({'name':path.name,'stages':len(config.stages)})
    provenance=json.loads((ROOT/'tests/fixtures/cli-capture-provenance.json').read_text(encoding='utf8'))
    for name,record in provenance.items():
        if hashlib.sha256((ROOT/'tests/fixtures'/name).read_bytes()).hexdigest()!=record['fixture_sha256']:
            raise RuntimeError('样本指纹不一致: '+name)
    result['fixture_provenance']='all_matched'
    run_id='delivery-'+os.urandom(4).hex()
    commands=[(['--help'],0),(['dashboard','--help'],0),
              (['run','workbench.example.toml','--task','离线交付验收','--run-id',run_id],11),
              (['status','workbench.example.toml','--run-id',run_id],0),
              (['approve','workbench.example.toml','--run-id',run_id,'--stage','acceptance'],0),
              (['resume','workbench.example.toml','--run-id',run_id],0)]
    for number,(args,expected) in enumerate(commands,1):
        run=subprocess.run([sys.executable,'-X','utf8','-m','orchestrator.cli',*args],cwd=ROOT,env=env,
                           capture_output=True,text=True,encoding='utf8',errors='replace',timeout=120)
        log = run.stdout+'\n'+run.stderr
        if redact_logs:
            log = Scrubber(environment_secrets()).text(log)
        (output/f'cli-{number}.log').write_text(log,encoding='utf8')
        result['cli'].append({'command':args[0],'expected_exit':expected,'actual_exit':run.returncode})
        if run.returncode!=expected:raise RuntimeError('CLI 冒烟失败: '+args[0])
    config=load_config(ROOT/'workbench.example.toml')
    state=json.loads((config.state_dir/'runs'/run_id/'state.json').read_text(encoding='utf8'))
    result['offline_stages']={name:{'status':s['status'],'attempt':s['current_attempt']}
                              for name,s in state['stages'].items()}
    if any(s['status']!='done' or s['current_attempt']!=1 for s in state['stages'].values()):
        raise RuntimeError('离线示例未复用有效 attempt')
    server=create_server(output/'smoke.sqlite3',[ROOT/'workbench.example.toml'],0)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=20)
    try:
        connection.request('POST','/api/session',json.dumps({'token':server.application.bootstrap}),
                           {'Content-Type':'application/json','Origin':server.application.origin})
        response=connection.getresponse();session=json.loads(response.read())
        if response.status!=200 or not session.get('csrf'):raise RuntimeError('HTTP 登录失败')
        cookie=response.getheader('Set-Cookie').split(';')[0]
        connection.request('GET','/api/projects',headers={'Cookie':cookie})
        response=connection.getresponse();projects=json.loads(response.read())
        if response.status!=200 or len(projects.get('projects',[]))!=1:
            raise RuntimeError('HTTP 项目列表失败')
        result['http']={'session_status':200,'projects_status':200,'projects':1,'loopback_only':True}
    finally:
        connection.close();server.application.closed.set()
        server.shutdown();server.server_close();thread.join(5)
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=False)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--quick', action='store_true', help='fast 的别名，目标总耗时不超过 120 秒')
    mode.add_argument('--suite', choices=('fast','slow','full'), default='full', help='显式测试分层；默认 full')
    parser.add_argument('--workers', type=int, default=1,
                        help='按测试模块并发执行的工作进程数（1..16），默认 1；Windows 受进程安全扫描竞争影响建议保持 1')
    parser.add_argument('--slow-threshold', type=float, default=2, help='慢用例记录阈值，秒')
    parser.add_argument('--redact-logs', action='store_true', help='对回归日志与 CLI 冒烟日志脱敏')
    parser.add_argument('--allow-linux-windows-skips', action='store_true',
                        help='仅 Linux 全量矩阵允许已登记的 Windows 专用跳过')
    parser.add_argument('--worker-input', help=argparse.SUPPRESS)
    parser.add_argument('--worker-output', help=argparse.SUPPRESS)
    parser.add_argument('--worker-evidence', help=argparse.SUPPRESS)
    args=parser.parse_args(argv)
    if args.worker_input:
        return _worker_main(args.worker_input, args.worker_output, args.worker_evidence,
                            redact_logs=args.redact_logs)
    if not args.output:
        parser.error('--output 为必填参数')
    if args.workers < 1 or args.workers > 16:
        parser.error('--workers 必须在 1 到 16 之间')
    if not math.isfinite(args.slow_threshold) or args.slow_threshold<=0:
        parser.error('--slow-threshold 必须为正数')
    selected_mode='fast' if args.quick else args.suite
    if args.allow_linux_windows_skips and (sys.platform != 'linux' or selected_mode != 'full'):
        parser.error('--allow-linux-windows-skips 只适用于 Linux full')
    output=Path(args.output).resolve()
    if output.is_relative_to(ROOT):raise ValueError('验证输出目录必须位于项目之外')
    output.mkdir(parents=True,exist_ok=True)
    os.chdir(ROOT)
    before=digest_files()
    (output/'source-before.json').write_text(json.dumps(before,ensure_ascii=False,indent=2),encoding='utf8')
    started=time.monotonic()
    info=smoke(output, redact_logs=args.redact_logs)
    (output/'smoke.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'smoke':'passed','examples':len(info['examples']),'git':info['git_version'],
                      'node':info['node_version']},ensure_ascii=True),flush=True)
    suite=collect_tests(args.quick, suite=selected_mode)
    ids=test_ids(suite)
    collected=suite.countTestCases()
    (output/'test-ids.json').write_text(json.dumps(ids,indent=2),encoding='utf8')
    print(json.dumps({'suite':selected_mode,'tests_collected':collected,'workers':args.workers}),flush=True)
    log_name='quick-regression.log' if args.quick else selected_mode+'-regression.log'
    evidence_dir=output/'failure-evidence'
    if args.workers > 1:
        run_data=_run_parallel_workers(
            group_tests_by_module(ids), output, output/log_name, evidence_dir,
            args.workers, redact_logs=args.redact_logs,
        )
    else:
        run_data=_run_suite_in_process(suite, output/log_name, evidence_dir, redact_logs=args.redact_logs)
    durations=run_data['durations']
    info['failure_evidence']=run_data['failure_evidence']
    after=digest_files()
    (output/'source-after.json').write_text(json.dumps(after,ensure_ascii=False,indent=2),encoding='utf8')
    (output/'test-durations.json').write_text(json.dumps(durations,ensure_ascii=False,indent=2),encoding='utf8')
    slow=sorted((r for r in durations if r['duration_s']>=args.slow_threshold),key=lambda r:r['duration_s'],reverse=True)
    (output/'slow-tests.json').write_text(json.dumps({'threshold_s':args.slow_threshold,'tests':slow},indent=2),encoding='utf8')
    proof={'mode':'quick' if args.quick else selected_mode, 'suite':selected_mode, 'workers':args.workers,
           'tests_collected':collected, 'log_file':log_name, 'slow_test_count':len(slow),
           'tests_run':run_data['tests_run'],'failures':run_data['failures'],
           'errors':run_data['errors'],'skipped':run_data['skipped'],
           'source_unchanged':before==after,'duration_s':round(time.monotonic()-started,3),
           'runtime':info,'source_files':len(before),'no_model_calls_requested':True}
    redact_verification(proof)
    proof['redaction']={'verification_json':'known_credentials_and_token_patterns',
                        'full_regression_log':'known_credentials_and_token_patterns' if args.redact_logs else 'raw_not_redacted',
                        'cli_logs':'known_credentials_and_token_patterns' if args.redact_logs else 'raw_not_redacted'}
    permitted_skips = set()
    if args.allow_linux_windows_skips:
        permitted_skips = {
            (f'test_process_start.ProcessStart.{name}', 'Windows Job 启动顺序回归')
            for name in ('test_cmd_child_cannot_escape_before_delayed_job_attachment',
                         'test_fast_resume_path_does_not_enumerate_system_threads',
                         'test_fast_resume_falls_back_to_thread_scan_when_unavailable',
                         'test_resume_failure_terminates_owned_process_and_leaves_no_job_handle',
                         'test_failed_resume_closes_job_and_kills_suspended_process',
                         'test_short_command_executes_after_job_attachment')
        } | {
            (f'test_codex_windows_profile.CodexWindowsProfile.{name}', 'Windows 沙箱配置仅适用于 Windows')
            for name in ('test_isolated_profile_keeps_explicit_backend_without_global_extensions',
                         'test_backend_change_invalidates_profile_fingerprint',
                         'test_invalid_backend_and_shape_are_rejected_before_execution')
        }
    unexpected_skips = [(entry['test'], entry['reason']) for entry in run_data['skipped']
                        if (entry['test'], entry['reason']) not in permitted_skips]
    if args.allow_linux_windows_skips:
        proof['unexpected_skips'] = [entry for entry in proof['skipped']
                                     if (entry['test'], entry['reason']) in unexpected_skips]
    all_passed = (
        not run_data['failures']
        and not run_data['errors']
        and run_data['tests_run'] == collected
    )
    proof['exit_code']=0 if all_passed and before==after and not unexpected_skips else 1
    if selected_mode=='fast':
        proof['quick_budget_s']=120
        proof['quick_within_budget']=proof['duration_s']<=120
        if not proof['quick_within_budget']:proof['exit_code']=1
    if selected_mode=='full':
        proof['full_target_s']=480
        proof['full_within_target']=proof['duration_s']<=480
    (output/'verification.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({k:v for k,v in proof.items() if k!='runtime'},ensure_ascii=True),flush=True)
    return proof['exit_code']


if __name__=='__main__':
    raise SystemExit(main())
