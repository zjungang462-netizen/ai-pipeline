"""第 2 阶段：OpenAI 兼容 API 适配器离线测试（mock HTTP，零依赖）。"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from orchestrator.adapters.openai_compat import OpenAICompatAdapter
from orchestrator.contracts import ErrorKind, RunContext


def make_ctx(tmp: Path):
    return RunContext(task="demo", run_id="r1", stage="requirements",
                      workdir=tmp, runs_dir=tmp / "runs")


def ok_response(text="需求分析完成", tokens=42):
    body = json.dumps({
        "choices": [{"message": {"content": text}}],
        "usage": {"total_tokens": tokens, "prompt_tokens": 10, "completion_tokens": 32},
    }).encode("utf-8")
    return io.BytesIO(body)


class TestAPIAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.adapter = OpenAICompatAdapter(
            name="api", base_url="https://example.test/v1", model="m",
            api_key_env="TEST_API_KEY", kind="report")
        self.env = mock.patch.dict(os.environ, {"TEST_API_KEY": "sk-test"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_preflight_missing_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            rep = self.adapter.preflight()
            self.assertFalse(rep.ok)
            self.assertTrue(rep.needs_login)

    def test_preflight_ok(self):
        rep = self.adapter.preflight()
        self.assertTrue(rep.ok)

    def test_success(self):
        with mock.patch("urllib.request.urlopen", return_value=ok_response()):
            res = self.adapter.run("prompt", self.tmp_path, make_ctx(self.tmp_path))
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "需求分析完成")
        self.assertEqual(res.usage["tokens"], 42)
        self.assertEqual(res.channel, "api")
        self.assertEqual(len(res.report_files), 1)

    def test_auth_error(self):
        err = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            res = self.adapter.run("prompt", self.tmp_path, make_ctx(self.tmp_path))
        self.assertFalse(res.ok)
        self.assertEqual(res.error_kind, ErrorKind.AUTH)

    def test_timeout(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            res = self.adapter.run("prompt", self.tmp_path, make_ctx(self.tmp_path))
        self.assertFalse(res.ok)
        self.assertEqual(res.error_kind, ErrorKind.TIMEOUT)

    def test_parse_failed(self):
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(b"not json")):
            res = self.adapter.run("prompt", self.tmp_path, make_ctx(self.tmp_path))
        self.assertFalse(res.ok)
        self.assertEqual(res.error_kind, ErrorKind.PARSE_FAILED)
        self.assertTrue(res.parse_unknown)

    def test_missing_key_at_run(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            res = self.adapter.run("prompt", self.tmp_path, make_ctx(self.tmp_path))
        self.assertFalse(res.ok)
        self.assertEqual(res.error_kind, ErrorKind.AUTH)

    def test_key_not_in_payload_log(self):
        # Key 只走 Authorization header，不出现在 stdout/产物
        with mock.patch("urllib.request.urlopen", return_value=ok_response()) as m:
            res = self.adapter.run("prompt", self.tmp_path, make_ctx(self.tmp_path))
            sent_req = m.call_args[0][0]
            self.assertEqual(sent_req.headers["Authorization"], "Bearer sk-test")
        self.assertNotIn("sk-test", res.stdout)
        self.assertNotIn("sk-test", res.text)


if __name__ == "__main__":
    unittest.main()
