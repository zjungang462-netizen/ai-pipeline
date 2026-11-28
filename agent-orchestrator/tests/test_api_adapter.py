"""第 2 阶段：OpenAI 兼容 API 适配器离线测试（mock HTTP，零依赖）。"""
from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from orchestrator.adapters.openai_compat import OpenAICompatAdapter, parse_sse_stream
from orchestrator.contracts import ErrorKind, RunContext
from orchestrator.safety import _SameOriginRedirectHandler


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
        with mock.patch("orchestrator.adapters.openai_compat._open_request", return_value=ok_response()):
            res = self.adapter.run("prompt", self.tmp_path, make_ctx(self.tmp_path))
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "需求分析完成")
        self.assertEqual(res.usage["tokens"], 42)
        self.assertEqual(res.channel, "api")
        self.assertEqual(len(res.report_files), 1)

    def test_auth_error(self):
        err = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
        with mock.patch("orchestrator.adapters.openai_compat._open_request", side_effect=err):
            res = self.adapter.run("prompt", self.tmp_path, make_ctx(self.tmp_path))
        self.assertFalse(res.ok)
        self.assertEqual(res.error_kind, ErrorKind.AUTH)

    def test_timeout(self):
        with mock.patch("orchestrator.adapters.openai_compat._open_request", side_effect=TimeoutError("timed out")):
            res = self.adapter.run("prompt", self.tmp_path, make_ctx(self.tmp_path))
        self.assertFalse(res.ok)
        self.assertEqual(res.error_kind, ErrorKind.TIMEOUT)

    def test_parse_failed(self):
        with mock.patch("orchestrator.adapters.openai_compat._open_request", return_value=io.BytesIO(b"not json")):
            res = self.adapter.run("prompt", self.tmp_path, make_ctx(self.tmp_path))
        self.assertFalse(res.ok)
        self.assertEqual(res.error_kind, ErrorKind.PARSE_FAILED)
        self.assertTrue(res.parse_unknown)

    def test_non_object_usage_is_parse_failure_and_remains_unknown(self):
        for usage in ([1], 'unexpected', 7):
            with self.subTest(usage=usage):
                body = json.dumps({
                    'choices': [{'message': {'content': 'done'}}],
                    'usage': usage,
                }).encode('utf-8')
                with mock.patch('orchestrator.adapters.openai_compat._open_request',
                                return_value=io.BytesIO(body)):
                    result = self.adapter.run('prompt', self.tmp_path, make_ctx(self.tmp_path))
                self.assertFalse(result.ok)
                self.assertEqual(result.error_kind, ErrorKind.PARSE_FAILED)
                self.assertTrue(result.parse_unknown)
                self.assertEqual(result.usage['tokens'], 0)
                self.assertTrue(result.usage['usage_unknown'])
                self.assertIn('usage', result.stderr)

    def test_stream_rejects_null_choice_empty_text_or_missing_finish_reason(self):
        cases = {
            'null_choice': ('{"choices":[null]}', 'choice'),
            'empty_text': ('{"choices":[{"delta":{"content":""},"finish_reason":"stop"}]}', '非空文本'),
            'missing_finish': ('{"choices":[{"delta":{"content":"partial"}}]}', 'finish_reason'),
        }
        for name, (payload, message) in cases.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, message):
                    parse_sse_stream(['data: ' + payload, 'data: [DONE]'])

    def test_stream_deadline_shuts_down_socket_to_unblock_read1(self):
        released = threading.Event()

        class Socket:
            def __init__(self):
                self.shutdown_calls = 0

            def shutdown(self, _how):
                self.shutdown_calls += 1
                released.set()

        class Stream:
            def __init__(self):
                self.socket = Socket()
                self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=self.socket))
                self.closed = False

            def read1(self, _size):
                if not released.wait(2):
                    raise AssertionError('socket shutdown did not unblock read1')
                return b''

            def close(self):
                self.closed = True

        stream = Stream()
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, '总时长超时'):
            parse_sse_stream(stream, deadline=started + 0.15)
        self.assertLess(time.monotonic() - started, 1)
        self.assertGreaterEqual(stream.socket.shutdown_calls, 1)
        self.assertTrue(stream.closed)

    def test_non_stream_response_over_byte_limit_is_rejected(self):
        with mock.patch('orchestrator.adapters.openai_compat.MAX_SSE_BYTES', 32), \
                mock.patch('orchestrator.adapters.openai_compat._open_request',
                           return_value=io.BytesIO(b'x' * 33)):
            res = self.adapter.run('prompt', self.tmp_path, make_ctx(self.tmp_path))
        self.assertFalse(res.ok)
        self.assertEqual(res.error_kind, ErrorKind.PARSE_FAILED)
        self.assertTrue(res.parse_unknown)
        self.assertEqual(res.stdout, '')
        self.assertIn('最大字节上限', res.stderr)

    def test_non_loopback_http_is_blocked_unless_explicitly_allowed(self):
        adapter = OpenAICompatAdapter(
            name='api', base_url='http://api.example.test/v1', model='m',
            api_key_env='TEST_API_KEY', kind='report')
        self.assertFalse(adapter.preflight().ok)
        with mock.patch('orchestrator.adapters.openai_compat._open_request') as request:
            result = adapter.run('prompt', self.tmp_path, make_ctx(self.tmp_path))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, ErrorKind.REJECTED)
        request.assert_not_called()

        adapter = OpenAICompatAdapter(
            name='api', base_url='http://api.example.test/v1', model='m',
            api_key_env='TEST_API_KEY', kind='report', allow_insecure_http=True)
        self.assertTrue(adapter.preflight().ok)
        with mock.patch('orchestrator.adapters.openai_compat._open_request',
                        return_value=ok_response()) as request:
            self.assertTrue(adapter.run('prompt', self.tmp_path, make_ctx(self.tmp_path)).ok)
        request.assert_called_once()

    def test_missing_key_at_run(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            res = self.adapter.run("prompt", self.tmp_path, make_ctx(self.tmp_path))
        self.assertFalse(res.ok)
        self.assertEqual(res.error_kind, ErrorKind.AUTH)

    def test_redirect_handler_keeps_credentials_on_same_origin_only(self):
        request = urllib.request.Request(
            'https://example.test/v1/chat/completions',
            headers={'Authorization': 'Bearer sk-test'},
        )
        handler = _SameOriginRedirectHandler()
        for target in ('http://example.test/v1/chat', 'https://example.test:444/v1/chat',
                       'https://other.test/v1/chat'):
            with self.subTest(target=target), self.assertRaises(urllib.error.HTTPError) as raised:
                handler.redirect_request(request, None, 302, 'Found', {}, target)
            self.assertEqual(raised.exception.code, 302)
        redirected = handler.redirect_request(
            request, None, 302, 'Found', {}, 'https://EXAMPLE.test:443/v1/next')
        self.assertEqual(redirected.get_header('Authorization'), 'Bearer sk-test')

    def test_model_catalog_does_not_send_key_over_insecure_http(self):
        from orchestrator.collaboration.model_catalog import _probe_openai_compat_models
        with mock.patch('orchestrator.collaboration.model_catalog.open_same_origin') as request:
            configured, models = _probe_openai_compat_models(
                'http://api.example.test/v1', 'TEST_API_KEY')
        self.assertFalse(configured)
        self.assertEqual(models, [])
        request.assert_not_called()

        response = io.BytesIO(json.dumps({'data': [{'id': 'model-a'}]}).encode('utf8'))
        with mock.patch('orchestrator.collaboration.model_catalog.open_same_origin',
                        return_value=response) as request:
            configured, models = _probe_openai_compat_models(
                'http://api.example.test/v1', 'TEST_API_KEY', True)
        self.assertTrue(configured)
        self.assertEqual(models, [{'id': 'model-a', 'label': 'model-a'}])
        request.assert_called_once()

    def test_key_not_in_payload_log(self):
        # Key 只走 Authorization header，不出现在 stdout/产物
        with mock.patch("orchestrator.adapters.openai_compat._open_request", return_value=ok_response()) as m:
            res = self.adapter.run("prompt", self.tmp_path, make_ctx(self.tmp_path))
            sent_req = m.call_args[0][0]
            self.assertEqual(sent_req.headers["Authorization"], "Bearer sk-test")
        self.assertNotIn("sk-test", res.stdout)
        self.assertNotIn("sk-test", res.text)


if __name__ == "__main__":
    unittest.main()
