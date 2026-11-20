"""工作台轮询的有界读缓存；只保存展示数据。"""
from __future__ import annotations

import json
import time
from collections import OrderedDict
from threading import RLock


def file_stamp(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


class ReadCache:
    def __init__(self, limit):
        self.limit = limit
        self.rows = OrderedDict()
        self.lock = RLock()
        self.clock = time.monotonic

    def lookup(self, key, version):
        with self.lock:
            saved = self.rows.get(key)
            if saved is not None and saved[0] == version and self.clock() < saved[1]:
                self.rows.move_to_end(key)
                return saved[2]
        return None

    def remember(self, key, version, value, *, ttl=float('inf')):
        with self.lock:
            self.rows[key] = (version, self.clock() + ttl, value)
            self.rows.move_to_end(key)
            while len(self.rows) > self.limit:
                self.rows.popitem(last=False)

    def read_json(self, path, *, max_bytes=None):
        version = file_stamp(path)
        if max_bytes is not None and version[2] > max_bytes:
            raise ValueError('展示文件超过读取上限')
        cached = self.lookup(path, version)
        if cached is not None:
            return cached
        with path.open('rb') as stream:
            raw = stream.read() if max_bytes is None else stream.read(max_bytes + 1)
        if max_bytes is not None and len(raw) > max_bytes:
            raise ValueError('展示文件超过读取上限')
        value = json.loads(raw.decode('utf8'))
        if isinstance(value, dict):
            try:
                if file_stamp(path) == version:
                    self.remember(path, version, value)
            except FileNotFoundError:
                pass
        return value
