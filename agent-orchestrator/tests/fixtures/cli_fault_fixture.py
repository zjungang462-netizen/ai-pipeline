"""确定性本机 CLI 故障注入器；不会访问任何模型服务。"""
import json
import os
import sys
import time
from pathlib import Path

if '--version' in sys.argv:
    print('1.5.9')
    raise SystemExit(0)

data=Path(os.environ['CLI_FIXTURE_DATA'])
mode=os.environ['CLI_FIXTURE_MODE']
count_file=data/'calls.txt'
count=int(count_file.read_text(encoding='utf8'))+1 if count_file.exists() else 1
count_file.write_text(str(count),encoding='utf8')
body=sys.stdin.buffer.read().decode('utf8')
prompt=json.loads(body)['message']['content']
(data/f'prompt-{count}.txt').write_text(prompt,encoding='utf8')

def emit(event):
    print(json.dumps(event,ensure_ascii=True),flush=True)

def usage(index,tokens=10,cache=20):
    emit({'event':'step_update','step_update':{'conversation_id':'fixture','step_index':index,
        'state':'DONE','step_type':'agent_response','usage':{'total_tokens':tokens,'cache_read_tokens':cache}}})

emit({'event':'init','init':{'model':'local-fault-fixture-not-a-model'}})
if (mode=='timeout' and count==1) or mode=='timeout_always':
    Path('module.py').write_text('VALUE = 1\n',encoding='utf8')
    usage(1)
    time.sleep(30)
elif mode=='timeout':
    assert Path('module.py').read_text(encoding='utf8')=='VALUE = 1\n','checkpoint was not restored'
    Path('module.py').write_text('VALUE = 2\n',encoding='utf8')
elif mode=='repair':
    Path('module.py').write_text('VALUE = 0\n' if count==1 else 'VALUE = 2\n',encoding='utf8')
elif mode=='normal':
    Path('module.py').write_text('VALUE = 2\n',encoding='utf8')
elif mode=='auth':
    print('auth failed',file=sys.stderr,flush=True)
    raise SystemExit(1)
elif mode=='crash':
    usage(1)
    print('injected process failure',file=sys.stderr,flush=True)
    raise SystemExit(7)
elif mode=='tool_loop':
    usage(1)
    for i in range(10):
        emit({'event':'step_update','step_update':{'conversation_id':'fixture','step_index':10+i,
            'state':'ACTIVE','step_type':'tool','tool_name':'read_file','tool_info':{'parameters':{'Path':str(Path('module.py').resolve())}}}})
        time.sleep(.1)
    time.sleep(30)
elif mode=='stream':
    secret=os.environ['CLI_FIXTURE_API_KEY']
    for part in (secret[:7],secret[7:]):
        sys.stdout.write(part)
        sys.stdout.flush()
        time.sleep(.1)
    sys.stdout.write('\n')
    usage(1)
    (data/'ready').write_text('ready',encoding='utf8')
    time.sleep(30)

emit({'event':'result','result':{'status':'SUCCESS','response':'fixture changed module.py; external tests decide completion',
    'usage':{'total_tokens':15,'cache_read_tokens':5}}})
