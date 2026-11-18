"""基准配置必须可复读，不能将脚本机制验证混入真实模型完成率。"""
import tempfile
import tomllib
import unittest
from unittest import mock
from pathlib import Path

from scripts.benchmark import save_config, Scripted, run_case
from orchestrator.contracts import PreflightReport


class BenchmarkContract(unittest.TestCase):
    def test_generated_toml_roundtrip_preserves_adapter_arrays_and_booleans(self):
        raw = {'task_workdir': 'D:/目录/work', 'adapters': {'x': {'type': 'fake', 'extra_env': ['A'], 'enabled': True}},
               'stages': [{'name': 'test', 'kind': 'test', 'test_cmd': ['python', '-c', 'assert True']}],
               'run_budget': {'unlimited_tokens': True, 'max_duration_s': 240}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.toml'
            save_config(path, raw)
            self.assertEqual(tomllib.loads(path.read_text(encoding='utf8')), raw)

    def test_scripted_adapter_is_explicitly_fake(self):
        self.assertEqual(Scripted('coding', 'first_pass').channel, 'fake')

    def test_unavailable_role_blocks_all_model_calls_before_design(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'roles.toml'
            save_config(config, {'adapters': {'role': {'type': 'fake'}}, 'stages': [
                {'name': kind, 'kind': kind, 'adapter': 'role'} for kind in ('report', 'code', 'review')]})
            unavailable = mock.Mock()
            unavailable.preflight.return_value = PreflightReport('role', False, detail='unavailable')
            unavailable._version_known.return_value = False
            with mock.patch('scripts.benchmark.build_all', return_value={'role': unavailable}):
                record = run_case(root, 'blocked', config)
            self.assertEqual(record['failure_category'], 'preflight_blocked')
            self.assertEqual(record['model_calls'], 0)
            unavailable.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
