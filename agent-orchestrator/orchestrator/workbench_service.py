"""工作台进程守护：仅恢复网页服务，自动任务恢复始终关闭。"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .state import StageLock, atomic_write_json


def health(port, pid):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{port}/api/health', timeout=2) as response:
            data = json.load(response)
        return data.get('status') == 'ok' and data.get('pid') == pid
    except (OSError, ValueError):
        return False


def port_busy(port):
    with socket.socket() as client:
        client.settimeout(.3)
        return client.connect_ex(('127.0.0.1', port)) == 0


def recovery_blockers(database):
    """只读检查；不实例化可能执行恢复的 Service。"""
    import sqlite3
    from .collaboration.projection import lifecycle
    path = Path(database).resolve()
    if not path.exists():
        return []
    blocked = []
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        for table, statuses in [('commands', ('queued',)), ('threads', ('queued', 'running'))]:
            query = ','.join('?' for _ in statuses)
            if db.execute(f'SELECT 1 FROM {table} WHERE status IN ({query}) LIMIT 1', statuses).fetchone():
                blocked.append(table)
        for row in db.execute('SELECT * FROM runs WHERE managed=1'):
            root = Path(row['state_dir']) / 'runs' / row['run_id']
            def read(name):
                p = root / name
                value = json.loads(p.read_text(encoding='utf8')) if p.exists() else {}
                if not isinstance(value, dict):
                    raise ValueError('运行状态形状无效')
                return value
            if lifecycle(read('state.json'), read('job.json')) in ('pending', 'running', 'recovering'):
                blocked.append(row['id'])
    return blocked


def serve_child(args):
    from .dashboard.server import create_server
    server = create_server(args.database, args.config, args.port, recover=False)
    app = server.application
    entry = Path(args.directory) / '工作台入口.md'
    entry.write_text('# 本机工作台\n\n本机私密入口，请勿转发。\n\n'
                     f'[打开工作台]({app.origin}/#token={app.bootstrap})\n', encoding='utf8')
    print('工作台已启动；会话入口仅保存在私密本地文件。', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def supervise(args):
    root = Path(args.directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock = StageLock(root / '.service.lock')
    if not lock.acquire():
        raise RuntimeError('已有工作台守护进程持锁')
    child = None
    receipt = {'supervisor_pid': os.getpid(), 'port': args.port, 'database': str(Path(args.database).resolve()),
               'source_root': str(Path(__file__).resolve().parents[1]), 'automatic_task_recovery': False,
               'started_at': time.time(), 'restarts_used': 0}
    def record(status, **extra):
        receipt.update(status=status, updated_at=time.time(), **extra)
        atomic_write_json(root / 'service.json', receipt)
    def event(kind, **extra):
        with (root / 'events.jsonl').open('a', encoding='utf8') as output:
            output.write(json.dumps(dict(at=time.time(), kind=kind, **extra)) + '\n')
    try:
        # stop 文件保持为显式停服意图，重新启动前需主动移除。
        if (root / 'stop').exists():
            record('stopped', reason='stop_requested')
            return 0
        for attempt in range(args.max_restarts + 1):
            if port_busy(args.port):
                record('blocked', reason='port_in_use')
                return 1
            blocked = recovery_blockers(args.database)
            if blocked:
                record('blocked', reason='active_work', blockers=blocked)
                return 1
            log = root / f'child-{attempt}.log'
            error = root / f'child-{attempt}-error.log'
            command = [sys.executable, '-u', '-X', 'utf8', '-m', 'orchestrator.workbench_service', 'child',
                       '--database', str(Path(args.database).resolve()), '--directory', str(root),
                       '--port', str(args.port)]
            for config in args.config:
                command += ['--config', str(Path(config).resolve())]
            record('starting', restarts_used=attempt, stdout=str(log), stderr=str(error))
            with log.open('ab') as stdout, error.open('ab') as stderr:
                child = subprocess.Popen(command, cwd=receipt['source_root'], stdout=stdout, stderr=stderr)
                record('starting', pid=child.pid)
                event('child_started', pid=child.pid, restart=attempt)
                startup = time.monotonic() + args.startup_timeout
                failures = 0
                while child.poll() is None:
                    if (root / 'stop').exists():
                        child.terminate()
                        child.wait(timeout=10)
                        record('stopped', reason='stop_requested')
                        return 0
                    if health(args.port, child.pid):
                        failures = 0
                        record('healthy')
                    else:
                        failures += 1
                        if time.monotonic() > startup and failures >= 3:
                            # 不强杀仍可能处理请求的进程，也不在未知状态启动第二份。
                            record('unhealthy', reason='health_failed')
                    time.sleep(args.interval)
                code = child.returncode
                event('child_exited', pid=child.pid, exit_code=code)
                record('exited', exit_code=code)
            if code == 0:
                record('stopped', reason='normal_exit')
                return 0
            if attempt < args.max_restarts:
                time.sleep(min(2 ** attempt, 10))
        record('failed', reason='restart_exhausted')
        return 1
    except (OSError, ValueError, RuntimeError) as error:
        event('supervisor_error', error_type=type(error).__name__)
        record('failed', reason=type(error).__name__)
        return 1
    finally:
        # 守护器正常退出时只终止自己创建的网页进程，不触碰独立任务进程。
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                event('child_stop_timeout', pid=child.pid)
        lock.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('run', 'child'))
    parser.add_argument('--database', required=True)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--config', action='append', default=[])
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--max-restarts', type=int, default=3)
    parser.add_argument('--interval', type=float, default=5)
    parser.add_argument('--startup-timeout', type=float, default=30)
    args = parser.parse_args(argv)
    import math
    if not 1 <= args.port <= 65535 or args.port == 8765:
        parser.error('端口须为 1..65535，8765 保留给本机代理')
    if not 0 <= args.max_restarts <= 5 or not math.isfinite(args.interval) or not .1 <= args.interval <= 60:
        parser.error('重启上限 0..5，检查间隔 .1..60 秒')
    if not math.isfinite(args.startup_timeout) or not 1 <= args.startup_timeout <= 300:
        parser.error('启动观察时间须为 1..300 秒')
    return serve_child(args) if args.mode == 'child' else supervise(args)


if __name__ == '__main__':
    raise SystemExit(main())
