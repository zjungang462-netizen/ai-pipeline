"""Qoder 真机未登录日志只证明认证拒绝，不证明成功协议。"""
import subprocess
import unittest
from pathlib import Path

from orchestrator.adapters.cli_qoder import QoderAdapter
from orchestrator.contracts import ErrorKind,RunContext


class QoderRealAuth(unittest.TestCase):
    def test_real_auth_rejection_never_counts_as_success(self):
        root=Path(__file__).parent
        stderr=(root/'fixtures/qoder_1.1.89_auth_stderr.txt').read_text(encoding='utf8')
        adapter=QoderAdapter('qoder');adapter._version='1.1.89'
        ctx=RunContext(task='probe',run_id='probe',stage='review',workdir=root,runs_dir=root,kind='review')
        result=adapter._parse(subprocess.CompletedProcess([],1,'',stderr),root,ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind,ErrorKind.AUTH)
        # 没有真机成功样本前不放行裸版本号上的合成成功响应。
        result=adapter._parse(subprocess.CompletedProcess([],0,'{"result":"synthetic"}',''),root,ctx)
        self.assertFalse(result.ok)
        self.assertTrue(result.parse_unknown)
