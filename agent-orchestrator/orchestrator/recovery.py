"""白名单内、固定基线、单 run 的代码恢复点；半成品从不等同于成功产物。"""
from __future__ import annotations

import json
import hashlib
import os
import time
from dataclasses import asdict
from pathlib import Path

from . import artifacts
from .evidence import seal,validate
from .safety import relative_target
from .state import atomic_write_json,sha256_file,sha256_text

MAX_FILE=2*1024*1024
MAX_TOTAL=6*1024*1024


class RecoveryError(ValueError):pass


def config_hash(config):
    return sha256_text(json.dumps(asdict(config),sort_keys=True,ensure_ascii=False,default=str))


def binding(engine,spec,base):
    adapter=engine.adapters[spec.adapter]
    profile=getattr(adapter,'input_fingerprint',lambda:{})()
    cfg=engine.config
    return {'run_id':engine.run_id,'stage':spec.name,'task_hash':sha256_text(engine.task),
            'config_hash':config_hash(cfg),'base_commit':base,'nonce':engine.state.invalidate_nonce.get(spec.name,0),
            'allowed_files':sorted(cfg.autonomy.allowed_files),'batch_limit':cfg.autonomy.repair_batch_size,
            'profile_hash':sha256_text(json.dumps(profile,sort_keys=True,ensure_ascii=False)),
            'template_hash':sha256_text(engine._read_prompt(spec))}


def capture(engine,spec,worktree,base,number,attempt_dir,reason):
    if not base or not worktree or not Path(worktree).is_dir():return None
    folder=Path(attempt_dir)/'recovery'
    root=Path(worktree).resolve()
    names=artifacts.changed_files_since(root,base)
    if not names:return None
    if len(names)>engine.config.autonomy.repair_batch_size or set(names)-set(engine.config.autonomy.allowed_files):
        raise RecoveryError('未保存恢复点：变更超出本轮白名单或文件批量')
    records=[]
    payloads=[]
    size=0
    for index,name in enumerate(names):
        target=relative_target(root,name)
        lexical=root/Path(name)
        if lexical.is_symlink() or target!=lexical.absolute():raise RecoveryError('恢复点不接受链接路径')
        if target.exists() and not target.is_file():raise RecoveryError('恢复点只接受普通文件或删除')
        if target.is_file() and target.stat().st_size>MAX_FILE:raise RecoveryError('恢复文件超过2MiB')
        body=target.read_bytes() if target.is_file() else None
        size+=len(body or b'')
        if size>MAX_TOTAL:raise RecoveryError('恢复点超过6MiB')
        before=artifacts._git(['rev-parse',f'{base}:{name}'],root,check=False)
        blob=before.stdout.strip() if before.returncode==0 else None
        filename=f'file-{index}.bin' if body is not None else None
        records.append({'path':name,'before_blob':blob,'content':filename,
                        'sha256':sha256_text('') if body is None else hashlib.sha256(body).hexdigest(),
                        'deleted':body is None})
        payloads.append((filename,body))
    if folder.exists():
        # 封存后、清理前崩溃时只接受同一份候选；绝不覆盖旧证据。
        reference=Path(attempt_dir)/'recovery-reference.json'
        try:
            ref=json.loads(reference.read_text(encoding='utf8'))
            metadata=json.loads((folder/'checkpoint.json').read_text(encoding='utf8'))
        except (OSError,ValueError) as error:
            raise RecoveryError('恢复点未完整封存，保留原工作区') from error
        if (not validate(folder,ref) or metadata.get('binding')!=binding(engine,spec,base)
                or metadata.get('files')!=records):
            raise RecoveryError('已有恢复点与现场不一致，保留原工作区')
        engine.state.control.setdefault('recoveries',{})[spec.name]=ref
        engine.state.save()
        return ref
    folder.mkdir(parents=True)
    for filename,body in payloads:
        if filename:(folder/filename).write_bytes(body)
    metadata={'schema_version':1,'binding':binding(engine,spec,base),'files':records,'captured_at':time.time(),
              'source_attempt':number,'reason':reason,'status':'unvalidated_candidate'}
    atomic_write_json(folder/'checkpoint.json',metadata)
    digest=seal(folder)
    ref={'stage':spec.name,'attempt':number,'outputs_hash':digest,'status':'available','files':names}
    atomic_write_json(Path(attempt_dir)/'recovery-reference.json',ref)
    engine.state.control.setdefault('recoveries',{})[spec.name]=ref
    engine.state.log_event('recovery_captured',spec.name,n=number,files=names,outputs_hash=digest)
    engine.state.save()
    return ref


