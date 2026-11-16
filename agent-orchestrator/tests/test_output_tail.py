"""分块尾部须与旧字符串切片一致，覆盖中文、短读、巨大块和真实管道。"""
import hashlib
import random
import subprocess
import sys
import tempfile
import unittest

from orchestrator.adapters.cli_base import run_cli_process
from orchestrator.output_tail import TextTail
from orchestrator.safety import build_subprocess_env


class OutputTail(unittest.TestCase):
    def test_arbitrary_chunks_match_last_characters_after_every_append(self):
        rng = random.Random(20261116)
        alphabet = 'ab汉字😀\n\r\0'
        for limit in (1, 17, 4096, 8193):
            with self.subTest(limit=limit):
                tail = TextTail(limit)
                expected = ''
                for _ in range(100):
                    value = ''.join(rng.choices(alphabet, k=rng.randrange(0, 12500)))
                    tail.append(value)
                    expected = (expected+value)[-limit:]
                    self.assertEqual(tail.getvalue(), expected)
                    self.assertEqual(tail.getvalue(), expected, '读取不可消耗尾部')

    def test_empty_exact_limit_and_huge_unicode_chunk(self):
        tail = TextTail(4097)
        self.assertEqual(tail.getvalue(), '')
        for value in ('x'*4097, '', '😀汉'*100000, '尾', '\n'):
            previous = tail.getvalue()
            tail.append(value)
            self.assertEqual(tail.getvalue(), (previous+value)[-4097:])

    def test_tiny_reads_and_append_after_partial_eviction(self):
        tail = TextTail(10000)
        body = '开始汉😀' * 4096
        for character in body:
            tail.append(character)
        self.assertEqual(tail.getvalue(), body[-10000:])
        tail.append('末尾'*6000)
        self.assertEqual(tail.getvalue(), ('末尾'*6000)[-10000:])

    def test_invalid_limits_fail_explicitly(self):
        for limit in (0, -1, True, 1.5, None):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                TextTail(limit)


class OutputTailProcess(unittest.TestCase):
    def execute(self, script, text=True, timeout=30):
        with tempfile.TemporaryDirectory() as folder:
            return run_cli_process([sys.executable, '-u', '-c',
                                    'import sys\nsys.stdin.buffer.read()\n'+script],
                                   cwd=folder, env=build_subprocess_env(), input='',
                                   text=text, timeout=timeout)

    def test_independent_stream_tails_preserve_unicode_and_return_code(self):
        limit = 4*1024*1024
        # 每条流字符数超过上限，UTF-8 字节边界与字符边界不同。
        repeat = 1100000
        script = (f'sys.stdout.buffer.write(("A汉😀\\n"*{repeat}+"OUT_END").encode("utf8"))\n'
                  f'sys.stderr.buffer.write(("B文😀\\n"*{repeat}+"ERR_END").encode("utf8"))\n'
                  'sys.exit(7)\n')
        result = self.execute(script, timeout=90)
        self.assertEqual(result.returncode, 7)
        for actual, prefix, ending in ((result.stdout, 'A汉😀\n', 'OUT_END'),
                                        (result.stderr, 'B文😀\n', 'ERR_END')):
            expected = (prefix*repeat+ending)[-limit:]
            self.assertEqual(len(actual), limit)
            self.assertEqual(hashlib.sha256(actual.encode('utf8')).digest(),
                             hashlib.sha256(expected.encode('utf8')).digest())

    def test_binary_return_still_uses_incremental_utf8_decoder(self):
        script = ('sys.stdout.buffer.write(b"A\\xf0\\x9f");sys.stdout.buffer.flush()\n'
                  'sys.stdout.buffer.write(b"\\x98\\x80\\xe6");sys.stdout.buffer.flush()\n'
                  'sys.stderr.buffer.write("错误".encode("utf8"))\n')
        result = self.execute(script, text=False)
        self.assertEqual(result.stdout, 'A😀�'.encode('utf8'))
        self.assertEqual(result.stderr, '错误'.encode('utf8'))

    def test_timeout_retains_both_tails(self):
        script = ('import time\n'
                  'sys.stdout.write("OUT_SENTINEL");sys.stdout.flush()\n'
                  'sys.stderr.write("ERR_SENTINEL");sys.stderr.flush()\n'
                  'time.sleep(30)\n')
        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            self.execute(script, timeout=3)
        self.assertIn('OUT_SENTINEL', raised.exception.stdout)
        self.assertIn('ERR_SENTINEL', raised.exception.stderr)


if __name__ == '__main__':
    unittest.main()
