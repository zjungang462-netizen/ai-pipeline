import csv
from pathlib import Path
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
