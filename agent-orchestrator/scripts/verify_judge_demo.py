"""从临时目录逐条执行评委 CLI 路径，核对测试打回、审批及续跑证据。"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHOWCASE = ROOT / 'kiroween-demo.example.toml'
RUN_ID = 'judge-smoke'


def check(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(path.read_bytes())


def verify(directory):
    config = directory / SHOWCASE.name
    shutil.copyfile(SHOWCASE, config)
    run_dir = directory / '.workbench/kiroween-demo-state/runs' / RUN_ID

    def invoke(command, *args, expected=0):
        cmd = [sys.executable, '-X', 'utf8', '-m', 'orchestrator.cli', '--lang', 'en',
               command, str(config), '--run-id', RUN_ID, *args]
        proc = subprocess.run(cmd, cwd=ROOT, timeout=90, capture_output=True,
                              encoding='utf8', errors='replace')
        detail = (proc.stderr or proc.stdout or '').strip()[-400:]
        check(proc.returncode == expected,
              f'{command} exited {proc.returncode}, expected {expected}' + (f': {detail}' if detail else ''))

    invoke('run', '--task', 'Build demo module', expected=11)
    invoke('status')
    stages = read(run_dir / 'state.json')['stages']
    check(stages['acceptance']['status'] == 'awaiting_approval', 'approval gate was not reached')
    for name in ('testing', 'coding'):
        check(len(stages[name]['attempts']) == 2, f'{name} did not run exactly twice')
    tests = run_dir / 'stages/testing/attempts'
    first, second = [read(tests / attempt / 'test_record.json') for attempt in ('1', '2')]
    check(first['exit_code'] == 1 and 'AssertionError' in first['output'], 'first assertion did not fail')
    check(second['exit_code'] == 0 and first['cmd'] == second['cmd'], 'same assertions did not pass')
    events = (run_dir / 'events.jsonl').read_text(encoding='utf8').splitlines()
    check(sum(json.loads(line)['kind'] == 'test_feedback' for line in events) == 1, 'missing or duplicate test feedback')
    review = read(run_dir / 'stages/acceptance/attempts/1/review_verdict.json')
    check(review['verdict'] == 'pass' and review['issues'] == [], 'review did not pass')
    invoke('approve', '--stage', 'acceptance')
    approved = read(run_dir / 'state.json')['stages']
    check(all(stage['status'] == 'done' for stage in approved.values()), 'approval did not finish the run')
    invoke('resume')
    check(read(run_dir / 'state.json')['stages'] == approved, 'resume changed completed attempts')
    print('Judge demo passed: test 1 -> 0, two coding attempts, review passed, approval completed.')


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='judge-demo-') as temp:
        verify(Path(temp))
