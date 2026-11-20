"""从非权威 CLI 快照生成有界只读活动；不启动执行器，不修改执行状态。"""
import json
import math
from pathlib import Path

from .safety import relative_target
from .cli_telemetry import counter


def read_activities(run, state, *, read_progress=None):
    result=[]
    for identifier,record in state.get('autonomy',{}).get('pending_calls',{}).items():
        if not record.get('channel','').startswith('cli_'):continue
        try:
            stage,number=identifier.rsplit('/',1)
            if stage!=record['stage'] or not number.isdigit():continue
            root=Path(run['state_dir'])/'runs'/run['run_id']
            path=relative_target(root,f'stages/{stage}/attempts/{number}/cli-progress.json')
            if read_progress is None:
                if path.stat().st_size>65536:continue
                value=json.loads(path.read_text(encoding='utf8'))
            else:
                value = read_progress(path, max_bytes=65536)
            if (not isinstance(value,dict) or value.get('schema_version')!=1 or value.get('run_id')!=run['run_id']
                    or value.get('call_id')!=identifier or value.get('stage')!=stage):continue
            if value.get('status') not in ('running','stopped','exited','failed','interrupted'):continue
            usage=value.get('usage')
            if not isinstance(usage,dict):continue
            activity={'stage':stage,'call_id':identifier,'channel':record['channel'],'revision':counter(value.get('revision')),
                      'status':value['status'],'tool':str(value.get('tool') or '')[:80],
                      'file':str(value.get('file') or '')[:240],'reason':str(value.get('reason') or '')[:80],
                      'known_tokens':counter(usage.get('known_tokens')),'cache_read_tokens':counter(usage.get('cache_read_tokens')),
                      'usage_complete':usage.get('usage_complete') is True,'not_delivery_evidence':True}
            for key in ('last_output_at','updated_at','duration_s'):
                raw=value.get(key)
                activity[key]=raw if type(raw) in (float,int) and math.isfinite(raw) and raw>=0 else 0
            result.append(activity)
        except (OSError,ValueError,TypeError,KeyError,RuntimeError):continue
    return result
