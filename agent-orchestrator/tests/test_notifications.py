"""外部通知 Webhook：载荷格式化、HMAC 签名、脱敏与容错回归测试。"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import unittest

from orchestrator.notifications import (
    WebhookNotifier,
    WebhookTarget,
    format_webhook_payload,
    sign_payload,
    validate_webhook_url,
)


def _start_webhook_receiver(status_code: int = 200):
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_POST(self):
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length) if length else b''
            received.append({
                'body': body,
                'json': json.loads(body.decode('utf-8')) if body else None,
                'signature': self.headers.get('X-Pipeline-Signature-256', ''),
            })
            self.send_response(status_code)
            self.end_headers()
            self.wfile.write(b'ok')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, received


class NotificationTests(unittest.TestCase):
    def test_validate_webhook_url_requires_https_except_loopback(self):
        self.assertEqual(validate_webhook_url('https://open.feishu.cn/open-apis/bot/v2/hook/abc'),
                         'https://open.feishu.cn/open-apis/bot/v2/hook/abc')
        self.assertEqual(validate_webhook_url('http://127.0.0.1:8080/hook'), 'http://127.0.0.1:8080/hook')
        for bad in ('http://example.com/hook', 'file:///etc/passwd', 'https://user:pass@example.com/hook'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_webhook_url(bad)

    def test_payload_formats_cover_feishu_dingtalk_slack_and_generic(self):
        feishu = format_webhook_payload('feishu', run_id='r1', event='done', stage='review', summary='全部通过')
        self.assertEqual(feishu['msg_type'], 'text')
        self.assertIn('r1', feishu['content']['text'])

        ding = format_webhook_payload('dingtalk', run_id='r1', event='failed', stage='testing', summary='单测失败')
        self.assertEqual(ding['msgtype'], 'text')
        self.assertIn('单测失败', ding['text']['content'])

        slack = format_webhook_payload('slack', run_id='r1', event='awaiting_approval', stage='review')
        self.assertIn('awaiting_approval', slack['text'])

        generic = format_webhook_payload('generic', run_id='r1', event='done', usage={'tokens': 123, 'ignored': True})
        self.assertEqual(generic['usage'], {'tokens': 123})

    def test_notifier_signs_redacts_and_never_raises_on_errors(self):
        ok_srv, ok_th, received = _start_webhook_receiver(200)
        self.addCleanup(lambda: (ok_srv.shutdown(), ok_srv.server_close(), ok_th.join(3)))
        err_srv, err_th, _ = _start_webhook_receiver(500)
        self.addCleanup(lambda: (err_srv.shutdown(), err_srv.server_close(), err_th.join(3)))

        os.environ['TEST_HOOK_OK'] = f'http://127.0.0.1:{ok_srv.server_port}/hook'
        os.environ['TEST_HOOK_ERR'] = f'http://127.0.0.1:{err_srv.server_port}/hook'
        os.environ['TEST_HOOK_SECRET'] = 'signing-secret-99887766'
        os.environ['TEST_LEAKED_API_KEY'] = 'sk-super-secret-value-123456'
        for key in ('TEST_HOOK_OK', 'TEST_HOOK_ERR', 'TEST_HOOK_SECRET', 'TEST_LEAKED_API_KEY'):
            self.addCleanup(lambda k=key: os.environ.pop(k, None))

        notifier = WebhookNotifier((
            WebhookTarget('feishu', 'TEST_HOOK_OK', format='feishu', secret_env='TEST_HOOK_SECRET'),
            WebhookTarget('failing', 'TEST_HOOK_ERR', format='generic'),
            WebhookTarget('missing', 'TEST_HOOK_UNSET', format='slack'),
            WebhookTarget('filtered', 'TEST_HOOK_OK', events=('failed',)),
        ))
        receipts = notifier.notify(
            run_id='run-42',
            event='done',
            stage='review',
            summary='完成，不要泄露 sk-super-secret-value-123456',
            usage={'tokens': 500, 'model_calls': 3},
        )
        by_name = {r.target: r for r in receipts}
        self.assertTrue(by_name['feishu'].ok)
        self.assertEqual(by_name['feishu'].status_code, 200)
        self.assertFalse(by_name['failing'].ok)
        self.assertEqual(by_name['failing'].status_code, 500)
        self.assertTrue(by_name['missing'].skipped)
        self.assertTrue(by_name['filtered'].skipped)

        self.assertEqual(len(received), 1)
        sent = received[0]
        self.assertEqual(sent['signature'], sign_payload(sent['body'], 'signing-secret-99887766'))
        self.assertNotIn('sk-super-secret-value-123456', sent['json']['content']['text'])
        self.assertIn('[REDACTED]', sent['json']['content']['text'])
