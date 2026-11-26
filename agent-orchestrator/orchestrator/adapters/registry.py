"""适配器声明注册表。模块清单显式列出，便于审计；不使用 entry points。

每个模块在末尾导出 SPEC。新增适配器：新建模块、导出 SPEC、在此登记一行，
并把类型加入 config.KNOWN_ADAPTER_TYPES（契约测试校验两者一致）。
"""
from __future__ import annotations

import importlib
from functools import lru_cache
from types import MappingProxyType
from typing import Mapping

from .spec import AdapterSpec

_SPEC_MODULES = ('fake', 'openai_compat', 'cli_codex', 'cli_qwen', 'cli_dsh', 'cli_agy',
                 'cli_qoder', 'cli_doubao', 'cli_claude', 'cli_grok')


@lru_cache(maxsize=1)
def specs() -> Mapping[str, AdapterSpec]:
    found: dict[str, AdapterSpec] = {}
    for mod in _SPEC_MODULES:
        spec = getattr(importlib.import_module(f'{__package__}.{mod}'), 'SPEC')
        if not isinstance(spec, AdapterSpec):
            raise TypeError(f'{mod}.SPEC 必须是 AdapterSpec')
        if spec.type in found:
            raise ValueError(f'适配器类型重复声明: {spec.type}')
        found[spec.type] = spec
    return MappingProxyType(found)


def get(type_: str) -> AdapterSpec | None:
    return specs().get(type_)
