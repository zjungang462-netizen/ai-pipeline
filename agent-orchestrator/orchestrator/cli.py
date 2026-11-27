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
import codecs
import ipaddress
import json
import os
import re
import sys
import uuid
from pathlib import Path

from .config import ConfigError, load_config
from .contracts import StageStatus
from .engine import Engine
from .adapters.factory import build_all
from .safety import WriteRootGuard, WriteRootViolation


_ENGLISH_HELP = {
    '只读检查状态 schema 兼容性': 'Read-only state-schema compatibility check',
    '停写后完整备份状态并校验 SHA-256': 'Back up stopped state and verify SHA-256',
    '校验备份并恢复到不存在的新目录，不自动执行任务': 'Validate a backup and restore to a new directory without running tasks',
    '契约化 schema 迁移（默认干跑，仅 --apply 写入并先备份原字节）': 'Contract-based schema migration (dry-run by default; --apply backs up original bytes)',
    '受控部署执行器（默认干跑，--apply 强制前置阶段与审批哈希绑定）': 'Controlled deployment (dry-run by default; --apply requires stages and approval hashes)',
    '启动受控远程 Worker 服务端（需 HMAC 密钥环境变量）': 'Start a controlled remote worker (requires an HMAC secret environment variable)',
    '导出运行/阶段/工具用量CSV；不估算真实账单': 'Export run/stage/tool usage CSV without estimating bills',
    '只读诊断配置、路径、工具和预算；不调用模型': 'Inspect config, paths, tools, and budgets without calling models',
    '预览过期状态清理；仅 --apply 实际执行': 'Preview stale-state cleanup; only --apply changes files',
    '启动新流水线': 'Start a new pipeline',
    '断点续跑': 'Resume a pipeline from its checkpoint',
    '查看状态': 'Show pipeline status',
    '批准某阶段': 'Approve a stage',
    '从指定阶段重跑（覆盖）': 'Rerun from a stage (overwrites its attempts)',
    '读取历史指标，生成待审批的轮次优化提案': 'Generate a reviewable tuning proposal from historical metrics',
    '扫描历史审查/测试失败，生成待审批的高频经验提案': 'Generate a reviewable lessons proposal from review/test failures',
    '校验提案哈希并批准写入 approved_lessons.json': 'Validate a proposal hash and approve approved_lessons.json',
    '基于终态 metrics.json 按阶段类型推荐模型与工具路由': 'Recommend model/tool routes from final metrics.json by stage kind',
    '批准提案，只写入新的配置副本': 'Approve a proposal into a new config copy only',
    '按显式预授权生成并选择下一次策略副本': 'Generate and select the next policy copy with explicit preauthorization',
    '按预授权策略运行，异常退出有限恢复': 'Run under preauthorized policy with bounded restart recovery',
    '启动本机协作工作台（任务、群聊、执行控制和证据）': 'Start the local workbench for tasks, discussion, execution, and evidence',
    '只预览（默认）': 'Preview only (default)',
    '加锁备份后原子写入': 'Lock, back up, and atomically write',
    '仅干跑检查（默认）': 'Dry-run checks only (default)',
    '校验审批后执行固定 argv': 'Validate approval, then execute the fixed argv',
    '前置必须 DONE 的阶段名，可重复': 'Required DONE stage name; repeatable',
    '输出结构化诊断': 'Print structured diagnostics',
    '复核并执行清理': 'Review and perform cleanup',
    'TOML 配置文件路径': 'Path to a TOML configuration file',
    '可选：将提案 JSON 写入状态目录之外的文件': 'Optional proposal JSON path outside the state directory',
    'lessons-propose 生成的提案 JSON 路径': 'Proposal JSON path produced by lessons-propose',
    '与一份真实配对评测 results.json 做只读编码路由对照': 'Read-only code-route comparison against real paired results.json',
    '登记配置文件；可重复指定': 'Registered config file; repeatable',
    '监听地址；默认仅回环，容器可用 0.0.0.0': 'Listen address; loopback by default, or 0.0.0.0 in a container',
    '外部访问源；非回环监听必须显式指定 HTTP(S) origin': 'External origin; required explicitly for non-loopback listeners',
    '启动后自动在默认浏览器打开本机工作台链接': 'Open the local workbench in the default browser',
    '前置阶段组合 outputs_hash 审批凭据': 'Approval credential for the combined prerequisite outputs_hash',
    '代码提交 SHA 审批凭据': 'Approval credential for the code commit SHA',
    '同时管理指定诊断包目录': 'Also manage the specified diagnostic-package directory',
    '检查并清理该仓库在状态根内的失效 worktree 登记': 'Inspect and remove stale worktree registrations in the state root',
    '输出语言（默认 zh；也可使用 PIPELINE_LANG=en）': 'Output language (default zh; PIPELINE_LANG=en is also supported)',
}

