"""离线、确定性的轮次提案。只写副本，批准前不影响任何 run。"""
from __future__ import annotations

import json
import re
import statistics
import time
import uuid
import hashlib
import tomllib
from pathlib import Path

from .config import ConfigError, load_config
from .state import StageLock, atomic_write_json, sha256_file


def propose(state_dir: Path, config_path: Path, last: int = 20) -> dict:
    if type(last) is not int or last < 5:
        raise ConfigError("tune 至少需要最近 5 条运行记录")
    config_path = config_path.resolve()
    cfg = load_config(config_path)
    config_hash = sha256_file(config_path)
    records = []
    for path in (state_dir / "runs").glob("*/metrics.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (isinstance(data, dict) and data.get("config_hash") == config_hash
                    and data.get('unknown_calls', 0) == 0):
                if data.get('human_interventions', 0) == 0:
                    records.append(data)
        except (OSError, ValueError):
            continue
    records.sort(key=lambda r: r.get("created_at", 0), reverse=True)
    records = records[:last]
    baseline = None
    inherited_no_improvement = 0
    if cfg.tune_proposal_id:
        receipt_path = state_dir / "tuning" / f"{cfg.tune_proposal_id}.approved.json"
        if receipt_path.is_file():
            receipt = _json_object(receipt_path)
            baseline = receipt.get("baseline")
            inherited_no_improvement = receipt.get("no_improvement_count", 0)
    median = statistics.median(r.get("tokens", 0) for r in records) if records else 0
    failure_rate = sum(r.get("status") == "failed" for r in records) / len(records) if records else 0
    no_improvement = (inherited_no_improvement + 1
                      if len(records) >= 5 and baseline and failure_rate >= baseline["failure_rate"]
                      else 0)
    stopped = (len(records) >= 5 and baseline and baseline["tokens_median"] > 0
               and median > baseline["tokens_median"] * 1.2)
    changes = []
    if len(records) >= 5 and not stopped and no_improvement < 2:
        if (cfg.autonomy.enabled and cfg.autonomy.repair_batch_size < cfg.autotune.max_batch_size
                and sum(r.get('repair_cycles', 0) >= 1 and r.get('status') == 'done' for r in records) >= 5):
            changes.append({'stage': None, 'section': 'autonomy', 'key': 'repair_batch_size',
                            'old': cfg.autonomy.repair_batch_size,
                            'new': cfg.autonomy.repair_batch_size + 1,
                            'reason': '至少五次成功运行需要返工；在预授权文件内试用更大批量减少调用'})
        for spec in cfg.stages:
            if not spec.feedback_to:
                continue
            relevant = [r.get("stages", {}).get(spec.name, {}) for r in records]
            if not all(relevant):
                continue
            # 成功首轮已足够时，只降低最坏开销上限，不宣称降低已发生的成本。
            if (spec.max_rounds > 1
                    and all(r.get('status') == 'done' for r in records)
                    and all(s.get("status") == "done" and s.get("rounds", 0) <= 1 for s in relevant)
                    and all(r.get('repair_cycles', 0) == 0 for r in records)):
                changes.append({"stage": spec.name, "key": "max_rounds",
                                "old": spec.max_rounds, "new": spec.max_rounds - 1,
                                "reason": "最近样本均首轮完成，建议降低最坏轮次上限"})
            elif (spec.kind == "review" and spec.max_rounds < 10
                  and sum(r.get("verify_passed") is True
                          and "review_exhausted" in r.get("fail_reasons", []) for r in records) >= 5):
                changes.append({"stage": spec.name, "key": "max_rounds",
                                "old": spec.max_rounds, "new": spec.max_rounds + 1,
                                "reason": "至少五次测试已通过但审查耗尽；仅提议人工评估增加一轮"})
        changes.extend(_repeated_prompt_checks(state_dir, records, cfg))
        changes.extend(_uncollected_test_candidates(state_dir, records, cfg))
    if no_improvement >= 2:
        reason = "连续两次策略调整未降低失败率，停止产出调整建议"
    elif stopped:
        reason = "token 中位数较提案基线增加超过 20%，停止产出调整建议"
    elif len(records) < 5:
        reason = "同一配置的有效终态样本不足 5 条"
    else:
        reason = "只提议 max_rounds、重复问题提示词检查项或登记过的测试命令；不修改模型、预算或源码"
    proposal = {"schema_version": 1, "id": "tune-" + uuid.uuid4().hex[:12],
                "created_at": time.time(), "config_path": str(config_path),
                "config_hash": config_hash, "sample_run_ids": [r["run_id"] for r in records],
                "baseline": {"tokens_median": median, "failure_rate": failure_rate},
                "no_improvement_count": no_improvement,
                "changes": changes, "reason": reason}
    directory = state_dir / "tuning"
    atomic_write_json(directory / f"{proposal['id']}.json", proposal)
    atomic_write_json(directory / "tune_proposal.json", proposal)
    return proposal


