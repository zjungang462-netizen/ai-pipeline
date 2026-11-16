"""命令行入口：run / resume / status / approve / rerun-from。

用法（项目根目录）：
    python -m orchestrator.cli run config.toml --task "需求描述" [--run-id ID]
    python -m orchestrator.cli resume config.toml --run-id ID
    python -m orchestrator.cli status config.toml --run-id ID
    python -m orchestrator.cli approve config.toml --run-id ID --stage design
    python -m orchestrator.cli rerun-from config.toml --run-id ID --stage coding

preflight 报告落盘到 runs/<run_id>/preflight.json（评审 M3）。
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from .config import ConfigError, load_config
from .contracts import StageStatus
from .engine import Engine
from .adapters.factory import build_all
from .safety import WriteRootGuard, WriteRootViolation


def _build_engine(cfg, run_id: str, task: str) -> Engine:
    stage_kind = {}
    for s in cfg.stages:
        stage_kind.setdefault(s.adapter, s.kind)
    adapters = build_all(cfg.adapters, stage_kind)
    return Engine(cfg, adapters, run_id=run_id, task=task)


def _preflight(eng: Engine) -> bool:
    reports = []
    all_ok = True
    for name, ad in eng.adapters.items():
        if name not in {s.adapter for s in eng.config.stages if s.kind != "test"}:
            continue
        rep = ad.preflight()
        reports.append(rep.to_dict())
        print(f"[preflight] {name}: ok={rep.ok} {rep.detail}")
        if not rep.ok:
            all_ok = False
    eng.state.run_dir.mkdir(parents=True, exist_ok=True)
    (eng.state.run_dir / "preflight.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    return all_ok


def cmd_run(args) -> int:
    from .tuning import select_policy_config
    cfg = load_config(args.config)
    if cfg.autonomy.enabled and cfg.autotune.enabled:
        source = select_policy_config(Path(args.config))
        cfg = load_config(source)
    run_id = args.run_id or ("run-" + uuid.uuid4().hex[:8])
    eng = _build_engine(cfg, run_id, args.task)
    if not _preflight(eng):
        print("preflight 未通过：请补齐工具路径/登录/API Key 后重试。")
        return 2
    # 可写根守卫（评审 M2）：真正校验工作目录在可写根内；逐次执行校验在引擎内
    guard = WriteRootGuard([cfg.task_workdir, eng.state.run_dir])
    try:
        guard.check(cfg.task_workdir)
    except WriteRootViolation as e:
        print(f"可写根校验失败: {e}")
        return 2
    print(f"可写根: {[str(r) for r in guard.roots]}")
    print(f"\n开始 run_id={run_id}\n")
    results = eng.run()   # run() 内部 prepare()，避免重复初始化
    return _print_results(results)


def cmd_resume(args) -> int:
    cfg = load_config(args.config)
    eng = _build_engine(cfg, args.run_id, args.task or "")
    if not eng.state.load():
        print(f"未找到 run: {args.run_id}")
        return 2
    results = eng.run()
    return _print_results(results)


def cmd_status(args) -> int:
    cfg = load_config(args.config)
    eng = _build_engine(cfg, args.run_id, "")
    if not eng.state.load():
        print(f"未找到 run: {args.run_id}")
        return 2
    print(f"run_id={args.run_id} task={eng.state.task}")
    for s in cfg.stages:
        st = eng.state.stage(s.name)
        print(f"  {s.name}: {st.status.value} attempt={st.current_attempt} "
              f"out={(st.outputs_hash or '')[:12]}")
    if eng.state.pending_approvals:
        print("等待审批:", ", ".join(eng.state.pending_approvals))
    print(f"用量: {eng.state.usage_total}")
    return 0


def cmd_approve(args) -> int:
    cfg = load_config(args.config)
    eng = _build_engine(cfg, args.run_id, "")
    if not eng.state.load():
        print(f"未找到 run: {args.run_id}")
        return 2
    if eng.approve(args.stage):
        print(f"已批准 {args.stage}，继续流水线...")
        results = eng.run()
        return _print_results(results)
    print(f"审批被拒绝：{args.stage} 产物已变更或无待审批项，需重新审批。")
    return 1


def cmd_rerun_from(args) -> int:
    cfg = load_config(args.config)
    eng = _build_engine(cfg, args.run_id, args.task or "")
    if not eng.state.load():
        print(f"未找到 run: {args.run_id}")
        return 2
    results = eng.rerun_from(args.stage)
    return _print_results(results)


def _print_results(results: dict[str, str]) -> int:
    for name, status in results.items():
        print(f"  {name}: {status}")
    bad = {StageStatus.FAILED.value, StageStatus.RUNNING.value}
    if any(status in bad for status in results.values()):
        return 1
    # N1：等待审批不是"成功完成"。与 worker 退出码对齐返回 11，
    # 避免脚本把"等人工审批"误判为流水线已完成。
    if any(status == StageStatus.AWAITING_APPROVAL.value for status in results.values()):
        print("流水线未完成：有阶段等待人工审批（approve 后继续）。")
        return 11
    return 0


def cmd_tune(args) -> int:
    from .tuning import propose
    directory = Path(args.state_dir).resolve()
    source = args.config
    if not source:
        candidates = set()
        for path in (directory / "runs").glob("*/metrics.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8")).get("config_path")
                if value:
                    candidates.add(value)
            except (OSError, ValueError):
                continue
        if len(candidates) != 1:
            raise ConfigError("请用 --config 指定要优化的配置文件（指标中没有唯一配置）")
        source = candidates.pop()
    proposal = propose(directory, Path(source), args.last)
    print(f"提案 {proposal['id']}：{proposal['reason']}")
    print(f"建议 {len(proposal['changes'])} 项；待审批文件：{directory / 'tuning' / 'tune_proposal.json'}")
    return 0


def cmd_approve_tune(args) -> int:
    from .tuning import approve_proposal
    directory = Path(args.state_dir).resolve()
    proposal = Path(args.proposal) if args.proposal else directory / "tuning" / "tune_proposal.json"
    output = approve_proposal(directory, proposal, Path(args.output))
    print(f"已批准并写入配置副本：{output}")
    return 0


def cmd_autotune(args) -> int:
    from .tuning import policy_tune
    print(json.dumps(policy_tune(Path(args.config)), ensure_ascii=False, indent=2))
    return 0


def cmd_unattended(args) -> int:
    from .supervisor import supervise
    result = supervise(Path(args.config), args.run_id or 'auto-' + uuid.uuid4().hex[:8],
                       args.task, args.max_restarts)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'done' else 1


def cmd_dashboard(args) -> int:
    from .dashboard.server import serve
    configs = [Path(p).resolve() for p in args.config]
    for path in configs:
        load_config(path)
    serve(Path(args.database).resolve(), configs, args.port)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="orchestrator", description="多智能体开发流水线编排器")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp, need_task=False):
        sp.add_argument("config", help="TOML 配置文件路径")
        sp.add_argument("--run-id", required=False)
        sp.add_argument("--task", required=need_task, default="")

    sp = sub.add_parser("run", help="启动新流水线")
    add_common(sp, need_task=True)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("resume", help="断点续跑")
    add_common(sp)
    sp.set_defaults(func=cmd_resume)

    sp = sub.add_parser("status", help="查看状态")
    add_common(sp)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("approve", help="批准某阶段")
    add_common(sp)
    sp.add_argument("--stage", required=True)
    sp.set_defaults(func=cmd_approve)

    sp = sub.add_parser("rerun-from", help="从指定阶段重跑（覆盖）")
    add_common(sp)
    sp.add_argument("--stage", required=True)
    sp.set_defaults(func=cmd_rerun_from)

    sp = sub.add_parser("tune", help="读取历史指标，生成待审批的轮次优化提案")
    sp.add_argument("--state-dir", required=True)
    sp.add_argument("--config")
    sp.add_argument("--last", type=int, default=20)
    sp.set_defaults(func=cmd_tune)

    sp = sub.add_parser("approve-tune", help="批准提案，只写入新的配置副本")
    sp.add_argument("--state-dir", required=True)
    sp.add_argument("--proposal")
    sp.add_argument("--output", required=True)
    sp.set_defaults(func=cmd_approve_tune)

    sp = sub.add_parser('autotune', help='按显式预授权生成并选择下一次策略副本')
    sp.add_argument('config')
    sp.set_defaults(func=cmd_autotune)

    sp = sub.add_parser('unattended', help='按预授权策略运行，异常退出有限恢复')
    add_common(sp, need_task=True)
    sp.add_argument('--max-restarts', type=int, default=2)
    sp.set_defaults(func=cmd_unattended)

    sp = sub.add_parser('dashboard', help='启动本机协作工作台（任务、群聊、执行控制和证据）')
    sp.add_argument('--config', action='append', required=True, help='登记配置文件；可重复指定')
    sp.add_argument('--database', default='.workbench/collaboration.sqlite3')
    sp.add_argument('--port', type=int, default=8765)
    sp.set_defaults(func=cmd_dashboard)

    args = p.parse_args(argv)
    if args.cmd in ("resume", "status", "approve", "rerun-from") and not args.run_id:
        p.error(f"{args.cmd} 需要 --run-id")
    try:
        return args.func(args)
    except (ConfigError, ValueError, OSError) as error:
        print(f"错误：{error}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
