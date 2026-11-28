"""安全约束（评审 M4/M5/M3）。

- 可写根白名单：可写根 = 当前 worktree + runs/<run_id>/；越界拒绝。
- env 白名单：子进程只注入 PATH/HOME/LANG + 工具必需凭据；API Key 只给 openai_compat。
- preflight：支持绝对路径、记录版本、落盘报告。
"""
from __future__ import annotations

import functools
import os
import re
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

from .contracts import PreflightReport


class WriteRootViolation(RuntimeError):
    """越界写入。"""


_WINDOWS_RESERVED_NAMES = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} | {
    prefix + digit for prefix in ('COM', 'LPT') for digit in '123456789¹²³'}


def windows_reserved_name(component: str) -> bool:
    """按 Windows 设备名规则检查单个路径段，包含扩展名和尾随点/空格变体。"""
    basename = component.rstrip(' .').split('.', 1)[0].rstrip(' ').upper()
    return basename in _WINDOWS_RESERVED_NAMES


def normalized_component(component: str) -> str:
    """按 Windows 规范化规则去掉段尾的点/空格，再做大小写无关比较。

    Windows 上 `.git.`、`.git `、`.git...` 都会解析到 `.git`；若只比较原始
    字符串，这些变体可以绕过 `.git` 保护。其它平台的路径规范化同样不接受
    这些等价形式，统一拒绝更安全。
    """
    return component.rstrip(' .')


def is_parent_component(component: str) -> bool:
    """是否为父目录引用（含尾随空格变体）；尾随点不属于合法父引用。"""
    return component.rstrip(' ') == '..'


def is_git_component(component: str) -> bool:
    return normalized_component(component).lower() == '.git'


def relative_target(root: Path, name: str) -> Path:
    """解析可移植的不受信路径；所有平台统一拒绝 Windows 保留设备名。"""
    rel = name.replace("\\", "/").strip()
    parts = rel.split("/")
    if (not rel or rel.startswith("/") or ":" in rel or '\0' in rel
            or PureWindowsPath(rel).drive
            or any(is_parent_component(p) or is_git_component(p)
                   or windows_reserved_name(p) for p in parts)):
        raise WriteRootViolation(f"非法相对路径: {name}")
    root = Path(root).resolve()
    target = (root / rel).resolve()
    # 原始字符串检查之后，再按解析结果检查一次：防止符号链接/大小写/等
    # 价形式落到 `.git` 内部（例如 root/.git./config 在 Windows 上被规范化）。
    try:
        resolved_parts = target.relative_to(root).parts
    except ValueError:
        raise WriteRootViolation(f"路径指向工作区外: {name}") from None
    if not resolved_parts:
        raise WriteRootViolation(f"非法相对路径: {name}")
    if any(is_git_component(p) for p in resolved_parts):
        raise WriteRootViolation(f"禁止访问 .git 内部: {name}")
    return target


# 快照噪音目录：缓存/依赖/版本库内部，不参与越界判定。
# 不按「凡是点开头就跳过」——否则 .hidden/ 下的越界文件永远进不了快照。
_SNAPSHOT_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
                       ".pytest_cache", ".mypy_cache", ".ruff_cache"}


