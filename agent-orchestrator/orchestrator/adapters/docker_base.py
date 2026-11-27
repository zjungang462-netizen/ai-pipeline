"""Docker Linux 容器适配器的公共隔离参数、挂载转义与清理工具。"""
from __future__ import annotations

import csv
import io
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from ..safety import build_subprocess_env
from .cli_base import run_cli_process

DOCKER_HOST = (
    'npipe:////./pipe/dockerDesktopLinuxEngine'
    if os.name == 'nt'
    else 'unix:///var/run/docker.sock'
)


def bind_mount(source: Path, target: str, *, readonly: bool = True) -> str:
    """构造 Docker --mount 参数值。Docker 按 CSV 解析字段，路径含逗号时必须转义。"""
    row = ['type=bind', f'source={source.as_posix()}', f'target={target}']
    if readonly:
        row.append('readonly')
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator='\n').writerow(row)
    return buffer.getvalue().removesuffix('\n')


def validate_proxy_url(proxy_url: str, label: str) -> None:
    proxy = urlsplit(proxy_url)
    if (
        proxy.scheme not in ('http', 'https')
        or not proxy.hostname
        or proxy.username
        or proxy.password
        or proxy.query
        or proxy.fragment
        or proxy.path not in ('', '/')
    ):
        raise ValueError(f'{label} proxy_url 必须是不含凭据的 HTTP(S) 代理地址')


def validate_image_ref(image: str, label: str) -> None:
    if (
        not image
        or not isinstance(image, str)
        or image.startswith('-')
        or any(ch.isspace() for ch in image)
    ):
        raise ValueError(f'{label} image 必须是已验证探针镜像名')


def base_run_args(*, host: str = DOCKER_HOST, user: str = '65534:65534') -> list[str]:
    """只读根文件系统、默认非 root、全量降权的最小容器执行参数。"""
    return [
        '--host', host, 'run', '--rm', '--pull=never', '--platform', 'linux/amd64',
        '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
        '--user', user,
    ]


def proxy_env_args(proxy_url: str | None, *, include_no_proxy: bool = False) -> list[str]:
    if not proxy_url:
        return []
    args: list[str] = []
    for key in ('HTTP_PROXY', 'HTTPS_PROXY'):
        args += ['--env', f'{key}={proxy_url}']
    if include_no_proxy:
        args += ['--env', 'NO_PROXY=localhost,127.0.0.1']
    return args


def cleanup_container(docker_exe: str | None, name: str, *,
                      host: str = DOCKER_HOST, runner=run_cli_process) -> None:
    """Docker 守护进程启动的容器不属于宿主 Docker CLI 的进程树，须显式清理。"""
    if not docker_exe:
        return
    try:
        runner(
            [docker_exe, '--host', host, 'rm', '-f', name],
            cwd=None, env=build_subprocess_env(), input=None, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
