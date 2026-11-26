"""产物管理（评审 B3）。

- 代码类产物 = commit/branch：上游在自身 worktree 提交，下游基于上游 commit 拉分支。
- 报告类产物 = 文件 + 哈希。
- 保留删除/重命名与 git 历史，可 diff 可复现。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .state import sha256_file

_VERIFIED_GIT_VERSION: dict[tuple[str, int], bool] = {}


class GitError(RuntimeError):
    pass


class GitContext:
    """为一次运行或只读操作保存 Git 可执行文件与基线。"""

    def __init__(self, configured: str | None = None):
        self.configured = configured
        self.executable: str | None = None
        self.baselines: dict[Path, str] = {}


def _git_registry_paths() -> list[str]:
    """从 Windows 注册表读 Git 安装路径（InstallPath），避免硬编码盘符。"""
    paths: list[str] = []
    try:
        import winreg
    except ImportError:
        return paths
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\GitForWindows"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\GitForWindows"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\GitForWindows"),
    ]
    for hive, sub in keys:
        try:
            with winreg.OpenKey(hive, sub) as k:
                install, _ = winreg.QueryValueEx(k, "InstallPath")
                if install:
                    paths.append(str(Path(install) / "cmd" / "git.exe"))
        except OSError:
            continue
    return paths


def _git_fallback_paths() -> list[str]:
    """常见 git 安装位置兜底（评审 G1：PATH 无 git 时不静默失效）。

    优先注册表 InstallPath（机器无关），再环境变量推导的 Program Files，
    最后 POSIX 常见路径。不硬编码具体盘符。
    """
    cands: list[str] = _git_registry_paths()
    for env_key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env_key)
        if base:
            cands.append(str(Path(base) / "Git" / "cmd" / "git.exe"))
    cands += ["/usr/bin/git", "/usr/local/bin/git"]
    return cands


def resolve_git(git: GitContext | None = None) -> str | None:
    """解析 git：配置路径 > PATH > 常见安装位置。返回可用 exe 或 None。"""
    if git is not None and git.executable is not None:
        return git.executable
    candidates: list[str] = []
    if git is not None and git.configured:
        candidates.append(git.configured)
    found = shutil.which("git")
    if found:
        candidates.append(found)
    candidates += _git_fallback_paths()
    for cand in candidates:
        if not cand:
            continue
        resolved = cand if Path(cand).is_file() else shutil.which(cand)
        if not resolved:
            continue
        try:
            cache_key = (str(Path(resolved).resolve()), Path(resolved).stat().st_mtime_ns)
        except OSError:
            cache_key = None
        if cache_key is not None and _VERIFIED_GIT_VERSION.get(cache_key):
            if git is not None:
                git.executable = cand
            return cand
        try:
            proc = subprocess.run([cand, "--version"], capture_output=True,
                                  text=True, timeout=15)
            if proc.returncode == 0:
                if cache_key is not None:
                    _VERIFIED_GIT_VERSION[cache_key] = True
                if git is not None:
                    git.executable = cand
                return cand
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


def _no_hooks_dir() -> str:
    """空目录当作 core.hooksPath，引擎发起的 git 不执行仓库 hook。"""
    path = Path(tempfile.gettempdir()) / "ai-pipeline-no-hooks"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve().as_posix()


def _git(args: list[str], cwd: Path, check: bool = True,
         timeout: int = 120, *, stdin: bytes | None = None,
         binary: bool = False, git: GitContext | None = None) -> subprocess.CompletedProcess:
    """git 子进程：env 白名单（不透传 API Key）+ 超时 + 禁用 hook。"""
    from .safety import build_subprocess_env
    exe = resolve_git(git) or "git"
    cmd = [exe, "-c", f"core.hooksPath={_no_hooks_dir()}", *args]
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), env=build_subprocess_env(),
            input=stdin,
            capture_output=True, text=not binary,
            encoding=None if binary else "utf-8", errors=None if binary else "replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {' '.join(args)} 超时（{timeout}s）") from e
    if check and proc.returncode != 0:
        output = proc.stderr.strip() or proc.stdout.strip()
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        raise GitError(f"git {' '.join(args)} 失败: {output}")
    return proc


def git_available(git: GitContext | None = None) -> bool:
    return resolve_git(git) is not None


def ensure_repo(workdir: Path, exclude: Path | None = None, *, git: GitContext | None = None) -> None:
    """确保 workdir 是一个 git 仓库（含初始提交）。

    首次初始化时把工作区已有文件（跳过隐藏目录、缓存目录与 exclude）提交为
    基线，而不是建空基线：否则既有源码会成为 untracked，coding worktree 合并
    回主线时永久报 "untracked working tree files would be overwritten"。
    提交仍走 `commit_files` 清单（非 git add -A 全仓）。
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    fresh = not (workdir / ".git").exists()
    if fresh:
        _git(["init", "-q"], workdir, git=git)
        cfg_file = workdir / ".git" / "config"
        if cfg_file.is_file():
            with cfg_file.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write("\n[user]\n\temail = orchestrator@local\n\tname = orchestrator\n")
        else:
            _git(["config", "user.email", "orchestrator@local"], workdir, git=git)
            _git(["config", "user.name", "orchestrator"], workdir, git=git)
    # 无任何提交时建基线：有既有文件就提交清单，否则空提交。
    has_head = False if fresh else (_git(["rev-parse", "--verify", "HEAD"], workdir, check=False, git=git).returncode == 0)
    if not has_head:
        existing = [p.relative_to(workdir).as_posix()
                    for p in workspace_files(workdir, exclude=exclude)]
        committed = commit_files(workdir, existing, "baseline: existing workspace", git=git) if existing else None
        if not committed:
            if existing:
                has_head = _git(["rev-parse", "--verify", "HEAD"], workdir, check=False, git=git).returncode == 0
            if not has_head:
                _git(["commit", "-q", "--no-gpg-sign", "--allow-empty", "-m", "baseline"], workdir, git=git)


