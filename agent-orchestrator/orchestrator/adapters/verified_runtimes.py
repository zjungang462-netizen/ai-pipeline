"""已验证容器 CLI 运行时的不可变版本、镜像摘要与二进制 SHA-256 清单。"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class VerifiedRuntime:
    channel: str
    version: str
    binary_sha256: str
    image: str
    short_version: str = ''
    notes: str = ''


_GROK_1_0_89 = VerifiedRuntime(
    channel='cli_grok',
    version='grok 1.0.89 (28be5bb1bf65)',
    short_version='1.0.89',
    binary_sha256='a3dface51d9bdc71fd121e0481dd02d9b46a721f7a6b892f756480e9666eba1f',
    image='python@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90',
    notes='Grok 1.0.89 Linux x86-64 原生程序与固定摘要 Python 镜像',
)

_DOUBAO_2_37_2 = VerifiedRuntime(
    channel='cli_doubao',
    version='doubao 2.37.2',
    short_version='2.37.2',
    binary_sha256='6120b2849c181466d1bfb003e6e9fea9ed97df8ef8e2ada653bf3fe0e99e14d1',
    image='sha256:cd46e779d4ab86102d00ca9bd0bca87f6f839215dc07d0f5c35c4eb65cfefce3',
    notes='豆包 CLI 2.37.2 Linux x64 原生程序与已验证本地探针镜像 ID',
)

VERIFIED_RUNTIME_CATALOG = MappingProxyType({
    'cli_grok': MappingProxyType({_GROK_1_0_89.version: _GROK_1_0_89}),
    'cli_doubao': MappingProxyType({_DOUBAO_2_37_2.version: _DOUBAO_2_37_2}),
})

DEFAULT_RUNTIME_VERSIONS = MappingProxyType({
    'cli_grok': _GROK_1_0_89.version,
    'cli_doubao': _DOUBAO_2_37_2.version,
})

# 向后兼容：每个通道默认已验证运行时。
VERIFIED_RUNTIMES = MappingProxyType({
    channel: VERIFIED_RUNTIME_CATALOG[channel][version]
    for channel, version in DEFAULT_RUNTIME_VERSIONS.items()
})


def list_verified_versions(channel: str) -> tuple[str, ...]:
    if channel not in VERIFIED_RUNTIME_CATALOG:
        raise KeyError(f'未登记的已验证容器运行时：{channel}')
    return tuple(VERIFIED_RUNTIME_CATALOG[channel])


def get_verified_runtime(channel: str, version: str | None = None) -> VerifiedRuntime:
    if channel not in VERIFIED_RUNTIME_CATALOG:
        raise KeyError(f'未登记的已验证容器运行时：{channel}')
    versions = VERIFIED_RUNTIME_CATALOG[channel]
    if version is None:
        return versions[DEFAULT_RUNTIME_VERSIONS[channel]]
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f'{channel} runtime_version 必须为非空字符串')
    requested = version.strip()
    if requested in versions:
        return versions[requested]
    for runtime in versions.values():
        if runtime.short_version and requested == runtime.short_version:
            return runtime
    supported = ', '.join(versions)
    raise ValueError(f'未验证的 {channel} 运行时版本：{requested}（已验证：{supported}）')
