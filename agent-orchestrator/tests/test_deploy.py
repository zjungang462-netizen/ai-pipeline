"""受控部署执行器：前置门禁、审批哈希绑定、干跑预览与审计回执测试。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from orchestrator import artifacts
from orchestrator.contracts import StageStatus
from orchestrator.deploy import (
    DeployApproval,
    DeployRejected,
    DeploySpec,
    execute_deploy,
    verify_deploy_gate,
)
from orchestrator.state import RunState, StageLock, _pid_alive


class DeployExecutorTests(unittest.TestCase):
    def _seeded_state(self, root: Path, *, review_done: bool = True,
                      state_dir: Path | None = None) -> RunState:
        state = RunState(state_dir or root / 'state', 'run-deploy-1')
        testing = state.stage('testing')
        testing.status = StageStatus.DONE
        testing.outputs_hash = 'hash-test-1'
        testing.commit_sha = 'abc1234'
        review = state.stage('review')
        review.status = StageStatus.DONE if review_done else StageStatus.FAILED
        review.outputs_hash = 'hash-review-1' if review_done else None
        state.save()
        return state

    def _git(self, args: list[str], cwd: Path) -> str:
        proc = subprocess.run(
            ['git', *args], cwd=str(cwd), env=dict(os.environ),
            capture_output=True, text=True, encoding='utf-8', errors='replace', check=True,
        )
        return proc.stdout.strip()

    def _seeded_git_state(self, root: Path, workdir: Path) -> tuple[RunState, str]:
        workdir.mkdir()
        hooks = root / 'no-hooks'
        hooks.mkdir()
        self._git(['init', '-q', '--template='], workdir)
        self._git(['config', 'user.name', 'Deploy Test'], workdir)
        self._git(['config', 'user.email', 'deploy-test@local'], workdir)
        self._git(['config', 'commit.gpgsign', 'false'], workdir)
        self._git(['config', 'core.hooksPath', str(hooks)], workdir)
        (workdir / 'release.txt').write_text('approved release\n', encoding='utf-8')
        self._git(['add', '--', 'release.txt'], workdir)
        self._git(['commit', '-q', '-m', 'approved release'], workdir)
        commit_sha = self._git(['rev-parse', 'HEAD'], workdir)
        state = self._seeded_state(root)
        state.stage('testing').commit_sha = commit_sha
        state.save()
        return state, commit_sha

    def _approval(self, state: RunState, commit_sha: str | None) -> DeployApproval:
        gate = verify_deploy_gate(state, ('testing', 'review'), None, dry_run=True)
        return DeployApproval(
            state.run_id, gate['outputs_hash'], commit_sha=commit_sha, approved_by='owner',
        )

    def _marker_spec(self, marker: Path) -> DeploySpec:
        script = (
            "from pathlib import Path; "
            f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')"
        )
        return DeploySpec(
            name='must-not-run', command=(sys.executable, '-c', script), timeout_s=30,
        )

    def test_gate_rejects_unfinished_stage_missing_approval_or_mismatched_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad_state = self._seeded_state(root, review_done=False)
            with self.assertRaises(DeployRejected):
                verify_deploy_gate(bad_state, ('testing', 'review'), None, dry_run=True)

            good_state = self._seeded_state(root, review_done=True)
            with self.assertRaises(DeployRejected):
                verify_deploy_gate(good_state, ('testing', 'review'), None, dry_run=False)

            wrong_hash = DeployApproval('run-deploy-1', 'wrong-hash', commit_sha='abc1234')
            with self.assertRaises(DeployRejected):
                verify_deploy_gate(good_state, ('testing', 'review'), wrong_hash, dry_run=False)

            gate = verify_deploy_gate(good_state, ('testing', 'review'), None, dry_run=True)
            wrong_commit = DeployApproval('run-deploy-1', gate['outputs_hash'], commit_sha='deadbeef')
            with self.assertRaises(DeployRejected):
                verify_deploy_gate(good_state, ('testing', 'review'), wrong_commit, dry_run=False)

    def test_gate_rejects_completed_code_after_required_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self._seeded_state(Path(tmp), review_done=True)
            coding = state.stage('coding')
            coding.status = StageStatus.DONE
            coding.outputs_hash = 'hash-later-code'
            coding.commit_sha = 'later-code-sha'
            state.save()
            with self.assertRaisesRegex(DeployRejected, 'coding'):
                verify_deploy_gate(state, ('testing', 'review'), None, dry_run=True)
            with self.assertRaisesRegex(DeployRejected, 'coding'):
                verify_deploy_gate(
                    state, ('testing', 'review'),
                    DeployApproval('run-deploy-1', 'unused', commit_sha='later-code-sha'),
                    dry_run=False,
                )

    @unittest.skipUnless(artifacts.git_available(), '需要 git')
    def test_dry_run_and_approved_deploy_write_audit_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / 'ws'
            state, commit_sha = self._seeded_git_state(root, ws)
            spec = DeploySpec(
                name='local-release',
                command=(sys.executable, '-c', 'print("deployed-ok")'),
                dry_run_command=(sys.executable, '-c', 'print("dry-run-ok")'),
                timeout_s=30,
            )

            dry = execute_deploy(spec, state, ws, ('testing', 'review'), dry_run=True)
            self.assertTrue(dry['ok'])
            self.assertTrue(dry['dry_run'])
            self.assertIn('dry-run-ok', dry['stdout'])

            approval = DeployApproval(
                run_id='run-deploy-1',
                outputs_hash=dry['gate']['outputs_hash'],
                commit_sha=commit_sha,
                approved_by='owner',
            )
            live = execute_deploy(spec, state, ws, ('testing', 'review'), approval=approval, dry_run=False)
            self.assertTrue(live['ok'])
            self.assertFalse(live['dry_run'])
            self.assertIn('deployed-ok', live['stdout'])

            receipt_file = state.run_dir / 'deployments' / live['deploy_id'] / 'deploy_receipt.json'
            self.assertTrue(receipt_file.is_file())
            saved = json.loads(receipt_file.read_text(encoding='utf-8'))
            self.assertEqual(saved['approved_by'], 'owner')
            self.assertEqual(saved['gate']['commit_sha'], commit_sha)

            # 干跑无 dry_run_command 时缺失命令入口返回 ok=False，且回执 command 脱敏已知密钥
            import os
            os.environ['TEST_DEPLOY_SECRET'] = 'deploy-secret-token-998877'
            self.addCleanup(lambda: os.environ.pop('TEST_DEPLOY_SECRET', None))
            missing_spec = DeploySpec(
                name='missing-cmd',
                command=('definitely_missing_deploy_bin_xyz', '--token', 'deploy-secret-token-998877'),
            )
            bad_dry = execute_deploy(missing_spec, state, ws, ('testing', 'review'), dry_run=True)
            self.assertFalse(bad_dry['ok'])
            self.assertNotIn('deploy-secret-token-998877', ' '.join(bad_dry['command']))
            self.assertIn('[REDACTED]', ' '.join(bad_dry['command']))

            # 前置阶段仅写 testing/review 时，仍须绑定上游已完成的 coding commit_sha，且超时异常不得泄露密钥
            state2 = RunState(root / 'state2', 'run-deploy-2')
            coding = state2.stage('coding')
            coding.status = StageStatus.DONE
            coding.outputs_hash = 'hash-code-2'
            coding.commit_sha = commit_sha
            t2 = state2.stage('testing')
            t2.status = StageStatus.DONE
            t2.outputs_hash = 'hash-test-2'
            r2 = state2.stage('review')
            r2.status = StageStatus.DONE
            r2.outputs_hash = 'hash-review-2'
            state2.save()
            gate2 = verify_deploy_gate(state2, ('testing', 'review'), None, dry_run=True)
            self.assertEqual(gate2['commit_sha'], commit_sha)
            with self.assertRaises(DeployRejected):
                verify_deploy_gate(
                    state2, ('testing', 'review'),
                    DeployApproval('run-deploy-2', gate2['outputs_hash'], commit_sha='wrongsha'),
                    dry_run=False,
                )
            timeout_spec = DeploySpec(
                name='timeout-cmd',
                command=(sys.executable, '-c', 'import time; time.sleep(5)', 'deploy-secret-token-998877'),
                timeout_s=1,
            )
            timed_out = execute_deploy(
                timeout_spec, state2, ws, ('testing', 'review'),
                approval=DeployApproval('run-deploy-2', gate2['outputs_hash'], commit_sha=commit_sha),
                dry_run=False,
            )
            self.assertFalse(timed_out['ok'])
            self.assertNotIn('deploy-secret-token-998877', timed_out['stderr'])
            self.assertIn('[REDACTED]', timed_out['stderr'])

    def test_report_only_deploy_does_not_require_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / 'report-ws'
            workdir.mkdir()
            state = RunState(root / 'report-state', 'run-report')
            for name, outputs_hash in (('testing', 'report-test'), ('review', 'report-review')):
                stage = state.stage(name)
                stage.status = StageStatus.DONE
                stage.outputs_hash = outputs_hash
            state.save()
            spec = DeploySpec(
                name='report-only', command=(sys.executable, '-c', 'print("report-ok")'), timeout_s=30,
            )
            receipt = execute_deploy(
                spec, state, workdir, ('testing', 'review'),
                approval=self._approval(state, None), dry_run=False,
            )
            self.assertTrue(receipt['ok'])
            self.assertIsNone(receipt['gate']['commit_sha'])
            self.assertIn('report-ok', receipt['stdout'])
            self.assertFalse((workdir / '.git').exists())
            self.assertFalse((workdir.parent / f'.{workdir.name}.pipeline.lock').exists())

    @unittest.skipUnless(artifacts.git_available(), '需要 git')
    def test_head_change_after_approval_rejects_without_running_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / 'ws'
            state, approved_sha = self._seeded_git_state(root, workdir)
            approval = self._approval(state, approved_sha)
            self._git(['commit', '-q', '--allow-empty', '-m', 'after approval'], workdir)
            self.assertNotEqual(self._git(['rev-parse', 'HEAD'], workdir), approved_sha)
            marker = root / 'head-change-ran'

            with self.assertRaisesRegex(DeployRejected, 'HEAD'):
                execute_deploy(
                    self._marker_spec(marker), state, workdir, ('testing', 'review'),
                    approval=approval, dry_run=False,
                )
            self.assertFalse(marker.exists())

    @unittest.skipUnless(artifacts.git_available(), '需要 git')
    def test_dirty_workdir_after_approval_rejects_without_running_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / 'ws'
            state, approved_sha = self._seeded_git_state(root, workdir)
            approval = self._approval(state, approved_sha)
            (workdir / 'unapproved.txt').write_text('not accepted\n', encoding='utf-8')
            marker = root / 'dirty-workdir-ran'

            with self.assertRaisesRegex(DeployRejected, '未提交改动'):
                execute_deploy(
                    self._marker_spec(marker), state, workdir, ('testing', 'review'),
                    approval=approval, dry_run=False,
                )
            self.assertFalse(marker.exists())

    @unittest.skipUnless(artifacts.git_available(), '需要 git')
    def test_ignored_file_after_approval_rejects_without_running_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / 'ws'
            state, approved_sha = self._seeded_git_state(root, workdir)
            approval = self._approval(state, approved_sha)
            info_dir = workdir / '.git' / 'info'
            info_dir.mkdir(exist_ok=True)
            (info_dir / 'exclude').write_text('secret.env\n', encoding='utf-8')
            (workdir / 'secret.env').write_text('not approved\n', encoding='utf-8')
            self.assertEqual(self._git(['status', '--porcelain'], workdir), '')
            marker = root / 'ignored-workdir-ran'

            with self.assertRaisesRegex(DeployRejected, 'ignored'):
                execute_deploy(
                    self._marker_spec(marker), state, workdir, ('testing', 'review'),
                    approval=approval, dry_run=False,
                )
            self.assertFalse(marker.exists())

    @unittest.skipUnless(artifacts.git_available(), '需要 git')
    def test_ignored_pipeline_state_dir_does_not_block_deploy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / 'ws'
            _, approved_sha = self._seeded_git_state(root, workdir)
            state_dir = workdir / '.orchestrator'
            state = self._seeded_state(root, state_dir=state_dir)
            state.stage('testing').commit_sha = approved_sha
            state.save()
            info_dir = workdir / '.git' / 'info'
            info_dir.mkdir(exist_ok=True)
            (info_dir / 'exclude').write_text('.orchestrator/\n', encoding='utf-8')
            marker = root / 'state-dir-deploy-ran'

            receipt = execute_deploy(
                self._marker_spec(marker), state, workdir, ('testing', 'review'),
                approval=self._approval(state, approved_sha), dry_run=False,
            )
            self.assertTrue(receipt['ok'])
            self.assertTrue(marker.is_file())

    @unittest.skipUnless(artifacts.git_available(), '需要 git')
    def test_busy_pipeline_lock_rejects_without_running_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / 'ws'
            state, approved_sha = self._seeded_git_state(root, workdir)
            approval = self._approval(state, approved_sha)
            marker = root / 'busy-lock-ran'
            pipeline_lock = workdir.parent / f'.{workdir.name}.pipeline.lock'

            with StageLock(pipeline_lock):
                with self.assertRaisesRegex(DeployRejected, '流水线锁'):
                    execute_deploy(
                        self._marker_spec(marker), state, workdir, ('testing', 'review'),
                        approval=approval, dry_run=False,
                    )
                self.assertTrue(pipeline_lock.is_file())
            self.assertFalse(marker.exists())
            self.assertFalse(pipeline_lock.exists())

    @unittest.skipUnless(artifacts.git_available(), '需要 git')
    def test_deploy_output_is_bounded_and_keeps_diagnostic_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / 'ws'
            state, approved_sha = self._seeded_git_state(root, workdir)
            script = (
                "import sys; "
                "sys.stdout.write('A' * 20000 + 'STDOUT_END'); "
                "sys.stderr.write('B' * 20000 + 'STDERR_END')"
            )
            spec = DeploySpec(
                name='large-output', command=(sys.executable, '-c', script), timeout_s=30,
            )
            receipt = execute_deploy(
                spec, state, workdir, ('testing', 'review'),
                approval=self._approval(state, approved_sha), dry_run=False,
            )
            self.assertTrue(receipt['ok'])
            self.assertEqual(len(receipt['stdout']), 16000)
            self.assertEqual(len(receipt['stderr']), 16000)
            self.assertTrue(receipt['stdout'].endswith('STDOUT_END'))
            self.assertTrue(receipt['stderr'].endswith('STDERR_END'))

    @unittest.skipUnless(artifacts.git_available(), '需要 git')
    def test_timeout_kills_descendant_after_direct_child_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / 'ws'
            state, approved_sha = self._seeded_git_state(root, workdir)
            ready = root / 'child.ready'
            marker = root / 'child.escaped'
            child_code = (
                "import os,time; from pathlib import Path; "
                f"Path({str(ready)!r}).write_text(str(os.getpid()), encoding='utf-8'); "
                "time.sleep(4); "
                f"Path({str(marker)!r}).write_text('escaped', encoding='utf-8')"
            )
            parent_code = (
                "import subprocess,sys; "
                f"subprocess.Popen([sys.executable, '-c', {child_code!r}])"
            )

            def cleanup_child() -> None:
                if not ready.is_file():
                    return
                try:
                    child_pid = int(ready.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    return
                if not _pid_alive(child_pid):
                    return
                if os.name == 'nt':
                    subprocess.run(
                        ['taskkill', '/PID', str(child_pid), '/T', '/F'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                    )
                else:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except OSError:
                        pass

            self.addCleanup(cleanup_child)
            spec = DeploySpec(
                name='timeout-tree', command=(sys.executable, '-c', parent_code), timeout_s=2,
            )
            receipt = execute_deploy(
                spec, state, workdir, ('testing', 'review'),
                approval=self._approval(state, approved_sha), dry_run=False,
            )
            self.assertFalse(receipt['ok'])
            self.assertEqual(receipt['exit_code'], -1)
            self.assertIn('超时', receipt['stderr'])
            self.assertTrue(ready.is_file(), '后代应已启动，才能验证超时清理')
            time.sleep(5)
            self.assertFalse(marker.exists(), '超时后部署后代仍在写入标记文件')
