"""分层必须完整且互斥，避免 quick/full 重复执行或遗漏新增测试。"""
import unittest

from scripts.verify_delivery import collect_tests, test_ids


class TestLayers(unittest.TestCase):
    def test_fast_and_slow_partition_the_full_collection(self):
        fast=set(test_ids(collect_tests(suite='fast')))
        slow=set(test_ids(collect_tests(suite='slow')))
        full=set(test_ids(collect_tests(suite='full')))
        self.assertFalse(fast & slow)
        self.assertEqual(fast | slow,full)
        self.assertTrue(fast and slow)

    def test_quick_uses_the_same_tests_as_fast(self):
        self.assertEqual(test_ids(collect_tests(quick=True)),test_ids(collect_tests(suite='fast')))
