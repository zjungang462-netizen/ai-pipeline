"""双模型并行 Best-of-N 竞技场：隔离 worktree 比拼、确定性测试验收与人工 ff-only 合并测试。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from orchestrator.arena import apply_arena_winner, run_arena
from orchestrator.state import atomic_write_json
from orchestrator.config import load_config
from orchestrator.contracts import Result


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ['git', *args], cwd=str(cwd), env=dict(os.environ), capture_output=True, text=True, encoding='utf-8', check=True
    )
    return proc.stdout.strip()


class TestArena(unittest.TestCase):
    def test_parallel_worktree_arena_diff_and_controlled_ff_apply(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        repo = root / 'repo'
        repo.mkdir()
        _git(['init'], repo)
        _git(['config', 'user.name', 'Test User'], repo)
        _git(['config', 'user.email', 'test@local'], repo)
        (repo / 'calc.py').write_text('def add(a, b):\n    return a - b\n', encoding='utf-8')
        (repo / '.gitignore').write_text('ignored-marker\n', encoding='utf-8')
        tests_dir = repo / 'tests'
        tests_dir.mkdir()
        (tests_dir / 'test_calc.py').write_text(
            'import unittest\nfrom pathlib import Path\nfrom calc import add\n\n'
            'class T(unittest.TestCase):\n'
            '    def test_add(self):\n'
            '        self.assertEqual(add(2, 3), 5)\n'
            '        self.assertFalse(Path("ignored-marker").exists())\n',
            encoding='utf-8',
        )
        _git(['add', '-A'], repo)
        _git(['commit', '-m', 'init buggy calc'], repo)
        base_sha = _git(['rev-parse', 'HEAD'], repo)

        cfg_path = root / 'project.toml'
        cfg_path.write_text(
            f'task_workdir = "{repo.as_posix()}"\n'
            f'state_dir = "{(root / "state").as_posix()}"\n'
            '[run_budget]\nmax_tokens = 20000\nmax_duration_s = 120\n'
            '[[stages]]\nname = "t"\nkind = "test"\ntest_cmd = ["python", "-c", "pass"]\n',
            encoding='utf-8',
        )
        cfg = load_config(cfg_path)

        # 拒绝非白名单测试预设、非 CLI 编码后端、以及 `-` 开头的模型参数注入
        with self.assertRaises(ValueError):
            run_arena(cfg, '修复 add', [
                {'label': 'A', 'adapter_type': 'cli_agy', 'model': 'm-a'},
                {'label': 'B', 'adapter_type': 'cli_agy', 'model': 'm-b'},
            ], test_preset='rm_rf')
        with self.assertRaises(ValueError):
            run_arena(cfg, '修复 add', [
                {'label': 'A', 'adapter_type': 'openai_compat', 'model': 'deepseek-chat'},
                {'label': 'B', 'adapter_type': 'cli_agy', 'model': 'm-b'},
            ])
        with self.assertRaises(ValueError):
            run_arena(cfg, '修复 add', [
                {'label': 'A', 'adapter_type': 'cli_agy', 'model': '--bad-flag'},
                {'label': 'B', 'adapter_type': 'cli_agy', 'model': 'm-b'},
            ])

        class _FakeCandidate:
            def __init__(self, label: str, model: str):
                self.label = label
                self.model = model

            def run(self, prompt, workdir: Path, ctx):
                if self.label == 'A':
                    (workdir / 'ignored-marker').touch()
                    (workdir / 'calc.py').write_text('def add(a, b):\n    return a + b\n', encoding='utf-8')
                    return Result(ok=True, text='方案 A：修正为加法', usage={'tokens': 120})
                (workdir / 'calc.py').write_text('def add(a, b):\n    return a * b\n', encoding='utf-8')
                return Result(ok=True, text='方案 B：误改为乘法', usage={'tokens': 150})

        res = run_arena(
            cfg,
            '修复 calc.py 中 add 函数并返回正确和',
            [
                {'label': 'A', 'adapter_type': 'cli_agy', 'model': 'gemini-3.8-flash-high'},
                {'label': 'B', 'adapter_type': 'cli_agy', 'model': 'claude-sonnet-5-5-high'},
            ],
            test_preset='python_unittest',
            allowed_files=['calc.py'],
            adapter_factory=lambda label, atype, model: _FakeCandidate(label, model),
        )

        self.assertEqual(res['base_sha'], base_sha)
        # 主工作区在人工拍板前保持原样，临时 worktree 已全部清理
        self.assertEqual(_git(['rev-parse', 'HEAD'], repo), base_sha)
        self.assertIn('return a - b', (repo / 'calc.py').read_text(encoding='utf-8'))
        self.assertFalse((root / 'state' / 'arenas' / res['arena_id'] / 'wt_A').exists())
        self.assertFalse((root / 'state' / 'arenas' / res['arena_id'] / 'wt_B').exists())

        by_label = {c['label']: c for c in res['candidates']}
        self.assertTrue(by_label['A']['ok'])
        self.assertEqual(by_label['A']['test_exit_code'], 0)
        self.assertEqual(by_label['A']['changed_files'], ['calc.py'])
        self.assertIn('+    return a + b', by_label['A']['diff'])
        self.assertNotEqual(by_label['B']['test_exit_code'], 0)
        self.assertIn('+    return a * b', by_label['B']['diff'])

        arena_dir = root / 'state' / 'arenas' / res['arena_id']
        self.assertFalse((arena_dir / 'test_A').exists())
        self.assertTrue(by_label['A']['apply_eligible'])
        self.assertFalse(by_label['B']['apply_eligible'])
        winner_sha = by_label['A']['commit_sha']
        digest = by_label['A']['test_evidence_sha256']

        def apply(label='A', sha=winner_sha, proof=digest):
            return apply_arena_winner(cfg, res['arena_id'], label, base_sha, sha, proof)

        with self.assertRaisesRegex(RuntimeError, '未通过验收'):
            apply('B', by_label['B']['commit_sha'])
        with self.assertRaisesRegex(RuntimeError, '不一致'):
            apply(sha=by_label['B']['commit_sha'])
        with self.assertRaisesRegex(RuntimeError, '不一致'):
            apply(proof='0' * 64)
        proof_path = arena_dir / 'attempt_A' / 'test_evidence.json'
        original = proof_path.read_bytes()
        edited = json.loads(original)
        edited['test_result']['output'] = 'changed'
        atomic_write_json(proof_path, edited)
        with self.assertRaisesRegex(RuntimeError, '验收证据.*不一致'):
            apply()
        proof_path.write_bytes(original)

        # 基线已动或工作区有改动时，不启动合并。
        wrong_head = _git(['commit-tree', 'HEAD^{tree}', '-p', base_sha, '-m', 'other'], repo)
        branch = _git(['symbolic-ref', '--short', 'HEAD'], repo)
        _git(['switch', '--detach', wrong_head], repo)
        with self.assertRaisesRegex(RuntimeError, 'HEAD 已变化'):
            apply()
        _git(['switch', branch], repo)
        (repo / 'dirty.tmp').write_text('dirty', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, '未提交改动'):
            apply()
        (repo / 'dirty.tmp').unlink()

        # 在合入完成但最终回执未落盘处注入故障。重试应补写回执。
        def fail_final_receipt(path, record):
            if record.get('applied_label'):
                raise OSError('receipt interrupted')
            atomic_write_json(path, record)

        with mock.patch('orchestrator.arena.atomic_write_json', side_effect=fail_final_receipt):
            with self.assertRaisesRegex(OSError, 'receipt interrupted'):
                apply()
        self.assertEqual(_git(['rev-parse', 'HEAD'], repo), winner_sha)
        saved = json.loads((arena_dir / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['apply_intent']['candidate_sha'], winner_sha)
        _git(['switch', '--detach', winner_sha], repo)
        with self.assertRaisesRegex(RuntimeError, '原分支'):
            apply()
        _git(['switch', branch], repo)
        applied = apply()
        self.assertTrue(applied['recovered'])
        self.assertEqual(applied['head_sha'], winner_sha)
        self.assertIn('return a + b', (repo / 'calc.py').read_text(encoding='utf-8'))
        saved = json.loads((arena_dir / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['applied_label'], 'A')
        self.assertEqual(saved['applied_commit_sha'], winner_sha)
        self.assertEqual(saved['applied_test_evidence_sha256'], digest)
        self.assertNotIn('apply_intent', saved)
        with self.assertRaisesRegex(RuntimeError, '禁止重复合并'):
            apply()

    def test_run_test_cmd_resolves_cmd_wrapper_via_which_without_shell(self):
        from unittest import mock
        from orchestrator.testrun import run_test_cmd

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        seen_argv: list[str] = []

        class _FakeProc:
            pid = 99999
            returncode = 0

            def __init__(self, argv, **kwargs):
                seen_argv.extend(argv)
                self._kwargs = kwargs
                import io
                self.stdout = io.BytesIO(b'ok\n')
                self.stderr = io.BytesIO(b'')

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

        with mock.patch('orchestrator.testrun.shutil.which', return_value='C:\\nodejs\\npm.cmd') as mock_which, \
                mock.patch('orchestrator.testrun.subprocess.Popen', side_effect=_FakeProc):
            rec = run_test_cmd(['npm', 'test'], root, timeout_s=30)
            self.assertTrue(rec['ok'])
            self.assertEqual(rec['cmd'], ['npm', 'test'])
            self.assertEqual(seen_argv, ['C:\\nodejs\\npm.cmd', 'test'])
            mock_which.assert_called_once()

    def test_arena_failed_candidate_preserves_stderr_and_skips_base_tests_and_rejects_dup_id(self):
        from orchestrator.contracts import ErrorKind
        from orchestrator.state import StageLock

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        repo = root / 'repo'
        repo.mkdir()
        _git(['init'], repo)
        _git(['config', 'user.name', 'Test User'], repo)
        _git(['config', 'user.email', 'test@local'], repo)
        (repo / 'calc.py').write_text('x = 1\n', encoding='utf-8')
        _git(['add', '-A'], repo)
        _git(['commit', '-m', 'init'], repo)
        base_sha = _git(['rev-parse', 'HEAD'], repo)

        cfg_path = root / 'project.toml'
        cfg_path.write_text(
            f'task_workdir = "{repo.as_posix()}"\n'
            f'state_dir = "{(root / "state").as_posix()}"\n'
            '[run_budget]\nmax_tokens = 20000\nmax_duration_s = 120\n'
            '[[stages]]\nname = "t"\nkind = "test"\ntest_cmd = ["python", "-c", "pass"]\n',
            encoding='utf-8',
        )
        cfg = load_config(cfg_path)

        class _MixedCandidate:
            def __init__(self, label: str):
                self.label = label
                self.model = 'm-' + label

            def run(self, prompt, workdir: Path, ctx):
                if self.label == 'A':
                    (workdir / 'calc.py').write_text('x = 3\n', encoding='utf-8')
                    return Result(
                        ok=False, exit_code=1, stderr='真实 CLI 鉴权失败详情',
                        error_kind=ErrorKind.AUTH, usage={'tokens': 88},
                    )
                (workdir / 'calc.py').write_text('x = 2\n', encoding='utf-8')
                return Result(ok=True, text='方案 B 完成', usage={'tokens': 42})

        res = run_arena(
            cfg,
            '修改 x',
            [
                {'label': 'A', 'adapter_type': 'cli_agy', 'model': 'm-a'},
                {'label': 'B', 'adapter_type': 'cli_agy', 'model': 'm-b'},
            ],
            test_preset='python_unittest',
            allowed_files=['calc.py'],
            arena_id='arena-failtest',
            adapter_factory=lambda label, atype, model: _MixedCandidate(label),
        )
        by_label = {c['label']: c for c in res['candidates']}
        # Result 无 .error 属性时不能退化为 AttributeError，应保留真实 stderr 与 tokens，且未改动的失败候选不误跑测试
        self.assertFalse(by_label['A']['ok'])
        self.assertIn('真实 CLI 鉴权失败详情', by_label['A']['error'])
        self.assertEqual(by_label['A']['tokens'], 88)
        self.assertIsNone(by_label['A']['test_exit_code'])
        self.assertTrue(by_label['A']['commit_sha'])
        with self.assertRaisesRegex(RuntimeError, '候选生成失败'):
            apply_arena_winner(cfg, res['arena_id'], 'A', base_sha, by_label['A']['commit_sha'], '0' * 64)
        self.assertTrue(by_label['B']['ok'])
        self.assertTrue(by_label['B']['commit_sha'])

        # 禁止覆盖已有竞技场记录
        with self.assertRaises(ValueError):
            run_arena(
                cfg,
                '再次覆盖',
                [
                    {'label': 'A', 'adapter_type': 'cli_agy', 'model': 'm-a'},
                    {'label': 'B', 'adapter_type': 'cli_agy', 'model': 'm-b'},
                ],
                arena_id='arena-failtest',
                adapter_factory=lambda label, atype, model: _MixedCandidate(label),
            )

        # .apply.lock 被占用时，直接调用 apply_arena_winner 也必须拒绝并发合并
        lock = StageLock(cfg.state_dir / 'arenas' / '.apply.lock')
        self.assertTrue(lock.acquire())
        try:
            with self.assertRaises(RuntimeError):
                apply_arena_winner(cfg, 'arena-failtest', 'B', base_sha, by_label['B']['commit_sha'], '0' * 64)
        finally:
            lock.release()

    def test_comparison_without_test_or_with_out_of_scope_changes_cannot_apply(self):
        from orchestrator.config import parse_config

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        repo = root / 'repo'
        repo.mkdir()
        _git(['init'], repo)
        _git(['config', 'user.name', 'Test User'], repo)
        _git(['config', 'user.email', 'test@local'], repo)
        _git(['commit', '--allow-empty', '-m', 'initial'], repo)
        cfg = parse_config({'task_workdir': 'repo', 'state_dir': 'state', 'stages': [
            {'name': 't', 'kind': 'test', 'test_cmd': ['python', '-c', 'pass']},
        ]}, base_dir=root)

        class FakeCandidate:
            def __init__(self, label):
                self.label = label

            def run(self, prompt, workdir, ctx):
                name = '中文 空格.txt' if self.label == 'A' else 'outside.txt'
                (workdir / name).write_text('content', encoding='utf-8')
                return Result(ok=True)

        options = dict(allowed_files=['中文 空格.txt'], adapter_factory=lambda label, *_: FakeCandidate(label))
        candidates = [{'label': label, 'adapter_type': 'cli_agy'} for label in 'AB']
        with mock.patch('orchestrator.arena.run_test_cmd', return_value={
            'ok': True, 'exit_code': 0, 'cmd': ['python', '-m', 'unittest', 'discover', '-s', 'tests'],
        }) as test_cmd:
            record = run_arena(cfg, 'edit', candidates, **options)
            test_cmd.assert_not_called()
            self.assertEqual(record['candidates'][0]['apply_blocked'], '本轮未选择验收测试')
            record_with_test = run_arena(cfg, 'edit', candidates, test_preset='python_unittest', **options)
        self.assertEqual(record_with_test['candidates'][0]['changed_files'], ['中文 空格.txt'])
        self.assertTrue(record_with_test['candidates'][0]['apply_eligible'])
        self.assertEqual(record_with_test['candidates'][1]['test_exit_code'], 0)
        for rec, idx, reason in [(record, 0, '未选择验收测试'), (record_with_test, 1, '超出允许')]:
            candidate = rec['candidates'][idx]
            with self.assertRaisesRegex(RuntimeError, reason):
                apply_arena_winner(cfg, rec['arena_id'], candidate['label'], rec['base_sha'],
                                   candidate['commit_sha'], candidate['test_evidence_sha256'] or '0' * 64)
        self.assertEqual(_git(['rev-parse', 'HEAD'], repo), record['base_sha'])

    def test_run_test_cmd_multibyte_utf8_under_char_limit_is_not_marked_truncated(self):
        import sys
        from orchestrator.testrun import run_test_cmd

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        # 8000 个中文字符约 24000 字节（> MAX_OUTPUT=20000 字节），但字符数 < 20000，不应误判截断
        rec = run_test_cmd([sys.executable, '-c', 'print("测" * 8000)'], root, timeout_s=30)
        self.assertTrue(rec['ok'])
        self.assertFalse(rec['truncated'])
        self.assertGreaterEqual(len(rec['output'].strip()), 8000)


if __name__ == '__main__':
    unittest.main()
