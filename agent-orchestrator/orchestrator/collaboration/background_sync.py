"""目录发现与数据库投影的分批轮巡，不以首页 100 条作为同步范围。"""
from __future__ import annotations

import os
from collections import OrderedDict
from pathlib import Path

from ..state import RunState
from .projection import _signature, directory, lifecycle, project, read_json


class BackgroundSync:
    BATCH = 50
    IMPORT_CACHE_LIMIT = 4096
    PROJECTION_CACHE_LIMIT = 512

    def __init__(self, service):
        self.service = service
        self.imports = OrderedDict()
        self.projected = OrderedDict()
        self.scans = {}
        self.project_position = 0
        self.run_after = None

    def close(self):
        for _, iterator in self.scans.values():
            iterator.close()
        self.scans.clear()

    def _import(self, entry, cfg, path, stats):
        if not (path / 'state.json').is_file():
            self.imports.pop((entry['id'], str(path)), None)
            return
        RunState(cfg.state_dir, path.name)  # 保留 run_id 校验，目录快照不作执行证据。
        key = (entry['id'], str(path))
        signature = (cfg.source_hash, _signature(path))
        if self.imports.get(key) == signature:
            self.imports.move_to_end(key)
            stats['imports_unchanged'] += 1
            return
        store = self.service.store
        identifier = store.register_run(entry['id'], path.name, entry['config'], cfg.state_dir)
        job = read_json(path / 'job.json')
        candidate = Path(job.get('config', entry['config'])).resolve()
        if candidate.parent == cfg.base_dir and candidate.suffix == '.toml' and candidate.is_file():
            pinned = self.service.config(candidate)
            if pinned.source_hash == job.get('config_hash'):
                store.pin_import_config(identifier, candidate)
        # 导入中发生状态提交时，不缓存不一致签名，下轮重新导入。
        if _signature(path) == signature[1]:
            self.imports[key] = signature
            self.imports.move_to_end(key)
            while len(self.imports) > self.IMPORT_CACHE_LIMIT:
                self.imports.popitem(last=False)
        stats['imports_updated'] += 1

    def _discover(self, entries, incremental, stats):
        valid = {entry['id'] for entry in entries}
        for key in list(self.scans):
            if key not in valid:
                self.scans.pop(key)[1].close()
        if not incremental:
            self.close()
            for entry in entries:
                if self.service.closed.is_set():return
                try:
                    cfg = self.service.config(entry['config'])
                except Exception:
                    # 单个登记配置不可读不能中止整轮；下一轮重试该配置。
                    stats['errors'] += 1
                    continue
                root = cfg.state_dir / 'runs'
                if not root.is_dir():continue
                with os.scandir(root) as scan:
                    for item in scan:
                        if self.service.closed.is_set():return
                        stats['directories_checked'] += 1
                        if item.is_dir(follow_symlinks=False):
                            try:self._import(entry, cfg, Path(item.path), stats)
                            except ValueError:stats['invalid_directories'] += 1
            return
        # 项目间逐条轮换。空目录每 tick 最多尝试一次，避免在空项目上空转。
        exhausted = set()
        configs = {}
        while entries and stats['directories_checked'] < self.BATCH and len(exhausted) < len(entries):
            if self.service.closed.is_set():return
            entry = entries[self.project_position % len(entries)]
            self.project_position += 1
            key = entry['id']
            if key in exhausted:continue
            if key not in configs:
                try:
                    configs[key] = self.service.config(entry['config'])
                except Exception:
                    # 该配置本轮不可用：计入错误并跳过，下一轮重新加载（不缓存失败）。
                    stats['errors'] += 1
                    exhausted.add(key)
                    continue
            cfg = configs[key]
            root = cfg.state_dir / 'runs'
            saved = self.scans.get(key)
            if saved and saved[0] != root:
                self.scans.pop(key)[1].close()
                saved = None
            if saved is None:
                try:saved = (root, os.scandir(root))
                except FileNotFoundError:
                    exhausted.add(key)
                    continue
                except OSError:
                    exhausted.add(key)
                    stats['errors'] += 1
                    continue
                self.scans[key] = saved
            try:item = next(saved[1])
            except StopIteration:
                self.scans.pop(key)[1].close()
                exhausted.add(key)
                continue
            except OSError:
                self.scans.pop(key)[1].close()
                exhausted.add(key)
                stats['errors'] += 1
                continue
            stats['directories_checked'] += 1
            if item.is_dir(follow_symlinks=False):
                try:self._import(entry, cfg, Path(item.path), stats)
                except ValueError:stats['invalid_directories'] += 1
                except OSError:stats['errors'] += 1

    def _project(self, run, recover, stats):
        store = self.service.store
        root = directory(run)
        key = (run['id'], str(root), run['config'])
        signature = _signature(root)
        saved = self.projected.get(key)
        queued = store.commands(run['id'])
        state = None
        if saved is not None and saved[0] == signature and not queued:
            status = saved[1]
            self.projected.move_to_end(key)
            stats['projections_unchanged'] += 1
        else:
            state, job = project(store, run, self.service._projection_cache)
            status = lifecycle(state, job)
            while len(self.service._projection_cache) > self.PROJECTION_CACHE_LIMIT:
                self.service._projection_cache.pop(next(iter(self.service._projection_cache)))
        if recover and run['managed'] and (
                status in ('running', 'pending', 'recovering') or queued):
            self.service.launch(run['id'])
        if recover:
            for topic in store.threads(run['id']):
                if topic['status'] in ('queued', 'running'):
                    self.service.launch_discussion(topic['id'])
        # 仅补仍在排队的收据，避免每轮对历史收据重复查询 finish。
        receipts = state.get('control', {}).get('receipts', {}) if state is not None else {}
        for command in queued:
            receipt = receipts.get(command['id'])
            if receipt is not None:
                store.finish(command['id'], receipt['status'], receipt['result'])
        if _signature(root) == signature:
            self.projected[key] = (signature, status)
            self.projected.move_to_end(key)
            while len(self.projected) > self.IMPORT_CACHE_LIMIT:
                self.projected.popitem(last=False)
        stats['runs_checked'] += 1

    def sync(self, recover=False, incremental=False):
        stats = dict(directories_checked=0, imports_updated=0, imports_unchanged=0,
                     invalid_directories=0, runs_checked=0, projections_unchanged=0, errors=0)
        if self.service.closed.is_set():
            self.close()
            return stats
        with self.service.store.session():
            self._discover(self.service.store.projects(), incremental, stats)
        after = self.run_after if incremental else None
        while not self.service.closed.is_set():
            with self.service.store.session():
                runs = self.service.store.sync_runs(after, self.BATCH)
                for run in runs:
                    if self.service.closed.is_set():break
                    if incremental:
                        # 任意单条失败都只计数并继续，绝不能让一条坏记录
                        # 或畸形状态文件卡死整轮后台同步（其它记录仍要推进）。
                        try:self._project(run, recover, stats)
                        except Exception:stats['errors'] += 1
                    else:
                        self._project(run, recover, stats)
            if incremental:
                self.run_after = runs[-1]['id'] if len(runs) == self.BATCH else None
                break
            if len(runs) < self.BATCH:break
            after = runs[-1]['id']
        return stats
