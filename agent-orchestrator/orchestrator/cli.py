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
    from .notifications import notify_from_env
    notify_from_env(run_id=run_id, results=results, usage=eng.state.usage_total)
    return _print_results(results)


def cmd_resume(args) -> int:
    cfg = load_config(args.config)
    eng = _build_engine(cfg, args.run_id, args.task or "")
    if not eng.state.load():
        print(f"未找到 run: {args.run_id}")
        return 2
    results = eng.run(resume_wait=not (eng.state.run_dir / 'job.json').exists())
    from .notifications import notify_from_env
    notify_from_env(run_id=args.run_id, results=results, usage=eng.state.usage_total)
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
    for stage, wait in eng.state.pending_waits.items():
        if isinstance(wait, dict) and wait.get('reason'):
            print(f"  {stage}：{wait['reason']}；恢复条件：{wait.get('condition') or '本人处理'}；"
                  f"下一步：{wait.get('resume_action', '')}")
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
    if any(status in (StageStatus.WAITING_EXTERNAL.value, StageStatus.WAITING_OWNER.value)
           for status in results.values()):
        print("流水线未完成：阶段等待恢复条件或本人处理（resume 后继续）。")
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
    if result['status'] == 'done':
        return 0
    if result['status'] in ('waiting_external', 'waiting_owner', 'waiting_approval', 'paused'):
        return 11
    return 1


def cmd_dashboard(args) -> int:
    from .dashboard.server import serve
    configs = [Path(p).resolve() for p in args.config]
    for path in configs:
        load_config(path)
    serve(Path(args.database).resolve(), configs, args.port, open_browser=bool(args.open))
    return 0


def cmd_doctor(args) -> int:
    from .doctor import diagnose
    report = diagnose(args.config)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for check in report['checks']:
            print(f"[{check['level']}] {check['field']}: {check['message']}"
                  + (f"；{check['hint']}" if check['hint'] else ''))
        print(report['scope'])
    return 0 if report['ok'] else 1


def cmd_clean(args) -> int:
    from .maintenance import clean_state
    report = clean_state(args.state_dir, apply=args.apply, older_than=args.older_than,
                         test_failures=args.test_failures, repo=args.repo,
                         max_evidence_bytes=int(args.max_evidence_mib*1024*1024))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


def cmd_export_usage(args) -> int:
    from .reporting import export_usage, usage_csv
    report = export_usage(args.state_dir, args.run_id)
    output = Path(args.output).absolute()
    state_root = Path(args.state_dir).resolve()
    if output.resolve().is_relative_to(state_root):
        raise ValueError('导出文件必须位于状态目录之外')
    with output.open('x', encoding='utf-8-sig', newline='') as stream:
        stream.write(usage_csv(report))
    print(json.dumps({'output': str(output), 'runs': len(report['runs']),
                      'errors': report['errors'], 'cost_status': 'not_reported'}, ensure_ascii=False))
    return 1 if report['errors'] else 0


