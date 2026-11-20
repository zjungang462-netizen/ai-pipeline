"""第 3 阶段：CLI 适配器离线测试（fixture 版本绑定 + 健壮性 + preflight + env 白名单）。"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.adapters.cli_codex import CodexAdapter, parse_codex_jsonl, strip_ansi
from orchestrator.adapters.cli_qwen import QwenAdapter
from orchestrator.adapters.cli_dsh import DshAdapter
from orchestrator.adapters.cli_doubao import DoubaoAdapter
from orchestrator.contracts import ErrorKind, RunContext
from orchestrator.safety import build_subprocess_env

FIXTURES = Path(__file__).parent / "fixtures"


def make_ctx(tmp: Path):
    return RunContext(task="demo", run_id="r1", stage="coding",
                      workdir=tmp, runs_dir=tmp / "runs")


def proc(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess(args=[], returncode=code,
                                       stdout=stdout, stderr=stderr)


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
        ad._version = "dsh 1.0"
        ad._resolved = "dsh"
        with tempfile.TemporaryDirectory() as d:
            res = ad._parse(proc(stdout=""), Path(d), make_ctx(Path(d)))
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "")   # 空跑，引擎会拒绝判完成

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


if __name__ == "__main__":
    unittest.main()