class WriteRootGuard:
    """可写根白名单（评审 M5）。--yolo 不解除此约束。

    G2 重做：**监视域（watch_roots）与可写根（roots）必须分离**。
    此前快照只扫 roots，导致 after-before 必然全在根内、越界判定恒为空（空转）。
    现在快照扫监视域（默认各可写根的父目录），差集里"不在任何可写根内"的才是越界。

    边界：只覆盖邻近目录，**不做全盘审计**——写到其他盘/远处目录无法捕获。
    """

    def __init__(self, roots: list[Path], watch_roots: list[Path] | None = None):
        self.roots = [Path(r).resolve() for r in roots]
        # 未显式配置时不遍历父目录；告警不是隔离机制。
        self.watch_roots = [Path(w).resolve() for w in (watch_roots or [])]

    def check(self, target: Path) -> None:
        t = Path(target).resolve()
        for root in self.roots:
            if t == root or root in t.parents:
                # 禁止写入 .git 内部
                if any(is_git_component(p) for p in t.relative_to(root).parts):
                    raise WriteRootViolation(f"禁止写入 .git 内部: {t}")
                return
        raise WriteRootViolation(f"越界写入（不在可写根内）: {t}")

    def is_inside(self, target: Path) -> bool:
        t = Path(target).resolve()
        return any(t == root or root in t.parents for root in self.roots)

    def scan_outside(self, base: Path, changed_rel: list[str]) -> list[str]:
        """给定基目录与相对变更清单，返回落在可写根之外的文件（绝对路径）。"""
        base = Path(base).resolve()
        outside: list[str] = []
        for rel in changed_rel:
            abs_path = (base / rel).resolve()
            if not self.is_inside(abs_path):
                outside.append(str(abs_path))
        return sorted(outside)

    def snapshot(self, max_files: int = 5000, max_depth: int = 4) -> dict[str, tuple[int, int]]:
        """监视域里可写根之外的文件：路径 -> (size, mtime_ns)。

        可写根整棵剪掉，名额只留给根外文件，避免工作区自己的文件把上限占满。
        隐藏目录会进入（不再因为点开头整棵跳过）。深度和文件数有上限。
        """
        out: dict[str, tuple[int, int]] = {}
        for root in self.watch_roots:
            if not root.is_dir() or self.is_inside(root):
                continue
            root_str = str(root)
            base_depth = root_str.rstrip(os.sep).count(os.sep)
            for dirpath, dirnames, filenames in os.walk(root_str):
                kept: list[str] = []
                for name in dirnames:
                    if name in _SNAPSHOT_SKIP_DIRS:
                        continue
                    if self.is_inside(Path(dirpath) / name):
                        continue
                    kept.append(name)
                dirnames[:] = sorted(kept)
                depth = dirpath.rstrip(os.sep).count(os.sep) - base_depth
                if depth >= max_depth:
                    dirnames[:] = []
                for fn in filenames:
                    path = str((Path(dirpath) / fn).resolve())
                    if self.is_inside(Path(path)):
                        continue
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
                    out[path] = (st.st_size, mtime_ns)
                    if len(out) >= max_files:
                        return out
        return out

    def diff_outside(self, before: dict[str, tuple[int, int]],
                     after: dict[str, tuple[int, int]]) -> list[str]:
        """根外新增、改写，或根外已有文件被删掉，都算越界。

        快照有数量上限，没进 after 不等于被删。只有路径确实不存在才算删除。
        """
        changed: list[str] = []
        for path, meta in after.items():
            if before.get(path) == meta:
                continue
            if not self.is_inside(Path(path)):
                changed.append(path)
        for path in before:
            if path in after or self.is_inside(Path(path)):
                continue
            if not Path(path).exists():
                changed.append(path)
        return sorted(changed)


# 子进程基础白名单（不含任何 API Key）
_BASE_ENV_KEYS = ("PATH", "PATHEXT", "HOME", "USERPROFILE", "LANG", "LC_ALL",
                  "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA",
                  "PROGRAMFILES", "PROGRAMDATA", "COMSPEC", "NUMBER_OF_PROCESSORS")

SECRET_ENV_RE = re.compile(
    r'api.?key|auth.?token|access.?token|password|passwd|passphrase'
    r'|secret|credential|token(?:_|$)|_key(?:_id)?$|(?:access|secret).?key|webhook', re.I)
MIN_SECRET_BYTES = 4


@functools.lru_cache(maxsize=512)
def _is_secret_env_name(name: str) -> bool:
    """名称→判定是纯函数；有界缓存避免每个响应重复跑正则。"""
    return bool(SECRET_ENV_RE.search(name))


def is_secret_env_name(name: str) -> bool:
    return _is_secret_env_name(str(name or ''))


# 环境变量快照缓存：每次调用仍完整读取 os.environ 作为键，任何增删改都会立刻命中新键，
# 不会出现 TTL 期间漏掉新密钥的问题。缓存只保留最近一次结果。
_ENV_SECRETS_LOCK = threading.Lock()
_ENV_SECRETS_CACHE: tuple[tuple[tuple[str, str], ...], tuple[str, ...], int, tuple[str, ...]] | None = None


