"""第 3 阶段：CLI 适配器离线测试（fixture 版本绑定 + 健壮性 + preflight + env 白名单）。"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from orchestrator.adapters.cli_base import CLIAdapter
from orchestrator.adapters.cli_codex import CodexAdapter, parse_codex_jsonl, strip_ansi
from orchestrator.adapters.cli_qwen import QwenAdapter
from orchestrator.adapters.cli_dsh import DshAdapter
from orchestrator.adapters.cli_doubao import DoubaoAdapter
from orchestrator.collaboration.service import Service
from orchestrator.contracts import ErrorKind, RunContext
from orchestrator.safety import adapter_env_allow, build_subprocess_env, environment_secrets, is_secret_env_name, resolve_exe

FIXTURES = Path(__file__).parent / "fixtures"


def make_ctx(tmp: Path):
    return RunContext(task="demo", run_id="r1", stage="coding",
                      workdir=tmp, runs_dir=tmp / "runs")


def proc(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess(args=[], returncode=code,
                                       stdout=stdout, stderr=stderr)


class _FixtureCLIAdapter(CLIAdapter):
    """测试用最小子类：run 由具体 CLI 工具子类实现，这里只验证基类 _exec。"""

    def run(self, prompt, workdir, ctx):
        raise NotImplementedError


class TestCodexParse(unittest.TestCase):
    def test_fixture_version_bound(self):
        raw = (FIXTURES / "codex_0.167.jsonl").read_text(encoding="utf-8")
        text, ok = parse_codex_jsonl(raw)
        self.assertTrue(ok)
        self.assertIn("支持添加待办", text)

    def test_unknown_version_rejected(self):
        ad = CodexAdapter(name="codex")
        ad._version = "some-other-tool 1.0"   # 不识别的工具版本
        ad._resolved = "codex"
        with tempfile.TemporaryDirectory() as d:
            res = ad._parse(proc(stdout='{"item":{"text":"x"}}'), Path(d), make_ctx(Path(d)))
        self.assertTrue(res.parse_unknown)
        self.assertEqual(res.error_kind, ErrorKind.PARSE_FAILED)

    def test_known_version_parses(self):
        ad = CodexAdapter(name="codex")
        ad._version = "codex-cli 0.167.0"
        ad._resolved = "codex"
        raw = (FIXTURES / "codex_0.167.jsonl").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as d:
            res = ad._parse(proc(stdout=raw), Path(d), make_ctx(Path(d)))
        self.assertTrue(res.ok)
        self.assertIn("支持列出待办", res.text)

    def test_ansi_and_chinese_and_long(self):
        ansi_text = "\x1b[32m完成\x1b[0m：中文输出" + "x" * 300000
        cleaned = strip_ansi(ansi_text)
        self.assertNotIn("\x1b", cleaned)
        self.assertIn("中文输出", cleaned)
        # 超长不崩
        text, ok = parse_codex_jsonl(cleaned)
        self.assertIsInstance(text, str)

    def test_malformed_jsonl_no_crash(self):
        text, ok = parse_codex_jsonl("{bad json\nnot json at all\n")
        self.assertFalse(ok)


class TestQwenParse(unittest.TestCase):
    def test_fixture(self):
        ad = QwenAdapter(name="qwen")
        ad._version = "qwen-code 0.5.0"
        ad._resolved = "qwen"
        raw = (FIXTURES / "qwen_0.5.json").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as d:
            res = ad._parse(proc(stdout=raw), Path(d), make_ctx(Path(d)))
        self.assertTrue(res.ok)
        self.assertIn("模块划分", res.text)

    def test_unknown_version(self):
        ad = QwenAdapter(name="qwen")
        ad._version = "some-other-tool 1.0"
        ad._resolved = "qwen"
        with tempfile.TemporaryDirectory() as d:
            res = ad._parse(proc(stdout='{"result":"x"}'), Path(d), make_ctx(Path(d)))
        self.assertTrue(res.parse_unknown)


class TestTextAdapters(unittest.TestCase):
    def test_unverified_doubao_stops_before_model_call(self):
        adapter = DoubaoAdapter(name="doubao")
        with tempfile.TemporaryDirectory() as d, mock.patch.object(adapter, "_exec") as execute:
            root = Path(d)
            self.assertFalse(adapter.preflight().ok)
            self.assertTrue(adapter.run("prompt", root, make_ctx(root)).parse_unknown)
            execute.assert_not_called()

    def test_dsh_empty_run(self):
        ad = DshAdapter(name="dsh")
        ad._version = "0.2.2-rc.3"
        ad._resolved = "dsh"
        with tempfile.TemporaryDirectory() as d:
            res = ad._parse(proc(stdout=""), Path(d), make_ctx(Path(d)))
        self.assertFalse(res.ok)
        self.assertTrue(res.parse_unknown)
        self.assertEqual(res.error_kind, ErrorKind.PARSE_FAILED)

    def test_doubao_auth_error(self):
        ad = DoubaoAdapter(name="doubao")
        ad._version = "doubao 1.0"
        ad._resolved = "doubao"
        with tempfile.TemporaryDirectory() as d:
            res = ad._parse(proc(stderr="请先登录", code=1), Path(d), make_ctx(Path(d)))
        self.assertFalse(res.ok)
        self.assertEqual(res.error_kind, ErrorKind.AUTH)


class TestPreflightAndEnv(unittest.TestCase):
    def test_preflight_missing_exe(self):
        ad = CodexAdapter(name="codex", exe="definitely_not_exist_xyz")
        with mock.patch("orchestrator.safety.shutil.which", return_value=None):
            rep = ad.preflight()
        self.assertFalse(rep.ok)
        self.assertIn("未找到", rep.detail)

    def test_env_whitelist_no_api_key(self):
        # 评审 M4/补测6：子进程 env 不含 API Key
        with mock.patch.dict("os.environ", {
            "DASHSCOPE_API_KEY": "sk-secret", "ARK_API_KEY": "sk-secret2",
            "PATH": "/usr/bin", "HOME": "/home/u",
        }):
            env = build_subprocess_env()
        self.assertNotIn("DASHSCOPE_API_KEY", env)
        self.assertNotIn("ARK_API_KEY", env)
        self.assertIn("PATH", env)

    def test_env_explicit_credential(self):
        with mock.patch.dict("os.environ", {"CODEX_API_KEY": "sk-codex", "PATH": "/x"}):
            env = build_subprocess_env(extra_allow=["CODEX_API_KEY"])
        self.assertIn("CODEX_API_KEY", env)   # 显式开启才透传

    def test_adapter_env_allow_contains_only_declared_names(self):
        adapters = {
            'api': SimpleNamespace(type='openai_compat', options={
                'api_key_env': 'API_KEY_ENV', 'extra_env': ['SHARED_ENV']}),
            'qwen': SimpleNamespace(type='cli_qwen', options={
                'extra_env': ['QWEN_API_KEY', 'SHARED_ENV']}),
            'fake': SimpleNamespace(type='fake', options={}),
        }
        self.assertEqual(adapter_env_allow(adapters), ['API_KEY_ENV', 'SHARED_ENV', 'QWEN_API_KEY'])

    def test_generic_credential_names_share_one_redaction_rule(self):
        env = {
            'GITHUB_TOKEN': 'ghp_' + 'G' * 30,
            'NPM_TOKEN': 'npm_' + 'N' * 30,
            'AWS_SECRET_ACCESS_KEY': 'aws-secret-value-123456',
            'MY_PASSWORD': 'short-pw-1234',
            'OPENAI_BASE_URL': 'https://example.invalid/v1',
            'SHARED_ENV': 'ordinary-value',
        }
        with mock.patch.dict(os.environ, env, clear=True):
            secrets = environment_secrets()
            service = Service.__new__(Service)
            cleaned = service.redact({'items': [*env.values()]})
        for name in ('GITHUB_TOKEN', 'NPM_TOKEN', 'AWS_SECRET_ACCESS_KEY', 'MY_PASSWORD'):
            self.assertTrue(is_secret_env_name(name))
            self.assertIn(env[name], secrets)
            self.assertNotIn(env[name], str(cleaned))
        for name in ('OPENAI_BASE_URL', 'SHARED_ENV'):
            self.assertFalse(is_secret_env_name(name))
            self.assertNotIn(env[name], secrets)
            self.assertIn(env[name], str(cleaned))

    def test_environment_secrets_cache_hit_is_consistent(self):
        secret = 'ghp_' + 'H' * 30
        with mock.patch.dict(os.environ, {'CACHE_HIT_TOKEN': secret}, clear=False):
            first = environment_secrets()
            second = environment_secrets()   # 第二次应命中同一环境快照
            self.assertEqual(first, second)
            self.assertIn(secret, first)
            service = Service.__new__(Service)
            self.assertNotIn(secret, str(service.redact({'value': secret})))

    def test_environment_secrets_cache_invalidates_when_env_changes(self):
        secret = 'ghp_' + 'N' * 30
        with mock.patch.dict(os.environ, {'CACHE_STABLE_VALUE': 'ordinary'}, clear=False):
            self.assertNotIn(secret, environment_secrets())
            with mock.patch.dict(os.environ, {'CACHE_NEW_TOKEN': secret}, clear=False):
                secrets = environment_secrets()   # 运行时新增的密钥必须立刻出现
                self.assertIn(secret, secrets)
                service = Service.__new__(Service)
                self.assertNotIn(secret, str(service.redact({'value': secret})))
            # 内层 patch 退出后密钥消失，缓存也必须立即回退，不能继续返回旧值。
            self.assertNotIn(secret, environment_secrets())

    def test_environment_secrets_extra_names_and_min_bytes_unchanged(self):
        with mock.patch.dict(os.environ,
                             {'CACHE_PLAIN': 'plain-value', 'CACHE_SHORT_TOKEN': 'abc'},
                             clear=False):
            self.assertNotIn('plain-value', environment_secrets())
            self.assertIn('plain-value', environment_secrets(extra_names=['CACHE_PLAIN']))
            self.assertNotIn('abc', environment_secrets())
            self.assertIn('abc', environment_secrets(min_bytes=1))

    def test_cli_scrubs_generic_explicit_credentials(self):
        secret = 'ghp_' + 'C' * 30
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = _FixtureCLIAdapter('fixture')
            adapter._resolved = 'fixture'
            adapter._version = '1'
            with mock.patch.dict(os.environ, {'GITHUB_TOKEN': secret, 'PATH': 'x'}, clear=True), \
                    mock.patch('orchestrator.adapters.cli_base.run_cli_process',
                               return_value=proc(stdout='token=' + secret, stderr=secret)):
                result = adapter._exec(['--version'], root, make_ctx(root))
        self.assertNotIn(secret, result.stdout)
        self.assertNotIn(secret, result.stderr)
        self.assertIn('[REDACTED]', result.stdout)

    def test_windows_npm_fallback_never_returns_powershell_script(self):
        with tempfile.TemporaryDirectory() as directory:
            npm = Path(directory) / 'npm'
            npm.mkdir()
            script = npm / 'qwen.ps1'
            script.write_text('fixture', encoding='utf-8')
            with mock.patch.dict(os.environ, {'APPDATA': directory}), \
                    mock.patch('orchestrator.safety.shutil.which', return_value=None):
                self.assertIsNone(resolve_exe('qwen'))
                command = npm / 'qwen.cmd'
                command.write_text('@echo off\r\n', encoding='utf-8')
                self.assertEqual(resolve_exe('qwen'), str(command))


if __name__ == "__main__":
    unittest.main()
