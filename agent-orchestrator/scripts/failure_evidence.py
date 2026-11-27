"""unittest 失败前的有界诊断留存；不改变执行结果、预算或临时目录清理规则。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import time
import unittest
import uuid
from pathlib import Path

from orchestrator.safety import MIN_SECRET_BYTES, SECRET_ENV_RE, environment_secrets

ENV_OUTPUT = 'ORCHESTRATOR_TEST_EVIDENCE'
MAX_ENTRIES = 4000
MAX_FILES = 80
MAX_JSON_BYTES = 512 * 1024
MAX_TAIL_BYTES = 16 * 1024
MAX_BUNDLE_BYTES = 2 * 1024 * 1024
JSON_NAMES = {'state.json', 'job.json', 'cli-progress.json', 'execution.json', 'test_record.json'}
LOG_NAMES = {'stdout.txt', 'stderr.txt', 'live-stdout.log', 'live-stderr.log',
             'worker.log', 'manager.log', 'test_output.txt', 'calls.txt'}
SKIP_DIRS = {'.git', '.codex', 'worktree', 'workspace', 'recovery', '__pycache__',
             'node_modules', '.venv', '.test-failures'}
# 敏感变量名与运行日志、API 脱敏共用同一识别规则。
SENSITIVE = SECRET_ENV_RE
# 可识别令牌前缀：长度下限取得保守，避免把普通文本误替换成 [REDACTED]。
TOKEN = re.compile(
    r'\b(?:sk-[A-Za-z0-9_-]{16,}'
    r'|gh[pousr]_[A-Za-z0-9]{24,}'
    r'|github_pat_[A-Za-z0-9_]{24,}'
    r'|AKIA[A-Z0-9]{16}'
    r'|npm_[A-Za-z0-9]{20,}'
    r'|hf_[A-Za-z0-9]{20,}'
    r'|glpat-[A-Za-z0-9_-]{20,}'
    r'|xox[bp]-[A-Za-z0-9-]{10,}'
    r'|AIza[0-9A-Za-z_-]{30,})\b')
URL_AUTH = re.compile(r'(https?://)[^\s/:]+:[^\s/@]+@')
# 凭据值下限（UTF-8 字节）：1-3 字节值常见于配置开关/编号，不做全局子串替换。
# 按字节而非字符计数，多字节的短凭据（如 2 个 CJK 字符）也能被覆盖。


class Scrubber:
    def __init__(self, secrets=(), roots=()):
        # roots 必须是构造参数：先构造再 extend 会让 guard 小于根路径长度，日志尾部残留路径片段。
        values=set()
        for item in (*secrets, *roots):
            value=str(item) if isinstance(item,Path) else item
            if isinstance(value,str) and len(value.encode('utf8'))>=MIN_SECRET_BYTES:
                values.add(value)
        self.guard=max((len(v.encode('utf8')) for v in values),default=0)*2
        self.values=sorted(values,key=len,reverse=True)

    def text(self, text):
        for value in self.values:
            text=text.replace(value,'[REDACTED]')
            text=text.replace(json.dumps(value,ensure_ascii=True)[1:-1],'[REDACTED]')
        return URL_AUTH.sub(r'\1[REDACTED]@',TOKEN.sub('[REDACTED]',text))

    def clean(self, value, depth=0):
        if depth>12:return '[DEPTH_LIMIT]'
        if isinstance(value,str):return self.text(value)[:MAX_TAIL_BYTES]
        if isinstance(value,dict):
            return {self.text(str(k))[:240]:('[REDACTED]' if SENSITIVE.search(str(k)) else self.clean(v,depth+1))
                    for k,v in list(value.items())[:160]}
        if isinstance(value,list):return [self.clean(v,depth+1) for v in value[:160]]
        if value is None or type(value) in (bool,int,float):return value
        return self.text(str(value))[:1000]


class RedactedLog:
    """有界流式脱敏；flush 不泄漏尚未凑齐的凭据或令牌。"""
    MAX_LINE = 64 * 1024

    def __init__(self, stream, secrets=()):
        self.stream = stream
        self.scrub = Scrubber([*environment_secrets(), *secrets])
        self.values = sorted(set(self.scrub.values + [json.dumps(v, ensure_ascii=True)[1:-1]
                                                     for v in self.scrub.values]), key=len, reverse=True)
        self.hold = max((len(v)-1 for v in self.values), default=0)
        self.pending = ''
        self.line = ''
        self.dropping = False
        self.finished = False

    def _lines(self, text, final=False):
        for part in text.splitlines(keepends=True):
            if not self.dropping:
                self.line += part
                if len(self.line) > self.MAX_LINE:
                    self.line = ''
                    self.dropping = True
                    self.stream.write('[REDACTED: oversize log line]\n')
            if part.endswith(('\n', '\r')):
                if not self.dropping:
                    self.stream.write(self.scrub.text(self.line))
                self.line = ''
                self.dropping = False
        if final and self.line:
            self.stream.write(self.scrub.text(self.line))
            self.line = ''

    def _drain(self, final=False):
        cut = len(self.pending) if final else max(0, len(self.pending)-self.hold)
        # 若切点落在某个已知值中间，延迟整段到下次 write。
        while cut and not final:
            before = cut
            for value in self.values:
                start = self.pending.find(value, max(0, cut-len(value)+1))
                if 0 <= start < cut < start+len(value):
                    cut = start
            if before == cut:
                break
        text, self.pending = self.pending[:cut], self.pending[cut:]
        for value in self.values:
            text = text.replace(value, '[REDACTED]')
        self._lines(text, final=final)

    def write(self, text):
        if self.finished:
            raise ValueError('脱敏日志已结束')
        # 限制一次大 write 造成的额外缓存，长行整体省略。
        for start in range(0, len(text), 4096):
            self.pending += text[start:start+4096]
            self._drain()
        return len(text)

    def flush(self):
        self.stream.flush()

    def finish(self):
        if not self.finished:
            self._drain(final=True)
            self.flush()
            self.finished = True

    def isatty(self):
        return False


def _select(name, value):
    if not isinstance(value,dict):raise ValueError('诊断元数据必须是对象')
    if name=='state.json':
        stages=value.get('stages',{})
        stage_keys=('status','current_attempt','error','inputs_hash','outputs_hash','commit_sha')
        events=value.get('events',[])
        event_keys=('kind','stage','n','ts','error_kind','reason','command_id','intent','status','used','limit')
        control=value.get('control',{})
        return {'schema_version':value.get('schema_version'),'revision':value.get('revision'),
                'stages':{k:{field:row.get(field) for field in stage_keys}
                          for k,row in list(stages.items())[:80] if isinstance(row,dict)},
                'usage_total':value.get('usage_total',{}),'stage_usage':value.get('stage_usage',{}),
                'events':[{k:e[k] for k in event_keys if k in e} for e in events[-80:] if isinstance(e,dict)],
                'control':{k:control[k] for k in ('paused','cancelled','cli_deadline') if k in control},
                'review_rounds':value.get('review_rounds',{})}
    keys={
        'job.json':('status','reason','exit_code','worker_pid','started_at','finished_at','max_duration_s',
                    'max_restarts','restarts_used','task_hash','config_hash'),
        'execution.json':('ok','exit_code','error_kind','parse_unknown','usage','tool_version','channel','prompt_hash'),
        'cli-progress.json':('schema_version','run_id','stage','call_id','status','reason','duration_s',
                             'started_at','updated_at','last_output_at','usage','tool','file','truncated'),
        'test_record.json':('ok','exit_code','duration_s','error','failure_ids','truncated','output'),
    }[name]
    result={key:value[key] for key in keys if key in value}
    return result


def classify(records):
    """仅用显式停止原因、错误类别和退出码分类；正文与断言措辞不作推断。"""
    causes=[]
    for record in records:
        value=record.get('data')
        if not isinstance(value,dict):continue
        signals=[]
        def walk(row):
            if isinstance(row,dict):
                for key,item in row.items():
                    if key in ('reason','error','error_kind') and isinstance(item,str):signals.append((key,item))
                    elif isinstance(item,(dict,list)) and key not in ('usage','output'):walk(item)
            elif isinstance(row,list):
                for item in row:walk(item)
        walk(value)
        categories=set()
        for key,signal in signals:
            if signal in ('timeout','call_timeout','idle_timeout') or (key=='error' and signal.startswith('测试超时')):
                categories.add('timeout')
            if signal in ('budget_limit','budget_exceeded','supervisor_wall_budget'):
                categories.add('budget_exhausted')
            if key in ('error','reason') and re.search(
                    r'(?:token|时长).*预算(?:不足|耗尽)|(?:总 token|总时长|时长).*超限|'
                    r'预算(?:超限|耗尽)|原 CLI 运行有效期已过',signal):
                categories.add('budget_exhausted')
            if signal=='crash':categories.add('process_crash')
            if signal=='auth':categories.add('authentication')
            if signal in ('tool_limit','repeated_read_limit','tool_error_limit'):categories.add('tool_limit')
        code=value.get('exit_code')
        if type(code) is int and code!=0 and not categories:
            categories.add('process_nonzero' if code!=-1 else 'unknown')
        for category in sorted(categories):
            causes.append({'category':category,'source':record['path'],'exit_code':code})
    return causes or [{'category':'unknown','source':None,'exit_code':None}]


def _is_link(path):
    info=path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info,'st_file_attributes',0)&0x400)


def _relative_label(index,root,path):
    """诊断包内只保留相对标签，任何源根绝对路径都不写包。"""
    try:
        relative=path.relative_to(root).as_posix()
    except ValueError:
        return 'root-'+str(index)
    return 'root-'+str(index)+('/'+relative if relative and relative!='.' else '')


def _retention_summary(plan):
    """只保留数字和有界原因代码，丢弃任意绝对路径/异常正文。"""
    summary={key:value for key in ('total_bytes','remaining_bytes')
             if type(value:=plan.get(key)) is int and value>=0}
    reasons=[]
    for issue in plan.get('issues',[])[:80]:
        reason=issue.get('reason') if isinstance(issue,dict) else None
        code=reason if isinstance(reason,str) and re.fullmatch(r'[a-z][a-z0-9_]{0,79}',reason) else 'retention_issue'
        if code not in reasons:reasons.append(code)
    summary['issues']=[{'reason':reason} for reason in reasons]
    return summary


def capture(test_id, roots, output=None, secrets=(), failure='', metadata=None):
    """只复制允许的诊断摘要；成功返回前根目录尚未被 TestCase 清理。"""
    target=Path(output or os.environ.get(ENV_OUTPUT) or Path.cwd()/'.test-failures').resolve()
    sources=[]
    for root in roots:
        path=Path(root).absolute()
        if path.exists() and not _is_link(path) and path.is_dir() and path not in sources:
            if target.is_relative_to(path.resolve()):raise ValueError('证据输出不能位于待清理根目录中')
            sources.append(path)
    # 源根路径（含用户名/临时目录名）也必须在构造 Scrubber 时注册，guard 才覆盖其长度；
    # 构造后再追加会让日志尾部读取窗口变短，从而残留根路径片段。
    root_values=[]
    for path in sources:
        resolved=path.resolve()
        root_values.extend((str(path),path.as_posix(),str(resolved),resolved.as_posix()))
    scrub=Scrubber([*environment_secrets(),*secrets],root_values)
    records=[];issues=[];checked=0;approx=0
    for index,root in enumerate(sources):
        pending=[root]
        while pending and checked<MAX_ENTRIES and len(records)<MAX_FILES and approx<MAX_BUNDLE_BYTES//2:
            directory=pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for item in entries:
                        checked+=1
                        if checked>MAX_ENTRIES:break
                        path=Path(item.path)
                        relative=_relative_label(index,root,path)
                        try:
                            if _is_link(path):continue
                            if item.is_dir(follow_symlinks=False):
                                if item.name not in SKIP_DIRS:pending.append(path)
                                continue
                            if not item.is_file(follow_symlinks=False) or item.name not in JSON_NAMES|LOG_NAMES:continue
                            if len(records)>=MAX_FILES or approx>=MAX_BUNDLE_BYTES//2:break
                            size=path.stat().st_size
                            with path.open('rb') as stream:
                                if item.name in JSON_NAMES:
                                    if size>MAX_JSON_BYTES:
                                        issues.append({'path':relative,'reason':'json_size_limit'});continue
                                    raw=stream.read(MAX_JSON_BYTES+1)
                                    value = json.loads(raw)
                                    if item.name == 'state.json' and isinstance(value, dict) and value.get('schema_version') == 3:
                                        from orchestrator.event_journal import read_events
                                        try:
                                            value['events'] = read_events(path.parent, value, max_bytes=MAX_JSON_BYTES)
                                        except (OSError, ValueError, RecursionError):
                                            issues.append({'path': relative, 'reason': 'event_journal_unavailable'})
                                    selected=_select(item.name,value)
                                    data=scrub.clean(selected)
                                    if isinstance(selected.get('output'),str):
                                        data['output']=scrub.text(selected['output'])[-MAX_TAIL_BYTES:]
                                else:
                                    if scrub.guard>MAX_JSON_BYTES:
                                        issues.append({'path':relative,'reason':'redaction_guard_limit'});continue
                                    offset=max(0,size-MAX_TAIL_BYTES-scrub.guard*2)
                                    stream.seek(offset)
                                    text=stream.read(MAX_TAIL_BYTES+scrub.guard*2).decode('utf8',errors='replace')
                                    text=scrub.text(text)
                                    if offset:text=text[scrub.guard:]
                                    data={'tail':text[-MAX_TAIL_BYTES:],'truncated':size>MAX_TAIL_BYTES}
                            record={'path':scrub.text(relative),'bytes':size,'data':data}
                            approx+=len(json.dumps(record,ensure_ascii=True).encode('utf8'))
                            records.append(record)
                        except (OSError,ValueError,TypeError,AttributeError,RecursionError):
                            # 单文件不可读/非法（含深层 JSON 的 RecursionError）只登记 issue。
                            issues.append({'path':scrub.text(relative),'reason':'unreadable_or_invalid'})
            except OSError:
                # 单个目录打不开（权限/消失/杀软占用）不得中止整包采集，继续其它目录与根。
                issues.append({'path':scrub.text(_relative_label(index,root,directory)),
                               'reason':'unreadable_dir'})
    # 原始根路径、环境、配置、提示词、源码树均不写入包；元数据由调用方显式提供。
    payload={'schema_version':1,'test_id':scrub.text(test_id),'created_at':time.time(),
             'failure':scrub.text(failure)[-MAX_TAIL_BYTES:],'metadata':scrub.clean(metadata or {}),
             'roots_available':len(sources),'records':records,'causes':classify(records),
             'issues':issues[:80],'entries_checked':checked,
             'limits':{'files':MAX_FILES,'entries':MAX_ENTRIES,'bundle_bytes':MAX_BUNDLE_BYTES},
             'not_authoritative_execution_evidence':True,'redaction_scope':'known credentials and recognizable token/url patterns'}
    raw=json.dumps(payload,ensure_ascii=True,indent=2).encode('utf8')
    while len(raw)>MAX_BUNDLE_BYTES and payload['records']:
        payload['records'].pop()
        payload['causes']=classify(payload['records'])
        payload['issues']=[{'reason':'bundle_size_limit'}]
        raw=json.dumps(payload,ensure_ascii=True,indent=2).encode('utf8')
    if len(raw)>MAX_BUNDLE_BYTES:raise ValueError('诊断包超过上限')
    from orchestrator.maintenance import boundary, prune_evidence
    from orchestrator.state import StageLock
    boundary(target/'.retention.lock', target)
    target.mkdir(parents=True,exist_ok=True)
    with StageLock(target/'.retention.lock'):
        folder=target/(hashlib.sha256(test_id.encode('utf8')).hexdigest()[:12]+'-'+uuid.uuid4().hex[:12])
        folder.mkdir()
        (folder/'evidence.json').write_bytes(raw)
        try:
            retention=prune_evidence(target, keep=(folder,))
        except (OSError, ValueError):
            # 保留策略不能吞掉本次失败现场；清理错误独立记入诊断包。
            payload['issues'].append({'reason':'retention_cleanup_failed'})
            raw=json.dumps(payload,ensure_ascii=True,indent=2).encode('utf8')
            if len(raw)<=MAX_BUNDLE_BYTES:
                (folder/'evidence.json').write_bytes(raw)
        else:
            payload['retention']=_retention_summary(retention)
            raw=json.dumps(payload,ensure_ascii=True,indent=2).encode('utf8')
            while len(raw)>MAX_BUNDLE_BYTES and payload['records']:
                payload['records'].pop()
                payload['causes']=classify(payload['records'])
                raw=json.dumps(payload,ensure_ascii=True,indent=2).encode('utf8')
            if len(raw)<=MAX_BUNDLE_BYTES:
                from orchestrator.state import atomic_write_json
                atomic_write_json(folder/'evidence.json',payload)
    return folder/'evidence.json'


def _roots(test):
    values=[]
    for name in ('root','base','workspace'):
        value=getattr(test,name,None)
        if isinstance(value,(Path,str)) and value:values.append(Path(value))
    tmp=getattr(test,'tmp',None)
    if isinstance(getattr(tmp,'name',None),str):values.append(Path(tmp.name))
    # 去掉嵌套重复根，避免同一个状态文件收集多次。
    result=[]
    for value in sorted(set(v.absolute() for v in values),key=lambda p:len(p.parts)):
        if not any(value.is_relative_to(p) for p in result):result.append(value)
    return result


def capture_test(test, failure='', output=None):
    if getattr(test,'_failure_evidence_attempted',False):return getattr(test,'failure_evidence',None)
    test._failure_evidence_attempted=True
    try:
        metadata=dict(getattr(test,'evidence_metadata',{}))
        metadata.update(python=sys.version.split()[0],platform=sys.platform,
                        test_duration_s=round(time.monotonic()-getattr(test,'_evidence_started',time.monotonic()),3))
        path=capture(test.id(),_roots(test),output=output,
                     secrets=getattr(test,'_evidence_secrets',()),failure=failure,
                     metadata=metadata)
        test.failure_evidence=str(path)
        return str(path)
    except Exception as error:
        # 留存失败不能吞掉或替换原始测试失败；不输出可能含秘密的异常正文。
        test.failure_evidence_error=type(error).__name__
        return None


class EvidenceTestCase(unittest.TestCase):
    """原样运行 unittest，在已有临时目录清理前留存失败现场。"""
    def run(self,result=None):
        self._failure_evidence_attempted=False
        self._evidence_started=time.monotonic()
        self._evidence_secrets=environment_secrets()
        return super().run(result)

    def register_evidence_secrets(self,*values):
        self._evidence_secrets.extend(values)

    def doCleanups(self):
        result=getattr(getattr(self,'_outcome',None),'result',None)
        records=[*getattr(result,'failures',()),*getattr(result,'errors',())]
        for test,trace in records:
            if test is self or getattr(test,'test_case',None) is self:
                capture_test(self,str(trace))
                break
        return super().doCleanups()


class EvidenceResultMixin:
    """交付入口对任意具有 root/tmp 的测试，在失败回调中提前采集。"""
    evidence_output=None
    def startTest(self,test):
        test._failure_evidence_attempted=False
        test._evidence_started=time.monotonic()
        test._evidence_secrets=environment_secrets()
        return super().startTest(test)

    def _capture(self,test,error):
        parent=getattr(test,'test_case',test)
        self._test_evidence_failures=getattr(self,'_test_evidence_failures',[])
        path=capture_test(parent,self._exc_info_to_string(error,test),self.evidence_output)
        self._test_evidence_failures.append({'test':test.id(),'evidence':path,
            'capture_error':getattr(parent,'failure_evidence_error',None)})

    def addFailure(self,test,error):
        self._capture(test,error)
        return super().addFailure(test,error)

    def addError(self,test,error):
        self._capture(test,error)
        return super().addError(test,error)

    def addSubTest(self,test,subtest,error):
        if error is not None:self._capture(subtest,error)
        return super().addSubTest(test,subtest,error)
