"""引擎检查点上的指令消费；权威收据与状态一起提交。"""
from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path

from .storage import Store
from ..state import StageLock


@contextmanager
def command_gate(directory):
    """指令准入与终态发布串行化；只覆盖交接，不阻塞正在执行的阶段。"""
    lock=StageLock(Path(directory)/'.control.lock')
    deadline=time.monotonic()+10
    while not lock.acquire():
        if time.monotonic()>=deadline:
            raise RuntimeError('控制指令交接繁忙，请稍后重试')
        time.sleep(.02)
    try:
        yield
    finally:
        lock.release()


class ControlSignal(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(status)


class Control:
    def __init__(self, database, run_key):
        self.store = Store(database)
        self.run_key = run_key

    def pause_after_worker(self, state):
        """成功 worker 已退出后的最后检查点；调用方持有控制门闩和运行写锁。"""
        for command in self.store.commands(self.run_key):
            if command['kind']!='pause':continue
            receipts=state.control.setdefault('receipts',{})
            if command['id'] in receipts:
                receipt=receipts[command['id']]
            else:
                state.control['paused']=True
                receipt={'status':'applied','result':{'lifecycle':'paused','checkpoint':True},'at':time.time()}
                receipts[command['id']]=receipt
                state.log_event('control_receipt','',command_id=command['id'],intent='pause',status='applied')
                state.save()
            self.store.finish(command['id'],receipt['status'],receipt['result'])
        return state.control.get('paused',False)

    def checkpoint(self, engine):
        state = engine.state
        receipts = state.control.setdefault('receipts', {})
        for command in self.store.commands(self.run_key):
            key, kind, payload = command['id'], command['kind'], command['payload']
            if kind == 'cancel':
                continue  # 活动进程树由 supervisor 拥有与终止。
            if key in receipts:
                receipt = receipts[key]
                self.store.finish(key, receipt['status'], receipt['result'])
                continue
            status, result = 'applied', {}
            try:
                if kind == 'pause':
                    state.control['paused'] = True
                    result = {'lifecycle': 'paused', 'checkpoint': True}
                elif kind == 'resume':
                    if state.control.get('paused'):
                        state.control['paused'] = False
                        result = {'lifecycle': 'running'}
                    else:
                        target = payload.get('wait_target')
                        if not isinstance(target, list) or len(target) != 3 or not isinstance(target[0], str):
                            raise ValueError('缺少等待回执目标')
                        stage = state.stages.get(target[0])
                        if (stage is None or target[0] not in state.pending_waits
                                or stage.status.value not in ('waiting_external', 'waiting_owner')
                                or target[1:] != [stage.current_attempt, stage.outputs_hash]):
                            raise ValueError('等待回执目标已过期')
                        state.control['resume_wait'] = target
                        result = {'stage': target[0], 'attempt': target[1]}
                elif kind == 'approve':
                    spec = engine.config.stage(payload['stage'])
                    stage = state.stage(spec.name)
                    expected = payload['target']
                    actual = {'attempt': stage.current_attempt, 'inputs_hash': stage.inputs_hash,
                              'outputs_hash': stage.outputs_hash}
                    if actual != expected:
                        raise ValueError('审批目标已过期，请刷新当前 attempt')
                    if spec.name not in state.pending_approvals:
                        # 审批已落盘但收据尚未写入的恢复窗口。
                        from ..evidence import approved, validate
                        directory = state.current_attempt_dir(spec.name)
                        record = {'n': stage.current_attempt, 'inputs_hash': stage.inputs_hash,
                                  'outputs_hash': stage.outputs_hash, 'commit_sha': stage.commit_sha}
                        if not directory or not approved(directory, record) or not validate(directory, record, git=engine.git):
                            raise ValueError('不存在有效的待审批产物')
                    elif not engine._approve_owned(spec.name):
                        raise ValueError('产物完整性或输入校验未通过')
                    note = str(payload.get('note') or '').strip()
                    if note:
                        state.control.setdefault('approval_notes', {})[spec.name] = note[:4000]
                    result = {'stage': spec.name, 'target': actual, **({'note': note[:4000]} if note else {})}
                elif kind == 'rerun':
                    if payload.get('revision') != state.revision:
                        raise ValueError('重跑请求的状态版本已变化')
                    engine._reset_from(payload['stage'])
                    state.control['paused'] = False
                    result = {'from_stage': payload['stage']}
                else:
                    raise ValueError('不支持的执行指令')
            except (ValueError, KeyError) as error:
                status, result = 'rejected', {'reason': str(error)}
            receipts[key] = {'status': status, 'result': result, 'at': time.time()}
            state.log_event('control_receipt', payload.get('stage', ''), command_id=key,
                            intent=kind, status=status)
            state.save()
            self.store.finish(key, status, result)
        if state.control.get('cancelled'):
            raise ControlSignal('cancelled')
        if state.control.get('paused'):
            raise ControlSignal('paused')
