"""停服后备份工作台数据库和已登记状态根，保留逐文件校验依据。"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator.state import atomic_write_json, sha256_file
from orchestrator.state_backup import backup_state
from orchestrator.workbench_service import recovery_blockers, port_busy


def snapshot(database, output, port=8766):
    database, output = Path(database).resolve(), Path(output).absolute()
    if port_busy(port):
        raise ValueError('请先停止工作台进程，再创建升级快照')
    if recovery_blockers(database):
        raise ValueError('存在活动任务、命令或会诊，不能创建升级快照')
    if output.exists():
        raise ValueError('快照输出必须是全新目录')
    output.mkdir(parents=True)
    uri = database.as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as source:
        source.row_factory = sqlite3.Row
        projects = [dict(row) for row in source.execute('SELECT * FROM projects')]
        states = [row[0] for row in source.execute('SELECT DISTINCT state_dir FROM runs')]
        runs = source.execute('SELECT COUNT(*) FROM runs').fetchone()[0]
        with closing(sqlite3.connect(output / 'collaboration.sqlite3')) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('数据库备份完整性检查失败')
    records = []
    for index, root in enumerate(states):
        report = backup_state(root, output / ('state-' + str(index)))
        records.append(dict(state_dir=root, **report))
    if port_busy(port) or recovery_blockers(database):
        raise ValueError('快照期间服务或任务被重新启动，请停止后复核')
    proof = dict(database=str(database), database_sha256=sha256_file(output / 'collaboration.sqlite3'),
                 projects=projects, runs=runs, states=records, model_calls=0,
                 scope='database_and_state_only; external_task_workspaces_not_included')
    atomic_write_json(output / 'snapshot.json', proof)
    return dict(output=str(output), projects=len(projects), runs=runs, states=len(records), model_calls=0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args(argv)
    print(json.dumps(snapshot(args.database, args.output, args.port), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
