"""固定配置的单次运行；异常退出由有上限的 supervisor 恢复。"""
import json
import sys
import traceback
from pathlib import Path

from .adapters.factory import build_all
from .config import load_config, ConfigError
from .engine import Engine
from .collaboration.control import ControlSignal


def main():
    try:
        job = json.loads(Path(sys.argv[1]).read_text(encoding='utf8'))
        config = load_config(job['config'])
        if config.source_hash != job['config_hash']:
            raise ConfigError('运行配置已改变')
        adapters = build_all(config.adapters, {s.adapter: s.kind for s in config.stages})
        for name in {s.adapter for s in config.stages if s.kind != 'test'}:
            report = adapters[name].preflight()
            if not report.ok:
                print(json.dumps({'preflight_failed': name, 'detail': report.detail}, ensure_ascii=False))
                return 2
        control = None
        if job.get('control_db'):
            from .collaboration.control import Control
            control = Control(job['control_db'], job['run_key'])
        engine = Engine(config, adapters, job['run_id'], job['task'], control=control)
        result = engine.run(resume_wait=True) if not control and job.get('resume_wait') else engine.run()
        print(json.dumps({'run_id': job['run_id'], 'results': result,
                          'usage': engine.state.usage_total}, ensure_ascii=False), flush=True)
        if any(s == 'waiting_external' for s in result.values()):
            return 13
        if any(s == 'waiting_owner' for s in result.values()):
            return 14
        if job.get('control_db') and any(s == 'awaiting_approval' for s in result.values()):
            return 11
        return 0 if result and all(s == 'done' for s in result.values()) else 1
    except ControlSignal as signal:
        return {'paused': 10, 'cancelled': 12}[signal.status]
    except (ConfigError, ValueError) as error:
        print(str(error), flush=True)
        return 2
    except Exception:
        traceback.print_exc()
        return 70


if __name__ == '__main__':
    raise SystemExit(main())
