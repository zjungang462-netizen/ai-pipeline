"""调用预算检查与已报告用量结算。"""
from __future__ import annotations

from .contracts import ErrorKind, Result, RunContext, StageSpec, StageStatus
import time


class BudgetExceeded(RuntimeError):
    pass


class BudgetPolicy:
    def _account_usage(self, result: Result, stage: str = "", call_id: str | None = None) -> None:
        """调用一旦发生就入账，失败和超限也算。续跑才能看见已经花掉的用量。"""
        unknown = self._settle_call(result, stage, call_id) if call_id else False
        tokens = result.usage.get('known_tokens',0) if unknown else result.usage.get('tokens') or 0
        self.state.usage_total["tokens"] += tokens
        self.state.usage_total["duration_s"] += result.usage.get("duration_s") or 0.0
        credits=result.usage.get('qoder_credits')
        if credits is not None:
            import math
            if type(credits) not in (int,float) or not math.isfinite(credits) or credits<0:
                raise ValueError('Qoder credits 必须为有限非负数')
            self.state.usage_total['qoder_credits']=self.state.usage_total.get('qoder_credits',0)+credits
        if stage:
            used = self.state.stage_usage.setdefault(stage, {"tokens": 0, "duration_s": 0.0})
            used['tokens'] += tokens
            used['duration_s'] += result.usage.get('duration_s') or 0
            if credits is not None:used['qoder_credits']=used.get('qoder_credits',0)+credits
            if result.usage.get('usage_unknown'):
                used['unknown_calls']=used.get('unknown_calls',0)+1
            for field in ('backend_model','backend_endpoint'):
                if isinstance(result.usage.get(field),str):used[field]=result.usage[field]
        self.state.save()

    def _check_budget(self, spec: StageSpec, result: Result) -> None:
        """用量已经计入 usage_total。阶段上限仍按这一次调用判断。"""
        b = self.config.run_budget
        used = self.state.usage_total
        stage_used = self.state.stage_usage.get(spec.name, result.usage)
        tokens = stage_used.get("tokens") or 0
        duration = stage_used.get("duration_s") or 0.0
        if b.max_tokens is not None and used["tokens"] > b.max_tokens:
            raise BudgetExceeded(f"run 总 token 超限: {used['tokens']} > {b.max_tokens}")
        if spec.max_tokens is not None and tokens > spec.max_tokens:
            raise BudgetExceeded(f"阶段 {spec.name} token 超限: {tokens} > {spec.max_tokens}")
        # 时长预算（评审 M3）
        if b.max_duration_s is not None and used["duration_s"] > b.max_duration_s:
            raise BudgetExceeded(
                f"run 总时长超限: {used['duration_s']:.0f}s > {b.max_duration_s}s")
        if spec.max_duration_s is not None and duration > spec.max_duration_s:
            raise BudgetExceeded(
                f"阶段 {spec.name} 时长超限: {duration:.0f}s > {spec.max_duration_s}s")

    def _before_call(self, spec: StageSpec, prompt: str = "") -> float:
        estimate = max(1, len(prompt) // 3) if spec.kind != "test" else 0
        run = self.state.usage_total
        stage = self.state.stage_usage.get(spec.name, {})
        for label, used, max_tokens, max_duration in (
                ("run", run, self.config.run_budget.max_tokens,
                 self.config.run_budget.max_duration_s),
                (f"阶段 {spec.name}", stage, spec.max_tokens, spec.max_duration_s)):
            if max_tokens is not None and used.get("tokens", 0) + estimate > max_tokens:
                raise BudgetExceeded(f"{label} token 预算不足，调用前拦截")
            if max_duration is not None and used.get("duration_s", 0) >= max_duration:
                raise BudgetExceeded(f"{label} 时长预算耗尽，调用前拦截")
        remaining = [float(spec.timeout_s)]
        deadline=self.state.control.get('cli_deadline')
        if deadline is not None:
            if deadline<=time.time():raise BudgetExceeded('原 CLI 运行有效期已过，不能通过续跑延长')
            remaining.append(deadline-time.time())
        for used, limit in ((run, self.config.run_budget.max_duration_s),
                            (stage, spec.max_duration_s)):
            if limit is not None:
                remaining.append(limit - used.get("duration_s", 0))
        return min(remaining)

    def _reject_budget(self, spec: StageSpec, error: Exception) -> StageStatus:
        st = self.state.stage(spec.name)
        st.status, st.error = StageStatus.FAILED, str(error)
        self.state.log_event("rejected", spec.name, reason=str(error))
        self.state.save()
        return StageStatus.FAILED
