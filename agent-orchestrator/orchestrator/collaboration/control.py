"""引擎检查点上的指令消费；权威收据与状态一起提交。"""
from __future__ import annotations

import time

from .storage import Store


class ControlSignal(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(status)


class Control:
    def __init__(self, database, run_key):
        self.store = Store(database)
        self.run_key = run_key

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
                    if not state.control.get('paused'):
                        raise ValueError('运行并未暂停')
                    state.control['paused'] = False
                    result = {'lifecycle': 'running'}
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
                        if not directory or not approved(directory, record) or not validate(directory, record):
                            raise ValueError('不存在有效的待审批产物')
                    elif not engine._approve_owned(spec.name):
                        raise ValueError('产物完整性或输入校验未通过')
                    result = {'stage': spec.name, 'target': actual}
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
