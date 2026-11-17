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
from .cli_claude import ClaudeAdapter

_REGISTRY = {
    "cli_codex": CodexAdapter,
    "cli_qwen": QwenAdapter,
    "cli_dsh": DshAdapter,
    "cli_agy": AgyAdapter,
    "cli_qoder": QoderAdapter,
    "cli_doubao": DoubaoAdapter,
    "cli_claude": ClaudeAdapter,
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
    if cls is QoderAdapter:
        kwargs.update(model=o.get('model'),reasoning_effort=o.get('reasoning_effort'),thinking=o.get('thinking'))
    if cls is ClaudeAdapter:
        kwargs.update(model=o.get('model'),reasoning_effort=o.get('reasoning_effort'),inherit_connection=o.get('inherit_connection',True))
    if cls is CodexAdapter:
        kwargs.update(model=o.get('model'), isolate_config=o.get('isolate_config', False),
                      reasoning_effort=o.get('reasoning_effort'))
    if cls is AgyAdapter:
        kwargs.update(model=o.get('model'), proxy_url=o.get('proxy_url'))
    if cls is DshAdapter:
        kwargs.update(permission_mode=o.get('permission_mode','read-only'),proxy_url=o.get('proxy_url'))
    return cls(**kwargs)
