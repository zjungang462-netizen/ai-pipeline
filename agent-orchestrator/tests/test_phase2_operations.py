"""运维命令特征回归：诊断无副作用、清理保留证据及 Windows 路径边界。"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.cli import main
from orchestrator.config import config_key_sets
from orchestrator.doctor import diagnose
from orchestrator.maintenance import (boundary, clean_state, evidence_plan,
                                      remove_checked, tree_info, worktree_plan)
from orchestrator.state import StageLock


class Operations(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.config = self.base/'config.toml'
        self.config.write_text('''task_workdir="workspace"
state_dir="state"
[[stages]]
name="testing"
kind="test"
test_cmd=["python", "-c", "pass"]
''', encoding='utf8')
        self.root = self.base/'state'
        self.run = self.root/'runs/demo'
        self.run.mkdir(parents=True)
        self.state = {'stages': {'coding': {'status': 'failed', 'current_attempt': 4}},
                      'pending_approvals': {}, 'control': {}}
        self.save()
        (self.run/'metrics.json').write_text('{"tokens":42}', encoding='utf8')

    def save(self):
        (self.run/'state.json').write_text(json.dumps(self.state), encoding='utf8')

    def attempt(self, number, marker=None):
        path = self.run/f'stages/coding/attempts/{number}'
        path.mkdir(parents=True)
        (path/'stderr.txt').write_text('failed', encoding='utf8')
        if marker:
            (path/marker).write_text('{}', encoding='utf8')
        self.age(path)
        return path

    def age(self, path, days=60):
        for p in [*path.rglob('*'), path]:
            os.utime(p, (time.time()-days*86400,)*2)

    def doctor(self, extra=''):
        if extra:
            with self.config.open('a', encoding='utf8') as stream: stream.write(extra)
        return diagnose(self.config)

    def test_doctor_valid_config_never_creates_workspace_or_executes_commands(self):
        before = self.config.read_bytes()
        with mock.patch('subprocess.run', side_effect=AssertionError('不能执行子进程')):
            report = self.doctor()
        self.assertTrue(report['ok'], report)
        self.assertFalse((self.base/'workspace').exists())
        self.assertEqual(self.config.read_bytes(), before)

    def test_doctor_collects_unknown_keys_with_suggestions(self):
        self.config.write_text('''task_workdir="workspace"
state_dir="state"
watch_enforc=true
[run_budget]
max_token=5
[[stages]]
name="testing"
kind="test"
apprval=true
test_cmd=["python", "-c", "pass"]
''', encoding='utf8')
        report = self.doctor()
        errors = [c for c in report['checks'] if c['level']=='error']
        self.assertFalse(report['ok'])
        self.assertEqual(len(errors), 3)
        self.assertTrue(any('watch_enforce' in c['hint'] for c in errors))

    def test_doctor_unknown_keys_in_all_policy_sections(self):
        schemas = config_key_sets()
        with self.config.open('a',encoding='utf8') as stream:
            stream.write('[autonomy]\nunknown=1\n[autotune]\nunknown=1\n[run_budget]\nunknown=1\n')
        report=self.doctor()
        fields={c['field'] for c in report['checks'] if c['level']=='error'}
        self.assertEqual(fields,{'autonomy.unknown','autotune.unknown','run_budget.unknown'})
        schemas['root'].add('unknown')
        self.assertNotIn('unknown', config_key_sets()['root'])

    def test_doctor_rejects_missing_config_and_invalid_utf8_without_echoing_content(self):
        self.assertFalse(diagnose(self.base/'missing.toml')['ok'])
        self.config.write_bytes(b'private=\xff')
        report = self.doctor()
        self.assertFalse(report['ok'])
        self.assertNotIn('private=', json.dumps(report))

    def test_doctor_finds_missing_prompt_and_command(self):
        self.config.write_text('''task_workdir="workspace"
state_dir="state"
[adapters.fake]
type="fake"
[[stages]]
name="report"
kind="report"
adapter="fake"
prompt="prompts/missing.md"
[[stages]]
name="test"
kind="test"
test_cmd=["missing-command-xyznothing"]
''', encoding='utf8')
        report=self.doctor()
        self.assertFalse(report['ok'])
        self.assertEqual({c['field'] for c in report['checks'] if c['level']=='error'},
                         {'stages.report.prompt','stages.test.test_cmd'})

    def test_doctor_checks_watch_relative_to_config_and_off(self):
        with self.config.open('r', encoding='utf8') as stream: text=stream.read()
        self.config.write_text('watch_root="workspace"\nwatch_enforce=true\n'+text, encoding='utf8')
        (self.base/'workspace').mkdir()
        self.assertFalse(self.doctor()['ok'])
        self.config.write_text('watch_root="off"\n'+text, encoding='utf8')
        self.assertTrue(self.doctor()['ok'])
        # 状态根包含多个 run，并不等于某一次运行的可写根。
        self.config.write_text('watch_root="state"\n'+text, encoding='utf8')
        self.assertTrue(self.doctor()['ok'])

    def test_doctor_budget_and_config_keys_datetime_use_strict_loader(self):
        report=self.doctor('\n[stages.config_keys]\nwhen=2026-11-17T00:00:00Z\n')
        self.assertFalse(report['ok'])
        self.assertIn('JSON', str(report))

    def test_doctor_reports_all_missing_required_fields(self):
        self.config.write_text('serial=true', encoding='utf8')
        fields={c['field'] for c in self.doctor()['checks']}
        self.assertTrue({'task_workdir','state_dir','stages'} <= fields)

    def test_clean_default_dry_run_keeps_success_approval_latest_and_metrics(self):
        failed=self.attempt(1)
        for number, marker in ((2,'success.json'), (3,'approval.json'), (4,None), (5,None)):
            self.attempt(number, marker)
        before=(self.run/'metrics.json').read_bytes()
        report=clean_state(self.root)
        self.assertTrue(report['ok'], report)
        self.assertTrue(report['dry_run'])
        self.assertEqual([a['path'] for a in report['actions']], [str(failed)])
        self.assertTrue(failed.exists())
        self.assertEqual((self.run/'metrics.json').read_bytes(),before)

    def test_clean_apply_removes_only_old_unprotected_attempts(self):
        old=self.attempt(1); success=self.attempt(2,'decision.json'); current=self.attempt(4)
        report=clean_state(self.root,apply=True)
        self.assertTrue(report['ok'], report)
        self.assertFalse(old.exists())
        self.assertTrue(success.exists() and current.exists())
        self.assertTrue((self.run/'metrics.json').exists())
        self.assertFalse((self.run/'.run.lock').exists())

    def test_clean_keeps_pending_approvals_recovery_and_invalid_state(self):
        old=self.attempt(1); self.attempt(4)
        self.state['pending_approvals']={'coding':{}}
        self.save()
        self.assertFalse(clean_state(self.root,apply=True)['actions'])
        self.state['pending_approvals']={}
        self.state['control']={'recoveries':{'coding':{'attempt':1}}};self.save()
        self.assertFalse(clean_state(self.root,apply=True)['actions'])
        self.state['control']={}; self.state['stages']['coding']['current_attempt']='4';self.save()
        self.assertFalse(clean_state(self.root,apply=True)['ok'])
        self.assertTrue(old.exists())

    def test_clean_locks_and_running_jobs_are_preserved(self):
        old=self.attempt(1);self.attempt(4)
        with StageLock(self.run/'.run.lock'):
            self.assertFalse(clean_state(self.root,apply=True)['actions'])
        (self.run/'job.json').write_text('{"status":"running"}',encoding='utf8')
        self.assertFalse(clean_state(self.root,apply=True)['actions'])
        self.assertTrue(old.exists())

    def test_clean_refuses_changed_directory_before_delete(self):
        old=self.attempt(1)
        info=tree_info(old)
        (old/'new.txt').write_text('new',encoding='utf8')
        with self.assertRaisesRegex(ValueError,'变化'):
            remove_checked(old,self.root,info['fingerprint'])
        self.assertTrue(old.exists())

    def test_clean_refuses_new_approval_during_lock_acquisition(self):
        old=self.attempt(1);self.attempt(4)
        original=StageLock.acquire
        def acquire(lock):
            result=original(lock)
            if lock.lock_path.name=='.run.lock':
                (old/'approval.json').write_text('{}',encoding='utf8')
            return result
        with mock.patch.object(StageLock,'acquire',acquire):
            report=clean_state(self.root,apply=True)
        self.assertFalse(report['ok'])
        self.assertTrue(old.exists())

    def test_clean_retains_symlink_or_junction_without_following(self):
        old=self.attempt(1);self.attempt(4)
        outside=self.base/'outside';outside.mkdir()
        (outside/'important.txt').write_text('keep',encoding='utf8')
        if os.name=='nt':
            subprocess.run(['cmd','/c','mklink','/J',str(old/'linked'),str(outside)],check=True,capture_output=True)
        else:
            (old/'linked').symlink_to(outside,target_is_directory=True)
        report=clean_state(self.root,apply=True)
        self.assertTrue(old.exists())
        self.assertEqual((outside/'important.txt').read_text(encoding='utf8'),'keep')
        self.assertTrue(any('链接' in k['reason'] for k in report['kept']))

    def test_clean_violations_only_when_expired_and_no_approval(self):
        old=self.run/'violations/coding';old.mkdir(parents=True)
        (old/'file.py').write_text('x',encoding='utf8');self.age(old)
        report=clean_state(self.root,apply=True)
        self.assertTrue(report['ok'],report)
        self.assertFalse(old.exists())

    def test_boundary_rejects_link_to_missing_target(self):
        target = self.base/'missing-target'
        link = self.base/'broken-link'
        if os.name == 'nt':
            subprocess.run(['cmd','/c','mklink','/J',str(link),str(target)],
                           check=True,capture_output=True)
        else:
            link.symlink_to(target,target_is_directory=True)
        # 目标不存在时 exists() 为 False；boundary 仍须按链接/reparse point 拒绝。
        self.assertFalse(link.exists())
        with self.assertRaises(ValueError):
            boundary(link,self.base)

    def test_clean_rejects_root_junction(self):
        alias=self.base/'alias'
        if os.name=='nt':
            subprocess.run(['cmd','/c','mklink','/J',str(alias),str(self.root)],check=True,capture_output=True)
        else:
            alias.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(ValueError):clean_state(alias,apply=True)

    def test_evidence_retention_age_cap_and_unknown_files(self):
        root=self.base/'evidence';root.mkdir()
        for n in range(3):
            folder=root/str(n);folder.mkdir();(folder/'evidence.json').write_text('x'*20,encoding='utf8')
            self.age(folder,days=20-n)
        (root/'manual.txt').write_text('manual',encoding='utf8')
        plan=evidence_plan(root,max_bytes=30)
        self.assertEqual(len(plan['actions']),2)
        self.assertEqual(plan['remaining_bytes'],26)
        self.assertTrue((root/'manual.txt').exists())

    def test_clean_cli_defaults_and_invalid_threshold(self):
        self.attempt(1);self.attempt(4)
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            code=main(['clean','--state-dir',str(self.root)])
        self.assertEqual(code,0)
        self.assertTrue(json.loads(out.getvalue())['dry_run'])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['clean','--state-dir',str(self.root),'--older-than','nan']),2)

    def test_doctor_json_exit_codes(self):
        with contextlib.redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(main(['doctor',str(self.config),'--json']),0)
        self.assertTrue(json.loads(stream.getvalue())['ok'])

    def test_worktree_prune_only_invalid_registrations_under_state_root(self):
        repo=self.base/'repo';repo.mkdir()
        def git(*args):
            return subprocess.run(['git','-C',str(repo),*args],env=dict(os.environ),check=True,capture_output=True)
        git('init','-q');git('config','user.name','test');git('config','user.email','test@example.invalid')
        git('commit','--allow-empty','-qm','base')
        target=self.root/'old-worktree'
        git('worktree','add','--detach',str(target))
        shutil.rmtree(target)
        row=worktree_plan(repo,self.root,time.time()+1)
        self.assertEqual(row['kind'],'worktree_registration')
        report=clean_state(self.root,repo=repo,older_than=0.000001,apply=True,now=time.time()+10)
        self.assertTrue(report['ok'],report)
        self.assertNotIn(str(target).replace('\\','/'),git('worktree','list','--porcelain').stdout.decode())
        outside=self.base/'outside-worktree'
        git('worktree','add','--detach',str(outside));shutil.rmtree(outside)
        with self.assertRaises(ValueError):worktree_plan(repo,self.root,time.time()+1)


if __name__=='__main__':unittest.main()