_ENGLISH_DIAGNOSTICS = {
    '本机静态检查；不探测登录态或模型连通性': 'Local static checks only; no login or model connectivity probes',
    '无法读取 UTF-8 TOML（{error}）': 'Cannot read UTF-8 TOML ({error})',
    '检查文件路径、编码和 TOML 语法': 'Check the file path, encoding, and TOML syntax',
    '未知配置字段': 'Unknown configuration field',
    '缺少必需配置': 'Missing required configuration',
    '对照示例填写该字段': 'Set this field as shown in the example',
    '修正后再次运行 doctor；使用与运行命令相同的严格校验': 'Fix the issue and rerun doctor; it uses the same strict validation as run',
    '字段、类型、阶段引用、预算与无人值守约束校验通过': 'Fields, types, stage references, budgets, and unattended constraints passed validation',
    '显式允许未完整覆盖的审查 pass；结果仍标记缺失清单': 'Incomplete review passes are explicitly allowed; missing coverage remains marked',
    '目标存在但不是目录': 'Target exists but is not a directory',
    '目标或最近存在的父目录不可写': 'Target or nearest existing parent is not writable',
    '修正目录权限；doctor 不创建目录': 'Fix directory permissions; doctor does not create directories',
    '目录尚不存在，运行时将创建': 'Directory does not exist yet; it will be created at runtime',
    '目录存在且访问权限检查通过': 'Directory exists and access checks passed',
    '适配器配置可构造；未执行版本、登录或网络探测': 'Adapter configuration is valid; no version, login, or network probes were executed',
    '适配器参数无效': 'Invalid adapter parameters',
    '检查 type、exe、timeout_s、模型和代理参数的类型及范围': 'Check the types and ranges of type, exe, timeout_s, model, and proxy options',
    '编码阶段需要 Git': 'Code stages require Git',
    '安装 Git 或修正 git_exe': 'Install Git or fix git_exe',
    '阶段额度大于运行总额度，实际仍受运行总额度限制': 'Stage budget exceeds the run budget; the run budget still applies',
    '提示词文件不存在': 'Prompt file does not exist',
    '命令入口不存在或不可执行': 'Command executable is missing or not executable',
    '检查工作目录、命令路径与 PATH；未执行该命令': 'Check the work directory, command path, and PATH; the command was not executed',
    '上下文文件不存在': 'Context file does not exist',
    '监视目录不存在': 'Watch directory does not exist',
    '监视域落在可写根内，运行时会禁用该域': 'Watch scope is inside the writable root and will be disabled at runtime',
    '监视域位于状态目录内，需确认未落入本次 run 的可写根': 'Watch scope is inside the state directory; ensure it is outside this run’s writable root',
    '开启强制监视但未设置 watch_root': 'Watch enforcement is enabled without watch_root',
    '指定有效的监视域': 'Set a valid watch scope',
    '当前没有 code 阶段；测试命令不受此越界快照约束': 'No code stage is configured; test commands are not covered by boundary snapshots',
    '删除未知字段或对照同包示例确认位置': 'Remove the unknown field or check its location in the bundled example',
    '凭据环境变量未设置': 'Required credential environment variable is not set',
    '设置配置指定的环境变量；不要将凭据写入 TOML': 'Set the environment variable named in the configuration; do not put credentials in TOML',
    '未列出的版本由适配器在调用模型前拦截': 'Unlisted versions are blocked by the adapter before any model call',
    '显式配置的可执行文件不存在或不可执行': 'The explicitly configured executable is missing or not executable',
    '使用可执行文件绝对路径或 PATH 命令名': 'Use an executable absolute path or a command available on PATH',
    '本机未找到 CLI 工具': 'The CLI tool was not found on this machine',
    '安装工具或配置 exe 绝对路径': 'Install the tool or configure an absolute exe path',
    '未配置已验证的容器 Linux 原生程序路径': 'No verified container-native Linux executable is configured',
    '参考 OPERATIONS.md 配置容器适配器参数': 'See OPERATIONS.md for container adapter parameters',
    '配置的容器 Linux 原生程序文件不存在': 'The configured container-native Linux executable does not exist',
    '检查 binary 绝对路径': 'Check the absolute binary path',
    '容器 Linux 原生程序 SHA-256 与已验证版本不匹配': 'The container-native executable SHA-256 does not match the verified version',
    '未找到 Grok 会话文件，运行前需先登录': 'The Grok session file was not found; sign in before running',
    '先执行 grok login 或检查 auth_file 路径': 'Run grok login or check the auth_file path',
    '配置的豆包 machine_id 文件不存在': 'The configured Doubao machine_id file does not exist',
    '准备持久化的 Linux machine-id 文件': 'Prepare a persistent Linux machine-id file',
    '豆包终态不报告 token，有限 token 预算下多轮返工易被未知用量预扣拦截': 'Doubao does not report final token usage; bounded token budgets can block rework through unknown-usage reservations',
    '建议启用 unlimited_tokens=true 并配置 max_duration_s 与 max_model_calls/max_unknown_calls': 'Consider unlimited_tokens=true with max_duration_s and max_model_calls/max_unknown_calls',
    '候选命令入口当前不可用；doctor 不会执行候选命令': 'A candidate command is unavailable; doctor does not execute candidate commands',
    '确认工具已安装，或从候选清单删除该命令': 'Install the tool or remove the command from the candidate list',
    '选择可写根外的有限监视目录': 'Choose a bounded watch directory outside the writable root',
}


