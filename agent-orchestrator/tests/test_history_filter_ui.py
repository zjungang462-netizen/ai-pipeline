"""全历史筛选前端异步契约，不安装 npm 依赖。"""
import shutil
import subprocess
import unittest
from pathlib import Path

class HistoryFilterUI(unittest.TestCase):
    def test_frontend_history_filter_contract(self):
        node=shutil.which('node');self.assertIsNotNone(node)
        result=subprocess.run([node,str(Path(__file__).parent/'js/history_filter_contract.cjs')],
            cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,encoding='utf8',errors='replace',timeout=60)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
        self.assertIn('HISTORY_FILTER_CONTRACT_OK',result.stdout)
