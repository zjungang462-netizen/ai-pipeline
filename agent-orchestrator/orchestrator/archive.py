"""固定提交、产物归档、工作区清理与测试产物记录。"""
from __future__ import annotations

import shutil
from dataclasses import asdict
from pathlib import Path

from . import artifacts
from .config import Config
from .contracts import Result, StageSpec
from .state import RunState


class StageArchiveError(RuntimeError):
    """产物归档/合并失败 => 阶段级失败，可重试，不中断整轮。"""


class ArchiveOperations:
    def __init__(self, config: Config, state: RunState, git: artifacts.GitContext):
        self.config = config
        self.state = state
        self.git = git

    def record_test_artifacts(self, spec: StageSpec, before: dict[str, str],
                              *, successful: bool) -> list[str]:
        """仅新文件和已绑定的输出可作为产物；返回被改写/删除的既有输入。"""
        after = artifacts.workspace_file_hashes(
            self.config.task_workdir, exclude=self.config.state_dir)
        known = self.state.test_artifact_hashes.get(spec.name, {})
        changed_inputs = sorted(rel for rel, digest in before.items()
                                if rel not in known and after.get(rel) != digest)
        # 失败测试产生的内容不能改写可信输出绑定，否则旧 pass 可能重新命中。
        if not successful or changed_inputs:
            return changed_inputs
        candidates = sorted(rel for rel in after if rel not in before or rel in known)
        limit = 500
        if len(candidates) > limit:
            self.state.log_event("test_artifacts_truncated", spec.name,
                                 total=len(candidates))
        recorded = {rel: after[rel] for rel in candidates[:limit]}
        self.state.test_artifact_hashes[spec.name] = recorded
        self.state.test_artifacts[spec.name] = list(recorded)
        if recorded:
            self.state.log_event("test_artifacts", spec.name, files=list(recorded)[:20],
                                 count=len(recorded))
        self.state.save()
        return changed_inputs

    def stage_base_sha(self, index: int, kind: str = "code", *, git_ready: bool) -> str | None:
        """代码阶段的基线 = 最近上游代码阶段的 commit（评审 B3：基于上游 commit）。

        非代码阶段不需要基线，也不触碰 git（纯报告/审查流程可能没有仓库）。
        """
        if not git_ready or kind != "code":
            return None
        for up in reversed(self.config.stages[:index]):
            sha = self.state.stage(up.name).commit_sha
            if sha:
                return sha
        if not self.config.task_workdir.is_dir():
            return None
        return artifacts.head_sha(self.config.task_workdir, git=self.git)

    def cleanup_worktree(self, worktree: Path | None, *, git_ready: bool) -> None:
        if worktree is not None and git_ready:
            if not artifacts.remove_worktree(self.config.task_workdir, worktree, git=self.git):
                self.state.log_event("worktree_cleanup_failed", "",
                                     path=str(worktree))
                self.state.save()

    def archive(self, spec: StageSpec, result: Result, attempt_dir: Path,
                base_sha: str | None, worktree: Path | None, *, git_ready: bool,
                task: str, review_missing: list[str]) -> str:
        """归档产物，计算 outputs_hash。

        outputs_hash 只基于产物**内容**（文件内容哈希 + 文本），不含 commit SHA/
        路径/时间戳，保证续跑时内容相同则哈希相同（评审 S1）。commit SHA 单独
        存为产物指针（代码类产物 = commit/branch，评审 B3）。
        """
        commit_sha = None
        if spec.kind == "code" and git_ready:
            repo = self.config.task_workdir
            # 按变更清单提交（评审 B4），不用 git add -A 全仓
            files = result.changed_files
            if worktree is not None:
                wt_sha = artifacts.commit_files(worktree, files, f"{spec.name}: {task[:40]}", git=self.git)
                branch = artifacts._git(["rev-parse", "--abbrev-ref", "HEAD"], worktree, git=self.git).stdout.strip()
                if wt_sha is None and artifacts.head_sha(worktree, git=self.git) == base_sha:
                    raise StageArchiveError("代码未发生变化，无法视为一次修复")
                try:
                    commit_sha = artifacts.merge_branch(repo, branch, git=self.git)
                except artifacts.GitError as e:
                    # 快进合并失败（主线已分叉）=> 本阶段失败，不中断整轮
                    raise StageArchiveError(f"合并 {branch} 失败: {e}") from e
                result.branch = branch
            else:
                commit_sha = artifacts.commit_files(repo, files, f"{spec.name}: {task[:40]}", git=self.git)
                if commit_sha is None:
                    raise StageArchiveError("代码未发生变化，无法视为一次修复")
                result.branch = f"stage/{spec.name}"
            result.commit_sha = commit_sha
        # 先归档最终文件；seal 统一对最终文件和提交树计算哈希。
        for rf in result.report_files:
            rf = Path(rf)
            if rf.is_file() and not rf.is_symlink():
                dest = attempt_dir / rf.name
                if rf.resolve() != dest.resolve():
                    shutil.copy2(rf, dest)
        # 原始输出留证
        (attempt_dir / "stdout.txt").write_text(result.stdout or result.text, encoding="utf-8")
        (attempt_dir / "final_text.txt").write_text(result.text, encoding="utf-8")
        if spec.kind == "review":
            # 仅接收本次调用产生的结论，并固定归档到本 attempt。
            sources = [result.verdict_file, attempt_dir / "review_verdict.json",
                       self.state.run_dir / f"{spec.name}_review_verdict.json",
                       self.state.run_dir / "review_verdict.json"]
            for source in sources:
                if source is not None and Path(source).is_file() and not Path(source).is_symlink():
                    dest = attempt_dir / "review_verdict.json"
                    if Path(source).resolve() != dest.resolve():
                        shutil.copy2(source, dest)
                    break
        if spec.kind == 'review':
            from .state import atomic_write_json
            atomic_write_json(attempt_dir/'review-coverage.json', {
                'schema_version': 1, 'complete': not review_missing, 'missing': review_missing,
                'policy': asdict(self.config.review_coverage),
                'scope': 'injected_prompt_files_only', 'channel': result.channel,
                'cli_extra_reads_verified': False})
        from .evidence import seal
        return seal(attempt_dir, self.config.task_workdir, result.commit_sha, git=self.git)
