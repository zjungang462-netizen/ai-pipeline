"""失败前留存、结构化原因分类、凭据替换与夹具清理的真实回归。"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.failure_evidence import (capture, classify, EvidenceTestCase, EvidenceResultMixin,
                                      ENV_OUTPUT, MAX_BUNDLE_BYTES, MAX_TAIL_BYTES,
                                      environment_secrets)
from scripts.verify_delivery import digest_files, redact_verification
from orchestrator.adapters.cli_agy import AgyAdapter
from orchestrator.contracts import RunContext, ErrorKind
from orchestrator.state import atomic_write_json


def bundle_text(data):
    """递归取出诊断包中的字符串；避免再次 JSON 编码导致"明文断言"真空。"""
    parts = []
    def walk(value):
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                parts.append(str(key))
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(data)
    return '\n'.join(parts)


class FailureEvidence(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)/'fixture';self.root.mkdir()
        self.output=Path(temporary.name)/'evidence'

    def collect(self, roots=None, **kwargs):
        path=capture('fixture.test_failure',[self.root] if roots is None else roots,
                     self.output,**kwargs)
        return path,json.loads(path.read_text(encoding='utf8'))

    def test_state_event_exit_code_and_log_tail_survive_root_cleanup(self):
        atomic_write_json(self.root/'state.json',{'revision':3,'task':'PRIVATE_TASK',
            'stages':{'coding':{'status':'failed','current_attempt':2,'error':'call_timeout'}},
            'usage_total':{'tokens':50,'reserved_tokens':100},
            'autonomy':{'execution_inputs':{'prompt':'PRIVATE_PROMPT'}},
            'events':[{'kind':'attempt_fail','n':2,'error_kind':'timeout','stage':'coding'}]})
        atomic_write_json(self.root/'execution.json',{'ok':False,'exit_code':-1,'error_kind':'timeout'})
        (self.root/'stderr.txt').write_text('x'*40000+'TAIL_SENTINEL',encoding='utf8')
        (self.root/'config.toml').write_text('secret="PRIVATE_CONFIG"',encoding='utf8')
        path,data=self.collect()
        (self.root/'state.json').unlink()
        text=path.read_text(encoding='utf8')
        self.assertNotIn('PRIVATE_TASK',text)
        self.assertNotIn('PRIVATE_PROMPT',text)
        self.assertNotIn('PRIVATE_CONFIG',text)
        state=next(r['data'] for r in data['records'] if r['path'].endswith('state.json'))
        self.assertEqual(state['stages']['coding']['current_attempt'],2)
        self.assertEqual(state['usage_total']['tokens'],50)
        self.assertEqual(state['events'][0]['error_kind'],'timeout')
        self.assertTrue(any(c['category']=='timeout' and c['exit_code']==-1 for c in data['causes']))
        log=next(r['data'] for r in data['records'] if r['path'].endswith('stderr.txt'))
        self.assertTrue(log['tail'].endswith('TAIL_SENTINEL'))
        self.assertLessEqual(len(log['tail']),MAX_TAIL_BYTES)
        self.assertTrue(log['truncated'])

    def test_redaction_covers_known_credentials_json_escapes_and_token_urls(self):
        secret='诊断私密-value-12345'
        key='arbitrary-private-value-123456'
        token='sk-'+'local-fixture-private-123456789'
        credential_url='http://'+'user:password@localhost'
        (self.root/'stderr.txt').write_text(secret+' '+key+' '+token+' '
            +credential_url+' '+json.dumps(secret,ensure_ascii=True),encoding='utf8')
        atomic_write_json(self.root/'cli-progress.json',{'reason':'call_timeout','file':secret,
                                                        'usage':{'api_key':key,'tokens':30}})
        with mock.patch.dict(os.environ,{'CAPTURE_API_KEY':key}):
            path,data=self.collect(secrets=[secret],failure='assert '+secret)
        text=path.read_text(encoding='utf8')
        for value in (secret,key,token,'user:password'):
            self.assertNotIn(value,text)
        self.assertNotIn(json.dumps(secret,ensure_ascii=True)[1:-1],text)
        self.assertIn('[REDACTED]',text)

    def test_environment_secret_names_and_short_values_are_redacted(self):
        env = {
            'AWS_ACCESS_KEY_ID': 'AKIA' + 'Q1' * 8,
            'GITHUB_TOKEN': 'ghp_' + 'G' * 30,
            'NPM_TOKEN': 'npm_' + 'N' * 30,
            'HUGGINGFACE_TOKEN': 'hf_' + 'H' * 30,
            'SSH_PASSPHRASE': 'correct horse battery staple',
            'MY_PASSWORD': 'abc123',        # 6 字符短值：原实现因 <8 被漏掉
            'SHORT_PASSWORD': 'ab12',       # 4 字符：新的值下限
            'OPENAI_API_KEY': 'sk-' + 'K' * 24,
        }
        (self.root / 'stderr.txt').write_text(
            '\n'.join(f'{name}={value}' for name, value in env.items()), encoding='utf8')
        with mock.patch.dict(os.environ, env):
            captured = set(environment_secrets())
            path, data = self.collect()
        text = bundle_text(data)
        for name, value in env.items():
            with self.subTest(name=name):
                self.assertIn(value, captured)      # 变量名类别被识别为凭据
                self.assertNotIn(value, text)       # 值不会明文进入诊断包

    def test_recognizable_token_prefixes_are_redacted_without_env_names(self):
        tokens = {
            'aws': 'AKIA' + 'IOSFODNN7EXAMPLE',
            'npm': 'npm_' + 'a' * 32,
            'hf': 'hf_' + 'b' * 32,
            'glpat': 'glpat-' + 'c' * 24,
            'slack': 'xoxb-' + '1234567890-abcdEFGH',
            'google': 'AIza' + 'D' * 35,
            'github': 'ghp_' + 'e' * 30,
        }
        # 保守性的反例：前缀相近但长度不足的普通文本不得被替换。
        benign = 'near misses: AKIA123 npm_short hf_short AIzaShort glpat-short'
        (self.root / 'stderr.txt').write_text(
            '\n'.join(tokens.values()) + '\n' + benign, encoding='utf8')
        with mock.patch('scripts.failure_evidence.environment_secrets', return_value=[]):
            path, data = self.collect()
        text = bundle_text(data)
        for name, value in tokens.items():
            with self.subTest(name=name):
                self.assertNotIn(value, text)
        self.assertIn(benign, text)
        self.assertGreaterEqual(text.count('[REDACTED]'), len(tokens))

    def test_boundary_redaction_precedes_tail_truncation(self):
        """秘密必须真正跨越最终 tail 起点，否则不脱敏也会通过（原用例是真空的）。"""
        cases = (
            ('raw-ascii', 'PRIVATE_BOUNDARY_CREDENTIAL_123456789',
             lambda value: value),
            # JSON 转义/多字节：日志里出现的是 ensure_ascii 转义形式。
            ('escaped-multibyte', '🔑🔑',
             lambda value: json.dumps(value, ensure_ascii=True)[1:-1]),
        )
        for name, secret, render in cases:
            with self.subTest(case=name):
                raw = render(secret)
                guard = len(secret.encode('utf8')) * 2
                size = MAX_TAIL_BYTES * 4
                tail_start = size - MAX_TAIL_BYTES
                start = tail_start - len(raw) // 2
                # 与实现核对：秘密整体进入读取窗口（offset=size-MAX_TAIL-guard*2），
                # 且起点在最终 tail 起点之前、终点在其之后。
                self.assertGreaterEqual(start, size - MAX_TAIL_BYTES - guard * 2)
                self.assertGreater(start + len(raw), tail_start)
                content = 'A' * start + raw + 'B' * (size - start - len(raw))
                # 鉴别力：不脱敏的朴素尾部确实含秘密片段（本用例能抓住回归）。
                self.assertIn(raw[len(raw) // 2:], content[-MAX_TAIL_BYTES:])
                (self.root / 'live-stderr.log').write_text(content, encoding='utf8')
                path, data = self.collect(secrets=[secret])
                text = bundle_text(data)
                self.assertNotIn(secret, text)
                self.assertNotIn(raw, text)
                self.assertNotIn(raw[len(raw) // 2:], text)
                self.assertNotIn(raw[-10:], text)

    def test_source_root_path_does_not_leak_at_tail_boundary(self):
        """无环境秘密时，源根路径（含用户名/临时目录名）也必须被脱敏。"""
        private = self.root.parent / 'USER-PRIVATE-DIR' / 'run-abc123'
        private.mkdir(parents=True)
        source = str(private.resolve())
        guard = len(source.encode('utf8')) * 2
        size = MAX_TAIL_BYTES * 4
        tail_start = size - MAX_TAIL_BYTES
        start = tail_start - len(source) // 2
        self.assertGreaterEqual(start, size - MAX_TAIL_BYTES - guard * 2)
        content = 'L' * start + source + 'M' * (size - start - len(source))
        self.assertIn(source[-20:], content[-MAX_TAIL_BYTES:])
        (private / 'stderr.txt').write_text(content, encoding='utf8')
        with mock.patch('scripts.failure_evidence.environment_secrets', return_value=[]):
            path = capture('root-path-boundary', [private], self.output)
        data=json.loads(path.read_text(encoding='utf8'))
        text = bundle_text(data)
        self.assertNotIn(source, text)
        for fragment in ('USER-PRIVATE-DIR', 'run-abc123', source[-20:]):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, text)
        self.assertIn('[REDACTED]', text)

    def test_structured_causes_are_separate_and_prose_never_decides(self):
        def row(data):return {'path':'fixture','data':data}
        cases=[({'error_kind':'timeout','exit_code':-1},'timeout'),
               ({'reason':'budget_limit'},'budget_exhausted'),
               ({'stages':{'coding':{'error':'run 总 token 超限: 120 > 100'}}},'budget_exhausted'),
               ({'error_kind':'crash','exit_code':7},'process_crash'),
               ({'exit_code':1},'process_nonzero'),
               ({'error':'预算.txt 文件不存在'},'unknown'),
               ({'output':'budget_limit timeout crash'},'unknown')]
        for data,expected in cases:
            with self.subTest(data=data):
                self.assertEqual(classify([row(data)])[0]['category'],expected)
        categories={v['category'] for v in classify([row({'error_kind':'timeout'}),row({'reason':'budget_limit'})])}
        self.assertEqual(categories,{'timeout','budget_exhausted'})

    def test_invalid_json_is_reported_without_guessing_reason(self):
        (self.root/'state.json').write_text('{"incomplete":',encoding='utf8')
        _,data=self.collect(failure='assertion mentioned budget')
        self.assertEqual(data['causes'][0]['category'],'unknown')
        self.assertEqual(data['issues'][0]['reason'],'unreadable_or_invalid')

    def test_unreadable_directory_is_recorded_and_other_roots_continue(self):
        first=self.root.parent/'first-root';first.mkdir()
        second=self.root.parent/'second-root';second.mkdir()
        atomic_write_json(first/'state.json',{'stages':{'coding':{'status':'failed'}}})
        locked=first/'locked';locked.mkdir()
        (locked/'stderr.txt').write_text('LOCKED_TAIL',encoding='utf8')
        (second/'stderr.txt').write_text('SECOND_ROOT_TAIL',encoding='utf8')
        real_scandir=os.scandir
        def fake_scandir(path):
            if Path(path)==locked:raise PermissionError('injected unreadable dir')
            return real_scandir(path)
        for secrets,label in (([], 'root-1/stderr.txt'), (['root'], '[REDACTED]-1/stderr.txt')):
            with self.subTest(secrets=secrets), \
                 mock.patch('scripts.failure_evidence.environment_secrets',return_value=secrets), \
                 mock.patch('scripts.failure_evidence.os.scandir',side_effect=fake_scandir):
                path,data=self.collect(roots=[first,second])
            issues=[issue for issue in data['issues'] if issue['reason']=='unreadable_dir']
            self.assertTrue(any('locked' in issue['path'] for issue in issues))
            records={record['path']:record['data'] for record in data['records']}
            # 单目录失败后仍采集其它根；路径标签也必须服从凭据脱敏。
            self.assertTrue(any(record['path'].endswith('state.json') for record in data['records']))
            self.assertIn('SECOND_ROOT_TAIL',records[label]['tail'])
            self.assertFalse(any('locked' in name for name in records))
            self.assertNotIn('LOCKED_TAIL',bundle_text(data))
            if secrets:
                self.assertNotIn('root-1/stderr.txt',records)

    def test_unreadable_directory_does_not_abort_capture_test(self):
        root=self.root.parent/'testcase-root';root.mkdir()
        atomic_write_json(root/'execution.json',{'exit_code':3,'error_kind':'crash'})
        locked=root/'locked';locked.mkdir()
        real_scandir=os.scandir
        def fake_scandir(path):
            if Path(path)==locked:raise PermissionError('injected unreadable dir')
            return real_scandir(path)
        class Failing(EvidenceTestCase):
            def runTest(self):self.fail('original failure')
        case=Failing();case.root=root
        with mock.patch.dict(os.environ,{ENV_OUTPUT:str(self.output)}), \
             mock.patch('scripts.failure_evidence.os.scandir',side_effect=fake_scandir):
            result=unittest.TestResult();case.run(result)
        self.assertEqual(len(result.failures),1)
        self.assertIsNone(getattr(case,'failure_evidence_error',None))
        data=json.loads(Path(case.failure_evidence).read_text(encoding='utf8'))
        self.assertEqual(data['causes'][0]['category'],'process_crash')
        self.assertIn('unreadable_dir',{issue['reason'] for issue in data['issues']})

    def test_deeply_nested_json_does_not_abort_capture(self):
        (self.root/'state.json').write_text('['*3000+']'*3000,encoding='utf8')
        (self.root/'stderr.txt').write_text('TAIL_AFTER_NESTED_JSON',encoding='utf8')
        path,data=self.collect()
        self.assertIn('unreadable_or_invalid',{issue['reason'] for issue in data['issues']})
        self.assertTrue(any(record['path'].endswith('stderr.txt') for record in data['records']))

    def test_symlink_is_not_followed_or_copied(self):
        outside=self.root.parent/'private';outside.mkdir()
        (outside/'stderr.txt').write_text('OUTSIDE_SECRET',encoding='utf8')
        link=self.root/'linked'
        try:link.symlink_to(outside,target_is_directory=True)
        except OSError:
            if os.name!='nt':raise
            subprocess.run(['cmd','/c','mklink','/J',str(link),str(outside)],capture_output=True,check=True)
        try:
            path,data=self.collect()
            self.assertNotIn('OUTSIDE_SECRET',path.read_text(encoding='utf8'))
            self.assertEqual(data['records'],[])
        finally:
            if link.is_symlink():link.unlink()
            else:os.rmdir(link)

    def test_bundle_is_bounded_and_each_failure_has_distinct_directory(self):
        for i in range(90):
            folder=self.root/str(i);folder.mkdir()
            (folder/'stderr.txt').write_text('汉😀'*20000,encoding='utf8')
        first,data=self.collect()
        second,_=self.collect()
        self.assertNotEqual(first.parent,second.parent)
        self.assertLessEqual(first.stat().st_size,MAX_BUNDLE_BYTES)
        self.assertLessEqual(len(data['records']),80)

    def test_capture_error_does_not_turn_failure_into_success_or_skip_cleanup(self):
        cleaned=[]
        base=self.root
        class Failing(EvidenceTestCase):
            def setUp(self):
                self.root=base
                self.addCleanup(lambda:cleaned.append(True))
            def runTest(self):self.fail('original failure')
        with mock.patch('scripts.failure_evidence.capture',side_effect=OSError('capture unavailable')):
            test=Failing();result=unittest.TestResult();test.run(result)
        self.assertEqual(len(result.failures),1)
        self.assertIn('original failure',result.failures[0][1])
        self.assertEqual(cleaned,[True])
        self.assertEqual(test.failure_evidence_error,'OSError')

    def test_plain_unittest_case_captures_before_temporary_cleanup(self):
        class Failing(EvidenceTestCase):
            def setUp(self):
                self.tmp=tempfile.TemporaryDirectory()
                self.addCleanup(self.tmp.cleanup)
                self.root=Path(self.tmp.name)
                atomic_write_json(self.root/'execution.json',{'error_kind':'crash','exit_code':9})
            def runTest(self):self.fail('injected failure')
        with mock.patch.dict(os.environ,{ENV_OUTPUT:str(self.output)}):
            case=Failing();result=unittest.TestResult();case.run(result)
        self.assertFalse(case.root.exists())
        self.assertEqual(len(result.failures),1)
        data=json.loads(Path(case.failure_evidence).read_text(encoding='utf8'))
        self.assertEqual(data['causes'][0]['category'],'process_crash')

    def test_result_mixin_captures_subtest_before_method_continues(self):
        class Failing(unittest.TestCase):
            def setUp(self):
                self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
                self.root=Path(self.tmp.name)
                atomic_write_json(self.root/'execution.json',{'exit_code':7,'error_kind':'crash'})
            def runTest(self):
                with self.subTest(mode='subtest'):self.fail('subtest failure')
                (self.root/'execution.json').unlink()
        target=self.output
        class Result(EvidenceResultMixin,unittest.TextTestResult):evidence_output=target
        case=Failing()
        result=unittest.TextTestRunner(stream=io.StringIO(),resultclass=Result).run(case)
        self.assertEqual(len(result.failures),1)
        self.assertFalse(case.root.exists())
        self.assertEqual(len(result._test_evidence_failures),1)
        data=json.loads(Path(case.failure_evidence).read_text(encoding='utf8'))
        self.assertEqual(data['causes'][0]['exit_code'],7)

    def test_result_mixin_does_not_capture_success_or_change_expected_failure(self):
        class Passing(unittest.TestCase):
            def runTest(self):pass
        class Expected(unittest.TestCase):
            @unittest.expectedFailure
            def runTest(self):self.fail('expected')
        target=self.output
        class Result(EvidenceResultMixin,unittest.TextTestResult):evidence_output=target
        result=unittest.TextTestRunner(stream=io.StringIO(),resultclass=Result).run(
            unittest.TestSuite([Passing(),Expected()]))
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(len(result.expectedFailures),1)
        self.assertFalse(self.output.exists())


class FaultClassification(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)/'fixture';self.root.mkdir()
        self.output=Path(temporary.name)/'evidence'
        self.workspace=self.root/'workspace';self.workspace.mkdir()

    def execute(self,mode):
        adapter=AgyAdapter('fixture');adapter._resolved=sys.executable;adapter._version='1.5.9'
        ctx=RunContext('fixture','fault','coding',self.workspace,self.root,kind='code',
                       attempt_dir=self.root/'attempt',timeout_s=1 if mode=='timeout' else 15)
        usage={'event':'step_update','step_update':{'conversation_id':'fixture','step_index':1,
               'step_type':'agent_response','state':'DONE','usage':{'total_tokens':30}}}
        script='import sys,json,time\nsys.stdin.buffer.read()\nprint('+repr(json.dumps(usage))+',flush=True)\n'
        if mode=='crash':script+='sys.stderr.write("fixture crash\\n");sys.exit(7)\n'
        else:script+='time.sleep(30)\n'
        if mode=='budget':ctx.remaining_tokens=20;ctx.token_reservation=10
        result=adapter._exec(['-u','-c',script],self.workspace,ctx)
        # 与引擎相同的执行记录格式，值来自真实进程结果，不由正文推断。
        atomic_write_json(ctx.attempt_dir/'execution.json',{'ok':result.ok,'error_kind':result.error_kind.value,
                          'exit_code':result.exit_code,'usage':result.usage})
        evidence=capture('fault-'+mode,[self.root],self.output)
        data=json.loads(evidence.read_text(encoding='utf8'))
        return result,data

    def test_real_timeout_is_distinct_from_process_crash(self):
        result,data=self.execute('timeout')
        self.assertEqual(result.error_kind,ErrorKind.TIMEOUT)
        categories={c['category'] for c in data['causes']}
        self.assertIn('timeout',categories)
        self.assertNotIn('process_crash',categories)

    def test_real_budget_stop_retains_known_usage_and_reservation_semantics(self):
        result,data=self.execute('budget')
        self.assertEqual(result.error_kind,ErrorKind.REJECTED)
        self.assertEqual(result.usage['known_tokens'],30)
        self.assertTrue(result.usage['usage_unknown'])
        self.assertIn('budget_exhausted',{c['category'] for c in data['causes']})
        self.assertNotIn('timeout',{c['category'] for c in data['causes']})

    def test_real_nonzero_process_has_original_exit_code(self):
        result,data=self.execute('crash')
        self.assertEqual(result.error_kind,ErrorKind.CRASH)
        self.assertEqual(result.exit_code,7)
        self.assertTrue(any(c['category']=='process_crash' and c['exit_code']==7 for c in data['causes']))


class DeliveryVerification(unittest.TestCase):
    def test_source_digest_covers_demo_delivery_sources(self):
        files=digest_files()
        self.assertIn('demo/run_api_demo.py',files)
        self.assertIn('scripts/failure_evidence.py',files)
        self.assertIn('scripts/verify_delivery.py',files)

    def test_verification_tracebacks_are_redacted_without_changing_exit_code(self):
        secret='CAPTURE_API_KEY_VALUE_123456'
        token='sk-'+'z'*24
        credential_url='http://'+'alice:pw123@host'
        proof={'tests_run':2,
               'failures':[{'test':'suite.test_a',
                            'traceback':f'AssertionError: {secret} {token} {credential_url}'}],
               'errors':[{'test':'suite.test_b','traceback':'RuntimeError: '+secret}],
               'skipped':[{'test':'suite.test_c','reason':secret}],
               'source_unchanged':False,'exit_code':1}
        with mock.patch.dict(os.environ,{'CAPTURE_API_KEY':secret}):
            redact_verification(proof)
        text=json.dumps(proof,ensure_ascii=False)
        for value in (secret,token,'alice:pw123'):
            with self.subTest(value=value):
                self.assertNotIn(value,text)
        self.assertIn('[REDACTED]',text)
        # 脱敏只作用于最终报告副本：测试结果与退出码保持不变。
        self.assertEqual(proof['exit_code'],1)
        self.assertEqual(proof['tests_run'],2)
        self.assertEqual(len(proof['failures']),1)


class DemoExitCode(unittest.TestCase):
    def test_demo_exit_codes_never_treat_failure_or_approval_as_success(self):
        from demo.run_api_demo import result_exit_code
        self.assertEqual(result_exit_code({'a':'done','b':'done'}),0)
        self.assertEqual(result_exit_code({'a':'done','b':'awaiting_approval'}),11)
        for status in ('failed','timeout','cancelled','interrupted','running','pending'):
            with self.subTest(status=status):
                self.assertEqual(result_exit_code({'a':'done','b':status}),1)
        self.assertEqual(result_exit_code({'a':'failed','b':'awaiting_approval'}),1)
        self.assertEqual(result_exit_code({}),1)


if __name__=='__main__':unittest.main()
