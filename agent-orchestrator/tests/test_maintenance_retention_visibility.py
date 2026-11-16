"""真实维护需求：自动留证时记录保留容量不足，不能只在手动 clean 时可见。"""
import json
import tempfile
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0,str(Path.cwd()))
from scripts.failure_evidence import capture,MAX_BUNDLE_BYTES
from orchestrator.maintenance import DEFAULT_EVIDENCE_BYTES


class RetentionVisibility(unittest.TestCase):
    def test_retention_issue_is_visible_without_absolute_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';source.mkdir()
            output=root/'evidence'
            reason='protected_files_over_capacity'
            plan={'actions':[],'issues':[{'path':'C:/private/path-do-not-copy','reason':reason}],
                  'total_bytes':DEFAULT_EVIDENCE_BYTES+100,'remaining_bytes':DEFAULT_EVIDENCE_BYTES+100}
            with mock.patch('orchestrator.maintenance.prune_evidence',return_value=plan):
                saved=capture('retention-failure',[source],output)
            raw=saved.read_bytes();data=json.loads(raw)
            self.assertIn('retention',data)
            self.assertIn(reason,json.dumps(data['retention']))
            self.assertEqual(data['retention']['remaining_bytes'],plan['remaining_bytes'])
            self.assertNotIn('path-do-not-copy',raw.decode('utf8'))
            self.assertLessEqual(len(raw),MAX_BUNDLE_BYTES)

    def test_retention_error_does_not_destroy_new_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';source.mkdir()
            with mock.patch('orchestrator.maintenance.prune_evidence',side_effect=OSError('unreadable')):
                saved=capture('retention-error',[source],root/'evidence')
            self.assertTrue(saved.is_file())
            data=json.loads(saved.read_bytes())
            self.assertTrue(any(row['reason']=='retention_cleanup_failed' for row in data['issues']))


if __name__=='__main__':unittest.main()
