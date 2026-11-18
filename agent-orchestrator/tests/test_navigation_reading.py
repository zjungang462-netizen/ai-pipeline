"""任务导航与实时阅读的交互回归，不连接模型。"""
import shutil
import subprocess
import unittest
from pathlib import Path


class NavigationReading(unittest.TestCase):
    def test_navigation_and_live_reading(self):
        node = shutil.which('node')
        self.assertIsNotNone(node)
        result = subprocess.run([node, str(Path(__file__).parent / 'js/navigation_reading.cjs')],
                                capture_output=True, text=True, encoding='utf8', timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('NAVIGATION_READING_OK: 10 scenarios', result.stdout)


if __name__ == '__main__':
    unittest.main()
