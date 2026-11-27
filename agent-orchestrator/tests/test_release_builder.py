"""独立 Git 发布夹具：原始字节、两个验证阶段、失败不发布与状态边界。"""
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from scripts.release import build_release, sha, verify


class ReleaseBuilder(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.repo=self.root/'repo';self.repo.mkdir()
        def git(*args):return subprocess.run(['git',*args],cwd=self.repo,env=dict(os.environ),check=True,capture_output=True)
        self.git=git
        git('init','-q');git('config','user.name','test');git('config','user.email','test@example.invalid')
        git('config','core.autocrlf','true')
        source=self.repo/'agent-orchestrator/orchestrator';source.mkdir(parents=True)
        (source/'_version.py').write_bytes(b"__version__ = '0.5.0'\n")
        (source/'text.py').write_bytes('内容 = "保持字节"\n'.encode('utf8'))
        git('add','.');git('commit','-qm','发布夹具')
        self.calls=[]

    def verifier(self,root,output):
        self.calls.append(root);output.mkdir()
        proof={'mode':'full','tests_run':5,'exit_code':0,'failures':[],'errors':[],'skipped':[],
               'unexpected_skips':[],'source_unchanged':True}
        for name in ('verification.json','smoke.json','source-before.json','source-after.json',
                     'test-durations.json','slow-tests.json','test-ids.json'):
            (output/name).write_text(json.dumps(proof if name=='verification.json' else {}),encoding='utf8')
        return proof

    def test_two_verifications_bind_head_raw_bytes_manifest_and_changelog(self):
        result=build_release(self.repo,self.root/'release',self.verifier)
        self.assertEqual(len(self.calls),2)
        self.assertNotEqual(self.calls[0],self.calls[1])
        package=Path(result['package']);self.assertEqual(sha(package.read_bytes()),result['sha256'])
        with zipfile.ZipFile(package) as z:
            raw=self.git('show','HEAD:agent-orchestrator/orchestrator/text.py').stdout
            self.assertEqual(z.read('agent-orchestrator/orchestrator/text.py'),raw)
            manifest=json.loads(z.read('SNAPSHOT.json'))
            self.assertEqual(manifest['files']['agent-orchestrator/orchestrator/text.py'],sha(raw))
            self.assertIn('发布夹具',z.read('delivery-metadata/CHANGELOG.md').decode('utf8'))
        self.assertFalse(self.git('status','--porcelain=v1').stdout)

    def test_second_verification_failure_never_creates_final_package(self):
        def verify(root,output):
            if self.calls:raise ValueError('package verification failed')
            return self.verifier(root,output)
        out=self.root/'failed'
        with self.assertRaises(ValueError):build_release(self.repo,out,verify)
        self.assertFalse((out/'release-result.json').exists())
        self.assertFalse(list(out.glob('agent-orchestrator-*.zip')))
        self.assertTrue((out/'candidate.zip').exists())

    def test_dirty_tree_and_inside_repo_output_rejected(self):
        with self.assertRaises(ValueError):build_release(self.repo,self.repo/'out',self.verifier)
        (self.repo/'untracked.txt').write_text('new',encoding='utf8')
        with self.assertRaises(ValueError):build_release(self.repo,self.root/'out',self.verifier)
        self.assertFalse(self.calls)

    def test_changed_source_during_verification_is_not_published(self):
        def verify(root,output):
            result=self.verifier(root,output)
            (root/'agent-orchestrator/orchestrator/text.py').write_text('mutated',encoding='utf8')
            return result
        with self.assertRaises(ValueError):build_release(self.repo,self.root/'mutated',verify)
        self.assertFalse((self.root/'mutated/release-result.json').exists())

    def test_linux_release_allows_registered_windows_skips(self):
        output=self.root/'linux-proof';output.mkdir()
        proof={'mode':'full','tests_run':5,'exit_code':0,'failures':[],'errors':[],
               'skipped':[{'test':'test_process_start.ProcessStart.test_short_command_executes_after_job_attachment',
                           'reason':'Windows Job 启动顺序回归'}],
               'unexpected_skips':[],'source_unchanged':True}
        (output/'verification.json').write_text(json.dumps(proof),encoding='utf8')
        with mock.patch('scripts.release.sys.platform','linux'), \
                mock.patch('scripts.release.subprocess.run',return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(verify(self.repo,output),proof)
        self.assertIn('--allow-linux-windows-skips',run.call_args.args[0])

    def test_release_rejects_unexpected_skips_even_when_process_succeeds(self):
        output=self.root/'unexpected-proof';output.mkdir()
        proof={'mode':'full','tests_run':5,'exit_code':0,'failures':[],'errors':[],
               'skipped':[{'test':'test_unregistered','reason':'not allowed'}],
               'unexpected_skips':[['test_unregistered','not allowed']],'source_unchanged':True}
        (output/'verification.json').write_text(json.dumps(proof),encoding='utf8')
        with mock.patch('scripts.release.subprocess.run',return_value=mock.Mock(returncode=0)):
            with self.assertRaisesRegex(ValueError,'不满足完整发布条件'):
                verify(self.repo,output)
