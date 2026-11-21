"""核心数据契约。

引擎只依赖本模块的 Result / StageSpec / RunContext，不感知任何工具的输出格式。
零外部依赖：仅标准库。
"""
from __future__ import annotations

from .schema import config_field

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class ErrorKind(str, Enum):
    NONE = "none"
    TIMEOUT = "timeout"
    AUTH = "auth"
    PARSE_FAILED = "parse_failed"
    CRASH = "crash"
    RATE_LIMIT = "rate_limit"      # 429/5xx，可退避重试（评审 M6）
    REJECTED = "rejected"          # 预算超限 / 空跑 / 越界，主动拒绝
    INTERRUPTED = "interrupted"    # 进程被 kill，attempt 无 success.json
    TOOL_LIMIT = 'tool_limit'      # 工具错误或重复读取达到有界阈值


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    STALE = "stale"                # inputs_hash 变化，必须重跑
    AWAITING_APPROVAL = "awaiting_approval"
    WAITING_EXTERNAL = "waiting_external"
    WAITING_OWNER = "waiting_owner"
    INTERRUPTED = "interrupted"
    REVIEW_FEEDBACK = "review_feedback"   # L2：审查不通过，已反馈待返工


@dataclass
class Result:
    """适配器统一返回契约（评审 M1）。

    changed_files 为空 => 不得判完成（防 CLI 退出 0 但空跑）。
    代码类产物以 commit_sha 表达；报告类产物以 report_files 表达。
    """
    ok: bool
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    text: str = ""                              # 最终文本（解析失败时=原始输出）
    changed_files: list[str] = field(default_factory=list)
    commit_sha: str | None = None
    branch: str | None = None
    report_files: list[Path] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)   # tokens/cost/duration
    error_kind: ErrorKind = ErrorKind.NONE
    channel: str = ""                           # api / cli_<tool> / fake
    tool_version: str | None = None
    parse_unknown: bool = False                 # 版本不认识/解析失败 => 禁止判完成
    verdict_file: Path | None = None            # 当前 attempt 的独立审查结论
    deferred: dict[str, Any] | None = None       # 通过 stage_result.json 请求显式等待

    def is_meaningful(self, kind: str = "code") -> bool:
        """按产物类型判定是否有可验收成果（评审 B4）。

        code 阶段：必须有 changed_files 或 commit_sha（纯文字不算）。
        report 阶段：必须有 report_files（纯文字不算）。
        """
        # 解析未知 => 一律不得判完成（自审查 B4：贯彻 parse_unknown 契约）
        if self.parse_unknown:
            return False
        if kind == "code":
            return bool(self.changed_files or self.commit_sha)
        if kind == "test":
            # 测试阶段产物 = 执行记录（退出码/耗时/输出），不是模型作文
            return bool(self.report_files)
        return bool(self.report_files)


@dataclass
class StageSpec:
    name: str = config_field('唯一阶段名；字母数字、下划线、点或横线，禁止父目录段')
    adapter: str = config_field('非 test 阶段必填，引用具名适配器', input_default="", required=True)
    prompt: str = config_field('非 test 阶段必填；提示词文件或内联文本', input_default="", required=True)
    timeout_s: int = config_field('单次执行超时，秒', default=300, numeric=True, minimum=0.001)
    approval: bool = config_field('成功产物是否等待人工审批', default=False)
    max_attempts: int = config_field('本轮适配器调用尝试次数上限', default=2, numeric=True, integer=True, minimum=1)
    max_tokens: int | None = config_field('该阶段累计 token 上限；空表示无阶段级上限', default=None, numeric=True, integer=True, minimum=0)
    max_duration_s: int | None = config_field('该阶段累计计量秒数上限；空表示无阶段级上限', default=None, numeric=True, minimum=0)
    kind: str = config_field('code/report/review/test；verify 是 test 的兼容别名', default='code')
    config_keys: dict[str, Any] = config_field('参与输入指纹的可 JSON 序列化配置表', default_factory=dict)
    # L2 审查反馈环（仅 kind=review 生效）
    feedback_to: str | None = config_field('review/test 反馈目标，必须是之前的阶段', default=None)
    max_rounds: int = config_field('反馈轮次上限，含首轮', default=1, numeric=True, integer=True, minimum=1)
    allow_wait: bool = config_field('允许 report 阶段返回结构化等待回执', default=False)
    # kind=test：不调用模型，真实执行命令；非零退出码即失败
    test_cmd: list[str] = config_field('test 必填的非空 argv 数组，其它类型禁止设置', default_factory=list)
    context_files: list[str] = config_field('显式上下文相对文件路径列表', default_factory=list)
    precheck_cmd: list[str] = config_field('仅 code 可设，运行前真实检查命令 argv', default_factory=list)


@dataclass
class RunContext:
    task: str
    run_id: str
    stage: str
    workdir: Path
    runs_dir: Path
    upstream_commits: list[str] = field(default_factory=list)
    upstream_reports: list[Path] = field(default_factory=list)
    allow_yolo: bool = False
    timeout_s: int = 300
    kind: str = ""                              # 当前阶段产物类型；空则用适配器自身 kind
    feedback: str = ""                          # L2：审查反馈文本（注入编码阶段）
    attempt_dir: Path | None = None
    allow_wait: bool = False
    output_tokens: int | None = None
    token_reservation: int = 0
    call_id: str | None = None
    base_commit: str | None = None
    remaining_tokens: int | None = None
    idle_timeout_s: float = 120
    max_tool_errors: int = 3
    max_repeated_reads: int = 4
    context_files: tuple[str, ...] = ()
    editable_files: tuple[str, ...] = ()


@dataclass
class PreflightReport:
    adapter: str
    ok: bool
    exe_path: str | None = None
    tool_version: str | None = None
    detail: str = ""
    needs_login: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "ok": self.ok,
            "exe_path": self.exe_path,
            "tool_version": self.tool_version,
            "detail": self.detail,
            "needs_login": self.needs_login,
        }
