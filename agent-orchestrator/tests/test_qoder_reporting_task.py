"""原有运维摘要对畸形事件/字段应保守降级，不让一条记录破坏失败看板。"""
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
from orchestrator.reporting import failure_categories,usage_summary


class ReportingMetadata(unittest.TestCase):
    def test_unhashable_error_and_event_kind_are_ignored_without_losing_failed_status(self):
        state={'stages':{'coding':{'status':'failed'}},'events':[{'kind':[],'error_kind':{'bad':1}},
            {'kind':'attempt_fail','error_kind':['timeout']}], 'usage_total':{'tokens':4}}
        self.assertEqual(failure_categories(state),['unknown'])
        self.assertEqual(usage_summary('run',state)['tokens'],4)

    def test_bad_fail_reasons_are_not_hashable_categories(self):
        state={'stages':{'coding':{'status':'failed'}},'events':[]}
        metrics={'fail_reasons':[[],{},'budget_exhausted']}
        self.assertEqual(failure_categories(state,metrics=metrics),['budget_exhausted'])

    def test_non_list_event_or_reason_collection_is_treated_as_missing(self):
        state={'stages':{'coding':{'status':'failed'}},'events':None}
        self.assertEqual(failure_categories(state,metrics={'fail_reasons':None}),['unknown'])

    def test_valid_explicit_signals_are_preserved_without_guessing_error_prose(self):
        state={'stages':{'test':{'status':'failed','error':'timeout word is not a signal'}},
               'events':[{'kind':'test_fail','error_kind':'timeout'}]}
        self.assertEqual(failure_categories(state),['test_failure','timeout'])
        self.assertEqual(failure_categories({'stages':{'x':{'status':'failed','error':'auth'}}}),['unknown'])


if __name__=='__main__':unittest.main()