def restore(engine,spec,worktree,base,number,attempt_dir):
    ref=engine.state.control.get('recoveries',{}).get(spec.name)
    if not ref or ref.get('status')=='consumed':return None
    if ref.get('status')=='rejected':raise RecoveryError('上次候选恢复失败，请显式从阶段重跑或使用新任务')
    if not worktree:raise RecoveryError('恢复必须在独立 worktree 内进行')
    folder=engine.state.attempts_dir(spec.name)/str(ref['attempt'])/'recovery'
    if not validate(folder,{'outputs_hash':ref['outputs_hash']}):raise RecoveryError('恢复点内容完整性不匹配')
    try:metadata=json.loads((folder/'checkpoint.json').read_text(encoding='utf8'))
    except (OSError,ValueError) as error:raise RecoveryError('恢复点元数据不可读') from error
    if metadata.get('binding')!=binding(engine,spec,base):raise RecoveryError('恢复点的任务、配置、基线或权限已经变化')
    root=Path(worktree).resolve()
    if artifacts.head_sha(root)!=base or artifacts.changed_files_since(root,base):raise RecoveryError('恢复目标不是干净的原始基线')
    items=metadata.get('files')
    if not isinstance(items,list) or not 1<=len(items)<=engine.config.autonomy.repair_batch_size:raise RecoveryError('恢复文件批量无效')
    prepared=[]
    seen=set()
    total=0
    for item in items:
        name=item.get('path')
        if not isinstance(name,str) or name in seen or name not in engine.config.autonomy.allowed_files:raise RecoveryError('恢复文件超出白名单')
        seen.add(name)
        target=relative_target(root,name)
        if target!=(root/Path(name)).absolute() or (root/Path(name)).is_symlink():raise RecoveryError('恢复目标含链接')
        before=artifacts._git(['rev-parse',f'{base}:{name}'],root,check=False)
        blob=before.stdout.strip() if before.returncode==0 else None
        if blob!=item.get('before_blob'):raise RecoveryError('恢复目标的源文件版本变化')
        if item.get('deleted'):
            body=None
        else:
            source=relative_target(folder,item.get('content',''))
            if not source.is_file() or source.stat().st_size>MAX_FILE:raise RecoveryError('恢复内容缺失或过大')
            if sha256_file(source)!=item.get('sha256'):raise RecoveryError('恢复文件哈希不匹配')
            body=source.read_bytes()
        total+=len(body or b'')
        if total>MAX_TOTAL:raise RecoveryError('恢复总量超限')
        prepared.append((target,body,target.read_bytes() if target.is_file() else None))
    try:
        for target,body,_ in prepared:
            if body is None:target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True,exist_ok=True)
                temporary=target.with_name(target.name+'.recovery-tmp')
                temporary.write_bytes(body)
                os.replace(temporary,target)
    except OSError as error:
        for target,_,original in reversed(prepared):
            try:
                if original is None:target.unlink(missing_ok=True)
                else:target.write_bytes(original)
                target.with_name(target.name+'.recovery-tmp').unlink(missing_ok=True)
            except OSError:pass
        raise RecoveryError('恢复应用失败；隔离工作区不能继续执行') from error
    proof={'source_attempt':ref['attempt'],'outputs_hash':ref['outputs_hash'],'target_attempt':number,
           'base_commit':base,'files':sorted(seen),'requires_testing':True}
    atomic_write_json(Path(attempt_dir)/'recovery-applied.json',proof)
    ref['status']='applied'
    ref['target_attempt']=number
    engine.state.log_event('recovery_applied',spec.name,n=number,source_attempt=ref['attempt'],files=sorted(seen))
    engine.state.save()
    return proof