def cmd_state_check(args) -> int:
    from .state_backup import inspect_state
    report = inspect_state(args.state_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['compatible'] else 1


def cmd_state_backup(args) -> int:
    from .state_backup import backup_state
    print(json.dumps(backup_state(args.state_dir, args.output), ensure_ascii=False, indent=2))
    return 0


def cmd_state_restore(args) -> int:
    from .state_backup import restore_state
    print(json.dumps(restore_state(args.backup, args.output), ensure_ascii=False, indent=2))
    return 0


def cmd_state_migrate(args) -> int:
    from .state_migration import DEFAULT_MIGRATION_REGISTRY
    receipt = DEFAULT_MIGRATION_REGISTRY.migrate_file(
        Path(args.state_file),
        target_version=args.target_version,
        apply=bool(args.apply),
        backup_dir=Path(args.backup_dir) if args.backup_dir else None,
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


def cmd_deploy(args) -> int:
    from .deploy import DeployApproval, DeployRejected, DeploySpec, execute_deploy
    from .state import RunState

    command = list(args.command or [])
    if command and command[0] == '--':
        command = command[1:]
    if not command:
        raise ValueError('deploy 必须在 -- 后提供固定 argv 命令')
    cfg = load_config(args.config)
    state = RunState(cfg.state_dir, args.run_id)
    if not state.load():
        print(f'未找到 run: {args.run_id}')
        return 2
    spec = DeploySpec(
        name=args.name,
        command=tuple(command),
        timeout_s=args.timeout,
        allow_env=tuple(args.allow_env or ()),
    )
    approval = None
    if args.apply and args.approve_hash:
        approval = DeployApproval(
            run_id=args.run_id,
            outputs_hash=args.approve_hash,
            commit_sha=args.approve_commit,
            approved_by=args.approved_by,
        )
    try:
        receipt = execute_deploy(
            spec, state, cfg.task_workdir, tuple(args.require_stage),
            approval=approval, dry_run=not args.apply,
        )
    except DeployRejected as error:
        print(f'部署拒绝：{error}')
        return 2
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if receipt['ok'] else 1


def cmd_worker_serve(args) -> int:
    import os
    from .remote_worker import RemoteJobReceipt, RemoteWorkerServer

    secret = os.environ.get(args.secret_env, '').strip()
    if not secret:
        raise ValueError(f'环境变量 {args.secret_env} 未设置')

    def _reject_unconfigured(spec):
        return RemoteJobReceipt(
            job_id=spec.job_id,
            worker_id=args.worker_id,
            status='rejected',
            exit_code=-1,
            stderr='未挂载具体执行器回调',
        )

    server = RemoteWorkerServer(args.worker_id, secret, _reject_unconfigured, host=args.host, port=args.port)
    print(json.dumps({'worker_id': args.worker_id, 'host': args.host, 'port': server.port}, ensure_ascii=False), flush=True)
    server.start()
    try:
        server._thread.join()
    except KeyboardInterrupt:
        server.close()
    return 0


def cmd_lessons_propose(args) -> int:
    from .lessons import propose_lessons
    from .state import atomic_write_json

    proposal = propose_lessons(
        Path(args.state_dir),
        min_occurrences=args.min_occurrences,
        max_items=args.max_items,
    )
    if args.output:
        out_path = Path(args.output).resolve()
        state_root = Path(args.state_dir).resolve()
        if out_path.is_relative_to(state_root):
            raise ValueError('经验提案输出文件必须位于状态目录之外')
        atomic_write_json(out_path, proposal)
    print(json.dumps(proposal, ensure_ascii=False, indent=2))
    return 0


def cmd_lessons_approve(args) -> int:
    from .lessons import approve_lessons

    proposal_path = Path(args.proposal)
    proposal = json.loads(proposal_path.read_text(encoding='utf-8'))
    target = approve_lessons(Path(args.state_dir), proposal, max_items=args.max_items)
    saved = json.loads(target.read_text(encoding='utf-8'))
    print(json.dumps({
        'approved_file': str(target),
        'items': len(saved.get('items') or []),
        'proposal_hash': saved.get('proposal_hash'),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_route_recommend(args) -> int:
    from .model_router import recommend_adapters

    report = recommend_adapters(
        Path(args.state_dir),
        stage_kind=args.stage_kind,
        min_runs=args.min_runs,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="orchestrator", description="多智能体开发流水线编排器")
    from ._version import __version__
    p.add_argument('--version', action='version', version='%(prog)s '+__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser('state-check', help='只读检查状态 schema 兼容性')
    sp.add_argument('--state-dir', required=True)
    sp.set_defaults(func=cmd_state_check)

    sp = sub.add_parser('state-backup', help='停写后完整备份状态并校验 SHA-256')
    sp.add_argument('--state-dir', required=True)
    sp.add_argument('--output', required=True)
    sp.set_defaults(func=cmd_state_backup)

    sp = sub.add_parser('state-restore', help='校验备份并恢复到不存在的新目录，不自动执行任务')
    sp.add_argument('--backup', required=True)
    sp.add_argument('--output', required=True)
    sp.set_defaults(func=cmd_state_restore)

    sp = sub.add_parser('state-migrate', help='契约化 schema 迁移（默认干跑，仅 --apply 写入并先备份原字节）')
    sp.add_argument('--state-file', required=True)
    sp.add_argument('--target-version', type=int)
    mig_mode = sp.add_mutually_exclusive_group()
    mig_mode.add_argument('--dry-run', action='store_true', help='只预览（默认）')
    mig_mode.add_argument('--apply', action='store_true', help='加锁备份后原子写入')
    sp.add_argument('--backup-dir')
    sp.set_defaults(func=cmd_state_migrate)

    sp = sub.add_parser('deploy', help='受控部署执行器（默认干跑，--apply 强制前置阶段与审批哈希绑定）')
    sp.add_argument('config', help='TOML 配置文件路径')
    sp.add_argument('--run-id', required=True)
    sp.add_argument('--name', default='deploy')
    sp.add_argument('--require-stage', action='append', required=True, help='前置必须 DONE 的阶段名，可重复')
    dep_mode = sp.add_mutually_exclusive_group()
    dep_mode.add_argument('--dry-run', action='store_true', help='仅干跑检查（默认）')
    dep_mode.add_argument('--apply', action='store_true', help='校验审批后执行固定 argv')
    sp.add_argument('--approve-hash', help='前置阶段组合 outputs_hash 审批凭据')
    sp.add_argument('--approve-commit', help='代码提交 SHA 审批凭据')
    sp.add_argument('--approved-by', default='owner')
    sp.add_argument('--timeout', type=int, default=120)
    sp.add_argument('--allow-env', action='append', default=[])
    sp.set_defaults(func=cmd_deploy, command=[])

    sp = sub.add_parser('worker-serve', help='启动受控远程 Worker 服务端（需 HMAC 密钥环境变量）')
    sp.add_argument('--worker-id', required=True)
    sp.add_argument('--secret-env', required=True)
    sp.add_argument('--host', default='127.0.0.1')
    sp.add_argument('--port', type=int, default=8770)
    sp.set_defaults(func=cmd_worker_serve)

    sp = sub.add_parser('export-usage', help='导出运行/阶段/工具用量CSV；不估算真实账单')
    sp.add_argument('--state-dir', required=True)
    sp.add_argument('--run-id')
    sp.add_argument('--output', required=True)
    sp.set_defaults(func=cmd_export_usage)

    sp = sub.add_parser('doctor', help='只读诊断配置、路径、工具和预算；不调用模型')
    sp.add_argument('config')
    sp.add_argument('--json', action='store_true', help='输出结构化诊断')
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser('clean', help='预览过期状态清理；仅 --apply 实际执行')
    sp.add_argument('--state-dir', required=True)
    mode = sp.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='只预览（默认）')
    mode.add_argument('--apply', action='store_true', help='复核并执行清理')
    sp.add_argument('--older-than', type=float, default=30, metavar='DAYS')
    sp.add_argument('--test-failures', help='同时管理指定诊断包目录')
    sp.add_argument('--max-evidence-mib', type=int, default=100)
    sp.add_argument('--repo', help='检查并清理该仓库在状态根内的失效 worktree 登记')
    sp.set_defaults(func=cmd_clean)

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

    sp = sub.add_parser("lessons-propose", help="扫描历史审查/测试失败，生成待审批的高频经验提案")
    sp.add_argument("--state-dir", required=True)
    sp.add_argument("--min-occurrences", type=int, default=2)
    sp.add_argument("--max-items", type=int, default=12)
    sp.add_argument("--output", help="可选：将提案 JSON 写入状态目录之外的文件")
    sp.set_defaults(func=cmd_lessons_propose)

    sp = sub.add_parser("lessons-approve", help="校验提案哈希并批准写入 approved_lessons.json")
    sp.add_argument("--state-dir", required=True)
    sp.add_argument("--proposal", required=True, help="lessons-propose 生成的提案 JSON 路径")
    sp.add_argument("--max-items", type=int, default=12)
    sp.set_defaults(func=cmd_lessons_approve)

    sp = sub.add_parser("route-recommend", help="基于终态 metrics.json 按阶段类型推荐模型与工具路由")
    sp.add_argument("--state-dir", required=True)
    sp.add_argument("--stage-kind", choices=("code", "report", "review"))
    sp.add_argument("--min-runs", type=int, default=2)
    sp.set_defaults(func=cmd_route_recommend)

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
    sp.add_argument('--open', action='store_true', help='启动后自动在默认浏览器打开本机工作台链接')
    sp.set_defaults(func=cmd_dashboard)

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    deploy_command: list[str] = []
    if raw_argv[:1] == ['deploy'] and '--' in raw_argv:
        sep = raw_argv.index('--')
        deploy_command = raw_argv[sep + 1:]
        raw_argv = raw_argv[:sep]
    args = p.parse_args(raw_argv)
    if args.cmd == 'deploy':
        args.command = deploy_command
    if args.cmd in ("resume", "status", "approve", "rerun-from") and not args.run_id:
        p.error(f"{args.cmd} 需要 --run-id")
    try:
        return args.func(args)
    except (ConfigError, ValueError, OSError) as error:
        print(f"错误：{error}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
