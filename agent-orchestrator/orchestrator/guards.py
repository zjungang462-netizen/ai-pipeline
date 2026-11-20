"""越界隔离与持久化失败守卫。"""
from __future__ import annotations

from .safety import WriteRootGuard
from .state import RunState, atomic_write_json
from .config import Config

from .contracts import StageSpec
from pathlib import Path
import uuid


class GuardControls:
    """显式依赖配置和当前运行状态的写入约束。"""

    def __init__(self, config: Config, state: RunState):
        self.config = config
        self.state = state

    def record_outside_evidence(self, spec: StageSpec, before: dict,
                                 after: dict, outside: list[str]) -> str | None:
        """留存有界快照元数据；不再读写无法归属的根外文件。"""
        target = self.state.run_dir / "violations" / spec.name / f"{uuid.uuid4().hex}.json"
        changes = [{'path': path, 'before': before.get(path), 'after': after.get(path)}
                   for path in outside[:100]]
        try:
            atomic_write_json(target, {'source_files_preserved': True,
                                      'observed_count': len(outside),
                                      'truncated': len(outside) > len(changes),
                                      'changes': changes})
        except OSError as error:
            self.state.log_event('watch_evidence_error', spec.name, reason=str(error))
            return None
        return target.relative_to(self.state.run_dir).as_posix()

    def write_violation_stands(self, spec: StageSpec, inputs_hash: str) -> str:
        """同一输入上已记录的根外写入 => 保持失败，不再调用模型。"""
        violation = self.state.control.get('write_violations', {}).get(spec.name)
        if isinstance(violation, dict) and violation.get('inputs_hash') == inputs_hash:
            return ('同一输入上已发生可写根外变更，续跑拒绝重试；'
                    '修正根外文件后可用 rerun-from 显式覆盖: '
                    + str(violation.get('files', [])[:5]))
        return ''

    def make_write_guard(self, spec: StageSpec):
        """显式配置有限监视域；可写根内部的域禁用并留事件。"""
        roots = [self.config.task_workdir, self.state.run_dir]
        wr_cfg = (self.config.watch_root or "").strip()
        if wr_cfg.lower() == "off":
            watch = list(roots)
        elif wr_cfg:
            wr = Path(wr_cfg)
            watch = [wr if wr.is_absolute() else self.config.base_dir / wr]
        else:
            watch = None
        if watch:
            guard_roots = [Path(r).resolve() for r in roots]
            usable = []
            for candidate in watch:
                resolved = Path(candidate).resolve()
                if any(resolved == r or r in resolved.parents for r in guard_roots):
                    self.state.log_event("watch_disabled", spec.name,
                                         reason="watch_root 位于可写根内部，无法捕获根外写入",
                                         path=str(resolved))
                    self.state.save()
                    continue
                usable.append(resolved)
            watch = usable or None
        guard = WriteRootGuard(roots, watch_roots=watch)
        return guard
