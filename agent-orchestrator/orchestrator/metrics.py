"""终态运行的不可变指标；暂停和锁占用不算一次完成。"""
from __future__ import annotations

import json
import os
import time

from .state import sha256_file, sha256_text


def write_metrics(engine, results: dict[str, str]) -> None:
    if not results or any(v in ("running", "awaiting_approval") for v in results.values()):
        return
    config, state = engine.config, engine.state
    path = state.run_dir / "metrics.json"
    if path.exists():
        return
    stages = {}
    reasons = set()
    for spec in config.stages:
        stage = state.stage(spec.name)
        stages[spec.name] = {"kind": spec.kind, "status": stage.status.value,
                             'attempts': len(stage.attempts),
                             "rounds": state.review_rounds.get(spec.name, 0),
                             "max_rounds": spec.max_rounds, "feedback_to": spec.feedback_to}
        if stage.status.value == "failed":
            if "预算" in (stage.error or "") or "超限" in (stage.error or ""):
                reasons.add("budget_exhausted")
            elif spec.kind == "test":
                reasons.add("verify_nonzero")
            elif spec.kind == "review":
                reasons.add("review_exhausted" if spec.feedback_to else "review_fail")
            else:
                reasons.add("execution_failed")
    tests = [v for v in stages.values() if v["kind"] == "test"]
    reviews = [v for v in stages.values() if v["kind"] == "review"]
    data = {"schema_version": 1, "run_id": engine.run_id, "created_at": time.time(),
            "task_hash": sha256_text(state.task), "stages": stages,
            "rounds": sum(state.review_rounds.values()),
            "status": "failed" if "failed" in results.values() else "done",
            "tokens": state.usage_total["tokens"], "duration_s": state.usage_total["duration_s"],
            "verify_passed": bool(tests) and all(v["status"] == "done" for v in tests),
            "review": ("fail" if any(v["status"] == "failed" for v in reviews)
                       else "pass" if reviews and all(v["status"] == "done" for v in reviews)
                       else "not_run"),
            "fail_reasons": sorted(reasons), "tune_proposal_id": config.tune_proposal_id,
            "config_path": str(config.source_path) if config.source_path else None,
            "config_hash": config.source_hash or None}
    data['reported_tokens'] = state.usage_total.get('reported_tokens', state.usage_total['tokens'])
    data['reserved_tokens'] = state.usage_total.get('reserved_tokens', 0)
    data['unknown_calls'] = state.usage_total.get('unknown_calls', 0)
    data['model_calls'] = sum(e['kind'] == 'model_call_started' for e in state.events)
    data['repair_cycles'] = sum(e['kind'] == 'repair_cycle' for e in state.events)
    # 按实际分配过的 attempt 计数；提示词返工提示不是额外调用的可靠代理。
    data['retries'] = sum(max(0, len({a.n for a in state.stage(s.name).attempts})-1)
                          for s in config.stages)
    data['attempts_total'] = sum(len({a.n for a in state.stage(s.name).attempts}) for s in config.stages)
    data['failed_attempts'] = sum(not a.success for s in config.stages for a in state.stage(s.name).attempts)
    data['retry_count_scope'] = 'actual_allocated_attempts_including_feedback_cycles'
    data['test_attempts'] = sum(e['kind'] == 'test_start' for e in state.events)
    data['failure_history'] = state.autonomy.get('failures', {})
    data['repair_batch_size'] = config.autonomy.repair_batch_size
    data['human_interventions'] = len(state.control.get('discussion_holds', {}))
    # 单写者持有 run 锁，先写完整临时文件，再以不覆盖的链接发布。
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, path)
    except FileExistsError:
        pass
    finally:
        temporary.unlink(missing_ok=True)
