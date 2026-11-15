"""适配器抽象基类。引擎只依赖此契约。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..contracts import PreflightReport, Result, RunContext


class Adapter(ABC):
    name: str = "base"
    channel: str = "base"

    @abstractmethod
    def preflight(self) -> PreflightReport:
        """探测可用性/版本/登录态/Key，返回报告（引擎落盘）。"""

    @abstractmethod
    def run(self, prompt: str, workdir: Path, ctx: RunContext) -> Result:
        """执行任务。解析失败须置 error_kind=parse_failed 且 parse_unknown=True。"""
