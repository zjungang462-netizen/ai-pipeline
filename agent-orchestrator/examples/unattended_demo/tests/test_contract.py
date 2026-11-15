"""受控双缺陷示例：测试由配置预先授权，编码阶段不能改写。"""
import unittest
from name_ops import clean_name
from count_ops import parse_count


class Contract(unittest.TestCase):
    def test_name(self):
        self.assertEqual(clean_name('  ALICE  '), 'alice')

    def test_count(self):
        self.assertEqual(parse_count('2'), 2)