def _repeated_prompt_checks(state_dir: Path, records: list[dict], cfg) -> list[dict]:
    """Repeated, located review findings may suggest a human-reviewed coding prompt check."""
    if len(records) < 5:
        return []
    runs_dir = (Path(state_dir) / "runs").resolve()
    findings: dict[tuple[str, str, str, str], set[str]] = {}
    for record in records:
        run_id = record.get("run_id")
        if not isinstance(run_id, str) or not run_id or Path(run_id).name != run_id:
            continue
        run_dir = (runs_dir / run_id).resolve()
        if run_dir.parent != runs_dir:
            continue
        for review in (stage for stage in cfg.stages if stage.kind == "review" and stage.feedback_to):
            attempts = run_dir / "stages" / review.name / "attempts"
            if not attempts.is_dir():
                continue
            candidates = sorted((p for p in attempts.iterdir() if p.is_dir() and p.name.isdigit()),
                                key=lambda p: int(p.name), reverse=True)
            for attempt in candidates:
                try:
                    verdict = json.loads((attempt / "review_verdict.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(verdict, dict) or verdict.get("verdict") != "fail":
                    continue
                issues = verdict.get("issues")
                if not isinstance(issues, list):
                    continue
                for issue in issues:
                    if not isinstance(issue, dict):
                        continue
                    file = issue.get("file")
                    problem = issue.get("problem")
                    expected = issue.get("expected")
                    if not all(isinstance(value, str) and value.strip() for value in (file, problem, expected)):
                        continue
                    file, problem, expected = (" ".join(value.split()) for value in (file, problem, expected))
                    if len(file) > 240 or len(problem) > 300 or len(expected) > 300:
                        continue
                    findings.setdefault((review.feedback_to, file, problem, expected), set()).add(run_id)
                break

    suggestions = []
    suggested_stages = set()
    for (target_name, file, problem, expected), run_ids in sorted(findings.items()):
        if len(run_ids) < 5:
            continue
        if target_name in suggested_stages:
            continue
        target = cfg.stage(target_name)
        if not target.prompt:
            continue
        source = (cfg.base_dir / target.prompt).resolve()
        try:
            source.relative_to(cfg.base_dir.resolve())
            source_hash = sha256_file(source)
        except (OSError, ValueError):
            continue
        if not source.is_file():
            continue
        check = (f"- 完成 `{file}` 相关改动前，核对：{expected}")
        suggestions.append({"stage": target.name, "key": "prompt_check", "old": target.prompt,
                            "source_hash": source_hash, "check": check,
                            "evidence": {"file": file, "problem": problem, "expected": expected,
                                         "distinct_runs": len(run_ids),
                                         "run_ids": sorted(run_ids)}})
        suggested_stages.add(target_name)
        if len(suggestions) == 5:
            break
    return suggestions


def _uncollected_test_candidates(state_dir: Path, records: list[dict], cfg) -> list[dict]:
    """Suggest a registered argv command only after repeated zero-test terminal failures."""
    if len(records) < 5:
        return []
    runs_dir = (Path(state_dir) / "runs").resolve()
    suggestions = []
    for spec in (stage for stage in cfg.stages if stage.kind == "test"):
        candidates = cfg.autotune.test_command_candidates.get(spec.name, [])
        candidate = next((item for item in candidates if item != spec.test_cmd), None)
        if candidate is None:
            continue
        failed_runs = []
        for record in records:
            run_id = record.get("run_id")
            if (not isinstance(run_id, str) or not run_id or Path(run_id).name != run_id
                    or record.get("stages", {}).get(spec.name, {}).get("status") != "failed"):
                continue
            run_dir = (runs_dir / run_id).resolve()
            if run_dir.parent != runs_dir:
                continue
            attempts = run_dir / "stages" / spec.name / "attempts"
            if not attempts.is_dir():
                continue
            numbered = sorted((p for p in attempts.iterdir() if p.is_dir() and p.name.isdigit()),
                              key=lambda p: int(p.name), reverse=True)
            for attempt in numbered:
                try:
                    test_record = json.loads((attempt / "test_record.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                output = test_record.get("output", "") if isinstance(test_record, dict) else ""
                error = test_record.get("error", "") if isinstance(test_record, dict) else ""
                if (isinstance(output, str) and isinstance(error, str)
                        and re.search(r"\bRan\s+0\s+tests\b|\bcollected\s+0\s+items\b|\bno\s+tests\s+ran\b",
                                      output, flags=re.IGNORECASE)
                        or error == "测试未收集到任何用例"):
                    failed_runs.append(run_id)
                break
        failed_runs = sorted(set(failed_runs))
        if len(failed_runs) < 5:
            continue
        suggestions.append({"stage": spec.name, "key": "test_cmd_candidate",
                            "old": list(spec.test_cmd), "new": list(candidate),
                            "evidence": {"kind": "zero_tests_collected",
                                         "distinct_runs": len(failed_runs),
                                         "run_ids": failed_runs}})
    return suggestions


def approve_proposal(state_dir: Path, proposal_path: Path, output: Path) -> Path:
    proposal_path, output = proposal_path.resolve(), output.resolve()
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    if not isinstance(proposal, dict):
        raise ConfigError("提案必须是 JSON 对象")
    if (not isinstance(proposal.get("config_path"), str)
            or not isinstance(proposal.get("config_hash"), str)
            or not isinstance(proposal.get("baseline"), dict)
            or not isinstance(proposal.get("sample_run_ids"), list)):
        raise ConfigError("提案缺少配置、基线或样本信息")
    identifier = proposal.get("id", "")
    if not re.fullmatch(r"tune-[0-9a-f]{12}", identifier):
        raise ConfigError("非法提案标识")
    lock = StageLock(state_dir / "tuning" / ".lock")
    with lock:
        original = Path(proposal["config_path"]).resolve()
        if output == original or output.exists():
            raise ConfigError("优化配置必须写入不存在的副本路径")
        if output.parent != original.parent:
            raise ConfigError("配置副本须与原文件同目录，保持所有相对路径语义")
        content = original.read_bytes()
        if hashlib.sha256(content).hexdigest() != proposal.get("config_hash"):
            raise ConfigError("原配置已变化，请重新生成提案")
        from .config import parse_config
        text = content.decode("utf-8")
        cfg = parse_config(tomllib.loads(text), base_dir=original.parent)
        changes = proposal.get("changes")
        if not isinstance(changes, list) or not changes:
            raise ConfigError("没有可批准的调整")
        # 按 TOML 阶段块替换数值，其他字段和注释逐字保留。
        spans = list(re.finditer(r"(?m)^\s*\[\[stages\]\]\s*(?:#.*)?$", text))
        edits = []
        seen = set()
        prompt_files = []
        for change in changes:
            if not isinstance(change, dict):
                raise ConfigError("提案条目必须是对象")
            if change.get('key') == 'repair_batch_size':
                old, new = change.get('old'), change.get('new')
                if ('batch' in seen or not cfg.autonomy.enabled or type(old) is not int
                        or type(new) is not int or old != cfg.autonomy.repair_batch_size
                        or new != old + 1 or new > cfg.autotune.max_batch_size):
                    raise ConfigError('修复批量提案越过预授权范围')
                seen.add('batch')
                section = re.search(r'(?m)^\[autonomy\]\s*$', text)
                if not section:
                    raise ConfigError('配置中缺少显式 autonomy 表')
                start = section.end()
                next_section = re.search(r'(?m)^\s*\[', text[start:])
                end = start + next_section.start() if next_section else len(text)
                field = re.search(r'(?m)^(\s*repair_batch_size\s*=\s*)(\d+)', text[start:end])
                if field:
                    edits.append((start + field.start(2), start + field.end(2), str(new)))
                else:
                    edits.append((end, end, f'\nrepair_batch_size = {new}\n'))
                continue
            name = change.get("stage")
            spec = cfg.stage(name)
            if change.get("key") == "prompt_check":
                evidence = change.get("evidence")
                valid_evidence = (isinstance(evidence, dict)
                                  and all(isinstance(evidence.get(key), str) and evidence[key].strip()
                                          for key in ("file", "problem", "expected"))
                                  and len(evidence.get("file", "")) <= 240
                                  and len(evidence.get("problem", "")) <= 300
                                  and len(evidence.get("expected", "")) <= 300
                                  and type(evidence.get("distinct_runs")) is int
                                  and evidence["distinct_runs"] >= 5
                                  and isinstance(evidence.get("run_ids"), list)
                                  and all(isinstance(run_id, str) and Path(run_id).name == run_id
                                          for run_id in evidence["run_ids"])
                                  and len(set(evidence["run_ids"])) >= 5)
                expected_check = (f"- 完成 `{evidence['file']}` 相关改动前，核对：{evidence['expected']}"
                                  if valid_evidence else None)
                if (name in seen or not spec.prompt or change.get("old") != spec.prompt
                        or not valid_evidence or change.get("check") != expected_check
                        or not isinstance(change.get("source_hash"), str)):
                    raise ConfigError("提示词提案与当前配置不匹配")
                source = (cfg.base_dir / spec.prompt).resolve()
                try:
                    source.relative_to(cfg.base_dir.resolve())
                except ValueError as error:
                    raise ConfigError("提示词提案只能修改配置目录内的文件") from error
                if not source.is_file() or sha256_file(source) != change["source_hash"]:
                    raise ConfigError("提示词文件已变化，请重新生成提案")
                candidate_name = f"{original.stem}.{identifier}.{name}.prompt.md"
                candidate_path = original.parent / candidate_name
                if candidate_path.exists():
                    raise ConfigError("提示词候选文件已存在，拒绝覆盖")
                prompt_files.append((candidate_path, source, change["check"]))
                seen.add(name)
                index = cfg.stage_index(name)
                start = spans[index].end()
                end = spans[index + 1].start() if index + 1 < len(spans) else len(text)
                next_table = re.search(r"(?m)^\s*\[", text[start:end])
                if next_table:
                    end = start + next_table.start()
                block = text[start:end]
                field = re.search(r'(?m)^(\s*prompt\s*=\s*)("(?:\\.|[^"\\])*"|\'[^\']*\')', block)
                if not field:
                    raise ConfigError("阶段提示词字段无法安全定位")
                edits.append((start + field.start(2), start + field.end(2), json.dumps(candidate_name)))
                continue
            if change.get("key") == "test_cmd_candidate":
                evidence = change.get("evidence")
                run_ids = evidence.get("run_ids") if isinstance(evidence, dict) else None
                registered = cfg.autotune.test_command_candidates.get(name, [])
                old, new = change.get("old"), change.get("new")
                if (name in seen or spec.kind != "test" or old != spec.test_cmd
                        or not isinstance(new, list) or new not in registered or new == old
                        or not isinstance(evidence, dict) or evidence.get("kind") != "zero_tests_collected"
                        or type(evidence.get("distinct_runs")) is not int
                        or evidence["distinct_runs"] < 5 or not isinstance(run_ids, list)
                        or not all(isinstance(run_id, str) and Path(run_id).name == run_id for run_id in run_ids)
                        or len(set(run_ids)) < 5):
                    raise ConfigError("测试命令提案不在登记候选范围内或证据不足")
                index = cfg.stage_index(name)
                start = spans[index].end()
                end = spans[index + 1].start() if index + 1 < len(spans) else len(text)
                next_table = re.search(r"(?m)^\s*\[", text[start:end])
                if next_table:
                    end = start + next_table.start()
                block = text[start:end]
                array = r'\[(?:[^\[\]"\']|"(?:\\.|[^"\\])*"|\'[^\']*\')*\]'
                field = re.search(r'(?m)^(\s*test_cmd\s*=\s*)(' + array + r')', block)
                if not field:
                    raise ConfigError("阶段 test_cmd 字段无法安全定位")
                edits.append((start + field.start(2), start + field.end(2), json.dumps(new, ensure_ascii=False)))
                seen.add(name)
                continue
            old, new = change.get("old"), change.get("new")
            if (name in seen or change.get("key") != "max_rounds" or not spec.feedback_to
                    or type(old) is not int or type(new) is not int
                    or old != spec.max_rounds or abs(new - old) != 1
                    or not 1 <= new <= max(10, old)):
                raise ConfigError("提案越过允许范围：只允许现有反馈阶段的 max_rounds 增减 1")
            seen.add(name)
            index = cfg.stage_index(name)
            start = spans[index].end()
            end = spans[index + 1].start() if index + 1 < len(spans) else len(text)
            block = text[start:end]
            # 最后一个 stages 块后可能跟 adapters；禁止把插入项写进 adapters 表。
            next_table = re.search(r"(?m)^\s*\[", block)
            if next_table:
                end = start + next_table.start()
                block = text[start:end]
            field = re.search(r"(?m)^(\s*max_rounds\s*=\s*)(\d+)", block)
            if field:
                edits.append((start + field.start(2), start + field.end(2), str(new)))
            else:
                prefix = "" if text[:end].endswith("\n") else "\n"
                edits.append((end, end, f"{prefix}max_rounds = {new}\n"))
        for start, end, replacement in sorted(edits, reverse=True):
            text = text[:start] + replacement + text[end:]
        # 只追加提案来源标识，作为后续跨 run 比较的依据。
        if "tune_proposal_id" in tomllib.loads(text):
            text = re.sub(r'(?m)^tune_proposal_id\s*=.*$', f'tune_proposal_id = "{identifier}"', text)
        else:
            text = f'tune_proposal_id = "{identifier}"\n' + text
        parse_config(tomllib.loads(text), base_dir=original.parent)
        created = []
        try:
            for candidate_path, source, check in prompt_files:
                with candidate_path.open("x", encoding="utf-8", newline="") as stream:
                    stream.write(source.read_text(encoding="utf-8").rstrip() +
                                 "\n\n## 历史问题检查项（人工批准）\n" + check + "\n")
                created.append(candidate_path)
            with output.open("x", encoding="utf-8", newline="") as stream:
                stream.write(text)
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        atomic_write_json(state_dir / "tuning" / f"{identifier}.approved.json", {
            "proposal_id": identifier, "approved_at": time.time(), "output": str(output),
            "baseline": proposal["baseline"], "source_run_ids": proposal["sample_run_ids"],
            "no_improvement_count": proposal.get("no_improvement_count", 0),
            'source_config': str(original), 'source_config_hash': proposal['config_hash'],
            'output_hash': sha256_file(output),
        })
        return output


def policy_tune(config_path: Path, expected_hash: str | None = None) -> dict:
    """在一次已结束运行之外生成下一次配置，不修改当前运行或原文件。"""
    config_path = config_path.resolve()
    cfg = load_config(config_path)
    if not cfg.autonomy.enabled or not cfg.autotune.enabled:
        return {'status': 'disabled'}
    if expected_hash and cfg.source_hash != expected_hash:
        raise ConfigError('运行期间配置已变化，拒绝自动调优')
    lock = StageLock(cfg.state_dir / 'tuning' / '.policy.lock')
    with lock:
        previous_pointer = _policy_pointer(cfg.state_dir, config_path)
        if previous_pointer.is_file():
            previous = _json_object(previous_pointer)
            if previous.get('status') == 'rolled_back' and previous.get('source_hash') == cfg.source_hash:
                return {'status': 'frozen', 'changes': [], 'reason': '已退化回退的策略禁止自动重复试用，请人工修改策略后重新评估'}
        source_manifest = {p.as_posix(): sha256_file(p)
                           for p in Path(__file__).parent.rglob('*.py')}
        proposal = propose(cfg.state_dir, config_path, max(20, cfg.autotune.min_runs))
        count = len(proposal['sample_run_ids'])
        result = {'status': 'proposed', 'proposal_id': proposal['id'],
                  'sample_count': count, 'changes': proposal['changes'], 'reason': proposal['reason']}
        if cfg.autotune.auto_apply and count >= cfg.autotune.min_runs and proposal['changes']:
            # 纯策略预授权：允许白名单字段，且绝不自动提高阶段轮数。
            safe = [c for c in proposal['changes']
                    if (c['key'] == 'max_rounds' and c['new'] < c['old'])
                    or c['key'] == 'repair_batch_size']
            if safe:
                proposal['changes'] = safe
                file = cfg.state_dir / 'tuning' / f"{proposal['id']}.json"
                atomic_write_json(file, proposal)
                output = config_path.with_name(config_path.stem + '.' + proposal['id'] + '.toml')
                approve_proposal(cfg.state_dir, file, output)
                result.update(status='applied_to_copy', config_path=str(output), changes=safe)
                pointer = {
                    'source': str(config_path), 'source_hash': cfg.source_hash,
                    'candidate': str(output), 'candidate_hash': sha256_file(output),
                    'proposal_id': proposal['id'], 'baseline': proposal['baseline'],
                    'status': 'trial'}
                atomic_write_json(_policy_pointer(cfg.state_dir, config_path), pointer)
                atomic_write_json(cfg.state_dir / 'tuning' / 'next-config.json', pointer)
        if any(not Path(p).is_file() or sha256_file(Path(p)) != h for p, h in source_manifest.items()):
            raise RuntimeError('策略优化不允许修改引擎源码')
        atomic_write_json(cfg.state_dir / 'tuning' / 'policy-result.json', result)
        return result


def select_policy_config(config_path: Path) -> Path:
    """下一次启动按哈希选择试用副本，指标退化时回到其来源配置。"""
    original = config_path.resolve()
    current = original
    visited = set()
    while current not in visited:
        visited.add(current)
        cfg = load_config(current)
        if not (cfg.autonomy.enabled and cfg.autotune.auto_apply):
            return current
        if cfg.tune_proposal_id:
            receipt_path = cfg.state_dir / 'tuning' / f'{cfg.tune_proposal_id}.approved.json'
            if receipt_path.is_file():
                receipt = _json_object(receipt_path)
                if receipt.get('output_hash') != cfg.source_hash:
                    raise ConfigError('策略副本已被修改，拒绝自动选择')
                trial = propose(cfg.state_dir, current, max(20, cfg.autotune.min_runs))
                if (len(trial['sample_run_ids']) >= cfg.autotune.min_runs
                        and ('20%' in trial['reason'] or '连续两次' in trial['reason'])):
                    source = Path(receipt['source_config']).resolve()
                    if sha256_file(source) != receipt['source_config_hash']:
                        raise ConfigError('回退源配置已被修改')
                    parent = _policy_pointer(cfg.state_dir, source)
                    record = _json_object(parent)
                    record['status'] = 'rolled_back'
                    atomic_write_json(parent, record)
                    return source
        file = _policy_pointer(cfg.state_dir, current)
        if not file.is_file():
            return current
        record = _json_object(file)
        candidate = Path(record['candidate']).resolve()
        if Path(record['source']).resolve() != current:
            return current
        if (record.get('status') == 'rolled_back' or cfg.source_hash != record['source_hash']
                or candidate.parent != current.parent or sha256_file(candidate) != record['candidate_hash']):
            return current
        current = candidate
    raise ConfigError('自动策略配置出现循环')


def _policy_pointer(state_dir, source):
    identifier = hashlib.sha256(str(Path(source).resolve()).encode('utf8')).hexdigest()[:16]
    return state_dir / 'tuning' / f'next-{identifier}.json'


def _json_object(path):
    value = json.loads(Path(path).read_text(encoding='utf8'))
    if not isinstance(value, dict):
        raise ConfigError(f'策略记录必须是 JSON 对象: {Path(path).name}')
    return value
