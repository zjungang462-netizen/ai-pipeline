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
import re
import shutil
import signal
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

# 快速集保留断言、协议和入口检查。多轮演练及竞技场集成由 CI 的 full 作业完整覆盖。
QUICK_TESTS = (
    'test_stage1.TestConfig', 'test_stage1.TestState',
    'test_stage1.TestEngine.test_approval_hash_check',
    'test_budget.TestBudget.test_invalid_call_budgets_are_rejected_by_config',
    'test_budget.TestBudget.test_run_token_limit_rejected',
    'test_api_adapter', 'test_cli_adapters', 'test_dsh_protocol', 'test_remote_worker',
    'test_fix_ninth.ConfigValidation',
    'test_cli_entry.TestCLIEntry.test_piped_chinese_output_survives_narrow_encoding',
    'test_cli_entry.TestCLIEntry.test_dashboard_startup_language_selects_url_and_message',
    'test_cli_entry.TestCLIEntry.test_dashboard_forwards_explicit_host_to_server',
    'test_cli_entry.TestCLIEntry.test_dashboard_rejects_empty_host',
    'test_cli_entry.TestCLIEntry.test_english_configuration_and_deploy_errors_are_localized',
    'test_cli_entry.TestCLIEntry.test_english_required_configuration_errors_are_localized',
    'test_cli_reliability.CLIReliability.test_live_files_exist_before_cli_exit_and_never_contain_secret',
    'test_execution_scope.ExecutionScope.test_arena_start_and_apply_refuse_analysis_project',
    'test_execution_scope.ExecutionScope.test_arena_refuses_fixed_test_project',
    'test_execution_scope.ExecutionScope.test_arena_refuses_project_with_simulated_stages',
    'test_execution_scope.ExecutionScope.test_only_exact_offline_showcase_is_offered_in_project_list',
    'test_execution_scope.ExecutionScope.test_second_run_for_busy_project_names_the_blocking_run',
    'test_execution_scope.ExecutionScope.test_task_creation_and_room_scope_frontend',
    'test_execution_scope.ExecutionScope.test_projects_stay_available_when_one_config_is_missing',
    'test_navigation_reading',
    'test_deterministic.TestNormalizeRepairDemo.test_fixed_assertions_reject_empty_and_incorrect_implementations',
    'test_deterministic.TestNormalizeRepairDemo.test_scripted_review_rejects_invalid_code_without_executing_it',
    'test_deterministic.TestNormalizeRepairDemo.test_demo_human_gate_exception_does_not_relax_ordinary_approval',
    'test_phase2_operations.Operations.test_doctor_valid_config_never_creates_workspace_or_executes_commands',
    'test_phase2_gates.Gates.test_empty_git_config_env_is_preserved_after_patch_restore',
    'test_phase2_gates.Gates.test_verification_redacts_failure_log_and_preserves_nonzero_exit',
    'test_phase2_gates.Gates.test_verification_smoke_uses_isolated_workspace_and_state',
    'test_phase2_gates.Gates.test_parallel_worker_malformed_output_is_reported_without_losing_valid_shard',
    'test_phase2_gates.Gates.test_parallel_worker_timeout_terminates_process_and_records_failure',
    'test_phase2_gates.Gates.test_parallel_suite_deadline_does_not_start_queued_workers',
    'test_test_layers.TestLayers.test_fast_and_slow_partition_the_full_collection',
    'test_test_layers.TestLayers.test_quick_uses_the_same_tests_as_fast',
    'test_test_layers.TestLayers.test_module_timings_balance_whole_modules_and_fill_missing_costs',
    'test_test_layers.TestLayers.test_checked_in_module_timing_baseline_loads',
    'test_test_layers.TestLayers.test_module_timing_loader_rejects_invalid_mappings',
    'test_phase5_ui', 'test_phase5_http.Phase5HTTP.test_i18n_static_is_served_as_executable_javascript',
    'test_phase5_http.Phase5HTTP.test_explicit_wildcard_bind_uses_configured_origin_and_host_checks',
    'test_phase5_http.Phase5HTTP.test_container_entry_starts_dashboard_and_excludes_local_state',
    'test_cli_current',
    'test_model_settings.ModelSettings.test_options_reject_commands_credentials_and_unknown_tools',
    'test_state_migration',
    'test_streaming_discussion.StreamingDiscussionTests.test_parse_sse_stream_accumulates_tokens_usage_and_rejects_incomplete_stream',
    'test_failure_evidence.FailureEvidence.test_unreadable_directory_is_recorded_and_other_roots_continue',
    'test_failure_evidence.DeliveryVerification.test_unexpected_skips_are_identified_without_linux_allowlist',
    'test_failure_evidence.DeliveryVerification.test_linux_allowlist_permits_only_registered_test_and_reason',
)


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


