"""逐 token 实时会诊：SSE 解析、批处理与工作台 SSE 端到端推送回归。"""
from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

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

    def test_sse_deadline_and_cancel_interrupt_blocking_or_trickling_reads(self):
        class BlockingResponse:
            def __init__(self):
                self.closed = threading.Event()

            def read1(self, size):
                self.closed.wait(2)
                return b''

            def close(self):
                self.closed.set()

        blocked = BlockingResponse()
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, '总时长超时'):
            parse_sse_stream(blocked, deadline=started + 0.1)
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(blocked.closed.is_set())

        cancelled = threading.Event()
        blocked = BlockingResponse()
        timer = threading.Timer(0.05, cancelled.set)
        timer.start()
        self.addCleanup(timer.join, 1)
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, '已取消'):
            parse_sse_stream(blocked, cancel_requested=cancelled.is_set)
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(blocked.closed.is_set())

        class TrickleResponse(BlockingResponse):
            def read1(self, size):
                time.sleep(0.02)
                return b'x'

        trickle = TrickleResponse()
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, '总时长超时'):
            parse_sse_stream(trickle, deadline=started + 0.11)
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(trickle.closed.is_set())

        class OversizedResponse(BlockingResponse):
            def read1(self, size):
                return b'x' * 65

        with self.assertRaisesRegex(ValueError, '最大字节上限'):
            parse_sse_stream(OversizedResponse(), max_bytes=64)

    def test_openai_stream_total_deadline_interrupts_slow_http_body(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_POST(self):
                length = int(self.headers.get('Content-Length', '0'))
                if length:
                    self.rfile.read(length)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                try:
                    for _ in range(30):
                        self.wfile.write(b'x')
                        self.wfile.flush()
                        time.sleep(0.1)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(3)))
        with mock.patch.dict(os.environ, {'TEST_STREAM_DEADLINE_KEY': 'dummy-deadline-key'}):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                adapter = OpenAICompatAdapter(
                    'api', f'http://127.0.0.1:{server.server_port}/v1', 'm1',
                    'TEST_STREAM_DEADLINE_KEY', stream_tokens=True, timeout_s=1,
                )
                started = time.monotonic()
                result = adapter.run('slow', root, RunContext('t', 'r', 'turn', root, root,
                                                             kind='report', timeout_s=1))
                self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, ErrorKind.TIMEOUT)

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
        # 校验事件流里落地了 consensus 消息，且两轮流式 turn_start 互不冲突（共 5 个独立 turn_start）
        evs = service.store.events(run_key, 0, 200)
        msg_roles = [e['payload']['role'] for e in evs if e['kind'] == 'message']
        self.assertIn('developer', msg_roles)
        self.assertIn('skeptic', msg_roles)
        self.assertIn('consensus', msg_roles)
        turn_starts = [e['payload'].get('turn') for e in evs if e['kind'] == 'discussion_turn_start']
        self.assertEqual(turn_starts, ['r1_developer', 'r1_skeptic', 'r2_developer', 'r2_skeptic', 'turn_consensus'])

    def test_roundtable_cli_agy_seat_readonly_and_mid_round_human_interjection(self):
        from orchestrator.collaboration.discussion import _build_seat_adapter, capture, execute
        from orchestrator.config import AdapterConfig, load_config

        captured_prompts: list[str] = []

        class RecordingHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_POST(self):
                length = int(self.headers.get('Content-Length', '0'))
                raw = self.rfile.read(length).decode('utf-8', errors='replace') if length else '{}'
                body = json.loads(raw)
                msgs = body.get('messages') or []
                captured_prompts.append(msgs[-1].get('content', '') if msgs else '')
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                self.end_headers()
                for line in [
                    'data: {"id":"rt-hi","model":"m-dev","choices":[{"delta":{"content":"收到引导，收敛方案。"},"finish_reason":"stop"}]}',
                    'data: {"id":"rt-hi","model":"m-dev","choices":[],"usage":{"prompt_tokens":8,"completion_tokens":6,"total_tokens":14}}',
                    'data: [DONE]',
                ]:
                    self.wfile.write((line + '\n').encode('utf-8'))
                    self.wfile.flush()

        mock_api = ThreadingHTTPServer(('127.0.0.1', 0), RecordingHandler)
        api_thread = threading.Thread(target=mock_api.serve_forever, daemon=True)
        api_thread.start()
        self.addCleanup(lambda: (mock_api.shutdown(), mock_api.server_close(), api_thread.join(3)))
        os.environ['TEST_RT_HI_KEY'] = 'dummy-rt-hi-key-12345678'
        self.addCleanup(lambda: os.environ.pop('TEST_RT_HI_KEY', None))

        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        ws = root / 'ws'
        ws.mkdir()
        cfg_path = root / 'project.toml'
        cfg_path.write_text(
            f'task_workdir = "{ws.as_posix()}"\n'
            f'state_dir = "{(root / "state").as_posix()}"\n'
            '[run_budget]\nmax_tokens = 20000\nmax_duration_s = 120\n'
            '[adapters.api]\ntype = "openai_compat"\n'
            f'base_url = "http://127.0.0.1:{mock_api.server_port}/v1"\n'
            'api_key_env = "TEST_RT_HI_KEY"\nmodel = "m-default"\n'
            '[adapters.agy]\ntype = "cli_agy"\nmodel = "gemini-4-neon-high"\n'
            'proxy_url = "http://127.0.0.1:7897"\nextra_env = ["RT_PROXY_CERT"]\n'
            'mode = "accept-edits"\nsandbox = false\n'
            '[[stages]]\nname = "design"\nkind = "report"\nadapter = "api"\nprompt = "设计"\n',
            encoding='utf8',
        )
        cfg = load_config(cfg_path)
        apis = [a for a in cfg.adapters.values() if a.type == 'openai_compat']
        agys = [a for a in cfg.adapters.values() if a.type == 'cli_agy']

        # 1) 验证 cli_agy 席位强制降权为只读 plan + sandbox=True，且继承 proxy_url
        seat = _build_seat_adapter('skeptic', 'cli_agy', 'claude-opus-5-5-high', cfg, apis, agys)
        self.assertEqual(seat.type, 'cli_agy')
        self.assertEqual(seat.options['mode'], 'plan')
        self.assertTrue(seat.options['sandbox'])
        self.assertEqual(seat.options['proxy_url'], 'http://127.0.0.1:7897')
        self.assertEqual(seat.options['extra_env'], ['RT_PROXY_CERT'])
        self.assertEqual(seat.options['model'], 'claude-opus-5-5-high')
        with self.assertRaisesRegex(ValueError, '没有可用的 cli_agy'):
            _build_seat_adapter('skeptic', 'cli_agy', '', cfg, apis, [])

        # 2) 验证 service.discuss 拒绝非白名单后端（如 cli_codex）
        server = create_server(root / 'db.sqlite3', [cfg_path], 0, recover=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.application.closed.set(), server.shutdown(),
                                 server.server_close(), thread.join(3)))
        service = server.application.service
        project_id = service.store.projects()[0]['id']
        run_key = service.store.register_run(project_id, 'run-hi', cfg_path, root / 'state')
        with self.assertRaises(ValueError):
            service.discuss(
                run_key, '测试后端白名单', ['developer'], 4000, 'idem-bad-backend-1234',
                role_adapters={'developer': 'cli_codex'},
            )

        source = cfg_path.read_text(encoding='utf8')
        no_agy = root / 'no-agy.toml'
        no_agy.write_text(source[:source.index('[adapters.agy]')] + source[source.index('[[stages]]'):],
                          encoding='utf8')
        project2 = service.store.register_project(no_agy)
        run2 = service.store.register_run(project2, 'run-no-agy', no_agy, root / 'state-no-agy')
        with self.assertRaisesRegex(ValueError, '未配置 cli_agy'):
            service.discuss(run2, '未配置后端', ['developer'], 4000, 'idem-no-agy-1234',
                            role_adapters={'developer': 'cli_agy'})

        # 3) 验证人工主持人插话仅注入第 2 轮及共识阶段，不污染第 1 轮
        run = service.store.run(run_key)
        snap = capture(run, question='讨论架构取舍')
        snap['roundtable'] = {'rounds': 2, 'role_models': {}, 'role_adapters': {}, 'consensus': True}
        topic = service.store.create_thread(
            run_key, '讨论架构取舍', ['developer'], 8000, snap, 'idem-hi-12345678'
        )
        # 在会诊启动时间戳之后插入一条人工主持人插话
        service.store.message(run_key, '请优先保证兼容旧接口，不要改数据库表结构', 'idem-human-note-1234')
        execute(service.store, topic['id'], stream_tokens=True)

        self.assertEqual(len(captured_prompts), 3)  # r1_developer, r2_developer, turn_consensus
        self.assertNotIn('人工主持人最新插话引导', captured_prompts[0])
        self.assertIn('人工主持人最新插话引导', captured_prompts[1])
        self.assertIn('不要改数据库表结构', captured_prompts[1])
        self.assertIn('人工主持人最新插话引导', captured_prompts[2])

    def test_discussion_failure_settles_usage_already_consumed_by_engine(self):
        import time as _time
        import warnings
        from types import SimpleNamespace
        from unittest import mock

        from orchestrator.collaboration.discussion import capture, execute
        from orchestrator.collaboration.service import Service
        from orchestrator.config import load_config
        from orchestrator.state import RunState, atomic_write_json

        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        ws = root / 'ws'
        ws.mkdir()
        (ws / 'app.py').write_text('VALUE = 1\n', encoding='utf8')
        state_dir = root / 'state'
        cfg_path = root / 'project.toml'
        cfg_path.write_text(
            f'task_workdir = "{ws.as_posix()}"\n'
            f'state_dir = "{state_dir.as_posix()}"\n'
            '[run_budget]\nmax_tokens = 20000\nmax_duration_s = 120\n'
            '[adapters.api]\ntype = "openai_compat"\n'
            'base_url = "https://example.invalid/v1"\napi_key_env = "TEST_UNUSED_KEY"\nmodel = "mock"\n'
            '[[stages]]\nname = "design"\nkind = "report"\nadapter = "api"\nprompt = "设计"\n',
            encoding='utf8',
        )
        cfg = load_config(cfg_path)
        service = Service(root / 'db.sqlite3', [cfg_path])
        project_id = service.store.projects()[0]['id']
        parent = RunState(state_dir, 'run-discussion-failure')
        parent.usage_total = {
            'tokens': 1000, 'reported_tokens': 1000, 'reserved_tokens': 0,
            'unknown_calls': 0, 'duration_s': 1.0,
        }
        parent.save()
        atomic_write_json(
            parent.run_dir / 'job.json',
            {'status': 'paused', 'started_at': _time.time(), 'max_duration_s': 120,
             'config_hash': cfg.source_hash},
        )
        run_key = service.store.register_run(project_id, 'run-discussion-failure', cfg_path, state_dir, managed=True)
        run = service.store.run(run_key)
        topic = service.store.create_thread(
            run_key, '讨论失败后的用量', ['developer'], 4000,
            capture(run, question='讨论失败后的用量'), 'idem-discussion-failure-001',
        )

        class FailingEngine:
            def __init__(self, *args):
                self.state = SimpleNamespace(usage_total={
                    'tokens': 25, 'reported_tokens': 25, 'reserved_tokens': 0,
                    'unknown_calls': 0, 'duration_s': 2.0,
                })

            def run(self):
                raise RuntimeError('failed after token consumption')

        with mock.patch('orchestrator.collaboration.discussion.Engine', return_value=FailingEngine()):
            with self.assertRaisesRegex(RuntimeError, 'failed after token consumption'):
                execute(service.store, topic['id'], stream_tokens=True)

        reloaded = RunState(state_dir, 'run-discussion-failure')
        reloaded.load()
        self.assertEqual(reloaded.usage_total['tokens'], 1025)
        self.assertEqual(reloaded.usage_total['reserved_tokens'], 0)
        self.assertEqual(reloaded.usage_total['reported_tokens'], 1025)
        hold = reloaded.control['discussion_holds'][topic['id']]
        self.assertTrue(hold['settled'])
        self.assertEqual(hold['usage']['tokens'], 25)

        topic2 = service.store.create_thread(
            run_key, '讨论结算失败后的原异常', ['developer'], 4000,
            capture(run, question='讨论结算失败后的原异常'), 'idem-discussion-settle-failure-002',
        )
        with mock.patch('orchestrator.collaboration.discussion.Engine', return_value=FailingEngine()), \
                mock.patch('orchestrator.collaboration.discussion.settle_parent',
                           side_effect=OSError('settlement failed')):
            with warnings.catch_warnings():
                warnings.simplefilter('error')
                with self.assertLogs('orchestrator.collaboration.discussion', level='WARNING') as logs:
                    with self.assertRaisesRegex(RuntimeError, 'failed after token consumption'):
                        execute(service.store, topic2['id'], stream_tokens=True)
        self.assertIn('用量结算失败', logs.output[0])
        reloaded = RunState(state_dir, 'run-discussion-failure')
        reloaded.load()
        self.assertEqual(reloaded.usage_total['tokens'], 5025)
        self.assertEqual(reloaded.usage_total['reserved_tokens'], 4000)
        self.assertFalse(reloaded.control['discussion_holds'][topic2['id']]['settled'])

    def test_non_inline_discussion_failure_settles_persisted_usage(self):
        import time as _time
        from unittest import mock

        from orchestrator.collaboration.discussion import capture, execute
        from orchestrator.collaboration.service import Service
        from orchestrator.config import load_config
        from orchestrator.state import RunState, atomic_write_json

        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        ws = root / 'ws'
        ws.mkdir()
        state_dir = root / 'state'
        cfg_path = root / 'project.toml'
        cfg_path.write_text(
            f'task_workdir = "{ws.as_posix()}"\n'
            f'state_dir = "{state_dir.as_posix()}"\n'
            '[run_budget]\nmax_tokens = 20000\nmax_duration_s = 120\n'
            '[adapters.api]\ntype = "openai_compat"\n'
            'base_url = "https://example.invalid/v1"\napi_key_env = "TEST_UNUSED_KEY"\nmodel = "mock"\n'
            '[[stages]]\nname = "design"\nkind = "report"\nadapter = "api"\nprompt = "设计"\n',
            encoding='utf8',
        )
        cfg = load_config(cfg_path)
        service = Service(root / 'db.sqlite3', [cfg_path])
        project_id = service.store.projects()[0]['id']
        parent = RunState(state_dir, 'run-discussion-worker-failure')
        parent.usage_total = {
            'tokens': 1000, 'reported_tokens': 1000, 'reserved_tokens': 0,
            'unknown_calls': 0, 'duration_s': 1.0,
        }
        parent.save()
        atomic_write_json(
            parent.run_dir / 'job.json',
            {'status': 'paused', 'started_at': _time.time(), 'max_duration_s': 120,
             'config_hash': cfg.source_hash},
        )
        run_key = service.store.register_run(
            project_id, 'run-discussion-worker-failure', cfg_path, state_dir, managed=True)
        run = service.store.run(run_key)

        for index, mode in enumerate(('supervise', 'load_config'), start=1):
            with self.subTest(mode=mode):
                topic = service.store.create_thread(
                    run_key, f'非 inline 异常结算 {mode}', ['developer'], 4000,
                    capture(run, question=f'非 inline 异常结算 {mode}'),
                    f'idem-discussion-worker-{index}',
                )

                def fake_supervise(config_path, run_id, task, max_restarts):
                    analysis = RunState(Path(config_path).parent / 'state', run_id)
                    analysis.usage_total = {
                        'tokens': 25, 'reported_tokens': 25, 'reserved_tokens': 0,
                        'unknown_calls': 0, 'duration_s': 2.0,
                    }
                    analysis.save()
                    if mode == 'supervise':
                        raise RuntimeError('worker failed after token consumption')
                    return {'status': 'failed'}

                patches = [mock.patch('orchestrator.supervisor.supervise', side_effect=fake_supervise)]
                if mode == 'load_config':
                    patches.append(mock.patch(
                        'orchestrator.config.load_config',
                        side_effect=[cfg, RuntimeError('analysis config failed after worker exit')],
                    ))
                with patches[0]:
                    if len(patches) == 2:
                        with patches[1]:
                            with self.assertRaisesRegex(RuntimeError, 'analysis config failed'):
                                execute(service.store, topic['id'], stream_tokens=False)
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'worker failed after token consumption'):
                            execute(service.store, topic['id'], stream_tokens=False)

                reloaded = RunState(state_dir, 'run-discussion-worker-failure')
                reloaded.load()
                self.assertEqual(reloaded.usage_total['tokens'], 1000 + 25 * index)
                self.assertEqual(reloaded.usage_total['reserved_tokens'], 0)
                hold = reloaded.control['discussion_holds'][topic['id']]
                self.assertTrue(hold['settled'])
                self.assertEqual(hold['usage']['tokens'], 25)

    def test_discussion_security_dotfile_exe_inheritance_budget_refund_and_cli_agy_capability(self):
        import time as _time
        from orchestrator.collaboration.discussion import (
            _build_seat_adapter,
            _safe_read_workspace_files,
            capture,
            execute,
        )
        from orchestrator.config import load_config
        from orchestrator.state import RunState, atomic_write_json

        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        ws = root / 'ws'
        (ws / '.github' / 'workflows').mkdir(parents=True)
        (ws / '.github' / 'workflows' / 'ci.yml').write_text('name: CI\n', encoding='utf-8')
        (ws / '.env').write_text('SECRET_TOKEN=super-secret-value\n', encoding='utf-8')
        (ws / '.env.production').write_text('API_KEY=prod-secret\n', encoding='utf-8')
        (ws / 'private.key').write_text('PRIVATE KEY DATA\n', encoding='utf-8')
        (ws / 'ok.py').write_text('OK = True\n', encoding='utf-8')

        # 合法点文件 .github/workflows/ci.yml 可读取，敏感凭据文件与越界路径均被拦截
        files = _safe_read_workspace_files(
            ws, ['.env', '.env.production', 'private.key', '../outside.txt', './.github/workflows/ci.yml', 'ok.py']
        )
        paths = [f['path'] for f in files]
        self.assertEqual(paths, ['.github/workflows/ci.yml', 'ok.py'])

        # cli_agy-only 项目：1) snapshot.capabilities.discussion 为 True；2) 席位继承 exe；3) 失败时退还父运行预留预算
        state_dir = root / 'state'
        cfg_path = root / 'agy_only.toml'
        cfg_path.write_text(
            f'task_workdir = "{ws.as_posix()}"\n'
            f'state_dir = "{state_dir.as_posix()}"\n'
            '[run_budget]\nmax_tokens = 20000\nmax_duration_s = 120\n'
            '[adapters.agy]\ntype = "cli_agy"\nexe = "C:/custom/agy.cmd"\nmodel = "gemini-4-neon-high"\n'
            'proxy_url = "http://127.0.0.1:7897"\n'
            '[[stages]]\nname = "code_review"\nkind = "report"\nadapter = "agy"\nprompt = "审查"\n',
            encoding='utf-8',
        )
        cfg = load_config(cfg_path)
        agys = [a for a in cfg.adapters.values() if a.type == 'cli_agy']
        seat = _build_seat_adapter('reviewer', 'cli_agy', '', cfg, [], agys)
        self.assertEqual(seat.options.get('exe'), 'C:/custom/agy.cmd')

        server = create_server(root / 'db.sqlite3', [cfg_path], 0, recover=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.application.closed.set(), server.shutdown(),
                                 server.server_close(), thread.join(3)))
        service = server.application.service
        project_id = service.store.projects()[0]['id']
        run_state = RunState(state_dir, 'run-agy-cap')
        run_state.stage('code_review')
        run_state.usage_total = {'tokens': 1000, 'reported_tokens': 1000, 'reserved_tokens': 0, 'duration_s': 1.0}
        run_state.save()
        atomic_write_json(
            run_state.run_dir / 'job.json',
            {'status': 'paused', 'started_at': _time.time(), 'max_duration_s': 120, 'config_hash': cfg.source_hash},
        )
        run_key = service.store.register_run(project_id, 'run-agy-cap', cfg_path, state_dir, managed=True)
        snap_view = service.snapshot(run_key)
        self.assertTrue(snap_view['capabilities']['discussion'])

        # 构造席位强制选择不存在的 openai_compat 触发异常，验证 reserve_parent 后预算自动结算退还
        run = service.store.run(run_key)
        snap = capture(run, question='测试预算回退')
        snap['roundtable'] = {'rounds': 1, 'role_models': {}, 'role_adapters': {'developer': 'openai_compat'}, 'consensus': False}
        topic = service.store.create_thread(run_key, '测试预算回退', ['developer'], 4000, snap, 'idem-refund-12345')
        with self.assertRaises(ValueError):
            execute(service.store, topic['id'], stream_tokens=False)
        reloaded = RunState(state_dir, 'run-agy-cap')
        reloaded.load()
        self.assertEqual(reloaded.usage_total['tokens'], 1000)
        self.assertEqual(reloaded.usage_total.get('reserved_tokens', 0), 0)

        # human_messages_since 超过 limit 时返回最新记录而非最早记录
        base_ts = _time.time() - 10
        for idx in range(6):
            service.store.message(run_key, f'插话-{idx}', f'idem-interj-{idx:04d}')
        newest = service.store.human_messages_since(run_key, base_ts, limit=3)
        self.assertEqual([r['body'] for r in newest], ['插话-3', '插话-4', '插话-5'])
