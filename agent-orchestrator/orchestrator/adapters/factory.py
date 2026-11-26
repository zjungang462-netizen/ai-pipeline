"""适配器工厂：按配置 type 实例化适配器。"""
from __future__ import annotations

from ..config import AdapterConfig
from .base import Adapter
from . import registry
from .spec import validate_options


def build_adapter(cfg: AdapterConfig, kind: str = "report") -> Adapter:
    t = cfg.type
    spec = registry.get(t)
    if spec is not None:
        # 按声明校验并填充默认值；未知键只在 parse_config/doctor 中告警，这里忽略。
        options, issues = validate_options(spec, cfg.name, cfg.options)
        errors = [i for i in issues if i.level == "error"]
        if errors:
            raise ValueError("；".join(str(i) for i in errors))
        if spec.kind_aware:
            options["kind"] = kind
        return spec.factory(name=cfg.name, **options)
    raise ValueError(f"未知适配器类型: {t}")


def build_all(adapters: dict[str, AdapterConfig],
              stage_kind: dict[str, str]) -> dict[str, Adapter]:
    """为每个适配器名构建实例；kind 取其主要使用阶段的类型。"""
    out: dict[str, Adapter] = {}
    for name, cfg in adapters.items():
        kind = stage_kind.get(name, "report")
        out[name] = build_adapter(cfg, kind)
    return out
