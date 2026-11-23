"""协作 SQLite 存储。阶段结果仍由原状态与产物决定。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Conflict(ValueError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def identifier(prefix):
    return prefix + '-' + uuid.uuid4().hex[:16]


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self._local = threading.local()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, config TEXT UNIQUE NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    config TEXT NOT NULL, state_dir TEXT NOT NULL, managed INTEGER NOT NULL DEFAULT 0,
                    parent_id TEXT, kind TEXT NOT NULL DEFAULT 'pipeline', created REAL NOT NULL,
                    UNIQUE(project_id,run_id));
                CREATE TABLE IF NOT EXISTS event_log (
                    cursor INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                    run_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS events_run ON event_log(run_id,cursor);
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, thread_id TEXT, source TEXT NOT NULL,
                    role TEXT NOT NULL, body TEXT NOT NULL, refs TEXT NOT NULL, created REAL NOT NULL,
                    idempotency TEXT, UNIQUE(run_id,idempotency));
                CREATE INDEX IF NOT EXISTS messages_run ON messages(run_id,created);
                CREATE TABLE IF NOT EXISTS commands (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    idempotency TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                    result TEXT, created REAL NOT NULL, UNIQUE(run_id,idempotency));
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, question TEXT NOT NULL, roles TEXT NOT NULL,
                    status TEXT NOT NULL, budget INTEGER NOT NULL, analysis_run TEXT,
                    result TEXT, snapshot TEXT NOT NULL, created REAL NOT NULL, idempotency TEXT NOT NULL,
                    UNIQUE(run_id,idempotency));
                CREATE TABLE IF NOT EXISTS projection_offsets (
                    run_id TEXT PRIMARY KEY, signature TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS task_templates (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL,
                    task TEXT NOT NULL, config_hash TEXT NOT NULL, UNIQUE(project_id,name));
                CREATE TABLE IF NOT EXISTS archived_runs (
                    run_id TEXT PRIMARY KEY, archived_at REAL NOT NULL);
            ''')

    @contextmanager
    def session(self):
        """一次查询批次复用连接，退出后关闭，避免 Windows 重复打开 WAL。"""
        if getattr(self._local, 'connection', None) is not None:
            yield
            return
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        self._local.connection = db
        try:
            yield
        finally:
            self._local.connection = None
            db.close()

    @contextmanager
    def connect(self):
        with self.session():
            db = self._local.connection
            with db:
                yield db

    def _event(self, db, event_id, run, kind, payload, created=None):
        db.execute('INSERT OR IGNORE INTO event_log(event_id,run_id,kind,payload,created) VALUES(?,?,?,?,?)',
                   (event_id, run, kind, encode(payload), created or time.time()))

    def event(self, event_id, run, kind, payload, created=None):
        with self.connect() as db:
            self._event(db, event_id, run, kind, payload, created)

    def register_project(self, config):
        config = str(Path(config).resolve())
        key = 'project-' + hashlib.sha256(config.encode()).hexdigest()[:12]
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO projects VALUES(?,?,?)', (key, Path(config).stem, config))
        return key

    def projects(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM projects ORDER BY name')]

    def project(self, key):
        with self.connect() as db:
            value = db.execute('SELECT * FROM projects WHERE id=?', (key,)).fetchone()
        if value is None:
            raise KeyError('项目未登记')
        return dict(value)

    def remove_project(self, key):
        with self.connect() as db:
            cur = db.execute('DELETE FROM projects WHERE id=?', (key,))
        if cur.rowcount == 0:
            raise KeyError('项目未登记')
        return True

    def register_run(self, project, run_id, config, state_dir, *, managed=False, parent=None, kind='pipeline'):
        key = project + ':' + run_id
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO runs VALUES(?,?,?,?,?,?,?,?,?)',
                       (key, project, run_id, str(Path(config).resolve()), str(Path(state_dir).resolve()),
                        int(managed), parent, kind, time.time()))
        return key

    def run(self, key):
        with self.connect() as db:
            row = db.execute('SELECT * FROM runs WHERE id=?', (key,)).fetchone()
        if row is None:
            raise KeyError('运行不存在')
        return dict(row)

    def set_run_archived(self, key, archived: bool):
        self.run(key)
        with self.connect() as db:
            if archived:
                db.execute('INSERT OR REPLACE INTO archived_runs(run_id,archived_at) VALUES(?,?)',
                           (key, time.time()))
            else:
                db.execute('DELETE FROM archived_runs WHERE run_id=?', (key,))
        return {'run_id': key, 'archived': bool(archived)}

    def is_run_archived(self, key) -> bool:
        with self.connect() as db:
            row = db.execute('SELECT 1 FROM archived_runs WHERE run_id=?', (key,)).fetchone()
        return row is not None

    def human_messages_since(self, run_id: str, since_ts: float, limit: int = 20) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT body, created FROM messages WHERE run_id=? AND source='human' AND created>? "
                "ORDER BY created DESC LIMIT ?",
                (run_id, float(since_ts), max(1, min(100, int(limit)))),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def project_runs(self, project_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute('SELECT * FROM runs WHERE project_id=? ORDER BY created DESC', (project_id,)).fetchall()
        return [dict(r) for r in rows]

    def pin_import_config(self, key, config):
        with self.connect() as db:
            db.execute('UPDATE runs SET config=? WHERE id=? AND managed=0', (str(Path(config).resolve()), key))

    def runs(self, limit=100, before=None):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM runs WHERE created<? ORDER BY created DESC LIMIT ?',
                              (before or time.time() + 1, min(100, max(1, limit)))).fetchall()
        return [dict(r) for r in rows]

    def sync_runs(self, after=None, limit=50):
        """内部同步轮巡按唯一主键分页，不受 UI 首页上限或时间戳并列影响。"""
        with self.connect() as db:
            if after is None:
                rows = db.execute('SELECT * FROM runs ORDER BY id LIMIT ?', (limit,)).fetchall()
            else:
                rows = db.execute('SELECT * FROM runs WHERE id>? ORDER BY id LIMIT ?', (after, limit)).fetchall()
        return [dict(row) for row in rows]

    def enqueue(self, run, kind, payload, idem):
        if not isinstance(idem, str) or not 8 <= len(idem) <= 160:
            raise ValueError('操作必须提供 8 到 160 字符的幂等标识')
        with self.connect() as db:
            old = db.execute('SELECT * FROM commands WHERE run_id=? AND idempotency=?', (run, idem)).fetchone()
            if old:
                if old['kind'] != kind or old['payload'] != encode(payload):
                    raise Conflict('同一幂等标识不能用于另一项操作')
                return self._command(old)
            key = identifier('cmd')
            db.execute('INSERT INTO commands(id,run_id,kind,payload,idempotency,created) VALUES(?,?,?,?,?,?)',
                       (key, run, kind, encode(payload), idem, time.time()))
            self._event(db, key + ':queued', run, 'command_queued', {'command_id': key, 'intent': kind})
            return {'id': key, 'run_id': run, 'kind': kind, 'payload': payload, 'status': 'queued', 'result': None}

    @staticmethod
    def _command(row):
        return {**dict(row), 'payload': json.loads(row['payload']),
                'result': json.loads(row['result']) if row['result'] else None}

    def commands(self, run, status='queued'):
        with self.connect() as db:
            return [self._command(r) for r in db.execute(
                'SELECT * FROM commands WHERE run_id=? AND status=? ORDER BY created,id', (run, status))]

    def command(self, key):
        with self.connect() as db:
            row = db.execute('SELECT * FROM commands WHERE id=?', (key,)).fetchone()
        if not row:
            raise KeyError('指令不存在')
        return self._command(row)

    def finish(self, key, status, result):
        with self.connect() as db:
            row = db.execute('SELECT * FROM commands WHERE id=?', (key,)).fetchone()
            if not row:
                raise KeyError('指令不存在')
            if row['status'] != 'queued':
                return
            db.execute('UPDATE commands SET status=?,result=? WHERE id=?', (status, encode(result), key))
            self._event(db, key + ':receipt', row['run_id'], 'command_receipt',
                        {'command_id': key, 'intent': row['kind'], 'status': status, 'result': result})

    def message(self, run, body, idem, *, source='human', role='你', refs=None, thread=None, key=None):
        if not isinstance(body, str) or not body.strip() or len(body) > 32000:
            raise ValueError('消息必须为 1 到 32000 字符')
        with self.connect() as db:
            old = db.execute('SELECT * FROM messages WHERE run_id=? AND idempotency=?', (run, idem)).fetchone()
            if old:
                if old['body'] != body or old['thread_id'] != thread:
                    raise Conflict('消息幂等标识已被另一条消息使用')
                return dict(old)
            key = key or identifier('msg')
            now = time.time()
            db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?,?)',
                       (key, run, thread, source, role, body, encode(refs or []), now, idem))
            self._event(db, key, run, 'message', {'message_id': key, 'source': source, 'role': role,
                                                'body': body, 'refs': refs or [], 'thread_id': thread}, now)
        return {'id': key, 'body': body, 'source': source, 'role': role, 'thread_id': thread}

    def events(self, run, after=0, limit=100):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM event_log WHERE run_id=? AND cursor>? ORDER BY cursor LIMIT ?',
                              (run, max(0, int(after)), min(500, max(1, limit)))).fetchall()
        return [{**dict(r), 'payload': json.loads(r['payload'])} for r in rows]

    def last_cursor(self, run):
        with self.connect() as db:
            return db.execute('SELECT COALESCE(MAX(cursor),0) FROM event_log WHERE run_id=?', (run,)).fetchone()[0]

    def history(self, run, before, limit=100):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM event_log WHERE run_id=? AND cursor<? ORDER BY cursor DESC LIMIT ?',
                              (run, before, min(200, max(1, limit)))).fetchall()
        return [{**dict(r), 'payload': json.loads(r['payload'])} for r in reversed(rows)]

    def threads(self, run):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM threads WHERE run_id=? ORDER BY created', (run,)).fetchall()
        return [self._thread(r) for r in rows]

    @staticmethod
    def _thread(row):
        data = dict(row)
        for key in ('roles', 'snapshot', 'result'):
            data[key] = json.loads(data[key]) if data[key] else None
        return data

    def thread(self, key):
        with self.connect() as db:
            row = db.execute('SELECT * FROM threads WHERE id=?', (key,)).fetchone()
        if not row:
            raise KeyError('议题不存在')
        return self._thread(row)

    def create_thread(self, run, question, roles, budget, snapshot, idem):
        with self.connect() as db:
            old = db.execute('SELECT * FROM threads WHERE run_id=? AND idempotency=?', (run, idem)).fetchone()
            if old:
                if old['question'] != question or old['roles'] != encode(roles):
                    raise Conflict('议题标识已使用')
                return self._thread(old)
            key = identifier('topic')
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (key, run, question, encode(roles), 'queued', budget, None, None,
                        encode(snapshot), time.time(), idem))
            self._event(db, key + ':queued', run, 'discussion_queued', {'thread_id': key, 'question': question})
        return self.thread(key)

    def update_thread(self, key, status, result=None, analysis_run=None):
        with self.connect() as db:
            row = db.execute('SELECT run_id FROM threads WHERE id=?', (key,)).fetchone()
            if not row:
                raise KeyError('议题不存在')
            db.execute('UPDATE threads SET status=?,result=?,analysis_run=COALESCE(?,analysis_run) WHERE id=?',
                       (status, encode(result) if result else None, analysis_run, key))
            self._event(db, key + ':' + status, row['run_id'], 'discussion_status',
                        {'thread_id': key, 'status': status, 'result': result})