class _CLIHelpFormatter(argparse.HelpFormatter):
    def __init__(self, *args, language='zh', **kwargs):
        self.language = language
        super().__init__(*args, **kwargs)

    def _split_lines(self, text, width):
        if self.language == 'en':
            text = _ENGLISH_HELP.get(text, text)
        return super()._split_lines(text, width)


class _CLIParser(argparse.ArgumentParser):
    def __init__(self, *args, language='zh', **kwargs):
        kwargs.setdefault('formatter_class',
                          lambda prog: _CLIHelpFormatter(prog, language=language))
        super().__init__(*args, **kwargs)


def _requested_language(argv):
    requested = os.environ.get('PIPELINE_LANG', 'zh').lower()
    for index, value in enumerate(argv):
        if value == '--':
            break
        if value == '--lang' and index + 1 < len(argv):
            requested = argv[index + 1].lower()
        elif value.startswith('--lang='):
            requested = value.split('=', 1)[1].lower()
    return 'en' if requested == 'en' else 'zh'


def _command_token(argv):
    index = 0
    while index < len(argv):
        value = argv[index]
        if value == '--lang' and index + 1 < len(argv):
            index += 2
            continue
        if value.startswith('--lang='):
            index += 1
            continue
        if value == '--' or value.startswith('-'):
            return None
        return value
    return None


def _split_deploy_argv(argv):
    separator = argv.index('--') if '--' in argv else -1
    if separator < 0 or _command_token(argv[:separator]) != 'deploy':
        return [], list(argv)
    return argv[separator + 1:], argv[:separator]


def _english(args) -> bool:
    return getattr(args, 'lang', 'zh') == 'en'


_ENGLISH_ERROR_RULES = (
    (r'^配置文件不存在: (.+)$', r'Configuration file does not exist: \1'),
    (r'^TOML 解析失败: (.+)$', r'TOML parse failed: \1'),
    (r'^(.+) 包含未知字段: (.+)（允许: (.+)）$', r'\1 contains unknown fields: \2 (allowed: \3)'),
    (r'^缺少 task_workdir$', 'Missing required field: task_workdir'),
    (r'^缺少 state_dir$', 'Missing required field: state_dir'),
    (r'^缺少 \[\[stages\]\] 配置$', 'At least one [[stages]] entry is required'),
    (r'^第 (\d+) 个阶段缺少 name$', r'Stage \1 is missing name'),
    (r'^阶段 (.+) 缺少 prompt$', r'Stage \1 is missing prompt'),
    (r'^阶段 (.+) 引用了未定义的适配器: (.+)$', r'Stage \1 references an undefined adapter: \2'),
    (r'^当前只支持 serial=true$', 'Only serial=true is currently supported'),
    (r'^未定义的阶段: (.+)$', r'Undefined stage: \1'),
    (r'^deploy 必须在 -- 后提供固定 argv 命令$', 'deploy requires a fixed argv command after --'),
    (r'^受控部署必须显式声明至少一个前置验收阶段$', 'Controlled deployment requires at least one explicit prerequisite acceptance stage'),
    (r'^前置阶段 (.+) 未完成或缺少封存 outputs_hash，拒绝部署$', r'Prerequisite stage \1 is not DONE or has no sealed outputs_hash; deployment rejected'),
    (r'^正式部署需要显式 DeployApproval，拒绝自动执行$', 'Applied deployment requires an explicit DeployApproval; automatic execution is rejected'),
    (r'^审批 run_id 与当前运行不一致$', 'Approval run_id does not match the current run'),
    (r'^审批 outputs_hash 与当前前置阶段封存产物不一致$', 'Approval outputs_hash does not match the sealed prerequisite artifacts'),
    (r'^审批 commit_sha 与已验证代码提交不一致$', 'Approval commit_sha does not match the verified code commit'),
    (r'^环境变量 (.+) 未设置$', r'Environment variable \1 is not set'),
    (r'^导出文件必须位于状态目录之外$', 'The export file must be outside the state directory'),
    (r'^--paired-results 仅适用于编码路由$', '--paired-results is supported only for coding-route recommendations'),
)