def head_sha(workdir: Path, *, git: GitContext | None = None) -> str:
    return _git(["rev-parse", "HEAD"], workdir, git=git).stdout.strip()


def changed_files_since(workdir: Path, base_sha: str, *, git: GitContext | None = None) -> list[str]:
    """相对基线的变更文件（含工作区未提交改动）。"""
    out = _git(["diff", "--name-only", "-z", base_sha], workdir, git=git).stdout
    files = [name for name in out.split("\0") if name]
    # 未跟踪文件也算变更
    untracked = _git(["ls-files", "-z", "--others", "--exclude-standard"], workdir, git=git).stdout
    files += [name for name in untracked.split("\0") if name]
    return sorted(set(files))


def add_worktree(repo: Path, base_sha: str, branch: str, dest: Path, *, git: GitContext | None = None) -> Path:
    """基于上游 commit 拉新分支建 worktree（评审 B3：非基于主干）。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(["worktree", "add", "-q", "-b", branch, str(dest), base_sha], repo, git=git)
    return dest


def remove_worktree(repo: Path, dest: Path, *, git: GitContext | None = None) -> bool:
    """移除 worktree；失败时 prune 失效登记并返回 False（由调用方告警）。"""
    proc = _git(["worktree", "remove", "--force", str(dest)], repo, check=False, git=git)
    if proc.returncode != 0:
        _git(["worktree", "prune"], repo, check=False, git=git)
        return False
    return True


def merge_branch(repo: Path, branch: str, *, git: GitContext | None = None) -> str:
    """把阶段分支合并回主线（快进），返回合并后 HEAD。"""
    _git(["merge", "--ff-only", "-q", branch], repo, git=git)
    return head_sha(repo, git=git)


def report_hashes(files: list[Path]) -> dict[str, str]:
    return {str(f): sha256_file(f) for f in files if Path(f).is_file()}


def unsafe_rel_paths(files: list[str]) -> list[str]:
    """绝对路径、`..` 或路径分段里的 `.git`（含尾随点/空格变体）。这些不能交给 git add。"""
    bad: list[str] = []
    for f in files:
        p = Path(f)
        parts = p.parts
        if (p.is_absolute()
                or any(_unsafe_part(part) for part in parts)):
            bad.append(f.replace("\\", "/"))
    return bad


def _unsafe_part(part: str) -> bool:
    """路径段是否越界/指向 .git；按 Windows 规范化（去尾随点/空格）判断。"""
    from .safety import is_git_component, is_parent_component
    return is_parent_component(part) or is_git_component(part)


def _git_relative_parts(rel: str) -> tuple[str, ...] | None:
    try:
        return Path(rel).parts
    except (TypeError, ValueError):
        return None


def remove_git_metadata_files(workdir: Path, rels: list[str]) -> None:
    """删掉清单里落到本仓库 `.git` 下的文件（模型刚写入的 hook）。不跟随 `..`。

    只处理 `.git` 目录**内部**的文件；清单精确为 `.git` 时是 worktree 的
    gitdir 指针文件，删除会让工作区变成非 git 目录，必须保留。
    """
    root = Path(workdir).resolve()
    for rel in rels:
        parts = _git_relative_parts(rel)
        if not parts or Path(rel).is_absolute():
            continue
        if any(p == ".." for p in parts):
            continue
        git_indexes = [i for i, p in enumerate(parts) if _is_git_dir_part(p)]
        if not git_indexes:
            continue
        # 最后一个组件就是 `.git` 本身 => 指针文件/目录入口，不在此处删除。
        if git_indexes[-1] == len(parts) - 1:
            continue
        target = (root / Path(rel)).resolve()
        try:
            rel_to_root = target.relative_to(root)
        except ValueError:
            continue
        if not any(_is_git_dir_part(part) for part in rel_to_root.parts):
            continue
        if target.is_file():
            try:
                target.unlink()
            except OSError:
                pass


def _is_git_dir_part(part: str) -> bool:
    from .safety import is_git_component
    return is_git_component(part)


def commit_files(workdir: Path, files: list[str], message: str, *, git: GitContext | None = None) -> str | None:
    """按清单提交（评审 B4）：只 git add 指定文件，不用 add -A 全仓。

    files 为相对 workdir 的路径。清单为空或无实际改动返回 None。
    越界或 `.git` 路径直接拒绝，不调用 git add。
    """
    if not files:
        return None
    bad = unsafe_rel_paths(files) + [f for f in files if not f or '\0' in f or Path(f) == Path('.')]
    if bad:
        raise GitError(f"拒绝提交空路径、越界或 .git 路径: {bad[:5]}")
    selected = {Path(f).as_posix() for f in files}
    # NUL 分隔的字面路径清单避免 Windows 命令行长度限制及 Git 路径表达式展开。
    pathspec = ('\0'.join(sorted(selected)) + '\0').encode('utf-8', errors='surrogateescape')
    options = ["--pathspec-from-file=-", "--pathspec-file-nul"]
    _git(["--literal-pathspecs", "add", *options], workdir, stdin=pathspec, binary=True, git=git)
    # 整个索引的 NUL 路径在 Python 侧与清单取交集，不把无关暂存算成代码变更。
    staged = _git(["diff", "--cached", "--no-renames", "--name-only", "-z"], workdir,
                  binary=True, git=git).stdout
    if set(staged.split(b'\0')).isdisjoint(pathspec[:-1].split(b'\0')):
        return None
    _git(["--literal-pathspecs", "commit", "-q", "--no-gpg-sign", "--only", "-m", message, *options],
         workdir, stdin=pathspec, binary=True, git=git)
    return head_sha(workdir, git=git)


def untracked_outside(workdir: Path, declared: list[str], *, git: GitContext | None = None) -> list[str]:
    """检测清单之外的未跟踪/改动文件（评审 B4：越界改动告警）。"""
    declared_set = {f.replace("\\", "/") for f in declared}
    changed = changed_files_since(workdir, head_sha(workdir, git=git), git=git)
    return [f for f in changed if f.replace("\\", "/") not in declared_set]


def normalize_rel(workdir: Path, files: list[str]) -> list[str]:
    """把变更文件清单归一化为相对 workdir 的路径（评审 B4：按清单提交）。"""
    workdir = Path(workdir).resolve()
    out: list[str] = []
    for f in files:
        p = Path(f)
        if p.is_absolute():
            try:
                rel = p.resolve().relative_to(workdir)
            except ValueError:
                rel = p  # 根外路径保留，交由越界扫描处理
        else:
            rel = p
        out.append(str(rel).replace("\\", "/"))
    return sorted(set(out))


def snapshot_files(workdir: Path) -> set[str]:
    """工作区文件快照（相对路径集合，排除 .git），用于越界改动检测（评审 M2）。"""
    workdir = Path(workdir)
    out: set[str] = set()
    for p in workdir.rglob("*"):
        if p.is_file() and ".git" not in p.parts:
            out.add(str(p.relative_to(workdir)).replace("\\", "/"))
    return out


def diff_summary(workdir: Path, commit_sha: str, max_lines: int = 200, *, git: GitContext | None = None) -> str:
    """上游代码变更摘要（评审 B1）：相对父提交的 diff，截断防超长。"""
    proc = _git(["diff", f"{commit_sha}~1", commit_sha], workdir, check=False, git=git)
    if proc.returncode != 0:
        # 无父提交（首个 commit）=> 用 show
        proc = _git(["show", "--format=", commit_sha], workdir, check=False, git=git)
    lines = (proc.stdout or "").splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"...（diff 截断，共 {len(proc.stdout.splitlines())} 行）"]
    return "\n".join(lines)


# 公开常量（N2）：工作区遍历时跳过的缓存/依赖/状态目录。
# 引擎等模块直接引用此名，不再依赖私有成员。
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
             ".state", ".smoke_state", ".full_state", ".orchestrator",
             ".demo_state", "attempts", "worktree", ".pytest_cache"}
_CODE_SUFFIX = {".py", ".js", ".ts", ".java", ".cs", ".go", ".rs", ".toml",
                ".md", ".json", ".yaml", ".yml", ".sql", ".vue", ".sh"}


def workspace_files(workdir: Path, exclude: Path | None = None):
    """提前剪枝，既不遍历依赖目录，也不跟随根外链接。"""
    root = Path(workdir).resolve()
    excluded = Path(exclude).resolve() if exclude else None
    for directory, names, files in os.walk(root):
        names[:] = sorted(n for n in names if n not in SKIP_DIRS
                          and not n.startswith(".")
                          and not (Path(directory) / n).is_symlink()
                          and (Path(directory) / n).resolve() != excluded)
        for name in sorted(files):
            target = Path(directory) / name
            if (not name.startswith(".") and target.is_file()
                    and target.resolve().is_relative_to(root)
                    and (not excluded or not target.resolve().is_relative_to(excluded))):
                yield target


def workspace_file_hashes(workdir: Path, exclude: Path | None = None,
                          ignore: set[str] | None = None) -> dict[str, str]:
    """工作区文件内容哈希（相对路径 -> sha256）；ignore 为要排除的相对路径。"""
    ignored = {p.replace("\\", "/") for p in (ignore or set())}
    result: dict[str, str] = {}
    for path in workspace_files(workdir, exclude):
        rel = path.relative_to(Path(workdir).resolve()).as_posix()
        if rel in ignored:
            continue
        result[rel] = sha256_file(path)
    return result


def workspace_hash(workdir: Path, exclude: Path | None = None,
                   ignore: set[str] | None = None,
                   ignore_hashes: dict[str, str] | None = None) -> str:
    from .state import sha256_text
    files = workspace_file_hashes(workdir, exclude=exclude, ignore=ignore)
    missing = []
    for rel, expected in (ignore_hashes or {}).items():
        if rel not in files:
            missing.append(rel)
        elif files[rel] == expected:
            del files[rel]
    # 输出丢失也必须使缓存失效；仅删除路径会与“首次执行前尚无输出”同哈希。
    values = sorted(files.items())
    if missing:
        return sha256_text(repr((values, sorted(missing))))
    return sha256_text(repr(values))


def verified_workspace_tree(workdir: Path, exclude: Path | None = None, *, git: GitContext | None = None) -> str | None:
    """仅当工作区范围及原始文件字节均与提交树一致时采用树指纹。"""
    root = Path(workdir).resolve()
    if not (root / ".git").exists():
        return None
    try:
        tree = _git(["rev-parse", "--verify", "HEAD^{tree}"], root, git=git).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tree):
            return None
        listing = _git(["ls-tree", "-r", "-z", "--full-tree", tree], root, git=git).stdout
        blobs = {}
        excluded = Path(exclude).resolve() if exclude else None
        for row in listing.split("\0"):
            if not row:
                continue
            meta, path = row.split("\t", 1)
            mode, kind, oid = meta.split(" ")
            parts = path.split("/")
            if (any(part.startswith(".") for part in parts)
                    or any(part in SKIP_DIRS for part in parts[:-1])
                    or (excluded and (root / path).is_relative_to(excluded))):
                continue
            if kind != "blob" or mode not in ("100644", "100755"):
                return None
            blobs[path] = oid
        paths = {file.relative_to(root).as_posix(): file for file in workspace_files(root, exclude)}
        if paths.keys() != blobs.keys() or any(
                path.is_symlink() or set(path.as_posix()) & {'\n', '\r'}
                for path in paths.values()):
            return None
        if paths:
            # 批量重读原文计算 Git blob OID（blob <size>\0<bytes>），不使用 Git stat 缓存或换行转换。
            algo = hashlib.sha256 if len(tree) == 64 else hashlib.sha1
            for name, path in paths.items():
                data = path.read_bytes()
                digest = algo(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()
                if digest != blobs[name]:
                    return None
        return tree
    except (GitError, OSError, ValueError):
        return None


def existing_code_context(workdir: Path, max_file_bytes: int = 12000,
                          max_total_bytes: int = 120000) -> str:
    """读取工作区现有源码，供"审查已有代码库"场景（无上游 coding 阶段）。

    跳过缓存/状态/依赖目录；单文件与总量都有上限，超限截断并标注。
    """
    return existing_code_context_info(workdir, max_file_bytes, max_total_bytes)[0]


def existing_code_context_info(workdir: Path, max_file_bytes: int = 12000,
                               max_total_bytes: int = 120000, max_files: int = 60,
                               exclude: Path | None = None) -> tuple[str, list[str]]:
    """返回正文与完整缺失清单；任何被截断/不可读源码都计入覆盖缺失。"""
    workdir = Path(workdir)
    if not workdir.is_dir():
        return "", []
    parts: list[str] = []
    total = 0
    skipped: list[str] = []
    included = 0
    for p in sorted(workspace_files(workdir, exclude=exclude)):
        rel = p.relative_to(workdir)
        relstr = rel.as_posix()
        if p.suffix.lower() not in _CODE_SUFFIX:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            skipped.append(relstr+'（不可读）')
            continue
        if total >= max_total_bytes or included >= max_files:
            skipped.append(relstr)
            continue
        try:
            with p.open("rb") as stream:
                data = stream.read(min(max_file_bytes, max_total_bytes - total))
            text = data.decode("utf-8", errors="replace")
        except OSError:
            skipped.append(relstr+'（不可读）')
            continue
        if b'\0' in data:
            skipped.append(relstr+'（二进制，未注入）')
            continue
        if size > len(data):
            text += f"\n...（文件截断，原 {size} 字节）"
            skipped.append(relstr+'（未完整注入）')
        parts.append(f"### 文件: {relstr}\n```\n{text}\n```")
        total += len(data)
        included += 1
    if skipped:
        parts.append(f"（因总量上限未纳入的文件: {', '.join(skipped[:20])}）")
    return "\n\n".join(parts), skipped


def baseline_sha(workdir: Path, *, git: GitContext | None = None) -> str | None:
    """没有固定基线时，在当前运行内复用首个根提交。"""
    key = Path(workdir).resolve()
    if git is not None and key in git.baselines:
        return git.baselines[key]
    proc = _git(["rev-list", "--max-parents=0", "HEAD"], workdir, check=False, git=git)
    result: str | None = None
    if proc.returncode == 0:
        roots = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
        result = roots[0] if roots else None
    if git is not None and result is not None:
        git.baselines[key] = result
    return result


def cumulative_diff(workdir: Path, commit_sha: str, max_lines: int = 600,
                    base_sha: str | None = None, *, git: GitContext | None = None) -> str:
    """相对项目基线的累积全量 diff + 文件清单（修复 M0）。

    审查阶段必须看到完整实现，而非仅末次提交（commit~1..commit）。
    否则返工轮次里审查员只看到增量测试改动，会误判"实现代码缺失"。
    """
    return cumulative_diff_info(workdir, commit_sha, max_lines=max_lines,
                                base_sha=base_sha, git=git)[0]


def cumulative_diff_info(workdir: Path, commit_sha: str, max_lines: int = 600,
                         base_sha: str | None = None, *, git: GitContext | None = None) -> tuple[str, int, bool]:
    """同 cumulative_diff，另返回 (文本, diff 总行数, 是否被截断)。"""
    base = base_sha or baseline_sha(workdir, git=git)
    if base and base != commit_sha:
        listing = _git(["diff", "--stat", base, commit_sha], workdir, check=False, git=git).stdout
        proc = _git(["diff", base, commit_sha], workdir, check=False, git=git)
    else:
        listing = _git(["ls-tree", "-r", "--name-only", commit_sha], workdir, check=False, git=git).stdout
        proc = _git(["show", "--format=", commit_sha], workdir, check=False, git=git)
    body = proc.stdout or ""
    lines = body.splitlines()
    truncated = len(lines) > max_lines
    if truncated:
        lines = lines[:max_lines] + [
            f"...（diff 截断，共 {len(body.splitlines())} 行）"]
    head = f"变更文件清单（相对项目基线，累积全量）:\n{listing.strip()}\n" if listing.strip() else ""
    return head + "\n".join(lines), len(body.splitlines()), truncated


def changed_files_at(workdir: Path, commit_sha: str, base_sha: str | None = None, *, git: GitContext | None = None) -> list[str]:
    """相对基线在该 commit 中变更的文件（含新增/修改/删除）。"""
    base = base_sha or baseline_sha(workdir, git=git)
    if base and base != commit_sha:
        out = _git(["diff", "--name-only", "-z", base, commit_sha], workdir, check=False, git=git).stdout
    else:
        out = _git(["ls-tree", "-r", "-z", "--name-only", commit_sha], workdir, check=False, git=git).stdout
    return [name for name in out.split("\0") if name]


def review_file_context(workdir: Path, commit_sha: str, paths: list[str],
                        per_file_bytes: int = 24000, total_bytes: int = 180000,
                        max_files: int = 60, *, git: GitContext | None = None) -> tuple[str, list[str]]:
    """审查上下文：按 commit 读取变更文件全文，返回 (文本, 未纳入文件清单)。

    当累积 diff 被截断时使用，避免默认 API 通道（无法读取工作区文件）的
    审查模型只看到前半段实现。返回的未纳入清单非空即表示覆盖不完整，调用方
    不得把通过结论直接当作完成。
    """
    parts: list[str] = []
    missing: list[str] = []
    used = 0
    included = 0
    for rel in paths:
        if included >= max_files or used >= total_bytes:
            missing.append(rel)
            continue
        proc = _git(["show", f"{commit_sha}:{rel}"], workdir, check=False, git=git)
        if proc.returncode != 0:
            # 该 commit 中已删除/不可读：由 diff 表达，不算覆盖缺失。
            continue
        raw = proc.stdout
        if "\0" in raw:
            missing.append(rel + "（二进制，未注入）")
            continue
        data = raw.encode("utf-8", errors="replace")
        if len(data) > per_file_bytes:
            missing.append(rel + "（超过单文件上限，未完整注入）")
            continue
        if used + len(data) > total_bytes:
            missing.append(rel)
            continue
        text = raw
        parts.append(f"### 实现文件: {rel}\n```\n{text}\n```")
        used += len(data)
        included += 1
    return "\n\n".join(parts), missing


def tree_content_hash(workdir: Path, commit_sha: str, *, git: GitContext | None = None) -> str:
    """基于 commit 树对象的内容哈希（评审 B3/S1）。

    用 git ls-tree -r -z 列出所有条目的 (mode,type,object) 并排序后哈希，
    路径不做 quotePath 转义；只反映树内容（含 submodule gitlink），与提交
    时间戳/基线、git 的 core.quotePath 配置无关 => 重跑内容相同则哈希相同。
    """
    out = _git(["ls-tree", "-r", "-z", "--full-tree", commit_sha], workdir,
               check=False, git=git).stdout
    entries = []
    for entry in out.split("\0"):
        if not entry or "\t" not in entry:
            continue
        meta, path = entry.split("\t", 1)
        parts = meta.split()
        if len(parts) >= 3:
            entries.append((path, f"{parts[0]} {parts[1]} {parts[2]}"))
    entries.sort()
    import hashlib
    payload = "\n".join(f"{p}:{m}" for p, m in entries)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
