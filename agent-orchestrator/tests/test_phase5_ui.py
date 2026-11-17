"""前端运维面板和重连生命周期契约。"""
import subprocess
import unittest
from pathlib import Path


class Phase5UI(unittest.TestCase):
    def test_templates_compare_failure_paging_and_budget_render(self):
        script=Path(__file__).parent/'js/insights.cjs'
        run=subprocess.run(['node',str(script)],capture_output=True,text=True,encoding='utf8',timeout=30)
        self.assertEqual(run.returncode,0,run.stdout+'\n'+run.stderr)
        self.assertIn('INSIGHTS_UI_OK',run.stdout)