def _english_error(error) -> str:
    text = str(error)
    for pattern, replacement in _ENGLISH_ERROR_RULES:
        if re.match(pattern, text):
            return re.sub(pattern, replacement, text)
    return text


def _say(args, chinese: str, english: str) -> None:
    print(english if _english(args) else chinese)


def _build_engine(cfg, run_id: str, task: str) -> Engine:
    stage_kind = {}
    for s in cfg.stages:
        stage_kind.setdefault(s.adapter, s.kind)
    adapters = build_all(cfg.adapters, stage_kind)
    return Engine(cfg, adapters, run_id=run_id, task=task)


def _preflight(eng: Engine, language='zh') -> bool:
    reports = []
    all_ok = True
    for name, ad in eng.adapters.items():
        if name not in {s.adapter for s in eng.config.stages if s.kind != "test"}:
            continue
        rep = ad.preflight()
        reports.append(rep.to_dict())
        detail = 'Offline fake adapter' if language == 'en' and rep.detail == '离线假适配器' else rep.detail
        print(f"[preflight] {name}: ok={rep.ok} {detail}")
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
    language = 'en' if _english(args) else 'zh'
    if not _preflight(eng, language):
        _say(args, "preflight 未通过：请补齐工具路径/登录/API Key 后重试。",
             "Preflight failed: configure the tool path, login, or API key and retry.")
        return 2
    # 可写根守卫（评审 M2）：真正校验工作目录在可写根内；逐次执行校验在引擎内
    guard = WriteRootGuard([cfg.task_workdir, eng.state.run_dir])
    try:
        guard.check(cfg.task_workdir)
    except WriteRootViolation as e:
        _say(args, f"可写根校验失败: {e}", f"Writable-root validation failed: {e}")
        return 2
    print(("Writable roots: " if _english(args) else "可写根: ") + repr([str(r) for r in guard.roots]))
    print(f"\n{'Starting' if _english(args) else '开始'} run_id={run_id}\n")
    results = eng.run()   # run() 内部 prepare()，避免重复初始化
    from .notifications import notify_from_env
    notify_from_env(run_id=run_id, results=results, usage=eng.state.usage_total)
    return _print_results(results, language)


def cmd_resume(args) -> int:
    cfg = load_config(args.config)
    eng = _build_engine(cfg, args.run_id, args.task or "")
    if not eng.state.load():
        _say(args, f"未找到 run: {args.run_id}", f"Run not found: {args.run_id}")
        return 2
    results = eng.run(resume_wait=not (eng.state.run_dir / 'job.json').exists())
    from .notifications import notify_from_env
    notify_from_env(run_id=args.run_id, results=results, usage=eng.state.usage_total)
    return _print_results(results, 'en' if _english(args) else 'zh')


def cmd_status(args) -> int:
    cfg = load_config(args.config)
    eng = _build_engine(cfg, args.run_id, "")
    if not eng.state.load():
        _say(args, f"未找到 run: {args.run_id}", f"Run not found: {args.run_id}")
        return 2
    print(f"run_id={args.run_id} task={eng.state.task}")
    for s in cfg.stages:
        st = eng.state.stage(s.name)
        print(f"  {s.name}: {st.status.value} attempt={st.current_attempt} "
              f"out={(st.outputs_hash or '')[:12]}")
    if eng.state.pending_approvals:
        _say(args, "等待审批: " + ", ".join(eng.state.pending_approvals),
             "Awaiting approval: " + ", ".join(eng.state.pending_approvals))
    for stage, wait in eng.state.pending_waits.items():
        if isinstance(wait, dict) and wait.get('reason'):
            if _english(args):
                print(f"  {stage}: {wait['reason']}; recovery condition: "
                      f"{wait.get('condition') or 'owner action'}; next: {wait.get('resume_action', '')}")
            else:
                print(f"  {stage}：{wait['reason']}；恢复条件：{wait.get('condition') or '本人处理'}；"
                      f"下一步：{wait.get('resume_action', '')}")
    _say(args, f"用量: {eng.state.usage_total}", f"Usage: {eng.state.usage_total}")
    return 0


