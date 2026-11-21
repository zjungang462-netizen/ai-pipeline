import csv
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from orchestrator.adapters.docker_base import (
    base_run_args,
    bind_mount,
    cleanup_container,
    proxy_env_args,
    validate_image_ref,
    validate_proxy_url,
)
from orchestrator.adapters.verified_runtimes import VERIFIED_RUNTIMES, get_verified_runtime
from scripts.multi_project_benchmark import CASES


class DockerBase(unittest.TestCase):
    def test_bind_mount_handles_commas_and_readonly_flag(self):
        path = Path('/tmp/dir, with comma/file')
        ro = bind_mount(path, '/opt/target', readonly=True)
        rw = bind_mount(path, '/workspace', readonly=False)
        self.assertEqual(
            next(csv.reader([ro])),
            ['type=bind', f'source={path.as_posix()}', 'target=/opt/target', 'readonly'],
        )
        self.assertEqual(
            next(csv.reader([rw])),
            ['type=bind', f'source={path.as_posix()}', 'target=/workspace'],
        )

    def test_validate_proxy_url_rejects_credentials_and_paths(self):
        validate_proxy_url('http://host.docker.internal:7897', 'Test')
        validate_proxy_url('https://proxy.local:8080/', 'Test')
        for bad in (
            'socks5://127.0.0.1:1080',
            'http://user:pass@127.0.0.1:7897',
            'http://127.0.0.1:7897/path',
            'http://127.0.0.1:7897?a=1',
            'http://127.0.0.1:7897#frag',
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_proxy_url(bad, 'Test')

    def test_validate_image_ref_rejects_flags_and_whitespace(self):
        validate_image_ref('pipeline-doubao-probe:2.37.2', 'Test')
        for bad in ('', '--privileged', 'repo tag', 'repo\nname', None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_image_ref(bad, 'Test')

    def test_base_run_args_and_proxy_env_and_cleanup(self):
        args = base_run_args(host='unix:///var/run/docker.sock')
        self.assertIn('--read-only', args)
        self.assertIn('--platform', args)
        self.assertIn('linux/amd64', args)
        self.assertIn('65534:65534', args)
        self.assertEqual(proxy_env_args(None), [])
        self.assertEqual(
            proxy_env_args('http://proxy:7897', include_no_proxy=True),
            ['--env', 'HTTP_PROXY=http://proxy:7897',
             '--env', 'HTTPS_PROXY=http://proxy:7897',
             '--env', 'NO_PROXY=localhost,127.0.0.1'],
        )
        runner = mock.Mock()
        cleanup_container(None, 'c1', runner=runner)
        runner.assert_not_called()
        cleanup_container('docker', 'c1', host='unix:///var/run/docker.sock', runner=runner)
        runner.assert_called_once()
        self.assertEqual(
            runner.call_args.args[0],
            ['docker', '--host', 'unix:///var/run/docker.sock', 'rm', '-f', 'c1'],
        )

    def test_verified_runtimes_manifest_uses_immutable_hashes_and_digests(self):
        self.assertIn('cli_grok', VERIFIED_RUNTIMES)
        self.assertIn('cli_doubao', VERIFIED_RUNTIMES)
        for channel, runtime in VERIFIED_RUNTIMES.items():
            with self.subTest(channel=channel):
                self.assertEqual(get_verified_runtime(channel), runtime)
                self.assertTrue(runtime.version.strip())
                self.assertRegex(runtime.binary_sha256, re.compile(r'^[0-9a-f]{64}$'))
                self.assertIn('sha256:', runtime.image)
        with self.assertRaises(KeyError):
            get_verified_runtime('cli_unknown')

    def test_multi_project_benchmark_cases_reproduce_defects_before_fix(self):
        self.assertIn('ledger', CASES)
        runner = (
            'import json, pathlib, sys\n'
            'root = pathlib.Path(sys.argv[1])\n'
            'results = {}\n'
            'for case in sorted(p for p in root.iterdir() if p.is_dir()):\n'
            '    sys.path.insert(0, str(case))\n'
            '    before = set(sys.modules)\n'
            '    try:\n'
            '        exec((case / "acceptance.py").read_text(encoding="utf8"), {"__name__": "__main__"})\n'
            '        results[case.name] = "passed"\n'
            '    except BaseException as exc:\n'
            '        results[case.name] = type(exc).__name__\n'
            '    finally:\n'
            '        sys.path.remove(str(case))\n'
            '        for mod in set(sys.modules) - before:\n'
            '            sys.modules.pop(mod, None)\n'
            'print(json.dumps(results))\n'
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, (files, task, assertions) in CASES.items():
                self.assertTrue(files and task.strip())
                ws = root / name
                ws.mkdir()
                for rel, body in files.items():
                    (ws / rel).write_text(body, encoding='utf8')
                (ws / 'acceptance.py').write_text(assertions, encoding='utf8')
            proc = subprocess.run(
                [sys.executable, '-I', '-B', '-X', 'utf8', '-c', runner, str(root)],
                capture_output=True, text=True, encoding='utf8', timeout=15,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            import json
            outcomes = json.loads(proc.stdout)
            for name in CASES:
                with self.subTest(case=name):
                    self.assertNotEqual(outcomes.get(name), 'passed', f'{name} 初始保护断言未复现缺陷')
