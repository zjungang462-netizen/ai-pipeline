"""干净目录离线验收：示例、HTTP/CLI 冒烟、样本指纹与原有全部测试。"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.failure_evidence import (EvidenceResultMixin, Scrubber,
                                      environment_secrets)


def digest_files():
    # demo/ 属于交付声明范围，源码指纹必须覆盖，否则演示入口改动无法被前后比对发现。
    files=[p for directory in ('orchestrator','tests','prompts','scripts','demo')
           for p in (ROOT/directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    files+=list(ROOT.glob('*.example.toml'))
    files+=[p for p in (ROOT/'README.md',ROOT/'RELEASE_NOTES.md',ROOT/'DELIVERY.md',ROOT/'.gitattributes') if p.exists()]
    return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


def redact_verification(proof, secrets=()):
    """verification.json 中失败/错误正文按已知环境凭据与可识别令牌/URL 规则脱敏。

    只替换最终报告里的 traceback/reason 文本，不触碰测试结果、跳过标记或退出码；
    主回归日志由 unittest stream 直接写入，保持原始内容（脱敏落差由 DELIVERY 说明）。
    """
    scrub=Scrubber([*environment_secrets(),*secrets])
    for key in ('failures','errors'):
        for item in proof.get(key) or []:
            if isinstance(item,dict) and isinstance(item.get('traceback'),str):
                item['traceback']=scrub.text(item['traceback'])
    for item in proof.get('skipped') or []:
        if isinstance(item,dict) and isinstance(item.get('reason'),str):
            item['reason']=scrub.text(item['reason'])
    return proof


def smoke(output):
    from orchestrator.config import load_config
    from orchestrator.dashboard.server import create_server
    result={'python':platform.python_version(),'utf8_mode':sys.flags.utf8_mode,
            'filesystem_encoding':sys.getfilesystemencoding(),'git_available':bool(shutil.which('git')),
            'node_available':bool(shutil.which('node')),'examples':[],'cli':[]}
    if sys.version_info<(3,11) or not result['git_available'] or not result['node_available']:
        raise RuntimeError('完整离线验收需要 Python 3.11+、Git 和 Node.js')
    env=dict(os.environ)
    env.pop('PYTHONPATH',None)
    for tool in ('git','node'):
        run=subprocess.run([tool,'--version'],cwd=ROOT,env=env,capture_output=True,text=True,
                           encoding='utf8',errors='replace',timeout=15)
        if run.returncode:raise RuntimeError(tool+' 版本探测失败')
        result[tool+'_version']=run.stdout.strip()
    for path in sorted(ROOT.glob('*.example.toml')):
        config=load_config(path)
        for stage in config.stages:
            # 只核对实际提示词文件；内联提示词由配置加载器校验。
            if stage.prompt.startswith('prompts/') and not (ROOT/stage.prompt).is_file():
                raise RuntimeError('示例提示词缺失: '+stage.prompt)
        result['examples'].append({'name':path.name,'stages':len(config.stages)})
    provenance=json.loads((ROOT/'tests/fixtures/cli-capture-provenance.json').read_text(encoding='utf8'))
    for name,record in provenance.items():
        if hashlib.sha256((ROOT/'tests/fixtures'/name).read_bytes()).hexdigest()!=record['fixture_sha256']:
            raise RuntimeError('样本指纹不一致: '+name)
    result['fixture_provenance']='all_matched'
    run_id='delivery-'+os.urandom(4).hex()
    commands=[(['--help'],0),(['dashboard','--help'],0),
              (['run','workbench.example.toml','--task','离线交付验收','--run-id',run_id],11),
              (['status','workbench.example.toml','--run-id',run_id],0),
              (['approve','workbench.example.toml','--run-id',run_id,'--stage','acceptance'],0),
              (['resume','workbench.example.toml','--run-id',run_id],0)]
    for number,(args,expected) in enumerate(commands,1):
        run=subprocess.run([sys.executable,'-X','utf8','-m','orchestrator.cli',*args],cwd=ROOT,env=env,
                           capture_output=True,text=True,encoding='utf8',errors='replace',timeout=120)
        (output/f'cli-{number}.log').write_text(run.stdout+'\n'+run.stderr,encoding='utf8')
        result['cli'].append({'command':args[0],'expected_exit':expected,'actual_exit':run.returncode})
        if run.returncode!=expected:raise RuntimeError('CLI 冒烟失败: '+args[0])
    config=load_config(ROOT/'workbench.example.toml')
    state=json.loads((config.state_dir/'runs'/run_id/'state.json').read_text(encoding='utf8'))
    result['offline_stages']={name:{'status':s['status'],'attempt':s['current_attempt']}
                              for name,s in state['stages'].items()}
    if any(s['status']!='done' or s['current_attempt']!=1 for s in state['stages'].values()):
        raise RuntimeError('离线示例未复用有效 attempt')
    server=create_server(output/'smoke.sqlite3',[ROOT/'workbench.example.toml'],0)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=20)
    try:
        connection.request('POST','/api/session',json.dumps({'token':server.application.bootstrap}),
                           {'Content-Type':'application/json','Origin':server.application.origin})
        response=connection.getresponse();session=json.loads(response.read())
        if response.status!=200 or not session.get('csrf'):raise RuntimeError('HTTP 登录失败')
        cookie=response.getheader('Set-Cookie').split(';')[0]
        connection.request('GET','/api/projects',headers={'Cookie':cookie})
        response=connection.getresponse();projects=json.loads(response.read())
        if response.status!=200 or len(projects.get('projects',[]))!=1:
            raise RuntimeError('HTTP 项目列表失败')
        result['http']={'session_status':200,'projects_status':200,'projects':1,'loopback_only':True}
    finally:
        connection.close();server.application.closed.set()
        server.shutdown();server.server_close();thread.join(5)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    output=Path(args.output).resolve()
    if output.is_relative_to(ROOT):raise ValueError('验证输出目录必须位于项目之外')
    output.mkdir(parents=True,exist_ok=True)
    os.chdir(ROOT)
    before=digest_files()
    (output/'source-before.json').write_text(json.dumps(before,ensure_ascii=False,indent=2),encoding='utf8')
    started=time.monotonic()
    info=smoke(output)
    (output/'smoke.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'smoke':'passed','examples':len(info['examples']),'git':info['git_version'],
                      'node':info['node_version']},ensure_ascii=True),flush=True)
    durations=[]
    class Result(EvidenceResultMixin,unittest.TextTestResult):
        evidence_output=output/'failure-evidence'
        def startTest(self,test):
            self.started=time.monotonic()
            super().startTest(test)
            self.stream.flush()
        def stopTest(self,test):
            durations.append({'test':test.id(),'duration_s':round(time.monotonic()-self.started,3)})
            super().stopTest(test);self.stream.flush()
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'))
    print(json.dumps({'tests_collected':suite.countTestCases()}),flush=True)
    with (output/'full-regression.log').open('w',encoding='utf8') as log:
        with contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
            result=unittest.TextTestRunner(stream=log,verbosity=2,resultclass=Result).run(suite)
    info['failure_evidence']=getattr(result,'_test_evidence_failures',[])
    after=digest_files()
    (output/'source-after.json').write_text(json.dumps(after,ensure_ascii=False,indent=2),encoding='utf8')
    (output/'test-durations.json').write_text(json.dumps(durations,ensure_ascii=False,indent=2),encoding='utf8')
    proof={'tests_run':result.testsRun,'failures':[{'test':t.id(),'traceback':s} for t,s in result.failures],
           'errors':[{'test':t.id(),'traceback':s} for t,s in result.errors],
           'skipped':[{'test':t.id(),'reason':s} for t,s in result.skipped],
           'source_unchanged':before==after,'duration_s':round(time.monotonic()-started,3),
           'runtime':info,'source_files':len(before),'no_model_calls_requested':True}
    redact_verification(proof)
    proof['redaction']={'verification_json':'known_credentials_and_token_patterns',
                        'full_regression_log':'raw_not_redacted'}
    proof['exit_code']=0 if result.wasSuccessful() and before==after and not result.skipped else 1
    (output/'verification.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({k:v for k,v in proof.items() if k!='runtime'},ensure_ascii=True),flush=True)
    return proof['exit_code']


if __name__=='__main__':
    raise SystemExit(main())
