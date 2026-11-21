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
    notes: str = ''


VERIFIED_RUNTIMES = MappingProxyType({
    'cli_grok': VerifiedRuntime(
        channel='cli_grok',
        version='grok 1.0.89 (28be5bb1bf65)',
        binary_sha256='a3dface51d9bdc71fd121e0481dd02d9b46a721f7a6b892f756480e9666eba1f',
        image='python@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90',
        notes='Grok 1.0.89 Linux x86-64 原生程序与固定摘要 Python 镜像',
    ),
    'cli_doubao': VerifiedRuntime(
        channel='cli_doubao',
        version='doubao 2.37.2',
        binary_sha256='6120b2849c181466d1bfb003e6e9fea9ed97df8ef8e2ada653bf3fe0e99e14d1',
        image='sha256:cd46e779d4ab86102d00ca9bd0bca87f6f839215dc07d0f5c35c4eb65cfefce3',
        notes='豆包 CLI 2.37.2 Linux x64 原生程序与已验证本地探针镜像 ID',
    ),
})


def get_verified_runtime(channel: str) -> VerifiedRuntime:
    if channel not in VERIFIED_RUNTIMES:
        raise KeyError(f'未登记的已验证容器运行时：{channel}')
    return VERIFIED_RUNTIMES[channel]
