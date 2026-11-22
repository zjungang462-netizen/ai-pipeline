"""双模型并行 Best-of-N 竞技场：隔离 worktree 比拼、确定性测试验收与人工 ff-only 合并测试。"""
from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from orchestrator.arena import apply_arena_winner, run_arena
from orchestrator.config import load_config
from orchestrator.contracts import Result


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ['git', *args], cwd=str(cwd), capture_output=True, text=True, encoding='utf-8', check=True
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
        tests_dir = repo / 'tests'
        tests_dir.mkdir()
        (tests_dir / 'test_calc.py').write_text(
            'import unittest\nfrom calc import add\n\n'
            'class T(unittest.TestCase):\n'
            '    def test_add(self):\n'
            '        self.assertEqual(add(2, 3), 5)\n',
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

        # 拒绝非白名单测试预设
        with self.assertRaises(ValueError):
            run_arena(cfg, '修复 add', [
                {'label': 'A', 'adapter_type': 'cli_agy', 'model': 'm-a'},
                {'label': 'B', 'adapter_type': 'cli_agy', 'model': 'm-b'},
            ], test_preset='rm_rf')

        class _FakeCandidate:
            def __init__(self, label: str, model: str):
                self.label = label
                self.model = model

            def run(self, prompt, workdir: Path, ctx):
                if self.label == 'A':
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

        # 工作区存在未提交改动时拒绝合并
        (repo / 'dirty.tmp').write_text('dirty', encoding='utf-8')
        with self.assertRaises(RuntimeError):
            apply_arena_winner(cfg, res['arena_id'], 'A', base_sha)
        (repo / 'dirty.tmp').unlink()

        # 人工明确选择方案 A 后，安全 ff-only 合并生效
        applied = apply_arena_winner(cfg, res['arena_id'], 'A', base_sha)
        self.assertTrue(applied['applied'])
        self.assertEqual(applied['head_sha'], by_label['A']['commit_sha'])
        self.assertIn('return a + b', (repo / 'calc.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