def schedule_shards(grouped, timing_rows, workers):
    """按历史耗时均衡分片，保留完整测试模块并减少 Windows 子进程启动次数。"""
    import statistics

    if not timing_rows or not grouped:
        return grouped
    if isinstance(timing_rows, dict):
        fallback = statistics.median(timing_rows.values())
        module_costs = [(timing_rows.get(module, fallback), names) for module, names in grouped]
    else:
        timings = {row['test']: row['duration_s'] for row in timing_rows}
        fallback = statistics.median(timings.values())
        module_costs = [(sum(timings.get(name, fallback) for name in names), names) for _, names in grouped]
    module_costs.sort(key=lambda module: -module[0])
    shards = [[] for _ in range(min(workers, len(module_costs)))]
    totals = [0.0] * len(shards)
    for duration, names in module_costs:
        index = min(range(len(shards)), key=lambda i: (totals[i], len(shards[i])))
        shards[index].extend(names)
        totals[index] += duration
    return [(f'shard-{index + 1:02d}', names) for index, names in enumerate(shards)]


def load_test_timings(path):
    rows = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(rows, dict):
        if not rows or any(
            not isinstance(module, str) or not module
            or type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0
            for module, duration in rows.items()
        ):
            raise ValueError('模块耗时文件应为非空模块名到有限非负秒数的映射')
        return rows
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get('test'), str)
        or type(row.get('duration_s')) not in (int, float) or not math.isfinite(row['duration_s'])
        or row['duration_s'] < 0 for row in rows
    ):
        raise ValueError('耗时文件应为验收生成的 test-durations.json 或模块耗时映射')
    return rows


def write_isolated_smoke_config(source: Path, output: Path) -> Path:
    output = Path(output).resolve()
    text = Path(source).read_text(encoding='utf8')
    paths = {
        'task_workdir': output / 'smoke-workspace',
        'state_dir': output / 'smoke-state',
    }
    for key, path in paths.items():
        pattern = re.compile(rf'(?m)^([ \t]*{re.escape(key)}[ \t]*=)[^\r\n]*')
        text, count = pattern.subn(lambda match: match.group(1) + json.dumps(str(path), ensure_ascii=False), text)
        if count != 1:
            raise RuntimeError(f'冒烟配置必须包含唯一的 {key}')
    config_dir = output / 'smoke-config'
    config_dir.mkdir(parents=True, exist_ok=True)
    target = config_dir / Path(source).name
    target.write_text(text, encoding='utf8')
    from orchestrator.config import load_config
    config = load_config(target)
    if config.task_workdir != paths['task_workdir'] or config.state_dir != paths['state_dir']:
        raise RuntimeError('冒烟配置隔离路径未生效')
    return target


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


def _worker_failure(module, detail, log_path):
    return {
        'module': module,
        'tests_run': 0,
        'durations': [],
        'failures': [],
        'errors': [{'test': f'{module}.<worker>', 'traceback': detail}],
        'skipped': [],
        'failure_evidence': [],
        'log_path': log_path,
    }


