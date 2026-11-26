"""分层必须完整且互斥，避免 quick/full 重复执行或遗漏新增测试。"""
import unittest

from scripts.verify_delivery import collect_tests, group_tests_by_module, schedule_shards, test_ids


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

    def test_group_tests_by_module_preserves_all_ids_deterministically(self):
        ids = test_ids(collect_tests(suite='fast'))
        grouped = group_tests_by_module(ids)
        flattened = [item for _, names in grouped for item in names]
        self.assertEqual(sorted(flattened), ids)
        self.assertEqual(len(flattened), len(set(flattened)))
        counts = [len(names) for _, names in grouped]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_timed_shards_balance_cost_without_splitting_modules(self):
        grouped = [('light', ['light.one', 'light.two']), ('heavy', ['heavy.one']), ('new', ['new.one'])]
        timings = [{'test': name, 'duration_s': duration} for name, duration in (
            ('light.one', 1.0), ('light.two', 1.0), ('heavy.one', 30.0))]
        shards = schedule_shards(grouped, timings, 2)
        self.assertEqual(shards, [('shard-01', ['heavy.one']), ('shard-02', ['light.one', 'light.two', 'new.one'])])
        self.assertEqual(schedule_shards(grouped, [], 2), grouped)
