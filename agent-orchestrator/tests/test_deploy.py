"""受控部署执行器：前置门禁、审批哈希绑定、干跑预览与审计回执测试。"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

from orchestrator.contracts import StageStatus
from orchestrator.deploy import (
    DeployApproval,
    DeployRejected,
    DeploySpec,
    execute_deploy,
    verify_deploy_gate,
)
from orchestrator.state import RunState


class DeployExecutorTests(unittest.TestCase):
    def _seeded_state(self, root: Path, *, review_done: bool = True) -> RunState:
        state = RunState(root / 'state', 'run-deploy-1')
        testing = state.stage('testing')
        testing.status = StageStatus.DONE
        testing.outputs_hash = 'hash-test-1'
        testing.commit_sha = 'abc1234'
        review = state.stage('review')
        review.status = StageStatus.DONE if review_done else StageStatus.FAILED
        review.outputs_hash = 'hash-review-1' if review_done else None
        state.save()
        return state

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

    def test_dry_run_and_approved_deploy_write_audit_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / 'ws'
            ws.mkdir()
            state = self._seeded_state(root, review_done=True)
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
                commit_sha='abc1234',
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
            self.assertEqual(saved['gate']['commit_sha'], 'abc1234')