def cmd_approve(args) -> int:
    cfg = load_config(args.config)
    if args.stage not in {stage.name for stage in cfg.stages}:
        _say(args, f"未知阶段：{args.stage}", f"Unknown stage: {args.stage}")
        return 2
    eng = _build_engine(cfg, args.run_id, "")
    if not eng.state.load():
        _say(args, f"未找到 run: {args.run_id}", f"Run not found: {args.run_id}")
        return 2
    if args.stage not in eng.state.pending_approvals:
        waiting = ("、" if not _english(args) else ", ").join(eng.state.pending_approvals)
        waiting = waiting or ("无" if not _english(args) else "none")
        _say(args, f"{args.stage} 没有待审批项（当前等待审批：{waiting}）。",
             f"{args.stage} has no pending approval (currently waiting: {waiting}).")
        return 1
    if eng.approve(args.stage):
        _say(args, f"已批准 {args.stage}，继续流水线...", f"Approved {args.stage}; continuing the pipeline...")
        results = eng.run()
        return _print_results(results, 'en' if _english(args) else 'zh')
    _say(args, f"审批未完成：{args.stage} 的状态、产物证据或并发占用可能已变化，请刷新状态后重试。",
         f"Approval was not completed: {args.stage} state, artifact evidence, or concurrent activity may have changed; refresh status and retry.")
    return 1


def cmd_rerun_from(args) -> int:
    cfg = load_config(args.config)
    if args.stage not in {stage.name for stage in cfg.stages}:
        _say(args, f"未知阶段：{args.stage}", f"Unknown stage: {args.stage}")
        return 2
    eng = _build_engine(cfg, args.run_id, args.task or "")
    if not eng.state.load():
        _say(args, f"未找到 run: {args.run_id}", f"Run not found: {args.run_id}")
        return 2
    results = eng.rerun_from(args.stage)
    return _print_results(results, 'en' if _english(args) else 'zh')


def _print_results(results: dict[str, str], language='zh') -> int:
    for name, status in results.items():
        print(f"  {name}: {status}")
    bad = {StageStatus.FAILED.value, StageStatus.RUNNING.value}
    if any(status in bad for status in results.values()):
        return 1
    # N1：等待审批不是"成功完成"。与 worker 退出码对齐返回 11，
    # 避免脚本把"等人工审批"误判为流水线已完成。
    if any(status == StageStatus.AWAITING_APPROVAL.value for status in results.values()):
        print("Pipeline incomplete: a stage awaits human approval (use approve to continue)."
              if language == 'en' else "流水线未完成：有阶段等待人工审批（approve 后继续）。")
        return 11
    if any(status in (StageStatus.WAITING_EXTERNAL.value, StageStatus.WAITING_OWNER.value)
           for status in results.values()):
        print("Pipeline incomplete: a stage awaits recovery or owner action (use resume to continue)."
              if language == 'en' else "流水线未完成：阶段等待恢复条件或本人处理（resume 后继续）。")
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
    host = args.host.strip()
    if not host:
        raise ValueError("--host must not be empty" if _english(args) else "--host 不能为空")
    try:
        loopback = host.lower() == 'localhost' or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback and not args.origin:
        raise ValueError('--origin is required when --host is not loopback' if _english(args)
                         else '--host 不是回环地址时必须指定 --origin')
    serve(Path(args.database).resolve(), configs, args.port, host=host,
          open_browser=bool(args.open), language='en' if _english(args) else 'zh',
          origin=args.origin)
    return 0