def _validate_worker_payload(payload, module):
    list_fields = ('durations', 'failures', 'errors', 'skipped', 'failure_evidence')
    if (not isinstance(payload, dict) or payload.get('module') != module
            or type(payload.get('tests_run')) is not int or payload['tests_run'] < 0
            or not all(isinstance(payload.get(key), list) for key in list_fields)):
        raise ValueError('worker 输出结构无效')
    if any(
        not isinstance(row, dict) or not isinstance(row.get('test'), str)
        or type(row.get('duration_s')) not in (int, float)
        or not math.isfinite(row.get('duration_s')) or row.get('duration_s') < 0
        for row in payload['durations']
    ):
        raise ValueError('worker 耗时记录无效')
    if any(
        not isinstance(row, dict) or not isinstance(row.get('test'), str)
        or not isinstance(row.get('traceback'), str)
        for key in ('failures', 'errors') for row in payload[key]
    ):
        raise ValueError('worker 失败或错误记录无效')
    if any(
        not isinstance(row, dict) or not isinstance(row.get('test'), str)
        or not isinstance(row.get('reason'), str)
        for row in payload['skipped']
    ):
        raise ValueError('worker 跳过记录无效')
    if any(
        not isinstance(row, dict) or not isinstance(row.get('test'), str)
        or (row.get('evidence') is not None and not isinstance(row.get('evidence'), str))
        or (row.get('capture_error') is not None and not isinstance(row.get('capture_error'), str))
        for row in payload['failure_evidence']
    ):
        raise ValueError('worker 失败证据记录无效')


def _terminate_worker_process(proc, env):
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _run_worker_command(cmd, *, cwd, env, timeout):
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding='utf8', errors='replace', start_new_session=os.name != 'nt',
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_worker_process(proc, env)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
            proc.wait(timeout=5)
        raise
    except BaseException:
        _terminate_worker_process(proc, env)
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _run_parallel_workers(grouped, output, log_path, evidence_dir, workers, *, redact_logs=False,
                          worker_timeout=600, deadline=None):
    work_dir = output / '_workers'
    work_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)

    def run_one(index, module, names):
        spec_file = work_dir / f'{index:03d}-{module}.in.json'
        out_file = work_dir / f'{index:03d}-{module}.out.json'
        worker_log = work_dir / f'{index:03d}-{module}.log'
        try:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return index, _worker_failure(module, '验收总时限已到，未启动此模块', worker_log)
            timeout = worker_timeout if remaining is None else min(worker_timeout, remaining)
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
            proc = _run_worker_command(cmd, cwd=ROOT, env=env, timeout=timeout)
            if proc.returncode != 0 or not out_file.is_file():
                detail = (proc.stderr or proc.stdout or f'worker 退出码 {proc.returncode}').strip()
                return index, _worker_failure(module, detail, worker_log)
            payload = json.loads(out_file.read_text(encoding='utf8'))
            _validate_worker_payload(payload, module)
            payload['log_path'] = worker_log
            return index, payload
        except subprocess.TimeoutExpired as error:
            return index, _worker_failure(module, f'验收 worker 超时（上限 {error.timeout:g} 秒）', worker_log)
        except Exception as error:
            return index, _worker_failure(module, f'{type(error).__name__}: {error}', worker_log)

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_one, idx, mod, names): idx
                   for idx, (mod, names) in enumerate(grouped)}
        for future in concurrent.futures.as_completed(futures):
            idx = futures[future]
            try:
                _, payload = future.result()
            except Exception as error:
                module = grouped[idx][0]
                payload = _worker_failure(module, f'{type(error).__name__}: {error}',
                                          work_dir / f'{idx:03d}-{module}.log')
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
    output = Path(output).resolve()
    if output.is_relative_to(ROOT):
        raise ValueError('冒烟输出目录必须位于项目之外')
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
    config_path=write_isolated_smoke_config(ROOT/'workbench.example.toml',output)
    config_arg=str(config_path)
    run_id='delivery-'+os.urandom(4).hex()
    commands=[(['--help'],0),(['dashboard','--help'],0),
              (['--lang','en','--help'],0),
              (['--lang','en','doctor','kiroween-demo.example.toml'],0),
              (['run',config_arg,'--task','离线交付验收','--run-id',run_id],11),
              (['status',config_arg,'--run-id',run_id],0),
              (['approve',config_arg,'--run-id',run_id,'--stage','acceptance'],0),
              (['resume',config_arg,'--run-id',run_id],0)]
    for number,(args,expected) in enumerate(commands,1):
        run=subprocess.run([sys.executable,'-X','utf8','-m','orchestrator.cli',*args],cwd=ROOT,env=env,
                           capture_output=True,text=True,encoding='utf8',errors='replace',timeout=120)
        log = run.stdout+'\n'+run.stderr
        if redact_logs:
            log = Scrubber(environment_secrets()).text(log)
        (output/f'cli-{number}.log').write_text(log,encoding='utf8')
        result['cli'].append({'command':args[0],'expected_exit':expected,'actual_exit':run.returncode})
        if run.returncode!=expected:raise RuntimeError('CLI 冒烟失败: '+args[0])
    config=load_config(config_path)
    state=json.loads((config.state_dir/'runs'/run_id/'state.json').read_text(encoding='utf8'))
    result['offline_stages']={name:{'status':s['status'],'attempt':s['current_attempt']}
                              for name,s in state['stages'].items()}
    if any(s['status']!='done' or s['current_attempt']!=1 for s in state['stages'].values()):
        raise RuntimeError('离线示例未复用有效 attempt')
    result['smoke_isolation']={
        'config':config_path.relative_to(output).as_posix(),
        'workspace':config.task_workdir.relative_to(output).as_posix(),
        'state':config.state_dir.relative_to(output).as_posix(),
        'outside_source_tree':not config.task_workdir.is_relative_to(ROOT) and not config.state_dir.is_relative_to(ROOT),
    }
    server=create_server(output/'smoke.sqlite3',[config_path],0)
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


