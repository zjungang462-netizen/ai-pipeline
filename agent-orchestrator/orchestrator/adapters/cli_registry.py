"""兼容入口：CLI 适配器已改由各模块的 SPEC 声明并经 factory 构建。

保留 build() 供 scripts/tool_inventory.py 与既有测试调用；新代码请使用
orchestrator.adapters.factory.build_adapter。
"""
from __future__ import annotations

from ..config import AdapterConfig
from .base import Adapter


def build(cfg: AdapterConfig, kind: str = "report") -> Adapter:
    if not cfg.type.startswith("cli_"):
        raise ValueError(f"未知 CLI 适配器类型: {cfg.type}")
    from .factory import build_adapter
    return build_adapter(cfg, kind)
