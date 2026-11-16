"""受保护的验收条件；模型不得修改本文件。"""
import unittest

from count_ops import parse_count
from name_ops import clean_name


class Contract(unittest.TestCase):
    def test_name_normalizes_case(self):
        self.assertEqual(clean_name('  Alice  '), 'alice')

    def test_name_empty(self):
        self.assertEqual(clean_name('   '), '')

    def test_name_preserves_internal_spacing(self):
        self.assertEqual(clean_name(' A  B '), 'a  b')

    def test_count_value(self):
        self.assertEqual(parse_count('7'), 7)

    def test_count_negative(self):
        self.assertEqual(parse_count('-2'), -2)

    def test_invalid_count(self):
        with self.assertRaises(ValueError):
            parse_count('invalid')
