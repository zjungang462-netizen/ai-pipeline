"""CLI 实时脱敏记录、事件去重计量与非权威活动快照。"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from .state import atomic_write_json
from .cli_tools import (READ_TOOLS, WRITE_TOOLS, ToolActivity, codex_activity,
                        file_parameter, read_signature)

MAX_LINE = 2 * 1024 * 1024
MAX_LOG = 16 * 1024 * 1024
_AGY_AUTH_PROMPT = 'Authentication required. Please visit the URL to log in:'
_CODEX_POLICY_REJECTION = re.compile(
    r'^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s+ERROR codex_core::tools::router: '
    r'error=exec_command failed: CreateProcess \{ message: "Rejected\(')


def counter(value):
    # 超大非负计数仍是已知下界，不能截断成零并绕过预算。
    return value if type(value) is int and value >= 0 else 0


class UsageLedger:
    """同一响应按标识取最大值；最终总量与分步累计取较大值，绝不再次相加。"""
    def __init__(self):
        self.responses = {}
        self.final = None
        self.complete = False

    def observe(self, event):
        if not isinstance(event, dict):
            return
        if event.get('type')=='status' and event.get('phase')=='step_end' and isinstance(event.get('usage'),dict):
            usage=event['usage']
            if (type(usage.get('totalTokens')) is int and usage['totalTokens']>=0
                    and type(event.get('turn')) is int and type(event.get('step')) is int):
                # dsh totalTokens 已含缓存，真实协议样本验证过，不能再次相加。
                self._put(('dsh',event['turn'],event['step']),{
                    'tokens':counter(usage['totalTokens']),'cli_reported_tokens':counter(usage['totalTokens']),
                    'cache_read_tokens':counter(usage.get('cacheReadTokens'))})
        elif event.get('type')=='final' and isinstance(event.get('reason'),dict):
            self.complete=event['reason'].get('kind')=='completed' and bool(self.responses)
        elif event.get('type') == 'turn.completed' and isinstance(event.get('usage'), dict):
            usage = event['usage']
            if not all(type(usage.get(k)) is int and usage[k] >= 0 for k in ('input_tokens','output_tokens')):
                return
            # Codex 每个 exec 只有一个用户 turn；可选 turn_id 支持显式多 turn。
            key = ('codex', str(event.get('turn_id') or 'single'))
            values = {'tokens':counter(usage.get('input_tokens'))+counter(usage.get('output_tokens')),
                      'cli_reported_tokens':counter(usage.get('input_tokens'))+counter(usage.get('output_tokens')),
                      'cache_read_tokens':counter(usage.get('cached_input_tokens'))}
            self._put(key,values)
            self.complete = True
        elif event.get('type') == 'turn.started':
            self.complete = False
        elif event.get('event') == 'step_update' and isinstance(event.get('step_update'), dict):
            step=event['step_update']
            if (step.get('state')!='DONE' or step.get('step_type')!='agent_response'
                    or type(step.get('step_index')) is not int or not isinstance(step.get('usage'),dict)):
                return
            usage=step['usage']
            if type(usage.get('total_tokens')) is not int or usage['total_tokens']<0:
                return
            key=('agy',str(step.get('conversation_id') or ''),step['step_index'])
            self._put(key,{'tokens':counter(usage['total_tokens'])+counter(usage.get('cache_read_tokens')),
                           'cli_reported_tokens':counter(usage['total_tokens']),
                           'cache_read_tokens':counter(usage.get('cache_read_tokens'))})
        elif event.get('event')=='result' and isinstance(event.get('result'),dict):
            usage=event['result'].get('usage')
            if isinstance(usage,dict) and type(usage.get('total_tokens')) is int and usage['total_tokens']>=0:
                self.final={'tokens':counter(usage['total_tokens'])+counter(usage.get('cache_read_tokens')),
                            'cli_reported_tokens':counter(usage['total_tokens']),
                            'cache_read_tokens':counter(usage.get('cache_read_tokens'))}
                self.complete=True

    def _put(self,key,values):
        old=self.responses.get(key,{})
        self.responses[key]={k:max(old.get(k,0),v) for k,v in values.items()}

    def observe_claude(self,event):
        """Claude真实message.id重复块去重，最终usage为累积总量，不与分步重复相加。"""
        raw=None;identifier=None;final=False
        if event.get('type')=='assistant' and isinstance(event.get('message'),dict):
            message=event['message'];raw=message.get('usage');identifier=message.get('id')
        elif event.get('type')=='result':raw=event.get('usage');final=True
        if not isinstance(raw,dict):return
        keys=('input_tokens','output_tokens','cache_read_input_tokens','cache_creation_input_tokens')
        if not all(type(raw.get(k)) is int and raw[k]>=0 for k in keys):return
        value={'tokens':sum(raw[k] for k in keys),'cli_reported_tokens':sum(raw[k] for k in keys),
               'cache_read_tokens':raw['cache_read_input_tokens']}
        if final:
            self.final=value
            self.complete=event.get('subtype')=='success' and event.get('is_error') is False and event.get('stop_reason')=='end_turn'
        elif isinstance(identifier,str):
            self._put(('claude',identifier),value);self.complete=False

    def snapshot(self):
        totals={key:sum(row[key] for row in self.responses.values())
                for key in ('tokens','cli_reported_tokens','cache_read_tokens')}
        if self.final:
            totals={k:max(v,self.final[k]) for k,v in totals.items()}
        return {**totals,'known_tokens':totals['tokens'],'completed_responses':len(self.responses),
                'usage_complete':self.complete,'usage_scope':'final_or_completed_responses' if self.complete else 'completed_responses_only'}


class StreamRedactor:
    """先替换已知完整密钥，再保留最长密钥长度的尾部，防止跨 chunk 泄漏。"""
    def __init__(self,secrets=()):
        self.secrets=sorted({v for v in secrets if isinstance(v,str) and len(v)>=8},key=len,reverse=True)
        self.hold=max((len(v)-1 for v in self.secrets),default=0)
        self.pending=''

    def feed(self,text,final=False):
        self.pending+=text
        cut=len(self.pending) if final else max(0,len(self.pending)-self.hold)
        changed=True
        while changed and cut:
            changed=False
            for secret in self.secrets:
                start=self.pending.find(secret,max(0,cut-len(secret)+1))
                while 0<=start<cut:
                    if start+len(secret)>cut:
                        cut=start
                        changed=True
                        break
                    start=self.pending.find(secret,start+1)
        result,self.pending=self.pending[:cut],self.pending[cut:]
        for secret in self.secrets:result=result.replace(secret,'[REDACTED]')
        return result


class CLITrace:
    def __init__(self,ctx,secrets=(),channel=''):
        self.ctx=ctx
        self.channel=channel
        self.stderr_line=''
        self.directory=Path(ctx.attempt_dir) if ctx.attempt_dir else None
        self.lock=threading.RLock()
        self.redactors={name:StreamRedactor(secrets) for name in ('stdout','stderr')}
        self.lines=''
        self.dropping_line=False
        self.ledger=UsageLedger()
        self.started=time.monotonic()
        self.start_wall=time.time()
        self.last_output=self.start_wall
        self.last_save=0.0
        self.revision=0
        self.files={}
        self.written={'stdout':0,'stderr':0}
        self.truncated=False
        self.status='running'
        self.reason=''
        self.tool=''
        self.current_file=''
        self.tool_errors=0
        self.finished_tools=set()
        self.started_tools=set()
        self.dsh_calls={}
        self.dsh_tool_names={}
        self.qoder_tools={}
        self.codex_thread=''
        self.codex_stderr=''
        self.dropping_stderr=False
        self.policy_failures=0
        self.reads={}
        self.closed=False
        self.dirty=True

    def _scrub(self,value):
        value=str(value)
        for secret in self.redactors['stdout'].secrets:
            value=value.replace(secret,'[REDACTED]')
        return value

    def _write(self,stream,text):
        if not self.directory or not text:
            return
        self.directory.mkdir(parents=True,exist_ok=True)
        if stream not in self.files:
            self.files[stream]=(self.directory/('live-'+stream+'.log')).open('w',encoding='utf8',newline='')
        remaining=MAX_LOG-self.written[stream]
        if remaining<=0:
            self.truncated=True
            self.reason=self.reason or 'output_limit'
            return
        encoded=text.encode('utf8')
        if len(encoded)>remaining:
            text=encoded[:remaining].decode('utf8',errors='ignore')
            self.truncated=True
            self.reason=self.reason or 'output_limit'
        self.files[stream].write(text)
        self.files[stream].flush()
        self.written[stream]+=len(text.encode('utf8'))

    def feed(self,stream,text):
        with self.lock:
            if self.closed:return
            self.dirty=True
            self.last_output=time.time()
            self._write(stream,self.redactors[stream].feed(text))
            if stream=='stderr' and self.channel=='cli_agy':
                lines=(self.stderr_line+text).split('\n')
                # 仅匹配反重力已实测的整行登录提示，不把模型正文或普通错误描述当认证失败。
                if any(line.strip()==_AGY_AUTH_PROMPT for line in lines):
                    self.reason=self.reason or 'authentication_required'
                self.stderr_line=lines[-1][-512:]
            if stream=='stderr' and self.channel=='cli_codex':
                self._codex_stderr(text)
            if stream=='stdout':
                self.lines+=text
                while '\n' in self.lines:
                    line,self.lines=self.lines.split('\n',1)
                    if not self.dropping_line:
                        if len(line)>MAX_LINE:
                            self.truncated=True
                            self.reason=self.reason or 'output_limit'
                        else:
                            self._event(line)
                    self.dropping_line=False
                if len(self.lines)>MAX_LINE:
                    self.lines=''
                    self.dropping_line=True
                    self.truncated=True
                    self.reason=self.reason or 'output_limit'
            self._save()

    def _event(self,line):
        try:event=json.loads(line)
        except (ValueError,TypeError):return
        if not isinstance(event,dict):return
        self.ledger.observe(event)
        if self.channel=='cli_claude':self.ledger.observe_claude(event)
        if self.channel in ('cli_qoder','cli_claude'):
            message=event.get('message')
            content=message.get('content',[]) if isinstance(message,dict) else []
            for item in content if isinstance(content,list) else []:
                if not isinstance(item,dict):continue
                if event.get('type')=='assistant' and item.get('type')=='tool_use' and isinstance(item.get('id'),str):
                    name=item.get('name')
                    # 仅映射真实抓取的读/写事件；不推断任意工具调用具有写权限。
                    mapped={'Read':'read_file','Edit':'edit_file','Write':'write_file','Glob':'glob','Grep':'grep'}.get(name)
                    if not mapped:continue
                    key=(self.channel,str(event.get('session_id') or ''),item['id'])
                    parameters=item.get('input',{})
                    if not isinstance(parameters,dict):parameters={}
                    self.qoder_tools[item['id']]=(key,mapped)
                    self._tool_event(ToolActivity(key,mapped,'ACTIVE',parameters,file_parameter(parameters),
                                     read_signature(mapped,parameters) if mapped in READ_TOOLS else '',mapped in WRITE_TOOLS))
                elif event.get('type')=='user' and item.get('type')=='tool_result':
                    previous=self.qoder_tools.get(item.get('tool_use_id'))
                    if previous:
                        key,name=previous
                        self._tool_event(ToolActivity(key,name,'ERROR' if item.get('is_error') else 'DONE',
                                                     writes=name in WRITE_TOOLS))
            return
        if self.channel=='cli_codex':
            if event.get('type')=='thread.started' and isinstance(event.get('thread_id'),str):
                self.codex_thread=event['thread_id']
            activity=codex_activity(event,self.codex_thread)
            if activity:
                self._tool_event(activity)
                return
        if event.get('type')=='tool_call' and isinstance(event.get('callId'),str):
            call=event['callId'];index=self.dsh_calls.setdefault(call,len(self.dsh_calls)+1)
            self.dsh_tool_names[call]=event.get('tool')
            event={'step_update':{'conversation_id':'dsh','step_index':index,'step_type':'tool','state':'ACTIVE',
                                 'tool_name':event.get('tool'),'tool_info':{'parameters':event.get('input',{})}}}
        elif (event.get('type')=='tool_result' and isinstance(event.get('callId'),str)
              and event['callId'] in self.dsh_calls):
            event={'step_update':{'conversation_id':'dsh','step_index':self.dsh_calls[event['callId']],
                                 'tool_name':self.dsh_tool_names.get(event['callId']),
                                 'step_type':'tool','state':'DONE' if event.get('status')=='completed' else 'ERROR'}}
        step=event.get('step_update')
        if not isinstance(step,dict) or step.get('step_type')!='tool':return
        index=step.get('step_index')
        if type(index) is not int:return
        key=(str(step.get('conversation_id') or ''),index)
        info=step.get('tool_info') if isinstance(step.get('tool_info'),dict) else {}
        tool=str(step.get('tool_name') or info.get('name') or '')[:80]
        parameters=info.get('parameters') if isinstance(info.get('parameters'),dict) else {}
        self._tool_event(ToolActivity(key,tool,step.get('state'),parameters,file_parameter(parameters),
                         read_signature(tool,parameters) if tool in READ_TOOLS else '',tool in WRITE_TOOLS))

    def _codex_stderr(self,text,final=False):
        lines=(self.codex_stderr+text).split('\n')
        self.codex_stderr=lines.pop()
        if final:
            lines.append(self.codex_stderr)
            self.codex_stderr=''
        for line in lines:
            # 此前置策略拒绝发生在命令启动前，没有 JSON item，不能漏计。
            # 只接受真机确认的日志前缀，不解析模型正文或任意 stderr 错误词。
            if not self.dropping_stderr and len(line)<=MAX_LINE and _CODEX_POLICY_REJECTION.match(line):
                self.policy_failures+=1
                self._tool_event(ToolActivity(('codex_policy',self.policy_failures),
                                              'command_execution','ERROR'))
            self.dropping_stderr=False
        if len(self.codex_stderr)>MAX_LINE:
            self.codex_stderr=''
            self.dropping_stderr=True

    def _tool_event(self,activity):
        key=activity.key
        if key in self.finished_tools or activity.state not in ('ACTIVE','DONE','ERROR'):return
        self.tool=self._scrub(activity.name)[:80]
        filename=activity.filename
        self.current_file=''
        if filename:
            try:
                root=Path(self.ctx.workdir).resolve()
                target=Path(filename)
                # 文件工具的相对路径以子进程任务目录为准，不能借用编排器自身的 cwd。
                if not target.is_absolute():target=root/target
                self.current_file=self._scrub(target.resolve().relative_to(root).as_posix())[:240]
            except (OSError,ValueError):self.current_file='[工作区外路径]'
        if key not in self.started_tools:
            self.started_tools.add(key)
            if activity.read_signature and activity.state in ('ACTIVE','DONE'):
                signature=activity.read_signature
                self.reads[signature]=self.reads.get(signature,0)+1
                if self.reads[signature]>self.ctx.max_repeated_reads:self.reason=self.reason or 'repeated_read_limit'
        if activity.state in ('DONE','ERROR'):
            self.finished_tools.add(key)
            self.tool_errors=self.tool_errors+1 if activity.state=='ERROR' else 0
            if activity.state=='DONE' and activity.writes:
                self.reads.clear()
            if self.tool_errors>=self.ctx.max_tool_errors:self.reason=self.reason or 'tool_error_limit'

    def snapshot(self):
        usage=self.ledger.snapshot()
        return {'schema_version':1,'run_id':self.ctx.run_id,'stage':self.ctx.stage,'call_id':self.ctx.call_id,
                'revision':self.revision,'status':self.status,'reason':self.reason,
                'started_at':self.start_wall,'last_output_at':self.last_output,'updated_at':time.time(),
                'duration_s':round(time.monotonic()-self.started,3),'tool':self.tool,'file':self.current_file,
                'consecutive_tool_errors':self.tool_errors,'usage':usage,'truncated':self.truncated}

    def _save(self,force=False):
        now=time.monotonic()
        if not force and now-self.last_save<0.25:return
        self.last_save=now
        self.dirty=False
        self.revision+=1
        if self.directory:
            self.directory.mkdir(parents=True,exist_ok=True)
            atomic_write_json(self.directory/'cli-progress.json',self.snapshot())

    def tick(self):
        with self.lock:
            usage=self.ledger.snapshot()
            limit=self.ctx.remaining_tokens
            if limit is not None:
                charge=usage['tokens']+(0 if usage['usage_complete'] else self.ctx.token_reservation)
                if charge>limit:self.reason=self.reason or 'budget_limit'
            if time.time()-self.last_output>self.ctx.idle_timeout_s:self.reason=self.reason or 'idle_timeout'
            if self.dirty or time.monotonic()-self.last_save>=2:self._save(force=not self.dirty)
            return self.reason

    def finish(self,status,reason=''):
        with self.lock:
            if self.closed:return
            if self.lines and not self.dropping_line:self._event(self.lines)
            if self.channel=='cli_codex':self._codex_stderr('',final=True)
            for name,redactor in self.redactors.items():self._write(name,redactor.feed('',final=True))
            self.status='stopped' if status=='exited' and self.reason else status
            self.reason=self.reason or reason
            self._save(force=True)
            for stream in self.files.values():stream.close()
            self.closed=True


def partial_usage(stdout):
    ledger=UsageLedger()
    for line in stdout.splitlines():
        try:ledger.observe(json.loads(line))
        except (ValueError,TypeError):continue
    return {**ledger.snapshot(),'usage_unknown':True}
