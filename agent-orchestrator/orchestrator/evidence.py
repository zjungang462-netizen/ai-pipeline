"""attempt 产物清单：所有恢复、审批与缓存共用内容校验。"""
from __future__ import annotations

import json
from pathlib import Path

from .state import atomic_write_json, sha256_file, sha256_text

METADATA = {"manifest.json", "success.json", "decision.json", "approval.json", "execution.json"}


def digest(manifest: dict) -> str:
    # commit 和仓库路径是定位信息；完成哈希只取内容，不含提交时间戳。
    if not isinstance(manifest, dict):
        raise TypeError("manifest 必须是对象")
    files = manifest["files"]
    if not isinstance(files, dict):
        raise TypeError("files 必须是对象")
    code = manifest.get("code")
    if code is not None and not isinstance(code, dict):
        raise TypeError("code 必须是对象")
    return sha256_text(json.dumps({
        "schema_version": 2, "files": files,
        "tree": (code or {}).get("tree"),
    }, ensure_ascii=False, sort_keys=True))


def seal(directory: Path, repo: Path | None = None,
         commit: str | None = None) -> str:
    directory = Path(directory)
    files = {p.name: sha256_file(p) for p in sorted(directory.iterdir())
             if p.is_file() and p.name not in METADATA and ".tmp." not in p.name}
    manifest = {"schema_version": 2, "files": files}
    if commit and repo:
        from .artifacts import tree_content_hash
        manifest["code"] = {"repository": str(repo.resolve()),
                            "commit": commit, "tree": tree_content_hash(repo, commit)}
    elif (directory / "manifest.json").is_file():
        old = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if isinstance(old, dict) and isinstance(old.get("code"), dict):
            manifest["code"] = old["code"]
    atomic_write_json(directory / "manifest.json", manifest)
    return digest(manifest)


def validate(directory: Path, record: dict) -> bool:
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
            return False
        code = manifest.get("code")
        if code is not None and not isinstance(code, dict):
            return False
        if manifest.get("schema_version") != 2 or digest(manifest) != record["outputs_hash"]:
            return False
        if not manifest["files"]:
            return False
        present = {p.name for p in directory.iterdir()
                   if p.is_file() and p.name not in METADATA and ".tmp." not in p.name}
        if present != set(manifest["files"]):
            return False
        from .safety import relative_target
        for name, expected in manifest["files"].items():
            if Path(name).name != name or sha256_file(relative_target(directory, name)) != expected:
                return False
        code = manifest.get("code")
        if record.get("commit_sha"):
            if not code or code["commit"] != record["commit_sha"]:
                return False
            from .artifacts import tree_content_hash
            if tree_content_hash(Path(code["repository"]), code["commit"]) != code["tree"]:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False


def approved(directory: Path, record: dict) -> bool:
    try:
        receipt = json.loads((directory / "approval.json").read_text(encoding="utf-8"))
        if not isinstance(receipt, dict):
            return False
        return all(receipt.get(k) == record.get(k)
                   for k in ("n", "inputs_hash", "outputs_hash"))
    except (OSError, ValueError, TypeError):
        return False
