"""真实维护需求：摘要字段本身损坏时保留可读运行量，不让失败看板异常。"""
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
from orchestrator.reporting import failure_categories,usage_summary


class MetadataShapes(unittest.TestCase):
    def test_invalid_stages_collection_does_not_hide_explicit_event(self):
        for stages in (None,[],42,'broken'):
            with self.subTest(stages=stages):
                self.assertEqual(failure_categories({'stages':stages,'events':[{'kind':'test_fail'}]}),['test_failure'])

    def test_invalid_summary_stage_collection_retains_run_usage(self):
        for stages in (None,[],42):
            with self.subTest(stages=stages):
                result=usage_summary('broken',{'stages':stages,'usage_total':{'tokens':15}})
                self.assertEqual(result['tokens'],15)
                self.assertEqual(result['stages'],[])

    def test_invalid_stage_usage_map_or_row_is_unknown_not_crash(self):
        for stage_usage in (None,[],{'test':None},{'test':[]}):
            with self.subTest(stage_usage=stage_usage):
                result=usage_summary('run',{'stages':{'test':{'status':'done'}},'stage_usage':stage_usage,'usage_total':{'tokens':5}})
                self.assertEqual(len(result['stages']),1)
                self.assertIsNone(result['stages'][0]['tokens'])

    def test_frozen_metrics_remain_authoritative_and_valid_classification_unchanged(self):
        state={'usage_total':{'tokens':999},'stages':{'x':{'status':'failed'}},'events':[{'kind':'test_fail'}]}
        metrics={'tokens':20,'stages':{'test':{'status':'done','tool':'test_cmd'}},'stage_usage':{'test':{'tokens':20}}}
        result=usage_summary('run',state,metrics)
        self.assertEqual(result['tokens'],20);self.assertEqual(result['stages'][0]['tokens'],20)
        self.assertEqual(failure_categories(state),['test_failure'])


if __name__=='__main__':unittest.main()
