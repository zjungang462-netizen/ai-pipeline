"""适配器工厂：按配置 type 实例化适配器。"""
from __future__ import annotations

from ..config import AdapterConfig
from .base import Adapter
from .fake import FakeAdapter
from .openai_compat import OpenAICompatAdapter


def build_adapter(cfg: AdapterConfig, kind: str = "report") -> Adapter:
    t = cfg.type
    o = cfg.options
    if t == "fake":
        return FakeAdapter(name=cfg.name, kind=o.get("kind"))
    if t == "openai_compat":
        return OpenAICompatAdapter(
            name=cfg.name, base_url=o["base_url"], model=o["model"],
            api_key_env=o["api_key_env"], kind=kind,
            temperature=float(o.get("temperature", 0.2)),
            max_tokens=o.get("max_tokens"),
            timeout_s=int(o.get("timeout_s", 120)),
            reasoning_effort=o.get('reasoning_effort'), thinking=o.get('thinking'),
            review_json_mode=o.get('review_json_mode', False),
        )
    # CLI 适配器在第 3 阶段接入
    if t.startswith("cli_"):
        from . import cli_registry
        return cli_registry.build(cfg, kind)
    raise ValueError(f"未知适配器类型: {t}")


def build_all(adapters: dict[str, AdapterConfig],
              stage_kind: dict[str, str]) -> dict[str, Adapter]:
    """为每个适配器名构建实例；kind 取其主要使用阶段的类型。"""
    out: dict[str, Adapter] = {}
    for name, cfg in adapters.items():
        kind = stage_kind.get(name, "report")
        out[name] = build_adapter(cfg, kind)
    return out
