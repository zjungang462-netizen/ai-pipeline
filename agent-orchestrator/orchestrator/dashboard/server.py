"""本地 HTTP/JSON/SSE 工作台，同源会话及明确的指令入口。"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from ..collaboration.service import Service
from ..collaboration.storage import Conflict
from ..collaboration.projection import read_artifact


class Application:
    def __init__(self, database, configs, *, recover=True):
        from .._version import __version__
        self.version = __version__
        self.service = Service(database, configs, recover=recover)
        self.bootstrap = secrets.token_urlsafe(32)
        self.session = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(24)
        self.origin = ''
        self.streams = threading.BoundedSemaphore(12)
        self.closed = self.service.closed
        self.projection_thread = None
        self.requests = threading.Condition()
        self.active_requests = 0

    def start_projection(self):
        def loop():
            while not self.closed.wait(.75):
                try:
                    self.service.sync(recover=self.service.recover, incremental=True)
                except Exception as error:
                    # 单轮投影失败（含畸形状态/坏配置/数据库瞬时错误）不得结束线程；
                    # 保留最后错误供诊断，并在页面上不做成“同步成功”。
                    reason = self.service.redact(str(error) or error.__class__.__name__)
                    self.service.last_sync_error = reason[:500]
        self.projection_thread = threading.Thread(target=loop, daemon=True)
        self.projection_thread.start()

    def close(self):
        with self.requests:
            self.closed.set()
        thread = self.projection_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(10)
            if thread.is_alive():
                raise RuntimeError('工作台投影线程尚未退出，不能确认资源已释放')
        self.service._background_sync.close()
        with self.requests:
            if not self.requests.wait_for(lambda: self.active_requests == 0, timeout=10):
                raise RuntimeError('工作台请求会话尚未退出，不能确认资源已释放')


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True

    def serve_forever(self, poll_interval=0.05):
        """缩短唤醒间隔，避免本地停服额外等待 0.5 秒。"""
        return super().serve_forever(poll_interval)

    def server_close(self):
        try:
            app = getattr(self, 'application', None)
            if app is not None:
                app.close()
        finally:
            super().server_close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    @property
    def app(self):
        return self.server.application

    def log_message(self, *args):
        pass  # 不把本地会话、任务文本或产物内容写入访问日志。

    def headers_common(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")

    def respond(self, value, status=200, cookie=False):
        data = json.dumps(self.app.service.redact(value), ensure_ascii=False).encode('utf8')
        self.send_response(status)
        self.headers_common()
        if status >= 400:
            self.close_connection = True
            self.send_header('Connection', 'close')
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        if cookie:
            self.send_header('Set-Cookie', f'workbench_session={self.app.session}; HttpOnly; SameSite=Strict; Path=/')
        self.end_headers()
        self.wfile.write(data)

    def authorised(self):
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            actual = cookie.get('workbench_session')
            return actual is not None and hmac.compare_digest(actual.value, self.app.session)
        except Exception:
            return False

    def handle_request(self, method):
        # 响应字节发完不等于 SQLite 会话已关闭；关闭服务须等待 finally。
        with self.app.requests:
            if self.app.closed.is_set():
                self.close_connection = True
                return
            self.app.active_requests += 1
        try:
            with self.app.service.store.session():
                return self.handle_session_request(method)
        finally:
            with self.app.requests:
                self.app.active_requests -= 1
                self.app.requests.notify_all()

    def handle_session_request(self, method):
        try:
            expected_host = urlsplit(self.app.origin).netloc
            if self.headers.get('Host') != expected_host:
                return self.respond({'error': '不接受其他 Host'}, 403)
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            if path == '/api/health' and method == 'GET':
                return self.respond({'status': 'ok', 'pid': os.getpid(), 'version': self.app.version})
            if method == 'GET' and path in ('/', '/app.js', '/insights.js', '/style.css'):
                name = {'/': 'index.html', '/app.js': 'app.js', '/insights.js': 'insights.js', '/style.css': 'style.css'}[path]
                body = (Path(__file__).parent / 'static' / name).read_bytes()
                self.send_response(200)
                self.headers_common()
                self.send_header('Content-Type', {'index.html': 'text/html; charset=utf-8',
                                                  'app.js': 'text/javascript; charset=utf-8',
                                                  'insights.js': 'text/javascript; charset=utf-8',
                                                  'style.css': 'text/css; charset=utf-8'}[name])
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = {}
            if method == 'POST':
                if self.headers.get('Origin') != self.app.origin:
                    return self.respond({'error': '写操作必须来自同一工作台'}, 403)
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 256000 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    return self.respond({'error': '请求必须为有界 JSON 对象'}, 400)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('请求必须为 JSON 对象')
            if path == '/api/session' and method == 'POST':
                token = body.get('token')
                if not isinstance(token, str) or not hmac.compare_digest(token, self.app.bootstrap):
                    return self.respond({'error': '请使用终端中本次启动的工作台链接'}, 401)
                return self.respond({'csrf': self.app.csrf, 'single_user': True, 'version': self.app.version}, cookie=True)
            if not self.authorised():
                return self.respond({'error': '请使用本机终端中的工作台链接建立会话'}, 401)
            if method == 'POST' and not hmac.compare_digest(self.headers.get('X-Workbench-CSRF', ''), self.app.csrf):
                return self.respond({'error': '请求令牌无效'}, 403)
            if path == '/api/session' and method == 'GET':
                return self.respond({'csrf': self.app.csrf, 'single_user': True, 'version': self.app.version})
            service, store = self.app.service, self.app.service.store
            from ..collaboration import insights
            parts = path.strip('/').split('/')
            if path == '/api/templates':
                if method == 'GET':
                    return self.respond({'templates': insights.templates(service)})
                if method == 'POST':
                    self.idem()  # 保存为按项目+名称的幂等 upsert，不执行任务。
                    return self.respond(insights.save_template(service, body['project_id'], body['name'],
                                        body['task'], body['expected_config_hash']), 201)
            if path == '/api/compare' and method == 'GET':
                if any(len(query.get(k, [])) != 1 for k in ('left', 'right')):
                    raise ValueError('left/right 各提供一个运行 ID')
                return self.respond(insights.compare(service, query['left'][0], query['right'][0]))
            if path == '/api/failures' and method == 'GET':
                if any(len(v) != 1 for v in query.values()): raise ValueError('查询参数不可重复')
                return self.respond(insights.failure_page(service, query.get('after', [None])[0],
                                                         int(query.get('limit', ['50'])[0])))
            if path == '/api/models/catalog' and method == 'GET':
                from ..collaboration.model_catalog import discover_local_catalog
                force = query.get('refresh', ['0'])[0] in ('1', 'true', 'yes')
                proxy_url = ''
                for proj in service.list_projects():
                    try:
                        cfg = service.config(proj['config'])
                        for ad in cfg.adapters.values():
                            if ad.type == 'cli_agy' and ad.options.get('proxy_url'):
                                proxy_url = ad.options['proxy_url']
                                break
                    except Exception:
                        pass
                    if proxy_url:
                        break
                return self.respond(discover_local_catalog(proxy_url=proxy_url, force=force))
            if path == '/api/projects' and method == 'GET':
                return self.respond({'projects': service.list_projects()})
            if path == '/api/projects/register' and method == 'POST':
                self.idem()
                from ..collaboration import project_manager
                return self.respond(project_manager.register_managed_project(service, body), 201)
            if len(parts) == 4 and parts[:2] == ['api', 'projects']:
                project_id, action = parts[2], parts[3]
                from ..collaboration import project_manager
                if action == 'route-recommend' and method == 'GET':
                    min_runs = int(query.get('min_runs', ['2'])[0])
                    return self.respond(project_manager.project_route_recommend(service, project_id, min_runs=min_runs))
                if action == 'lessons' and method == 'GET':
                    min_occ = int(query.get('min_occurrences', ['2'])[0])
                    return self.respond(project_manager.project_lessons(service, project_id, min_occurrences=min_occ))
                if action == 'stages' and method == 'GET':
                    return self.respond(project_manager.project_stage_definition(service, project_id))
            if len(parts) == 5 and parts[:2] == ['api', 'projects'] and parts[3:] == ['lessons', 'approve'] and method == 'POST':
                self.idem()
                from ..collaboration import project_manager
                raw_max = body.get('max_items', 12)
                if type(raw_max) is not int:
                    raise ValueError('max_items 必须是整数')
                return self.respond(project_manager.approve_project_lessons(service, parts[2], body.get('proposal'), max_items=raw_max))
            if path == '/api/runs':
                if method == 'GET':
                    for name in ('limit', 'before', 'cursor', 'q', 'project_id', 'status', 'sort'):
                        if name in query and len(query[name]) != 1:
                            raise ValueError(f'{name} 不可重复提供')
                    raw_limit = int(query.get('limit', ['50'])[0])
                    raw_before = float(query['before'][0]) if 'before' in query else None
                    raw_cursor = query['cursor'][0] if 'cursor' in query else None
                    raw_q = query['q'][0] if 'q' in query else None
                    raw_project_id = query['project_id'][0] if 'project_id' in query else None
                    raw_status = query['status'][0] if 'status' in query else None
                    raw_sort = query['sort'][0] if 'sort' in query else None
                    # 非法参数或游标不一致由 service 层抛 ValueError → 400
                    page = service.list_runs_page(raw_limit, raw_before, raw_cursor,
                                                  raw_q, raw_project_id, raw_status, raw_sort)
                    return self.respond(page)
                if method == 'POST':
                    value = service.create_run(body['project_id'], body['task'], self.idem(),
                                               body.get('parent_id'), body.get('expected_config_hash'), body.get('execution_options'))
                    return self.respond(value, 202)
            if len(parts) == 3 and parts[:2] == ['api', 'commands'] and method == 'GET':
                return self.respond(store.command(parts[2]))
            if len(parts) >= 4 and parts[:2] == ['api', 'runs']:
                key, action = parts[2:4]
                run = store.run(key)
                if action == 'snapshot' and method == 'GET':
                    return self.respond(service.snapshot(key))
                if action == 'usage' and method == 'GET':
                    return self.respond(insights.run_summary(service, key))
                if action == 'events' and method == 'GET':
                    service.sync_run_events(run)
                    if 'before' in query:
                        events = store.history(key, int(query['before'][0]), int(query.get('limit', ['100'])[0]))
                    else:
                        events = store.events(key, int(query.get('after', ['0'])[0]), int(query.get('limit', ['100'])[0]))
                    return self.respond({'events': events, 'last_cursor': store.last_cursor(key)})
                if action == 'stream' and method == 'GET':
                    return self.stream(run, query)
                if action == 'messages' and method == 'POST':
                    return self.respond(store.message(key, body['body'], self.idem()), 201)
                if action == 'commands' and method == 'POST':
                    return self.respond(service.command(key, body['intent'], body.get('payload', {}), self.idem()), 202)
                if action == 'discussions':
                    if method == 'GET':
                        return self.respond({'threads': store.threads(key)})
                    if method == 'POST':
                        return self.respond(service.discuss(key, body['question'], body.get('roles', ['developer', 'reviewer']),
                                                            body.get('budget', 6000), self.idem()), 202)
                if action == 'artifacts' and method == 'GET' and len(parts) == 7:
                    return self.respond(read_artifact(run, '/'.join(parts[4:])))
                if action == 'diff' and method == 'GET' and len(parts) == 5:
                    from ..collaboration.projection import code_diff
                    return self.respond(code_diff(run, parts[4], query.get('mode', ['previous'])[0]))
            return self.respond({'error': '接口不存在'}, 404)
        except Conflict as error:
            return self.respond({'error': str(error)}, 409)
        except KeyError as error:
            return self.respond({'error': str(error)}, 404)
        except (ValueError, TypeError) as error:
            return self.respond({'error': str(error)}, 400)
        except (OSError, RuntimeError) as error:
            return self.respond({'error': str(error)}, 503)

    def idem(self):
        value = self.headers.get('Idempotency-Key', '')
        if not 8 <= len(value) <= 160:
            raise ValueError('缺少合法幂等标识')
        return value

    def stream(self, run, query):
        if not self.app.streams.acquire(blocking=False):
            return self.respond({'error': '实时连接数量已到上限'}, 429)
        try:
            cursor = max(0, int(self.headers.get('Last-Event-ID') or query.get('after', ['0'])[0]))
            self.send_response(200)
            self.headers_common()
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()
            self.wfile.write(b'retry: 1500\n\n')
            self.wfile.flush()
            last_heartbeat = time.monotonic()
            while not self.app.closed.is_set():
                self.app.service.sync_run_events(run)
                events = self.app.service.store.events(run['id'], cursor, 100)
                for event in self.app.service.redact(events) if events else ():
                    packet = 'id: ' + str(event['cursor']) + '\nevent: update\ndata: ' + json.dumps(
                        event, ensure_ascii=False) + '\n\n'
                    self.wfile.write(packet.encode('utf8'))
                    cursor = event['cursor']
                if events or time.monotonic() - last_heartbeat > 12:
                    if not events:
                        self.wfile.write(b': heartbeat\n\n')
                    self.wfile.flush()
                    last_heartbeat = time.monotonic()
                self.app.closed.wait(.5)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.close_connection = True
            self.app.streams.release()

    def do_GET(self):
        self.handle_request('GET')

    def do_POST(self):
        self.handle_request('POST')


def create_server(database, configs, port=8765, *, recover=True):
    app = Application(database, configs, recover=recover)
    server = WorkbenchServer(('127.0.0.1', port), Handler)
    server.application = app
    app.origin = f'http://127.0.0.1:{server.server_port}'
    app.start_projection()
    return server


def serve(database, configs, port=8765, *, open_browser=False):
    server = create_server(database, configs, port)
    app = server.application
    url = f'{app.origin}/#token={app.bootstrap}'
    print(f'工作台：{url}', flush=True)
    print('仅本机单用户。关闭页面不停止已启动的独立任务进程。', flush=True)
    if open_browser:
        import webbrowser
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        server.serve_forever()
    finally:
        app.closed.set()
        server.server_close()