def environment_secrets(extra_names=(), *, min_bytes: int = MIN_SECRET_BYTES) -> list[str]:
    global _ENV_SECRETS_CACHE
    explicit = {str(name) for name in (extra_names or ())}
    explicit_key = tuple(sorted(explicit))
    for _ in range(3):
        try:
            snapshot = tuple(os.environ.items())
            break
        except RuntimeError:
            # 另一线程正在改 os.environ；重试取一致快照，绝不返回过期密钥列表。
            continue
    else:
        snapshot = tuple(os.environ.items())
    with _ENV_SECRETS_LOCK:
        cached = _ENV_SECRETS_CACHE
    if (cached is not None and cached[0] == snapshot
            and cached[1] == explicit_key and cached[2] == min_bytes):
        return list(cached[3])
    values = tuple(
        value for name, value in snapshot
        if isinstance(value, str) and len(value.encode('utf8')) >= min_bytes
        and (is_secret_env_name(name) or name in explicit)
    )
    with _ENV_SECRETS_LOCK:
        _ENV_SECRETS_CACHE = (snapshot, explicit_key, min_bytes, values)
    return list(values)


def build_subprocess_env(extra_allow: list[str] | None = None) -> dict[str, str]:
    """白名单 env（评审 M4）：默认不透传任何 *_API_KEY / *_TOKEN。

    extra_allow 为该工具显式声明需要的凭据变量名（须配置开启并告警）。
    """
    env: dict[str, str] = {}
    for key in _BASE_ENV_KEYS:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    for key in (extra_allow or []):
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    return env


def adapter_env_allow(adapters):
    """仅收集适配器显式声明的凭据与连接环境变量名。"""
    names = []
    for adapter in adapters.values():
        if adapter.type == 'openai_compat':
            names.append(adapter.options['api_key_env'])
        names.extend(adapter.options.get('extra_env') or ())
    return list(dict.fromkeys(names))


def _request_origin(url):
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    default_port = 443 if scheme == 'https' else 80 if scheme == 'http' else None
    port = parsed.port if parsed.port is not None else default_port
    return scheme, (parsed.hostname or '').lower(), port


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """拒绝跨源重定向，避免认证头或签名载荷被转发。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _request_origin(req.full_url) != _request_origin(newurl):
            raise urllib.error.HTTPError(
                req.full_url, code, 'Cross-origin redirect rejected', headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_same_origin(request, timeout):
    return urllib.request.build_opener(_SameOriginRedirectHandler()).open(request, timeout=timeout)


def resolve_exe(name: str, configured: str | None = None) -> str | None:
    """解析可执行文件：优先配置绝对路径，其次 PATH，再常见 npm 全局目录。"""
    if configured:
        p = Path(configured)
        if p.is_file():
            return str(p)
        # 配置可能是命令名，落到 PATH 探测
        found = shutil.which(configured)
        if found:
            return found
    found = shutil.which(name)
    if found:
        return found
    # Windows npm/pnpm 全局包常不在 PATH
    appdata = os.environ.get("APPDATA")
    if appdata:
        for cand in (Path(appdata) / "npm" / f"{name}.cmd",
                     Path(appdata) / "npm" / name):
            if cand.is_file():
                return str(cand)
    return None


def preflight_cli(adapter_name: str, exe_name: str, configured: str | None,
                  version_args: list[str] | None = None,
                  needs_login: bool = False) -> PreflightReport:
    """CLI preflight：探测路径与版本，不假设工具存在（评审 M3）。"""
    import subprocess
    exe = resolve_exe(exe_name, configured)
    if not exe:
        return PreflightReport(
            adapter=adapter_name, ok=False,
            detail=f"未找到可执行文件 {exe_name}（可在配置 exe 填绝对路径）",
            needs_login=needs_login,
        )
    version = None
    if version_args:
        try:
            proc = subprocess.run([exe, *version_args], capture_output=True,
                                  env=build_subprocess_env(),
                                  text=True, encoding="utf-8", errors="replace", timeout=30)
            version = (proc.stdout or proc.stderr).strip().splitlines()[0] if (proc.stdout or proc.stderr) else None
        except (OSError, subprocess.TimeoutExpired):
            version = None
    return PreflightReport(
        adapter=adapter_name, ok=True, exe_path=exe, tool_version=version,
        detail="已探测到可执行文件", needs_login=needs_login,
    )
