"""真实 Qwen 0.26.0 数组协议样本与严格成功终态校验。"""
import copy
import json
import subprocess
import unittest
from pathlib import Path

from orchestrator.adapters.cli_qwen import QwenAdapter
from orchestrator.contracts import RunContext


class Qwen026Real(unittest.TestCase):
    def setUp(self):
        self.adapter=QwenAdapter('qwen');self.adapter._version='0.26.0'
        self.root=Path(__file__).parent
        self.events=json.loads((self.root/'fixtures/qwen_0.26.0_real.json').read_bytes())

    def parse(self,events):
        ctx=RunContext(task='probe',run_id='probe',stage='review',workdir=self.root,runs_dir=self.root,kind='review')
        proc=subprocess.CompletedProcess([],0,json.dumps(events),'')
        return self.adapter._parse(proc,self.root,ctx)

    def test_real_sample_extracts_final_text_and_reported_usage(self):
        result=self.parse(self.events)
        self.assertTrue(result.ok)
        self.assertEqual(result.text,'QWEN_PROTOCOL_CAPTURE_20261117')
        self.assertEqual(result.usage['tokens'],7282)
        self.assertEqual(result.usage['cache_read_tokens'],0)
        self.assertFalse(result.usage['usage_unknown'])

    def test_missing_duplicate_nonterminal_or_error_results_never_pass(self):
        for label,events in (
            ('missing',self.events[:-1]),
            ('duplicate',self.events+[self.events[-1]]),
            ('nonterminal',self.events+[{'type':'assistant'}]),
            ('error',self.events[:-1]+[{**self.events[-1],'is_error':True}])):
            with self.subTest(label=label):
                result=self.parse(events)
                self.assertFalse(result.ok)
                self.assertTrue(result.parse_unknown)

    def test_version_and_usage_mismatch_never_pass(self):
        for value in (False,-1,'7282'):
            events=copy.deepcopy(self.events);events[-1]['usage']['total_tokens']=value
            self.assertFalse(self.parse(events).ok)
        events=copy.deepcopy(self.events);events[0]['qwen_code_version']='0.27.0'
        self.assertFalse(self.parse(events).ok)
        self.adapter._version='0.26.99'
        self.assertFalse(self.parse(self.events).ok)

    def test_assistant_text_without_success_result_cannot_complete(self):
        result=self.parse([{'type':'assistant','message':'claims success'}])
        self.assertFalse(result.ok)
