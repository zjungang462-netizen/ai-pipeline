"""安全约束（评审 M4/M5/M3）。

- 可写根白名单：可写根 = 当前 worktree + runs/<run_id>/；越界拒绝。
- env 白名单：子进程只注入 PATH/HOME/LANG + 工具必需凭据；API Key 只给 openai_compat。
- preflight：支持绝对路径、记录版本、落盘报告。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path, PureWindowsPath

from .contracts import PreflightReport


class WriteRootViolation(RuntimeError):
    """越界写入。"""


_WINDOWS_RESERVED_NAMES = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} | {
    prefix + digit for prefix in ('COM', 'LPT') for digit in '123456789¹²³'}


def windows_reserved_name(component: str) -> bool:
    """按 Windows 设备名规则检查单个路径段，包含扩展名和尾随点/空格变体。"""
    basename = component.rstrip(' .').split('.', 1)[0].rstrip(' ').upper()
    return basename in _WINDOWS_RESERVED_NAMES


def relative_target(root: Path, name: str) -> Path:
    """解析可移植的不受信路径；所有平台统一拒绝 Windows 保留设备名。"""
    rel = name.replace("\\", "/").strip()
    parts = rel.split("/")
    if (not rel or rel.startswith("/") or ":" in rel or '\0' in rel
            or PureWindowsPath(rel).drive
            or any(p == ".." or p.lower() == ".git" or windows_reserved_name(p) for p in parts)):
        raise WriteRootViolation(f"非法相对路径: {name}")
    target = (Path(root).resolve() / rel).resolve()
    if not target.is_relative_to(Path(root).resolve()):
        raise WriteRootViolation(f"路径指向工作区外: {name}")
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
                if ".git" in t.relative_to(root).parts:
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
                     Path(appdata) / "npm" / f"{name}.ps1",
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
                                  text=True, encoding="utf-8", errors="replace", timeout=30)
            version = (proc.stdout or proc.stderr).strip().splitlines()[0] if (proc.stdout or proc.stderr) else None
        except (OSError, subprocess.TimeoutExpired):
            version = None
    return PreflightReport(
        adapter=adapter_name, ok=True, exe_path=exe, tool_version=version,
        detail="已探测到可执行文件", needs_login=needs_login,
    )
