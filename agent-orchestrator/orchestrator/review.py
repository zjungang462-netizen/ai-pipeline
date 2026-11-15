"""审查结论：只认独立结论文件，不从报告正文推断。

约定（见 prompts/review.md）：审查阶段把结论写到
    runs/<run_id>/review_verdict.json
字段固定：
    {"verdict": "pass" | "fail",
     "issues": [{"file": "相对路径", "problem": "问题", "expected": "期望"}]}

引擎只对该文件做 json.loads + 严格校验：
- 文件缺失、JSON 非法、verdict 非 pass/fail、issues 结构不合法 => 待人工确认。
- Markdown 报告只给人看，**不参与判定**。

success.json 只表示「这次 attempt 的产物已归档」，不表达审查是否通过。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

VERDICT_FILENAME = "review_verdict.json"


@dataclass
class Issue:
    """机器可执行的反馈条目：文件 + 问题 + 期望。"""
    file: str = ""
    problem: str = ""
    expected: str = ""

    def has_location(self) -> bool:
        return bool(self.file.strip())

    def to_text(self) -> str:
        s = f"{self.file}: {self.problem}" if self.file else self.problem
        if self.expected:
            s += f"（期望: {self.expected}）"
        return s.strip(": ")

    def to_dict(self) -> dict[str, str]:
        return {"file": self.file, "problem": self.problem, "expected": self.expected}


@dataclass
class ReviewVerdict:
    verdict: str                                  # "pass" | "fail"
    issues: list[Issue] = field(default_factory=list)
    source: str = ""                              # 结论文件路径（留证）

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    def located_issues(self) -> list[Issue]:
        """只有带文件位置的条目才可用于自动返工。"""
        return [i for i in self.issues if i.has_location()]

    def issue_texts(self) -> list[str]:
        return [i.to_text() for i in self.issues]


class VerdictError(ValueError):
    """结论文件缺失或不合法 => 调用方置待人工确认。"""


def verdict_path(runs_dir: Path, stage: str = "") -> Path:
    """结论文件固定路径。多审查阶段时按阶段名区分。"""
    base = Path(runs_dir)
    return base / (f"{stage}_{VERDICT_FILENAME}" if stage else VERDICT_FILENAME)


def _parse_issue(item: object) -> Issue:
    if isinstance(item, str):
        # 纯字符串没有文件位置 => 保留但不可用于自动返工
        if not item.strip():
            raise VerdictError("issues 含空条目")
        return Issue(problem=item.strip())
    if not isinstance(item, dict):
        raise VerdictError(f"issues 条目必须是对象或字符串，收到 {type(item).__name__}")
    file = str(item.get("file") or item.get("location") or "").strip()
    problem = str(item.get("problem") or item.get("issue") or "").strip()
    expected = str(item.get("expected") or item.get("expect") or "").strip()
    if not problem:
        raise VerdictError("issues 条目缺少 problem")
    return Issue(file=file, problem=problem, expected=expected)


def load_verdict(path: Path, workdir: Path | None = None) -> ReviewVerdict:
    """严格读取结论文件。任何不合法都抛 VerdictError（=> 待人工确认）。"""
    path = Path(path)
    if not path.is_file():
        raise VerdictError(f"结论文件缺失: {path.name}（审查未给出可判定结论）")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise VerdictError(f"结论文件不是合法 JSON: {e}") from e
    if not isinstance(data, dict):
        raise VerdictError("结论文件根必须是对象")
    v = str(data.get("verdict", "")).strip().lower()
    if v not in ("pass", "fail"):
        raise VerdictError(f"verdict 必须是 pass 或 fail，收到 {v!r}")
    raw_issues = data.get("issues", [])
    if raw_issues is None:
        raw_issues = []
    if not isinstance(raw_issues, list):
        raise VerdictError("issues 必须是数组")
    issues = [_parse_issue(x) for x in raw_issues]
    from .safety import relative_target, WriteRootViolation
    for issue in issues:
        if issue.file:
            try:
                relative_target(workdir or Path.cwd(), issue.file)
            except WriteRootViolation as error:
                raise VerdictError(str(error)) from error
    # 一致性：pass 却带问题 => 视为不合法，交人工（不替模型猜）
    if v == "pass" and issues:
        raise VerdictError("verdict=pass 但 issues 非空，结论自相矛盾")
    return ReviewVerdict(verdict=v, issues=issues, source=str(path))


def format_feedback(verdict: ReviewVerdict, round_no: int) -> str:
    """只输出机器可执行的条目（文件/问题/期望），不重贴需求全文。"""
    lines = [f"## 审查反馈（第 {round_no} 轮，结论：不通过）",
             "只修复以下条目，不要改动无关文件："]
    for i, issue in enumerate(verdict.located_issues(), 1):
        lines.append(f"{i}. 文件 `{issue.file}` — {issue.problem}")
        if issue.expected:
            lines.append(f"   期望：{issue.expected}")
    return "\n".join(lines)


def format_test_feedback(record: dict, round_no: int) -> str:
    """测试失败直接作为反馈，不经审查模型复述。"""
    lines = [f"## 测试失败反馈（第 {round_no} 轮）",
             f"命令：{' '.join(record.get('cmd', []))}",
             f"退出码：{record.get('exit_code')}　耗时：{record.get('duration_s', 0):.1f}s",
             "请修复导致失败的代码（输出节选）：", "```",
             (record.get("output") or "")[-4000:], "```"]
    if record.get('failure_ids'):
        lines += ['失败用例标识：'] + record['failure_ids']
    return "\n".join(lines)
