"""CLI 适配器注册表：按 type 分发到具体工具适配器。"""
from __future__ import annotations

from ..config import AdapterConfig
from .base import Adapter
from .cli_codex import CodexAdapter
from .cli_qwen import QwenAdapter
from .cli_dsh import DshAdapter
from .cli_agy import AgyAdapter
from .cli_qoder import QoderAdapter
from .cli_doubao import DoubaoAdapter

_REGISTRY = {
    "cli_codex": CodexAdapter,
    "cli_qwen": QwenAdapter,
    "cli_dsh": DshAdapter,
    "cli_agy": AgyAdapter,
    "cli_qoder": QoderAdapter,
    "cli_doubao": DoubaoAdapter,
}


def build(cfg: AdapterConfig, kind: str = "report") -> Adapter:
    cls = _REGISTRY.get(cfg.type)
    if cls is None:
        raise ValueError(f"未知 CLI 适配器类型: {cfg.type}")
    o = cfg.options
    kwargs = {
        "name": cfg.name,
        "exe": o.get("exe") or None,
        "extra_env": o.get("extra_env") or [],
        "timeout_s": int(o.get("timeout_s", 300)),
    }
    if cls is QwenAdapter and o.get("model"):
        kwargs["model"] = o["model"]
    return cls(**kwargs)
