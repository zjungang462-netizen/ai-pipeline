"""时间排序必须覆盖全历史，游标不能跨方向复用。"""
import base64
import http.client
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from orchestrator.dashboard.server import create_server
from orchestrator.state import atomic_write_json


class TimeSort(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        cfg = self.root / 'config.toml'
        cfg.write_text('task_workdir="ws"\nstate_dir="state"\n[[stages]]\n'
                       'name="verify"\nkind="test"\ntest_cmd=["python","-c","print(1)"]\n', encoding='utf8')
        self.server = create_server(self.root / 'db.sqlite3', [cfg], 0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        def close():
            self.server.shutdown()
            self.server.server_close()
            thread.join(5)
        self.addCleanup(close)
        service = self.server.application.service
        project = service.store.projects()[0]['id']
        self.rows = []
        with service.store.session():
            for n in range(231):
                run = f'run-{n:04d}'
                key = service.store.register_run(project, run, cfg, self.root / 'state')
                stamp = 1700000000 + n // 3
                with service.store.connect() as db:
                    db.execute('UPDATE runs SET created=? WHERE id=?', (stamp, key))
                atomic_write_json(self.root / 'state/runs' / run / 'state.json', {
                    'task': f'历史 needle {n}', 'stages': {'verify': {'status': 'done'}}, 'events': []})
                self.rows.append((stamp, key))
        self.cookie = ''
        status, _, headers = self.request('/api/session', {'token': self.server.application.bootstrap})
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]

    def request(self, path, body=None):
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=20)
        headers = {'Cookie': self.cookie}
        if body is not None:
            headers.update({'Origin': self.server.application.origin, 'Content-Type': 'application/json'})
        try:
            client.request('POST' if body is not None else 'GET', path,
                           json.dumps(body) if body is not None else None, headers)
            response = client.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            client.close()

    def page(self, **query):
        status, page, _ = self.request('/api/runs?' + urlencode(query))
        self.assertEqual(status, 200, page)
        return page

    def test_both_directions_cover_all_history_with_equal_timestamps(self):
        for direction in ('newest', 'oldest'):
            with self.subTest(direction=direction):
                page = self.page(limit=19, sort=direction)
                found = []
                while True:
                    found.extend(r['id'] for r in page['runs'])
                    if not page['has_more']:
                        break
                    page = self.page(limit=19, cursor=page['next_cursor'])
                expected = [key for _, key in sorted(self.rows, reverse=direction == 'newest')]
                self.assertEqual(found, expected)
                self.assertEqual(len(set(found)), len(found))

    def test_filtered_ascending_continues_with_bound_before_and_sort(self):
        boundary = 1700000030
        page = self.page(limit=7, q='NEEDLE', status='done', sort='oldest', before=boundary)
        found = []
        while True:
            found.extend(r['id'] for r in page['runs'])
            if not page['has_more']:
                break
            page = self.page(limit=7, cursor=page['next_cursor'])
        self.assertEqual(found, [key for stamp, key in sorted(self.rows) if stamp < boundary])

    def test_cursor_rejects_other_direction_and_invalid_sort(self):
        page = self.page(limit=7, sort='oldest')
        for query in ({'cursor': page['next_cursor'], 'sort': 'newest'}, {'sort': ''}, {'sort': 'ASC;DROP TABLE runs'}):
            status, _, _ = self.request('/api/runs?' + urlencode(query))
            self.assertEqual(status, 400)
        status, _, _ = self.request('/api/runs?sort=newest&sort=oldest')
        self.assertEqual(status, 400)

    def test_old_cursor_versions_keep_original_descending_semantics(self):
        page = self.page(limit=7)
        payload = json.loads(base64.urlsafe_b64decode(page['next_cursor']))
        for version in (1, 2):
            old = dict(payload, v=version)
            old.pop('sort')
            if version == 1:
                for key in ('qh', 'pi', 'st'):
                    old.pop(key)
            token = base64.urlsafe_b64encode(json.dumps(old).encode()).decode()
            continuation = self.page(limit=7, cursor=token)
            self.assertEqual([r['id'] for r in continuation['runs']],
                             [key for _, key in sorted(self.rows, reverse=True)][7:14])

    def test_refresh_in_ascending_order_preserves_boundary_after_new_task(self):
        first = self.page(limit=7, sort='oldest')
        newest = max(self.rows)
        self.assertNotEqual(first['runs'][0]['id'], newest[1])
        repeated = self.page(limit=7, sort='oldest')
        self.assertEqual(first, repeated)
        self.assertEqual(self.page(limit=7, cursor=first['next_cursor'], sort='oldest')['runs'][0]['id'],
                         sorted(self.rows)[7][1])


class TimeSortFrontend(unittest.TestCase):
    def test_sort_and_layout_interactions(self):
        node = shutil.which('node')
        self.assertIsNotNone(node)
        result = subprocess.run([node, str(Path(__file__).parent / 'js/time_sort.cjs')],
                                capture_output=True, text=True, encoding='utf8', timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('TIME_SORT_UI_OK', result.stdout)


if __name__ == '__main__':
    unittest.main()