def cmd_doctor(args) -> int:
    from .doctor import diagnose
    report = diagnose(args.config)
    if _english(args):
        report = dict(report)
        report['checks'] = [dict(check) for check in report['checks']]
        for check in report['checks']:
            message = check['message']
            if message.startswith('无法读取 UTF-8 TOML（'):
                detail = message.split('（', 1)[1][:-1]
                message = _ENGLISH_DIAGNOSTICS['无法读取 UTF-8 TOML（{error}）'].format(error=detail)
            if message.startswith('已验证版本：'):
                message = 'Verified versions: ' + message.split('：', 1)[1]
            if message.startswith('需要已验证版本 '):
                message = 'Verified version required: ' + message.split(' ', 1)[1]
            check['message'] = _english_error(_ENGLISH_DIAGNOSTICS.get(message, message))
            hint = _ENGLISH_DIAGNOSTICS.get(check['hint'], check['hint'])
            if hint.startswith('改为 '):
                hint = 'Change to ' + hint[3:]
            check['hint'] = hint
        report['scope'] = _ENGLISH_DIAGNOSTICS.get(report['scope'], report['scope'])
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        separator = '; ' if _english(args) else '；'
        for check in report['checks']:
            print(f"[{check['level']}] {check['field']}: {check['message']}"
                  + (separator + check['hint'] if check['hint'] else ''))
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
        _say(args, f'未找到 run: {args.run_id}', f'Run not found: {args.run_id}')
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
        _say(args, f'部署拒绝：{error}',
             'Deployment rejected: ' + _english_error(error))
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
    try:
        server.start()
        server._thread.join()
    except KeyboardInterrupt:
        pass
    finally:
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

    paired_results = getattr(args, 'paired_results', None)
    if paired_results and args.stage_kind not in (None, 'code'):
        raise ValueError('--paired-results 仅适用于编码路由')
    report = recommend_adapters(
        Path(args.state_dir),
        stage_kind=args.stage_kind,
        min_runs=args.min_runs,
    )
    if paired_results:
        from .route_shadow import review_paired_results

        report['shadow_comparison'] = review_paired_results(Path(paired_results), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    _harden_output_streams()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    language = _requested_language(raw_argv)
    p = _CLIParser(prog="orchestrator", language=language,
                   description=("Multi-agent development pipeline orchestrator" if language == 'en'
                                else "多智能体开发流水线编排器"))
    from ._version import __version__
    p.add_argument('--version', action='version', version='%(prog)s '+__version__)
    p.add_argument('--lang', choices=('zh', 'en'), default=language,
                   help='输出语言（默认 zh；也可使用 PIPELINE_LANG=en）')
    sub = p.add_subparsers(dest="cmd", required=True,
                           parser_class=lambda **kwargs: _CLIParser(language=language, **kwargs))

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
    sp.add_argument("--paired-results", type=Path, help="与一份真实配对评测 results.json 做只读编码路由对照")
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
    sp.add_argument('--host', default='127.0.0.1', help='监听地址；默认仅回环，容器可用 0.0.0.0')
    sp.add_argument('--origin', help='外部访问源；非回环监听必须显式指定 HTTP(S) origin')
    sp.add_argument('--open', action='store_true', help='启动后自动在默认浏览器打开本机工作台链接')
    sp.set_defaults(func=cmd_dashboard)

    for parser in sub.choices.values():
        parser.add_argument('--lang', choices=('zh', 'en'), default=argparse.SUPPRESS,
                            help='输出语言（默认 zh；也可使用 PIPELINE_LANG=en）')
    deploy_command, raw_argv = _split_deploy_argv(raw_argv)
    args = p.parse_args(raw_argv)
    if args.cmd == 'deploy':
        args.command = deploy_command
    if args.cmd in ("resume", "status", "approve", "rerun-from") and not args.run_id:
        p.error(f"{args.cmd} requires --run-id" if _english(args) else f"{args.cmd} 需要 --run-id")
    try:
        return args.func(args)
    except (ConfigError, ValueError, OSError) as error:
        detail = _english_error(error) if _english(args) else str(error)
        print(("Error: " if _english(args) else "错误：") + detail)
        return 2


def _harden_output_streams() -> None:
    """命令行输出流写不出某个字符时不崩溃。

    终端保持原编码；被重定向或走管道的流，如果本地编码写不了中文，改用 UTF-8。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        encoding = getattr(stream, "encoding", None)
        if reconfigure is None or not encoding:
            continue
        try:
            encoding = codecs.lookup(encoding).name
        except LookupError:
            continue
        if encoding != "utf-8" and not stream.isatty():
            try:
                "流程阶段审批".encode(encoding)
            except UnicodeEncodeError:
                encoding = "utf-8"
        try:
            reconfigure(encoding=encoding, errors="backslashreplace")
        except (OSError, ValueError):
            continue


if __name__ == "__main__":
    sys.exit(main())
