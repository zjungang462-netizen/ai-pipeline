"""中断调用的已知下界与未知预留分列；权威结算仍在 run 写锁内完成。"""
import json
import time
from pathlib import Path

from .cli_telemetry import counter


def pending_progress(state,identifier,record):
    try:
        stage,number=identifier.rsplit('/',1)
        if not number.isdigit() or stage!=record['stage']:return {}
        from .safety import relative_target
        path=relative_target(state.run_dir,f'stages/{stage}/attempts/{number}/cli-progress.json')
        if path.stat().st_size>65536:return {}
        value=json.loads(path.read_text(encoding='utf8'))
        if (not isinstance(value,dict) or value.get('schema_version')!=1 or value.get('run_id')!=state.run_id
                or value.get('stage')!=stage or value.get('call_id')!=identifier):return {}
        return value
    except (OSError,ValueError,KeyError,RuntimeError,TypeError):return {}


def recover_pending_usage(state):
    """死进程/取消的 pending 只结算一次；不退款，不把已完成响应丢成零用量。"""
    pending=state.autonomy.pop('pending_calls',{})
    if not pending:return []
    total=state.usage_total
    total.setdefault('reserved_tokens',sum(counter(r.get('tokens')) for r in pending.values()))
    total.setdefault('reported_tokens',max(0,total['tokens']-total['reserved_tokens']))
    recovered=[]
    for identifier,record in pending.items():
        progress=pending_progress(state,identifier,record)
        usage=progress.get('usage',{})
        known=counter(usage.get('known_tokens')) if isinstance(usage,dict) else 0
        stage=record['stage']
        elapsed=min(max(0,time.time()-record['started_at']),record['timeout_s'])
        total['unknown_calls']=total.get('unknown_calls',0)+1
        total['tokens']+=known
        total['reported_tokens']+=known
        total['partial_reported_tokens']=total.get('partial_reported_tokens',0)+known
        total['duration_s']+=elapsed
        used=state.stage_usage.setdefault(stage,{'tokens':0,'duration_s':0.0})
        used['tokens']+=known
        used['duration_s']+=elapsed
        state.log_event('inflight_reserved',stage,call=identifier,reserved_tokens=record['tokens'],known_tokens=known)
        recovered.append((identifier,record))
    return recovered
