"""独立于 Web 生命周期的任务 supervisor 入口。"""
import json
import sys
from pathlib import Path

from .storage import Store
from .projection import directory
from ..supervisor import supervise


if __name__ == '__main__':
    store = Store(sys.argv[1])
    run = store.run(sys.argv[2])
    job = json.loads((directory(run) / 'job.json').read_text(encoding='utf8'))
    result = supervise(Path(job['config']), run['run_id'], job['task'],
                       control_db=store.path, run_key=run['id'])
    print(json.dumps({'status': result['status']}, ensure_ascii=False), flush=True)
