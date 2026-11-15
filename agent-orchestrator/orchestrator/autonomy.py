"""显式预授权的无人值守控制；失败收敛、写入范围和调用预算均有上限。"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

from . import artifacts
from .contracts import ErrorKind, Result, StageStatus
from .safety import relative_target
from .state import atomic_write_json, sha256_text


class AutonomousControls:
    def _feedback(self, name):
        value = self.state.feedback.get(name, "")
        if not value and self.config.autonomy.enabled:
            value = self.state.autonomy.get('feedback_history', {}).get(name, '')
        return value

    def _consume_feedback(self, name):
        value = self.state.feedback.pop(name, None)
        if value and self.config.autonomy.enabled:
            self.state.autonomy.setdefault('feedback_history', {})[name] = value

    def _source_context(self, spec):
        if not spec.context_files:
            return ''
        remaining = self.config.autonomy.context_chars
        parts = []
        for name in spec.context_files:
            target = relative_target(self.config.task_workdir, name)
            if not target.is_file():
                parts.append(f'文件 `{name}` 尚不存在。')
                continue
            limit = min(16000, remaining)
            if limit <= 0:
                parts.append('其余文件因上下文上限未展开。')
                break
            with target.open(encoding='utf8', errors='replace') as stream:
                text = stream.read(limit + 1)
            truncated = len(text) > limit
            text = text[:limit]
            remaining -= len(text)
            parts.append(f'<file path="{name}">\n{text}\n</file>' + ('\n该文件已截断，不要猜测缺失原文。' if truncated else ''))
        return '\n\n'.join(parts)

    def _recover_pending_calls(self):
        pending = self.state.autonomy.pop('pending_calls', {})
        for identifier, record in pending.items():
            # 发送前已经扣留预算；进程中断后不退回未知用量。
            self.state.usage_total['unknown_calls'] = self.state.usage_total.get('unknown_calls', 0) + 1
            elapsed = min(max(0, time.time() - record['started_at']), record['timeout_s'])
            self.state.usage_total['duration_s'] += elapsed
            stage = self.state.stage_usage.setdefault(record['stage'], {'tokens': 0, 'duration_s': 0.0})
            stage['duration_s'] += elapsed
            self.state.stage(record['stage']).status = StageStatus.INTERRUPTED
            self.state.log_event('inflight_reserved', record['stage'], call=identifier,
                                 reserved_tokens=record['tokens'])
        if pending:
            self.state.save()

    def _bootstrap_declared_files(self):
        paths = self.config.autonomy.baseline_paths
        if not paths:
            return
        files = []
        for name in paths:
            target = relative_target(self.config.task_workdir, name)
            candidates = artifacts.workspace_files(target) if target.is_dir() else [target]
            for file in candidates:
                if file.is_file():
                    relative_target(self.config.task_workdir, file.relative_to(self.config.task_workdir).as_posix())
                    files.append(file.relative_to(self.config.task_workdir).as_posix())
        if files:
            changed = set(artifacts.changed_files_since(self.config.task_workdir,
                                                       artifacts.head_sha(self.config.task_workdir)))
            selected = sorted(changed.intersection(files))
            if selected:
                artifacts.commit_files(self.config.task_workdir, selected, 'baseline: declared unattended inputs')
        self.state.autonomy['base_commit'] = artifacts.head_sha(self.config.task_workdir)
        self.state.save()

    def _call_limits(self, spec, adapter, prompt):
        """按字节保守估算输入，并给输出设置剩余额度内的实际 API 上限。"""
        estimate = max(1, math.ceil(len(prompt.encode('utf8')) / 2))
        cap = min(getattr(adapter, 'max_tokens', None) or self.config.autonomy.output_tokens,
                  self.config.autonomy.output_tokens)
        for used, maximum in ((self.state.usage_total, self.config.run_budget.max_tokens),
                               (self.state.stage_usage.get(spec.name, {}), spec.max_tokens)):
            if maximum is not None:
                cap = min(cap, maximum - used.get('tokens', 0) - estimate)
        if cap < 64:
            from .engine import BudgetExceeded
            raise BudgetExceeded('剩余 token 预算不足以保留输入和最小输出，调用前拦截')
        return int(cap), int(estimate + cap)

    def _reserve_call(self, spec, number, context):
        identifier = f'{spec.name}/{number}'
        self.state.log_event('model_call_started', spec.name, n=number)
        if not self.config.autonomy.enabled:
            return None
        reservation = context.token_reservation
        pending = self.state.autonomy.setdefault('pending_calls', {})
        pending[identifier] = {'stage': spec.name, 'tokens': reservation,
                               'started_at': time.time(), 'timeout_s': context.timeout_s}
        self.state.usage_total['tokens'] += reservation
        self.state.usage_total['reserved_tokens'] = self.state.usage_total.get('reserved_tokens', 0) + reservation
        used = self.state.stage_usage.setdefault(spec.name, {'tokens': 0, 'duration_s': 0.0})
        used['tokens'] += reservation
        self.state.save()
        return identifier

    def _settle_call(self, result, stage, identifier):
        pending = self.state.autonomy.get('pending_calls', {})
        reservation = pending.pop(identifier)['tokens'] if identifier in pending else 0
        unknown = bool(result.usage.get('usage_unknown'))
        if not unknown:
            self.state.usage_total['tokens'] -= reservation
            self.state.usage_total['reserved_tokens'] = self.state.usage_total.get('reserved_tokens', 0) - reservation
            self.state.stage_usage.setdefault(stage, {'tokens': 0, 'duration_s': 0.0})['tokens'] -= reservation
            self.state.usage_total['reported_tokens'] = self.state.usage_total.get('reported_tokens', 0) + (result.usage.get('tokens') or 0)
        else:
            self.state.usage_total['unknown_calls'] = self.state.usage_total.get('unknown_calls', 0) + 1
            # 留存预扣，后续 account_usage 不再把未知请求当作免费。
            result.usage['reserved_tokens'] = reservation
        return unknown

    def _save_attempt_trace(self, spec, directory, prompt, result):
        (directory / 'stdout.txt').write_text(result.stdout or result.text, encoding='utf8')
        (directory / 'stderr.txt').write_text(result.stderr, encoding='utf8')
        atomic_write_json(directory / 'execution.json', {
            'prompt_hash': sha256_text(prompt), 'ok': result.ok,
            'error_kind': result.error_kind.value, 'usage': result.usage,
            'tool_version': result.tool_version,
        })

    def _recovery_hint(self, spec, error):
        if not self.config.autonomy.enabled:
            return
        hint = (f'上次调用失败：{str(error)[:1500]}\n'
                '请仅返回机器可解析的最小结果。代码优先使用 # patch: 路径后接 edits JSON，'
                'old 必须唯一匹配当前原文，不要省略文件或夹带散文。'
                if spec.kind == 'code' else
                f'上次结论无法解析：{str(error)[:1000]}。请只返回 '
                '{"verdict":"pass或fail","issues":[{"file":"相对路径","problem":"问题","expected":"期望"}]}，'
                'pass 时 issues 为空，不要输出分析过程。')
        self.state.autonomy.setdefault('recovery_hints', {})[spec.name] = hint
        self.state.log_event('recovery_hint', spec.name, reason=str(error)[:1000])
        self.state.save()

    def _check_code_scope(self, spec, result, workdir, base):
        if not self.config.autonomy.enabled:
            return ''
        actual = artifacts.normalize_rel(workdir, artifacts.changed_files_since(workdir, base))
        allowed = set(self.config.autonomy.allowed_files)
        forbidden = set(actual) - allowed
        if forbidden:
            return f'禁止修改未预授权文件: {sorted(forbidden)}'
        if len(actual) > self.config.autonomy.repair_batch_size:
            result.ok = False
            result.error_kind = ErrorKind.PARSE_FAILED
            result.stderr = f'本轮最多修改 {self.config.autonomy.repair_batch_size} 个文件，实际修改 {len(actual)} 个；请缩小补丁范围'
        elif not actual:
            result.ok = False
            result.error_kind = ErrorKind.PARSE_FAILED
            result.stderr = '代码未发生变化，无法视为一次修复'
        else:
            result.changed_files = actual
        return ''

    def _feedback_limit(self, spec, failure):
        if not self.config.autonomy.enabled:
            return ''
        target = spec.feedback_to
        tracker = self.state.autonomy.setdefault('convergence', {}).setdefault(
            target, {'repairs': 0, 'stagnant': 0, 'last_tree': None})
        code = self.state.stage(target).commit_sha
        tree = artifacts.tree_content_hash(self.config.task_workdir, code) if code else self.state.stage(target).outputs_hash
        if tree == tracker['last_tree']:
            tracker['stagnant'] += 1
        else:
            tracker['stagnant'] = 0
        tracker['last_tree'] = tree
        tracker['last_failure_hash'] = sha256_text(failure)
        if tracker['stagnant'] >= self.config.autonomy.max_stagnant_rounds:
            return '连续多轮代码未变化，停止无进展返工'
        if tracker['repairs'] >= self.config.autonomy.max_repair_rounds:
            return '已达到无人值守总返工轮次上限'
        tracker['repairs'] += 1
        self.state.log_event('repair_cycle', spec.name, target=target,
                             repairs=tracker['repairs'], stagnant=tracker['stagnant'])
        return ''

    def _run_precheck(self, spec):
        if not self.config.autonomy.enabled or not spec.precheck_cmd:
            return None
        if self.state.feedback.get(spec.name):
            return None
        records = self.state.autonomy.setdefault('prechecks', {})
        nonce = self.state.invalidate_nonce.get(spec.name, 0)
        if records.get(spec.name, {}).get('nonce') == nonce:
            return records[spec.name]['record']
        from .testrun import run_test_cmd
        from .review import format_test_feedback
        timeout = self._before_call(spec)
        record = run_test_cmd(spec.precheck_cmd, self.config.task_workdir, timeout)
        self._account_usage(Result(ok=record['ok'], usage={'duration_s': record['duration_s']}), spec.name)
        records[spec.name] = {'nonce': nonce, 'record': record}
        self.state.log_event('precheck', spec.name, ok=record['ok'], failure_ids=record.get('failure_ids', []))
        if not record['ok']:
            self.state.feedback[spec.name] = format_test_feedback(record, 0)
        self.state.save()
        return record
