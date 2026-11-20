"""按状态快照中的字节位置读取已提交的运行事件。"""
from __future__ import annotations

import json
import os
from pathlib import Path


def read_events(run_dir: Path, snapshot: dict, *, max_bytes: int | None = None) -> list[dict]:
    """兼容旧快照的内嵌事件。新快照只读取已提交的日志前缀。"""
    if snapshot.get('schema_version') != 3:
        events = snapshot.get('events', [])
        if not isinstance(events, list):
            raise ValueError('state.json events 字段必须是数组')
        return events
    offset = snapshot.get('events_bytes')
    if type(offset) is not int or offset < 0 or 'events' in snapshot:
        raise ValueError('state.json 事件日志位置无效')
    if not offset:
        return []
    if max_bytes is not None and offset > max_bytes:
        raise ValueError('已提交事件超过诊断读取上限')
    path = _journal_path(run_dir)
    events = []
    with path.open('rb') as stream:
        if os.fstat(stream.fileno()).st_size < offset:
            raise ValueError('事件日志短于快照记录的位置')
        remaining = offset
        while remaining:
            line = stream.readline(remaining)
            if not line.endswith(b'\n'):
                raise ValueError('事件日志的已提交记录不完整')
            event = json.loads(line.decode('utf-8'))
            if not isinstance(event, dict):
                raise ValueError('事件日志记录必须是对象')
            events.append(event)
            remaining -= len(line)
    return events


def append_events(run_dir: Path, offset: int, events: list[dict]) -> int:
    """单写者持锁调用。替换未提交尾部并同步本次追加，之后才能发布快照。"""
    if not events:
        return offset
    data = b''.join(
        (json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
        for event in events
    )
    path = _journal_path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    with path.open('r+b' if offset or path.exists() else 'xb') as stream:
        size = stream.seek(0, os.SEEK_END)
        if size < offset:
            raise ValueError('事件日志短于快照记录的位置')
        if size > offset:
            stream.truncate(offset)
        stream.seek(offset)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return offset + len(data)


def _journal_path(run_dir: Path) -> Path:
    from .maintenance import boundary, linked

    path = run_dir / 'events.jsonl'
    if path.is_symlink() or (path.exists() and (linked(path) or not path.is_file())):
        raise ValueError('事件日志必须是普通文件')
    boundary(path, run_dir)
    return path
