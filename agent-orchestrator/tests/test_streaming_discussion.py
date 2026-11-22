"""逐 token 实时会诊：SSE 解析、批处理与工作台 SSE 端到端推送回归。"""
from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from orchestrator.adapters.openai_compat import OpenAICompatAdapter, parse_sse_stream
from orchestrator.collaboration.discussion_stream import (
    DiscussionEventSink,
    TokenBatcher,
    run_streaming_turn,
)
from orchestrator.contracts import ErrorKind, RunContext
from orchestrator.dashboard.server import create_server


def _start_mock_sse_server(chunks: list[str]):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_POST(self):
            length = int(self.headers.get('Content-Length', '0'))
            if length:
                self.rfile.read(length)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.end_headers()
            for line in chunks:
                self.wfile.write((line + '\n').encode('utf-8'))
                self.wfile.flush()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class StreamingDiscussionTests(unittest.TestCase):
    def test_parse_sse_stream_accumulates_tokens_usage_and_rejects_incomplete_stream(self):
        seen: list[str] = []
        lines = [
            b': keep-alive\n',
            b'data: {"id":"chat-1","model":"mock-v1","choices":[{"delta":{"content":"\xe4\xbd\xa0\xe5\xa5\xbd"}}]}\n',
            b'\n',
            b'data: {"id":"chat-1","model":"mock-v1","choices":[{"delta":{"content":"\xef\xbc\x8c\xe4\xb8\x96\xe7\x95\x8c"},"finish_reason":"stop"}]}\n',
            b'data: {"id":"chat-1","model":"mock-v1","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n',
            b'data: [DONE]\n',
        ]
        parsed = parse_sse_stream(lines, on_token=seen.append)
        self.assertEqual(seen, ['你好', '，世界'])
        self.assertEqual(parsed['choices'][0]['message']['content'], '你好，世界')
        self.assertEqual(parsed['usage']['total_tokens'], 15)

        # 未收到 [DONE] 必须报错
        with self.assertRaises(ValueError):
            parse_sse_stream(lines[:-1])

        # 超出 max_bytes 必须拒绝
        with self.assertRaises(ValueError):
            parse_sse_stream(lines, max_bytes=32)

    def test_openai_compat_stream_tokens_reports_usage_and_marks_missing_usage_unknown(self):
        sse_with_usage = [
            'data: {"id":"c1","model":"m1","choices":[{"delta":{"content":"第一段"}}]}',
            'data: {"id":"c1","model":"m1","choices":[{"delta":{"content":"第二段"},"finish_reason":"stop"}]}',
            'data: {"id":"c1","model":"m1","choices":[],"usage":{"prompt_tokens":12,"completion_tokens":8,"total_tokens":20}}',
            'data: [DONE]',
        ]
        server, thread = _start_mock_sse_server(sse_with_usage)
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(3)))
        os.environ['TEST_STREAM_API_KEY'] = 'dummy-stream-key-12345678'
        self.addCleanup(lambda: os.environ.pop('TEST_STREAM_API_KEY', None))

        received: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = OpenAICompatAdapter(
                'api', f'http://127.0.0.1:{server.server_port}/v1', 'm1', 'TEST_STREAM_API_KEY',
                stream_tokens=True, on_token=received.append,
            )
            ctx = RunContext('t', 'r1', 'turn_1', root, root, kind='report')
            result = adapter.run('分析问题', root, ctx)
            self.assertTrue(result.ok)
            self.assertEqual(received, ['第一段', '第二段'])
            self.assertEqual(result.text, '第一段第二段')
            self.assertEqual(result.usage['tokens'], 20)
            self.assertFalse(result.usage.get('usage_unknown'))

        # 服务端未返回 usage 时保守标记 usage_unknown=True
        sse_no_usage = [
            'data: {"id":"c2","model":"m1","choices":[{"delta":{"content":"无用量"},"finish_reason":"stop"}]}',
            'data: [DONE]',
        ]
        server2, thread2 = _start_mock_sse_server(sse_no_usage)
        self.addCleanup(lambda: (server2.shutdown(), server2.server_close(), thread2.join(3)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter2 = OpenAICompatAdapter(
                'api', f'http://127.0.0.1:{server2.server_port}/v1', 'm1', 'TEST_STREAM_API_KEY',
                stream_tokens=True,
            )
            res2 = adapter2.run('分析问题', root, RunContext('t', 'r2', 'turn_1', root, root, kind='report'))
            self.assertTrue(res2.ok)
            self.assertTrue(res2.usage.get('usage_unknown'))

        # 中途断流无 [DONE] => PARSE_FAILED + usage_unknown
        sse_broken = ['data: {"id":"c3","model":"m1","choices":[{"delta":{"content":"半截"}}]}']
        server3, thread3 = _start_mock_sse_server(sse_broken)
        self.addCleanup(lambda: (server3.shutdown(), server3.server_close(), thread3.join(3)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter3 = OpenAICompatAdapter(
                'api', f'http://127.0.0.1:{server3.server_port}/v1', 'm1', 'TEST_STREAM_API_KEY',
                stream_tokens=True,
            )
            res3 = adapter3.run('分析问题', root, RunContext('t', 'r3', 'turn_1', root, root, kind='report'))
            self.assertFalse(res3.ok)
            self.assertEqual(res3.error_kind, ErrorKind.PARSE_FAILED)
            self.assertTrue(res3.usage.get('usage_unknown'))

    def test_token_batcher_merges_and_flushes_cleanly(self):
        emitted: list[tuple[int, str]] = []
        now = [0.0]
        batcher = TokenBatcher(lambda seq, text: emitted.append((seq, text)),
                               max_chars=5, max_interval_s=1.0, clock=lambda: now[0])
        batcher.push('ab')
        batcher.push('cd')
        self.assertEqual(emitted, [])
        batcher.push('ef')
        self.assertEqual(emitted, [(1, 'abcdef')])
        batcher.push('g')
        batcher.flush()
        self.assertEqual(emitted, [(1, 'abcdef'), (2, 'g')])

    def test_streaming_discussion_events_are_delivered_over_dashboard_sse_endpoint(self):
        sse_lines = [
            'data: {"id":"c-live","model":"m1","choices":[{"delta":{"content":"建议修复"}}]}',
            'data: {"id":"c-live","model":"m1","choices":[{"delta":{"content":"边界校验"},"finish_reason":"stop"}]}',
            'data: {"id":"c-live","model":"m1","choices":[],"usage":{"prompt_tokens":5,"completion_tokens":4,"total_tokens":9}}',
            'data: [DONE]',
        ]
        mock_api, api_thread = _start_mock_sse_server(sse_lines)
        self.addCleanup(lambda: (mock_api.shutdown(), mock_api.server_close(), api_thread.join(3)))
        os.environ['TEST_STREAM_API_KEY'] = 'dummy-stream-key-87654321'
        self.addCleanup(lambda: os.environ.pop('TEST_STREAM_API_KEY', None))

        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        cfg = root / 'project.toml'
        cfg.write_text(
            'task_workdir = "ws"\nstate_dir = "state"\n'
            '[run_budget]\nmax_tokens = 1000\nmax_duration_s = 60\n'
            '[[stages]]\nname = "t"\nkind = "test"\ntest_cmd = ["python", "-c", "pass"]\n',
            encoding='utf8',
        )
        server = create_server(root / 'db.sqlite3', [cfg], 0, recover=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.application.closed.set(), server.shutdown(),
                                 server.server_close(), thread.join(3)))
        service = server.application.service
        project_id = service.store.projects()[0]['id']
        run_key = service.store.register_run(project_id, 'run-sse', cfg, root / 'state')

        adapter = OpenAICompatAdapter(
            'developer', f'http://127.0.0.1:{mock_api.server_port}/v1', 'm1', 'TEST_STREAM_API_KEY',
        )
        sink = DiscussionEventSink(service.store, run_key, 'topic-1', 'developer', max_chars=2)
        result = run_streaming_turn(
            adapter, '请分析', root, RunContext('t', 'run-sse', 'turn_1_developer', root, root, kind='report'), sink
        )
        self.assertTrue(result.ok)

        # 通过工作台真实 HTTP 端点校验流式会诊事件序列与内容
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
        self.addCleanup(conn.close)
        conn.request('POST', '/api/session', json.dumps({'token': server.application.bootstrap}),
                     {'Content-Type': 'application/json', 'Origin': server.application.origin})
        resp = conn.getresponse()
        resp.read()
        cookie = resp.getheader('Set-Cookie').split(';')[0]
        conn.request('GET', f'/api/runs/{run_key}/events?after=0&limit=50', headers={'Cookie': cookie})
        ev_resp = conn.getresponse()
        payload = json.loads(ev_resp.read().decode('utf-8'))
        kinds = [e['kind'] for e in payload['events']]
        self.assertIn('discussion_turn_start', kinds)
        self.assertIn('discussion_token', kinds)
        self.assertIn('discussion_turn_end', kinds)
        tokens_joined = ''.join(
            e['payload']['delta'] for e in payload['events'] if e['kind'] == 'discussion_token'
        )
        self.assertEqual(tokens_joined, '建议修复边界校验')

    def test_roundtable_multi_round_debate_at_file_and_consensus(self):
        from orchestrator.collaboration.discussion import capture, execute
        sse_lines = [
            'data: {"id":"rt-1","model":"m-debate","choices":[{"delta":{"content":"### 已达成一致\\n- 保留依赖版本\\n### 建议执行步骤\\n- 修正端口"},"finish_reason":"stop"}]}',
            'data: {"id":"rt-1","model":"m-debate","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":15,"total_tokens":25}}',
            'data: [DONE]',
        ]
        mock_api, api_thread = _start_mock_sse_server(sse_lines)
        self.addCleanup(lambda: (mock_api.shutdown(), mock_api.server_close(), api_thread.join(3)))
        os.environ['TEST_RT_API_KEY'] = 'dummy-rt-key-12345678'
        self.addCleanup(lambda: os.environ.pop('TEST_RT_API_KEY', None))

        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        ws = root / 'ws'
        ws.mkdir()
        (ws / 'app.py').write_text('PORT = 3008\n', encoding='utf8')
        cfg = root / 'project.toml'
        cfg.write_text(
            f'task_workdir = "{ws.as_posix()}"\n'
            f'state_dir = "{(root / "state").as_posix()}"\n'
            '[run_budget]\nmax_tokens = 20000\nmax_duration_s = 120\n'
            '[adapters.api]\ntype = "openai_compat"\n'
            f'base_url = "http://127.0.0.1:{mock_api.server_port}/v1"\n'
            'api_key_env = "TEST_RT_API_KEY"\nmodel = "m-default"\n'
            '[[stages]]\nname = "design"\nkind = "report"\nadapter = "api"\nprompt = "设计"\n',
            encoding='utf8',
        )
        server = create_server(root / 'db.sqlite3', [cfg], 0, recover=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.application.closed.set(), server.shutdown(),
                                 server.server_close(), thread.join(3)))
        service = server.application.service
        project_id = service.store.projects()[0]['id']
        run_key = service.store.register_run(project_id, 'run-rt', cfg, root / 'state')
        run = service.store.run(run_key)

        # @app.py 自动纳入固定快照；越界路径 ../secret.txt 被安全忽略
        snap = capture(run, extra_files=['../secret.txt'], question='请结合 @app.py 讨论端口配置')
        self.assertEqual([s['path'] for s in snap['sources']], ['app.py'])
        self.assertIn('PORT = 3008', snap['sources'][0]['body'])

        snap['roundtable'] = {
            'rounds': 2,
            'role_models': {'developer': 'm-dev', 'skeptic': 'm-red'},
            'consensus': True,
        }
        topic = service.store.create_thread(
            run_key, '请结合 @app.py 讨论端口配置', ['developer', 'skeptic'], 8000, snap, 'idem-rt-12345678'
        )
        execute(service.store, topic['id'], stream_tokens=True)

        done_topic = service.store.thread(topic['id'])
        self.assertEqual(done_topic['status'], 'completed')
        self.assertEqual(done_topic['result']['rounds'], 2)
        self.assertIn('已达成一致', done_topic['result'].get('consensus', ''))
        # 2 轮 × 2 角色 + 1 共识 = 5 个分析阶段全部 done
        self.assertEqual(len(done_topic['result']['results']), 5)
        self.assertTrue(all(v == 'done' for v in done_topic['result']['results'].values()))
        # 校验事件流里落地了 consensus 消息
        evs = service.store.events(run_key, 0, 100)
        msg_roles = [e['payload']['role'] for e in evs if e['kind'] == 'message']
        self.assertIn('developer', msg_roles)
        self.assertIn('skeptic', msg_roles)
        self.assertIn('consensus', msg_roles)