def permitted_platform_skips(allow_linux_windows_skips):
    if not allow_linux_windows_skips:
        return set()
    return {
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


def unexpected_skip_entries(skipped, permitted):
    return [(entry['test'], entry['reason']) for entry in skipped
            if (entry['test'], entry['reason']) not in permitted]


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=False)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--quick', action='store_true', help='fast 的别名，目标总耗时不超过 120 秒')
    mode.add_argument('--suite', choices=('fast','slow','full'), default='full', help='显式测试分层；默认 full')
    parser.add_argument('--workers', type=int, default=1,
                        help='按测试模块并发执行的工作进程数（1..16），默认 1')
    parser.add_argument('--worker-timeout', type=float, default=600,
                        help='并行验收中单个工作进程的最长等待秒数，超时终止进程树并记录失败')
    parser.add_argument('--suite-timeout', type=float, default=1200,
                        help='并行回归阶段的总时限秒数；单进程模式不强制此限制')
    parser.add_argument('--timing-baseline', type=Path,
                        help='并行时按 test-durations.json 或模块耗时映射排程')
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
    if not math.isfinite(args.worker_timeout) or args.worker_timeout <= 0:
        parser.error('--worker-timeout 必须为正数')
    if not math.isfinite(args.suite_timeout) or args.suite_timeout <= 0:
        parser.error('--suite-timeout 必须为正数')
    timing_rows = []
    if args.timing_baseline:
        if args.workers < 2:
            parser.error('--timing-baseline 需要至少两个工作进程')
        try:
            timing_rows = load_test_timings(args.timing_baseline)
        except (OSError, ValueError) as error:
            parser.error(str(error))
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
            schedule_shards(group_tests_by_module(ids), timing_rows, args.workers), output, output/log_name, evidence_dir,
            args.workers, redact_logs=args.redact_logs, worker_timeout=args.worker_timeout,
            deadline=time.monotonic() + args.suite_timeout,
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
           'worker_timeout_s':args.worker_timeout, 'suite_timeout_s':args.suite_timeout,
           'timeout_enforced':args.workers>1,
           'tests_collected':collected, 'log_file':log_name, 'slow_test_count':len(slow),
           'scheduling': 'duration_balanced_shards' if timing_rows else 'module_test_count',
           'tests_run':run_data['tests_run'],'failures':run_data['failures'],
           'errors':run_data['errors'],'skipped':run_data['skipped'],
           'source_unchanged':before==after,'duration_s':round(time.monotonic()-started,3),
           'runtime':info,'source_files':len(before),'no_model_calls_requested':True}
    redact_verification(proof)
    proof['redaction']={'verification_json':'known_credentials_and_token_patterns',
                        'full_regression_log':'known_credentials_and_token_patterns' if args.redact_logs else 'raw_not_redacted',
                        'cli_logs':'known_credentials_and_token_patterns' if args.redact_logs else 'raw_not_redacted'}
    permitted_skips = permitted_platform_skips(args.allow_linux_windows_skips)
    unexpected_skips = set(unexpected_skip_entries(run_data['skipped'], permitted_skips))
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
