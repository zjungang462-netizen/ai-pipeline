"""快速门禁、增量扫描和日志保留的真实文件/流边界回归。"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from scripts.failure_evidence import RedactedLog, capture
from scripts.precommit import check_repository, credential_findings
from scripts import verify_delivery
from orchestrator.config import load_config
from orchestrator.maintenance import prune_evidence


class Gates(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)

    def test_redaction_handles_every_chunk_boundary_and_flush(self):
        secret='cross-line-\n短密钥-123456'
        token='sk-'+'A'*28
        url='http://'+'user:password'+'@localhost/path'
        text='before '+secret+' '+token+' '+url+' after\n'
        for size in (1,2,7,31,4096):
            with self.subTest(size=size):
                out=io.StringIO();stream=RedactedLog(out,secrets=[secret])
                for n in range(0,len(text),size):
                    stream.write(text[n:n+size]);stream.flush()
                stream.finish();stream.finish()
                value=out.getvalue()
                for private in (secret,token,'user:password','短密钥'):
                    self.assertNotIn(private,value)
                self.assertIn('before',value);self.assertIn('after',value)

    def test_flush_does_not_publish_partial_token(self):
        out=io.StringIO();stream=RedactedLog(out)
        stream.write('sk-'+'K'*8);stream.flush()
        self.assertEqual(out.getvalue(),'')
        stream.write('K'*20);stream.finish()
        self.assertEqual(out.getvalue(),'[REDACTED]')

    def test_oversize_line_is_bounded_and_omitted(self):
        out=io.StringIO();stream=RedactedLog(out)
        stream.write('prefix '+('sk-'+'Z'*100000)+'\nnext line\n');stream.finish()
        self.assertNotIn('ZZZZ',out.getvalue())
        self.assertIn('oversize',out.getvalue())
        self.assertTrue(out.getvalue().endswith('next line\n'))
        self.assertLessEqual(len(stream.pending)+len(stream.line),128)

    def test_short_environment_secret_and_json_escape_redacted(self):
        secret='短密'
        out=io.StringIO()
        with mock.patch.dict(os.environ,{'TEST_PASSWORD':secret}):
            stream=RedactedLog(out)
        stream.write(secret+' '+json.dumps(secret,ensure_ascii=True));stream.finish()
        self.assertNotIn(secret,out.getvalue())
        self.assertNotIn('\\u77ed',out.getvalue())

    def test_capture_prunes_expired_bundles_preserves_manual_files(self):
        root=self.root/'source';root.mkdir()
        output=self.root/'evidence';old=output/'old';old.mkdir(parents=True)
        (old/'evidence.json').write_text('{}',encoding='utf8')
        for p in (old/'evidence.json',old):os.utime(p,(time.time()-40*86400,)*2)
        manual=output/'manual.txt';manual.write_text('keep',encoding='utf8')
        saved=capture('failure',[root],output)
        self.assertTrue(saved.is_file())
        self.assertFalse(old.exists())
        self.assertEqual(manual.read_text(encoding='utf8'),'keep')

    def git(self,*args):
        return subprocess.run(['git','-C',str(self.root),*args],env=dict(os.environ),check=True,capture_output=True)

    def repo(self):
        self.git('init','-q');self.git('config','user.name','test')
        self.git('config','user.email','test@example.invalid')
        self.git('commit','--allow-empty','-qm','base')

    def test_precommit_catches_staged_secret_even_when_worktree_is_clean(self):
        self.repo()
        token='sk-'+'B'*30
        path=self.root/'source.py';path.write_text('key="'+token+'"\n',encoding='utf8')
        self.git('add','source.py')
        path.write_text('key=""\n',encoding='utf8')
        report=check_repository(self.root)
        self.assertFalse(report['ok'])
        self.assertTrue(any(r['source']=='index' and r['kind']=='token_pattern' for r in report['issues']))
        self.assertNotIn(token,json.dumps(report))

    def test_precommit_catches_untracked_secret_url_and_private_key(self):
        self.repo()
        content='http://'+'user:password'+'@localhost\n-----BEGIN '+'PRIVATE KEY-----\n'
        (self.root/'new.txt').write_text(content,encoding='utf8')
        report=check_repository(self.root)
        self.assertFalse(report['ok'])
        self.assertTrue({'url_credentials','private_key'} <= {r['kind'] for r in report['issues']})

    def test_precommit_rejects_unknown_config_in_index_and_worktree(self):
        self.repo()
        path=self.root/'pipeline.example.toml'
        path.write_text('task_workdir="w"\nstate_dir="s"\nwatch_enforc=true\n',encoding='utf8')
        self.git('add',path.name)
        report=check_repository(self.root)
        self.assertFalse(report['ok'])
        self.assertTrue({'index','working_tree'} <= {r['source'] for r in report['issues'] if r['kind']=='config_or_file'})

    def test_precommit_accepts_valid_config_and_does_not_read_ignored_config(self):
        self.repo()
        (self.root/'.gitignore').write_text('private.toml\n',encoding='utf8')
        (self.root/'private.toml').write_text('invalid local data',encoding='utf8')
        (self.root/'pipeline.example.toml').write_text('''task_workdir="w"
state_dir="s"
[[stages]]
name="t"
kind="test"
test_cmd=["python"]
''',encoding='utf8')
        report=check_repository(self.root)
        self.assertTrue(report['ok'],report)
        self.assertEqual(report['config_checks'],1)

    def test_precommit_caps_text_size(self):
        report=credential_findings(b'x'*(2*1024*1024+1),'big.txt','working_tree')
        self.assertEqual(report[0]['kind'],'scan_size_limit')

    def test_precommit_rejects_literal_credential_without_token_prefix(self):
        value='long-'+str(123456789)
        rows=credential_findings(('api_key="'+value+'"').encode(),'config.toml','index')
        self.assertEqual(rows[0]['kind'],'literal_credential')
        self.assertNotIn(value,json.dumps(rows))

    def test_evidence_total_cap_removes_oldest_but_keeps_current_bundle(self):
        output=self.root/'evidence';output.mkdir()
        folders=[]
        for n in range(3):
            folder=output/str(n);folder.mkdir()
            (folder/'evidence.json').write_bytes(b'x'*20)
            for path in (folder/'evidence.json',folder):os.utime(path,(time.time()-100+n,)*2)
            folders.append(folder)
        report=prune_evidence(output,max_bytes=30,keep=(folders[-1],))
        self.assertEqual(len(report['actions']),2)
        self.assertTrue(folders[-1].is_dir())
        self.assertFalse(folders[0].exists() or folders[1].exists())

    def test_quick_is_explicit_core_subset_and_full_still_discovers_all(self):
        quick=verify_delivery.collect_tests(True)
        full=verify_delivery.collect_tests(False)
        self.assertGreater(quick.countTestCases(),60)
        self.assertGreater(full.countTestCases(),479)
        self.assertLess(quick.countTestCases(),full.countTestCases())

    def test_verification_redacts_failure_log_and_preserves_nonzero_exit(self):
        token='sk-'+'C'*30
        class Fails(unittest.TestCase):
            def runTest(self):self.fail(token)
        root=self.root/'project';root.mkdir()
        output=self.root/'proof'
        cwd=Path.cwd();self.addCleanup(os.chdir,cwd)
        smoke=dict(examples=[],git_version='test',node_version='test')
        with mock.patch.object(verify_delivery,'ROOT',root), \
             mock.patch.object(verify_delivery,'smoke',return_value=smoke), \
             mock.patch.object(verify_delivery,'digest_files',return_value={}), \
             mock.patch.object(verify_delivery,'collect_tests',return_value=unittest.TestSuite([Fails()])), \
             contextlib.redirect_stdout(io.StringIO()):
            code=verify_delivery.main(['--quick','--redact-logs','--output',str(output)])
        self.assertEqual(code,1)
        self.assertNotIn(token,(output/'quick-regression.log').read_text(encoding='utf8'))
        proof=json.loads((output/'verification.json').read_text(encoding='utf8'))
        self.assertEqual(proof['mode'],'quick')
        self.assertEqual(proof['tests_run'],1)
        self.assertEqual(len(proof['failures']),1)
        self.assertNotIn(token,json.dumps(proof))

    def test_verification_smoke_uses_isolated_workspace_and_state(self):
        source=Path(__file__).resolve().parents[1]/'workbench.example.toml'
        before=source.read_bytes()
        output=(self.root/'proof').resolve()
        config_path=verify_delivery.write_isolated_smoke_config(source,output)
        original=load_config(source)
        isolated=load_config(config_path)
        self.assertEqual(isolated.task_workdir,output/'smoke-workspace')
        self.assertEqual(isolated.state_dir,output/'smoke-state')
        self.assertEqual([(s.name,s.kind,s.test_cmd,s.approval) for s in isolated.stages],
                         [(s.name,s.kind,s.test_cmd,s.approval) for s in original.stages])
        self.assertEqual(isolated.run_budget,original.run_budget)
        self.assertEqual(source.read_bytes(),before)

    def test_parallel_worker_malformed_output_is_reported_without_losing_valid_shard(self):
        output=self.root/'parallel'
        bad_modules=('bad-json','bad-shape','bad-duration','bad-skipped','bad-failure','bad-evidence')
        def fake_run(command, *, cwd, env, timeout):
            spec_path=Path(command[command.index('--worker-input')+1])
            spec=json.loads(spec_path.read_text(encoding='utf8'))
            out_path=Path(command[command.index('--worker-output')+1])
            module=spec['module']
            if module=='bad-json':
                out_path.write_text('{broken',encoding='utf8')
                return subprocess.CompletedProcess(command,0,'','')
            payload={
                'module':module,'tests_run':1,'durations':[],
                'failures':[],'errors':[],'skipped':[],'failure_evidence':[],
            }
            if module=='bad-shape':payload.pop('module')
            elif module=='bad-duration':payload['durations']=[{}]
            elif module=='bad-skipped':payload['skipped']=[None]
            elif module=='bad-failure':payload['failures']=[{'test':'bad.failure'}]
            elif module=='bad-evidence':payload['failure_evidence']=[{'test':'bad.evidence','evidence':7}]
            out_path.write_text(json.dumps(payload),encoding='utf8')
            return subprocess.CompletedProcess(command,0,'','')
        grouped=[(module,[module.replace('-','_')+'.one']) for module in (*bad_modules,'good')]
        with mock.patch.object(verify_delivery,'_run_worker_command',side_effect=fake_run):
            merged=verify_delivery._run_parallel_workers(
                grouped,output,output/'combined.log',output/'evidence',2)
        self.assertEqual(merged['tests_run'],1)
        self.assertEqual(merged['failures'],[])
        self.assertEqual(len(merged['errors']),len(bad_modules))
        errors={item['test']:item['traceback'] for item in merged['errors']}
        self.assertEqual(set(errors),{module+'.<worker>' for module in bad_modules})
        self.assertIn('JSONDecodeError',errors['bad-json.<worker>'])
        self.assertIn('worker 输出结构无效',errors['bad-shape.<worker>'])
        self.assertIn('worker 耗时记录无效',errors['bad-duration.<worker>'])
        self.assertIn('worker 跳过记录无效',errors['bad-skipped.<worker>'])
        self.assertIn('worker 失败或错误记录无效',errors['bad-failure.<worker>'])
        self.assertIn('worker 失败证据记录无效',errors['bad-evidence.<worker>'])

    def test_parallel_worker_timeout_terminates_process_and_records_failure(self):
        output=self.root/'timeout'
        command=[sys.executable,'worker.py']
        proc=mock.Mock(pid=4321,returncode=-9)
        proc.communicate.side_effect=[subprocess.TimeoutExpired(command,0.01),('','')]
        proc.poll.return_value=-9
        with mock.patch.object(verify_delivery.subprocess,'Popen',return_value=proc), \
                mock.patch.object(verify_delivery,'_terminate_worker_process') as terminate:
            merged=verify_delivery._run_parallel_workers(
                [('hung',['hung.test'])],output,output/'combined.log',output/'evidence',1,
                worker_timeout=0.01)
        terminate.assert_called_once()
        self.assertEqual(merged['tests_run'],0)
        self.assertEqual(len(merged['errors']),1)
        self.assertEqual(merged['errors'][0]['test'],'hung.<worker>')
        self.assertIn('worker 超时',merged['errors'][0]['traceback'])
        self.assertIn('0.01',merged['errors'][0]['traceback'])

    def test_parallel_suite_deadline_does_not_start_queued_workers(self):
        output=self.root/'deadline'
        with mock.patch.object(verify_delivery,'_run_worker_command') as run:
            merged=verify_delivery._run_parallel_workers(
                [('queued',['queued.test'])],output,output/'combined.log',output/'evidence',1,
                deadline=time.monotonic()-1)
        run.assert_not_called()
        self.assertEqual(merged['tests_run'],0)
        self.assertEqual(merged['errors'][0]['test'],'queued.<worker>')
        self.assertIn('总时限',merged['errors'][0]['traceback'])

    def test_empty_git_config_env_is_preserved_after_patch_restore(self):
        # 隔离 Windows 环境写回：空值是合法的 Git 配置重置，不能删除整组变量。
        env=dict(os.environ)
        env.update({'GIT_CONFIG_COUNT':'2',
                    'GIT_CONFIG_KEY_0':'http.extraHeader','GIT_CONFIG_VALUE_0':'',
                    'GIT_CONFIG_KEY_1':'core.autocrlf','GIT_CONFIG_VALUE_1':'false'})
        code=textwrap.dedent(r'''
            import os
            import sys
            from pathlib import Path
            from unittest import mock

            sys.path.insert(0, str(Path.cwd() / 'tests'))
            from test_phase2_gates import Gates
            from scripts import verify_delivery

            before=dict(os.environ)
            with mock.patch.dict(os.environ, {'DELIVERY_TEST_MARKER':'temporary'}):
                pass
            assert dict(os.environ) == before
            with mock.patch.object(verify_delivery, '_worker_main', return_value=0):
                assert verify_delivery.main(['--worker-input','unused']) == 0
            assert dict(os.environ) == before

            case=Gates()
            case.setUp()
            try:
                case.git('init','-q')
                assert case.git('config','--get','http.extraHeader').stdout == b'\n'
                assert case.git('config','--get','core.autocrlf').stdout == b'false\n'
                assert dict(os.environ) == before
            finally:
                case.doCleanups()
        ''')
        result=subprocess.run([sys.executable,'-X','utf8','-c',code],
                              cwd=Path(__file__).resolve().parents[1],env=env,
                              capture_output=True,text=True,encoding='utf8',timeout=60)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
