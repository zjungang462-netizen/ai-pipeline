"""第四轮报告的真实路径、HTTP/测试、上下文和失败语义回归。"""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import artifacts
from orchestrator.adapters.fake import FakeAdapter
from orchestrator.adapters.factory import build_all
from orchestrator.adapters.openai_compat import OpenAICompatAdapter
from orchestrator.config import parse_config
from orchestrator.engine import Engine
from orchestrator.review import VerdictError, load_verdict
from orchestrator.safety import WriteRootViolation, relative_target


class Round4(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def engine(self,state_inside=False):
        cfg=parse_config({'task_workdir':'workspace','state_dir':'workspace/runtime_records' if state_inside else 'state',
            'stages':[{'name':'coding','kind':'code','adapter':'fake','prompt':'{upstream}\n{feedback}'}],
            'adapters':{'fake':{'type':'fake'}}},base_dir=self.root)
        cfg.task_workdir.mkdir(exist_ok=True)
        with mock.patch('orchestrator.engine.artifacts.git_available',return_value=False):
            return Engine(cfg,{'fake':FakeAdapter(kind='code')},'run','task')

    def code(self,name):
        return f'```python\n# file: {name}\nVALUE=1\n```\n```python\n# file: ok.py\nVALUE=2\n```'

    def test_windows_reserved_names_are_rejected_in_all_components(self):
        names=['CON','con.py','PRN','aux.py','NUL.txt','COM1.log','LPT9','COM¹.txt','lpt².py',
               'nested/AUX/ok.py','nested/CON .txt','CONIN$','CONOUT$.log']
        for name in names:
            with self.subTest(name=name),self.assertRaises(WriteRootViolation):
                relative_target(self.root,name)

    def test_similar_nonreserved_names_are_allowed(self):
        for name in ('console.py','auxiliary.py','COM10.txt','LPT0.py','中文目录/normal.py'):
            with self.subTest(name=name):
                self.assertTrue(relative_target(self.root,name).is_relative_to(self.root))

    def test_reserved_file_falls_back_and_does_not_block_later_file(self):
        for name in ('CON','aux.py','NUL','COM1.txt','nested/PRN.py'):
            with self.subTest(name=name),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                files=OpenAICompatAdapter._write_code_files(self.code(name),root,'coding')
                self.assertEqual(len(files),2)
                self.assertEqual((root/'coding_1.py').read_text(encoding='utf8'),'VALUE=1\n')
                self.assertEqual((root/'ok.py').read_text(encoding='utf8'),'VALUE=2\n')

    def test_reserved_device_is_not_sent_to_filesystem_writer(self):
        original=Path.write_text
        def strict_windows_write(path,*args,**kwargs):
            if path.name.upper().split('.')[0] in ('CON','AUX','NUL'):
                raise OSError('simulated Windows device rejection')
            return original(path,*args,**kwargs)
        with mock.patch.object(Path,'write_text',new=strict_windows_write):
            files=OpenAICompatAdapter._write_code_files(self.code('CON'),self.root,'coding')
        self.assertEqual(len(files),2)
        self.assertTrue((self.root/'ok.py').is_file())

    def test_fallback_preserves_explicit_and_existing_names(self):
        (self.root/'coding_1.py').write_text('existing',encoding='utf8')
        text=self.code('CON')+'\n```python\n# file: coding_1_1.py\nEXPLICIT=3\n```'
        files=OpenAICompatAdapter._write_code_files(text,self.root,'coding')
        self.assertEqual(len(files),3)
        self.assertEqual((self.root/'coding_1.py').read_text(encoding='utf8'),'existing')
        self.assertEqual((self.root/'coding_1_1.py').read_text(encoding='utf8'),'EXPLICIT=3\n')
        self.assertEqual((self.root/'coding_1_2.py').read_text(encoding='utf8'),'VALUE=1\n')

    def test_patch_does_not_rename_invalid_target(self):
        with self.assertRaises(WriteRootViolation):
            OpenAICompatAdapter._apply_text_patch(self.root,'aux.py',json.dumps({'edits':[{'old':'a','new':'b'}]}))
        self.assertFalse((self.root/'coding_1.py').exists())

    def test_fallback_target_still_requires_root_boundary(self):
        from orchestrator import safety
        original=safety.relative_target
        def guarded(root,name):
            if name=='coding_1.py':
                raise WriteRootViolation('fallback linked outside')
            return original(root,name)
        with mock.patch.object(safety,'relative_target',side_effect=guarded),self.assertRaises(WriteRootViolation):
            OpenAICompatAdapter._write_code_files(self.code('CON'),self.root,'coding')
        self.assertFalse((self.root/'ok.py').exists())

    def test_rework_lists_existing_packages_without_unrelated_bodies(self):
        engine=self.engine()
        root=engine.config.task_workdir
        (root/'todo/todoapp').mkdir(parents=True)
        (root/'todo/todoapp/store.py').write_text('TARGET_CONTENT',encoding='utf8')
        (root/'todoapp.py').write_text('UNRELATED_PRIVATE_BODY',encoding='utf8')
        context=engine._rework_context('修复 `todo/todoapp/store.py` 的边界问题')
        self.assertIn('todo/todoapp/store.py',context)
        self.assertIn('todoapp.py',context)
        self.assertIn('TARGET_CONTENT',context)
        self.assertNotIn('UNRELATED_PRIVATE_BODY',context)
        self.assertIn('允许修改范围',context)
        self.assertIn('不擅自删除',context)

    def test_inventory_omits_dependencies_hidden_files_and_runtime_state(self):
        engine=self.engine(state_inside=True)
        root=engine.config.task_workdir
        for name in ('node_modules/pkg/leak.js','.git/info/leak','runtime_records/leak.py','__pycache__/leak.py'):
            path=root/name
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('PRIVATE',encoding='utf8')
        (root/'.env').write_text('PRIVATE',encoding='utf8')
        (root/'visible.py').write_text('PUBLIC',encoding='utf8')
        context=engine._rework_file_inventory()
        self.assertIn('visible.py',context)
        self.assertNotIn('leak',context)
        self.assertNotIn('.env',context)

    def test_inventory_has_file_character_and_directory_limits(self):
        engine=self.engine()
        root=engine.config.task_workdir
        for i in range(8):
            (root/f'file-{i:02}.py').write_text('content',encoding='utf8')
        context=engine._rework_file_inventory(max_files=2,max_chars=100)
        self.assertEqual(sum(line.startswith('- ') for line in context.splitlines()),2)
        self.assertIn('截断',context)
        for path in root.glob('*.py'):
            path.unlink()
        for i in range(20):
            (root/f'dir-{i:02}').mkdir()
        walked=[]
        original=os.walk
        def count_walk(*args,**kwargs):
            for entry in original(*args,**kwargs):
                walked.append(entry[0])
                yield entry
        with mock.patch('orchestrator.engine.os.walk',side_effect=count_walk):
            context=engine._rework_file_inventory(max_directories=2)
        self.assertLessEqual(len(walked),3)
        self.assertIn('截断',context)

    def test_inventory_does_not_read_unmentioned_content(self):
        engine=self.engine()
        path=engine.config.task_workdir/'other.py'
        path.write_text('MUST_NOT_READ',encoding='utf8')
        original=Path.open
        def guarded(file,*args,**kwargs):
            if file==path:
                raise AssertionError('unmentioned content opened')
            return original(file,*args,**kwargs)
        with mock.patch.object(Path,'open',new=guarded):
            context=engine._rework_context('未提供具体位置')
        self.assertIn('other.py',context)
        self.assertNotIn('MUST_NOT_READ',context)

    def test_load_verdict_requires_an_explicit_task_root(self):
        file=self.root/'verdict.json'
        file.write_text('{"verdict":"pass","issues":[]}',encoding='utf8')
        with self.assertRaisesRegex(VerdictError,'workdir'):
            load_verdict(file)
        self.assertTrue(load_verdict(file,workdir=self.root).passed)

    def test_explicit_root_does_not_consult_cwd(self):
        file=self.root/'verdict.json'
        file.write_text(json.dumps({'verdict':'fail','issues':[{'file':'src/app.py','problem':'bug'}]}),encoding='utf8')
        with mock.patch('orchestrator.review.Path.cwd',side_effect=AssertionError('must not guess cwd')):
            self.assertFalse(load_verdict(file,workdir=self.root).passed)

    def test_pass_with_suggestions_is_still_rejected(self):
        file=self.root/'verdict.json'
        file.write_text('{"verdict":"pass","issues":["rename suggestion"]}',encoding='utf8')
        with self.assertRaises(VerdictError):
            load_verdict(file,workdir=self.root)

    def test_test_worktree_failure_is_archived_as_infrastructure_not_budget(self):
        cfg=parse_config({'task_workdir':'repo','state_dir':'state','stages':[
            {'name':'coding','kind':'code','adapter':'fake','prompt':'p','max_attempts':1},
            {'name':'testing','kind':'test','test_cmd':[sys.executable,'-c','print("SHOULD_NOT_RUN")']}],
            'adapters':{'fake':{'type':'fake'}}},base_dir=self.root)
        if not artifacts.git_available():
            self.skipTest('requires git')
        adapter=FakeAdapter(kind='code')
        engine=Engine(cfg,{'fake':adapter},'run','task')
        original=artifacts.add_worktree
        def add(repo,base,branch,worktree,*,git=None):
            if branch.startswith('verify/'):
                raise artifacts.GitError('injected worktree setup failure')
            return original(repo,base,branch,worktree,git=git)
        with mock.patch('orchestrator.engine.artifacts.add_worktree',side_effect=add),mock.patch(
                'orchestrator.testrun.run_test_cmd',side_effect=AssertionError('must not execute fallback')):
            self.assertEqual(engine.run()['testing'],'failed')
        self.assertEqual(adapter.calls,1)
        self.assertEqual(sum(e['kind']=='worktree_error' for e in engine.state.events),1)
        self.assertFalse(any(e['kind']=='rejected' and e.get('stage')=='testing' for e in engine.state.events))
        attempt=engine.state.current_attempt_dir('testing')
        record=json.loads((attempt/'test_record.json').read_text(encoding='utf8'))
        self.assertEqual(record['exit_code'],-1)
        self.assertIn('未执行测试',record['error'])
        self.assertFalse(engine.state.is_complete('testing',engine.state.stage('testing').inputs_hash))

    def test_reserved_name_local_http_to_git_and_real_test_pipeline(self):
        if not artifacts.git_available():
            self.skipTest('requires git')
        response_text=self.code('CON')
        class Handler(http.server.BaseHTTPRequestHandler):
            calls=0
            def do_POST(inner):
                inner.rfile.read(int(inner.headers['Content-Length']))
                Handler.calls+=1
                body=json.dumps({'choices':[{'message':{'content':response_text},'finish_reason':'stop'}],
                                 'usage':{'total_tokens':37}}).encode('utf8')
                inner.send_response(200)
                inner.send_header('Content-Type','application/json')
                inner.send_header('Content-Length',str(len(body)))
                inner.end_headers()
                inner.wfile.write(body)
            def log_message(inner,*args):
                pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        command=[sys.executable,'-X','utf8','-c','import coding_1,ok; assert coding_1.VALUE==1 and ok.VALUE==2; print("RESERVED_NAME_PIPELINE_OK")']
        cfg=parse_config({'task_workdir':'repo','state_dir':'state','run_budget':{'max_tokens':4000,'max_duration_s':60},
            'stages':[{'name':'coding','kind':'code','adapter':'api','prompt':'p','max_attempts':1},
                      {'name':'testing','kind':'test','test_cmd':command}],
            'adapters':{'api':{'type':'openai_compat','base_url':f'http://127.0.0.1:{server.server_port}',
                               'model':'local-response-fixture','api_key_env':'ROUND4_LOCAL_HTTP_KEY'}}},base_dir=self.root)
        engine=Engine(cfg,build_all(cfg.adapters,{}),'run','write safe files')
        with mock.patch.dict(os.environ,{'ROUND4_LOCAL_HTTP_KEY':'local-not-secret'}):
            self.assertEqual(set(engine.run().values()),{'done'})
        self.assertEqual(Handler.calls,1)
        self.assertEqual(engine.state.usage_total['tokens'],37)
        self.assertTrue((cfg.task_workdir/'coding_1.py').is_file())
        self.assertTrue((cfg.task_workdir/'ok.py').is_file())


if __name__=='__main__':
    unittest.main()
