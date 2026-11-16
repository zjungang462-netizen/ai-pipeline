"""服务关闭须等待正在投影的线程，避免 Windows 文件/SQLite 清理竞争。"""
import tempfile
import http.client
import threading
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.dashboard.server import Handler, create_server


class DashboardShutdown(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = self.root/'pipeline.toml'
        config.write_text('task_workdir="workspace"\nstate_dir="state"\n'
                          '[[stages]]\nname="test"\nkind="test"\n'
                          'test_cmd=["python","-c","print(1)"]\n', encoding='utf8')
        self.server = create_server(self.root/'state.sqlite3', [config], 0)
        self.addCleanup(self.server.server_close)

    def test_close_joins_projection_and_can_be_repeated(self):
        thread = self.server.application.projection_thread
        self.assertTrue(thread.is_alive())
        self.server.server_close()
        self.assertTrue(self.server.application.closed.is_set())
        self.assertTrue(self.server.application.service.closed.is_set())
        self.assertFalse(thread.is_alive())
        self.server.server_close()

    def test_close_waits_for_inflight_projection(self):
        entered = threading.Event()
        release = threading.Event()
        closing = threading.Event()
        finished = threading.Event()
        failures = []

        def sync(**kwargs):
            entered.set()
            release.wait(10)

        def close():
            closing.set()
            try:
                self.server.server_close()
            except Exception as error:
                failures.append(error)
            finally:
                finished.set()

        with mock.patch.object(self.server.application.service, 'sync', side_effect=sync):
            self.assertTrue(entered.wait(5))
            worker = threading.Thread(target=close)
            worker.start()
            try:
                self.assertTrue(closing.wait(5))
                self.assertFalse(finished.wait(.1), '不能在投影仍访问磁盘时提前返回')
            finally:
                release.set()
                worker.join(12)
        self.assertFalse(worker.is_alive())
        self.assertFalse(failures)
        self.assertFalse(self.server.application.projection_thread.is_alive())

    def test_sync_after_close_does_not_access_run_directories(self):
        service = self.server.application.service
        self.server.server_close()
        with mock.patch.object(service, 'config', side_effect=AssertionError('关闭后不得继续扫描项目')):
            service.sync(recover=True)

    def test_close_waits_after_response_until_request_session_exits(self):
        responded = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        errors = []
        original = Handler.handle_session_request

        def delayed(handler, method):
            value = original(handler, method)
            responded.set()
            release.wait(10)
            return value

        def close():
            try:
                self.server.server_close()
            except Exception as error:
                errors.append(error)
            finally:
                closed.set()

        serving = threading.Thread(target=self.server.serve_forever, daemon=True)
        serving.start()
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        worker = None
        try:
            with mock.patch.object(Handler, 'handle_session_request', delayed):
                connection.request('GET', '/api/session')
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 401)
                self.assertTrue(responded.wait(5))
                self.assertEqual(self.server.application.active_requests, 1)
                self.server.shutdown()
                serving.join(5)
                worker = threading.Thread(target=close)
                worker.start()
                self.assertFalse(closed.wait(.1), '响应已送达仍须等待数据库会话退出')
                release.set()
                worker.join(12)
        finally:
            release.set()
            connection.close()
            if worker is not None:worker.join(12)
            self.server.shutdown()
            serving.join(5)
        self.assertFalse(errors)
        self.assertTrue(closed.is_set())
        self.assertEqual(self.server.application.active_requests, 0)


if __name__ == '__main__':
    unittest.main()
