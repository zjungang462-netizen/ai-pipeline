"""受控失败经验沉淀：从历史审查与测试失败中提取高频缺陷模式，经审批后注入检查清单。

安全约束：
- 只读扫描已封存的 review_verdict.json 与 test_record.json，绝不修改历史运行产物。
- 仅统计达到 min_occurrences 阈值的高频模式，不把偶发单次失败固化为规则。
- 必须经显式 approve 写入 approved_lessons.json 才会在后续提示词中生效。
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import time
from typing import Any

from .cli_telemetry import StreamRedactor
from .state import atomic_write_json, sha256_text

APPROVED_LESSONS_FILE = 'approved_lessons.json'
MAX_APPROVED_ITEMS = 12
MAX_ITEM_CHARS = 240


def _clean_text(value: str, max_len: int = MAX_ITEM_CHARS) -> str:
    text = ' '.join(str(value or '').split()).strip()
    if len(text) > max_len:
        text = text[: max_len - 1] + '…'
    return text


def _iter_attempts(state_dir: Path):
    runs_dir = Path(state_dir) / 'runs'
    if not runs_dir.is_dir():
        return
    for run_dir in sorted(runs_dir.iterdir()):
        stages_dir = run_dir / 'stages'
        if not stages_dir.is_dir():
            continue
        for stage_dir in sorted(stages_dir.iterdir()):
            attempts_dir = stage_dir / 'attempts'
            if not attempts_dir.is_dir():
                continue
            for attempt_dir in sorted(attempts_dir.iterdir()):
                if attempt_dir.is_dir():
                    yield run_dir.name, stage_dir.name, attempt_dir


def propose_lessons(
    state_dir: Path,
    *,
    min_occurrences: int = 2,
    max_items: int = MAX_APPROVED_ITEMS,
    secrets: tuple[str, ...] = (),
) -> dict[str, Any]:
    """扫描历史运行中的审查 fail 问题与测试 failure_ids，生成待审批的高频经验提案。"""
    if type(min_occurrences) is not int or min_occurrences < 1:
        raise ValueError('min_occurrences 必须为正整数')
    if type(max_items) is not int or not 1 <= max_items <= 50:
        raise ValueError('max_items 必须在 1..50 之间')

    root = Path(state_dir).resolve()
    redactor = StreamRedactor(secrets)
    review_counts: Counter[tuple[str, str]] = Counter()
    review_examples: dict[tuple[str, str], dict[str, Any]] = {}
    test_counts: Counter[str] = Counter()
    scanned_runs: set[str] = set()

    for run_id, _stage, attempt_dir in _iter_attempts(root):
        scanned_runs.add(run_id)
        verdict_file = attempt_dir / 'review_verdict.json'
        if verdict_file.is_file():
            try:
                data = json.loads(verdict_file.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                data = None
            if isinstance(data, dict) and data.get('verdict') == 'fail' and isinstance(data.get('issues'), list):
                for issue in data['issues']:
                    if not isinstance(issue, dict):
                        continue
                    rel = _clean_text(issue.get('file', ''), 120) or '(未指定文件)'
                    problem = _clean_text(redactor.feed(str(issue.get('problem', '')), final=True), 160)
                    expected = _clean_text(redactor.feed(str(issue.get('expected', '')), final=True), 160)
                    if not problem:
                        continue
                    key = (rel, problem.casefold())
                    review_counts[key] += 1
                    review_examples.setdefault(key, {'file': rel, 'problem': problem, 'expected': expected})

        test_file = attempt_dir / 'test_record.json'
        if test_file.is_file():
            try:
                rec = json.loads(test_file.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                rec = None
            if isinstance(rec, dict) and rec.get('ok') is False and isinstance(rec.get('failure_ids'), list):
                for fid in rec['failure_ids']:
                    cleaned = _clean_text(redactor.feed(str(fid), final=True), 120)
                    if cleaned:
                        test_counts[cleaned] += 1

    items: list[dict[str, Any]] = []
    for key, count in review_counts.most_common():
        if count < min_occurrences:
            continue
        sample = review_examples[key]
        advice = f"[{sample['file']}] 避免：{sample['problem']}"
        if sample['expected']:
            advice += f"；期望：{sample['expected']}"
        items.append({
            'id': 'lesson-' + sha256_text(f"review:{sample['file']}:{sample['problem']}")[:12],
            'source': 'review_verdict',
            'occurrences': count,
            'file': sample['file'],
            'checklist': _clean_text(advice, MAX_ITEM_CHARS),
        })

    for fid, count in test_counts.most_common():
        if count < min_occurrences:
            continue
        items.append({
            'id': 'lesson-' + sha256_text(f'test:{fid}')[:12],
            'source': 'test_failure',
            'occurrences': count,
            'file': '',
            'checklist': _clean_text(f'重点回归历史高频失败测试：{fid}', MAX_ITEM_CHARS),
        })

    items.sort(key=lambda row: (-row['occurrences'], row['id']))
    selected = items[:max_items]
    proposal = {
        'schema_version': 1,
        'created_at': round(time.time(), 3),
        'state_dir': str(root),
        'min_occurrences': min_occurrences,
        'scanned_runs': len(scanned_runs),
        'items': selected,
    }
    proposal['proposal_hash'] = sha256_text(json.dumps(selected, ensure_ascii=False, sort_keys=True))
    return proposal


def approve_lessons(
    state_dir: Path,
    proposal: dict[str, Any],
    *,
    max_items: int = MAX_APPROVED_ITEMS,
) -> Path:
    """校验提案哈希并将已批准的高频检查清单原子写入 state_dir/approved_lessons.json。"""
    if not isinstance(proposal, dict) or proposal.get('schema_version') != 1:
        raise ValueError('无效的经验提案格式')
    items = proposal.get('items')
    if not isinstance(items, list):
        raise ValueError('经验提案缺少 items 列表')
    expected_hash = sha256_text(json.dumps(items, ensure_ascii=False, sort_keys=True))
    if proposal.get('proposal_hash') != expected_hash:
        raise ValueError('经验提案内容哈希不匹配，拒绝批准')

    sanitized: list[dict[str, Any]] = []
    for item in items[:max_items]:
        if not isinstance(item, dict) or not isinstance(item.get('checklist'), str) or not item['checklist'].strip():
            raise ValueError('经验条目缺少有效 checklist 文本')
        sanitized.append({
            'id': str(item.get('id') or sha256_text(item['checklist'])[:12]),
            'source': str(item.get('source') or 'manual'),
            'occurrences': int(item.get('occurrences') or 1),
            'checklist': _clean_text(item['checklist'], MAX_ITEM_CHARS),
        })

    target = Path(state_dir).resolve() / APPROVED_LESSONS_FILE
    atomic_write_json(target, {
        'schema_version': 1,
        'approved_at': round(time.time(), 3),
        'proposal_hash': expected_hash,
        'items': sanitized,
    })
    return target


def load_approved_lessons_text(state_dir: Path, *, max_items: int = MAX_APPROVED_ITEMS) -> str:
    """读取已批准的检查清单文本；文件缺失或损坏时安全返回空字符串，不影响主流程。"""
    target = Path(state_dir) / APPROVED_LESSONS_FILE
    if not target.is_file() or target.is_symlink():
        return ''
    try:
        data = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return ''
    if not isinstance(data, dict) or data.get('schema_version') != 1 or not isinstance(data.get('items'), list):
        return ''
    lines = []
    for item in data['items'][:max_items]:
        if isinstance(item, dict) and isinstance(item.get('checklist'), str) and item['checklist'].strip():
            lines.append('- ' + _clean_text(item['checklist'], MAX_ITEM_CHARS))
    if not lines:
        return ''
    return '### 已批准的历史高频缺陷检查清单（仅供参考，不授予额外写权限）\n' + '\n'.join(lines)
