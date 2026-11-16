"""越界隔离与持久化失败守卫。"""
from __future__ import annotations

from .safety import WriteRootGuard

from .contracts import ErrorKind, Result, RunContext, StageSpec, StageStatus
from pathlib import Path
import shutil
import uuid


class GuardControls:
    def _quarantine_new_outside(self, spec: StageSpec, before: dict,
                                outside: list[str]) -> list[str]:
        """把本次新写出的根外文件移入运行目录隔离区（可恢复，不直接删除）。

        改写过的既有根外文件留在原地并由持久化越界记录阻止续跑重试。
        返回实际移动的原始路径列表。
        """
        target = self.state.run_dir / "violations" / spec.name
        moved: list[str] = []
        for path in outside:
            if path in before:
                continue
            source = Path(path)
            try:
                if not source.is_file():
                    continue
                target.mkdir(parents=True, exist_ok=True)
                destination = target / f"{uuid.uuid4().hex[:8]}-{source.name}"
                shutil.move(str(source), str(destination))
                moved.append(path)
            except OSError:
                continue
        return moved

    def _write_violation_stands(self, spec: StageSpec, inputs_hash: str) -> str:
        """同一输入上已记录的根外写入 => 保持失败，不再调用模型。"""
        violation = self.state.control.get('write_violations', {}).get(spec.name)
        if isinstance(violation, dict) and violation.get('inputs_hash') == inputs_hash:
            return ('同一输入上已发生可写根外变更，续跑拒绝重试；'
                    '修正根外文件后可用 rerun-from 显式覆盖: '
                    + str(violation.get('files', [])[:5]))
        return ''

    def _make_write_guard(self, spec: StageSpec):
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
