"""审查覆盖配置、缺失清单证据、人工/失败/显式放行与恢复回归。"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from orchestrator import artifacts
from orchestrator.adapters.base import Adapter
from orchestrator.config import ConfigError,parse_config
from orchestrator.contracts import PreflightReport,Result
from orchestrator.engine import Engine
from orchestrator.doctor import diagnose


class PassingReview(Adapter):
    channel='fake'
    def preflight(self):return PreflightReport(adapter='review',ok=True)
    def run(self,prompt,workdir,ctx):
        verdict=ctx.attempt_dir/'review_verdict.json'
        verdict.write_text('{"verdict":"pass","issues":[]}',encoding='utf8')
        report=ctx.attempt_dir/'review_report.md';report.write_text('review pass',encoding='utf8')
        return Result(ok=True,text='pass',report_files=[report],verdict_file=verdict,channel=self.channel)


class ReviewCoveragePolicy(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.ws=self.root/'ws';self.ws.mkdir()
        (self.ws/'a.py').write_text('VALUE = 1\n'*20,encoding='utf8')
        (self.ws/'b.py').write_text('OTHER = 2\n',encoding='utf8')

    def raw(self,action='manual',autonomy=False):
        result={'task_workdir':'ws','state_dir':'state',
                'adapters':{'review':{'type':'fake'}},
                'review_coverage':{'existing_per_file_bytes':8,'existing_total_bytes':12,'max_files':1,'incomplete':action},
                'stages':[{'name':'review','kind':'review','adapter':'review','prompt':'review','max_attempts':1}]}
        if autonomy:
            result['autonomy']={'enabled':True}
            result['run_budget']={'max_tokens':50000,'max_duration_s':120}
        return result

    def engine(self,action='manual',autonomy=False):
        cfg=parse_config(self.raw(action,autonomy),base_dir=self.root)
        return Engine(cfg,{'review':PassingReview()},run_id='review',task='review')

    def test_existing_context_reports_every_truncated_and_uncovered_file(self):
        text,missing=artifacts.existing_code_context_info(self.ws,max_file_bytes=8,max_total_bytes=12,max_files=1)
        self.assertIn('a.py',text)
        self.assertTrue(any('a.py' in n for n in missing))
        self.assertIn('b.py',missing)

    def test_default_incomplete_review_requires_approval_and_persists_evidence(self):
        engine=self.engine()
        self.assertEqual(engine.run()['review'],'awaiting_approval')
        directory=engine.state.current_attempt_dir('review')
        coverage=json.loads((directory/'review-coverage.json').read_bytes())
        success=json.loads((directory/'success.json').read_bytes())
        self.assertFalse(coverage['complete'])
        self.assertEqual(success['review_coverage']['missing'],coverage['missing'])
        self.assertEqual(coverage['scope'],'injected_prompt_files_only')
        self.assertFalse(coverage['cli_extra_reads_verified'])

    def test_incomplete_fail_policy_stays_failed_after_resume(self):
        engine=self.engine('fail')
        self.assertEqual(engine.run()['review'],'failed')
        restarted=self.engine('fail')
        self.assertEqual(restarted.run()['review'],'failed')
        self.assertEqual(len(restarted.state.stage('review').attempts),1)

    def test_explicit_allow_passes_but_preserves_incomplete_marking(self):
        engine=self.engine('allow')
        self.assertEqual(engine.run()['review'],'done')
        self.assertTrue(any(e['kind']=='review_incomplete_allowed' for e in engine.state.events))
        data=json.loads((engine.state.current_attempt_dir('review')/'success.json').read_bytes())
        self.assertFalse(data['review_coverage']['complete'])

    def test_autonomy_manual_policy_does_not_silently_autoapprove(self):
        engine=self.engine(autonomy=True)
        self.assertEqual(engine.run()['review'],'failed')

    def test_policy_change_invalidates_previous_pass(self):
        engine=self.engine('allow')
        self.assertEqual(engine.run()['review'],'done')
        changed=self.engine('fail')
        self.assertEqual(changed.run()['review'],'failed')
        self.assertEqual(len(changed.state.stage('review').attempts),2)

    def test_schema_rejects_unknown_policy_bad_bounds_and_actions(self):
        for field,value in (('diff_lines',0),('max_files',False),('total_bytes',9000000),
                            ('incomplete','guess'),('unknown',1)):
            with self.subTest(field=field):
                raw=self.raw();raw['review_coverage'][field]=value
                with self.assertRaises(ConfigError):parse_config(raw,base_dir=self.root)

    def test_cached_autonomy_prompt_restores_missing_coverage(self):
        engine=self.engine(autonomy=True);engine.prepare()
        spec=engine.config.stages[0]
        first=engine._inputs_hash(spec,0)
        missing=list(engine._review_missing['review'])
        self.assertTrue(missing)
        engine._review_missing={}
        self.assertEqual(engine._inputs_hash(spec,0),first)
        self.assertEqual(engine._review_missing['review'],missing)

    def test_verified_tree_rejects_live_workspace_changes(self):
        for path in self.ws.glob('*.py'):
            path.write_bytes(path.read_bytes().replace(b'\r\n', b'\n'))
        artifacts.ensure_repo(self.ws)
        artifacts._git(['config', 'core.autocrlf', 'false'], self.ws)
        ignored = self.ws / '.gitignore'
        ignored.write_text('ignored.py\n', encoding='utf-8')
        artifacts.commit_files(self.ws, ['.gitignore'], 'add ignore rules')
        head = artifacts.head_sha(self.ws)
        tree = artifacts.verified_workspace_tree(self.ws)
        self.assertEqual(tree, artifacts._git(['rev-parse', 'HEAD^{tree}'], self.ws).stdout.strip())
        source = self.ws / 'a.py'
        original = source.read_bytes()
        before = source.stat()
        source.write_bytes(original.replace(b'1', b'9'))
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertIsNone(artifacts.verified_workspace_tree(self.ws))
        source.write_bytes(original.replace(b'\n', b'\r\n'))
        self.assertIsNone(artifacts.verified_workspace_tree(self.ws))
        source.write_bytes(original)
        (self.ws / 'ignored.py').write_text('SECRET_VALUE = 2\n', encoding='utf-8')
        self.assertIsNone(artifacts.verified_workspace_tree(self.ws))
        self.assertEqual(artifacts.head_sha(self.ws), head)
        self.assertEqual(artifacts._git(['diff', '--cached', '--name-only'], self.ws).stdout, '')

    def test_git_review_resume_revalidates_dirty_input_on_same_engine(self):
        for path in self.ws.glob('*.py'):
            path.write_bytes(path.read_bytes().replace(b'\r\n', b'\n'))
        artifacts.ensure_repo(self.ws)
        engine = self.engine('allow')
        self.assertEqual(engine.run()['review'], 'done')
        self.assertEqual(engine.run()['review'], 'done')
        self.assertEqual(len(engine.state.stage('review').attempts), 1)
        (self.ws / 'b.py').write_text('OTHER = 7\n', encoding='utf-8')
        self.assertEqual(engine.run()['review'], 'done')
        self.assertEqual(len(engine.state.stage('review').attempts), 2)
