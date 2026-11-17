"""配置加载与校验（评审 S4：零依赖，tomllib + 手写校验替代 pydantic）。"""
from __future__ import annotations

from .schema import config_field, default_value, describe, field_metadata

import tomllib
import math
import re
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import StageSpec


class ConfigError(ValueError):
    """配置非法时抛出，信息指明具体字段。"""


# 已注册的适配器类型（与各 adapters 模块对应）
KNOWN_ADAPTER_TYPES = {
    "fake",
    "openai_compat",
    "cli_codex",
    "cli_qwen",
    "cli_qoder",
    "cli_dsh",
    "cli_doubao",
    "cli_agy",
    "cli_claude",
}

# 配置表允许的键集合：未知键直接报错，避免拼写错误让安全/预算字段静默失效。


def config_key_sets() -> dict[str, set[str]]:
    """诊断与校验复用 dataclass 字段定义。"""
    return {name: set(describe(cls)) for name, cls in CONFIG_SECTIONS.items()}


def _reject_unknown(raw: dict, allowed: set[str], label: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"{label} 包含未知字段: {unknown}（允许: {sorted(allowed)}）")


@dataclass
class AdapterConfig:
    name: str
    type: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunBudget:
    max_tokens: int | None = config_field('运行累计 token 上限；unlimited_tokens=true 时不设置', default=None, numeric=True, integer=True, minimum=0)
    max_duration_s: int | None = config_field('运行累计秒数上限；无人值守时必须为正数', default=None, numeric=True, minimum=0)
    unlimited_tokens: bool = config_field('显式取消运行/阶段/输出 token 限额，保留墙钟与轮次约束；模型服务仍可能有自身上限', default=False)


@dataclass
class Autonomy:
    enabled: bool = config_field('显式开启无人值守', default=False)
    auto_approve: list[str] = config_field('预授权自动批准的阶段名列表', default_factory=list)
    allowed_files: list[str] = config_field('允许修改的相对文件路径白名单', default_factory=list)
    baseline_paths: list[str] = config_field('首次初始化加入基线的相对路径', default_factory=list)
    max_repair_rounds: int = config_field('整个反馈链的最大修复轮数', default=4, numeric=True, integer=True, minimum=1, maximum=20, validation_order=2, limit_message='max_repair_rounds 上限为 20')
    max_stagnant_rounds: int = config_field('连续无进展的最大轮数', default=2, numeric=True, integer=True, minimum=1)
    repair_batch_size: int = config_field('单轮最多修改文件数', default=1, numeric=True, integer=True, minimum=1, maximum=3, validation_order=1, limit_message='repair_batch_size 上限为 3')
    output_tokens: int = config_field('有限 token 模式的单次输出额度；unlimited_tokens 时不施加此本地限制', default=2048, numeric=True, integer=True, minimum=1, maximum=16384, validation_order=3, limit_message='output_tokens 上限为 16384')
    context_chars: int = config_field('上下文字符数上限', default=32000, numeric=True, integer=True, minimum=1, maximum=128000, validation_order=4, limit_message='context_chars 上限为 128000')
    cli_input_overhead_tokens: int = config_field('CLI 工具上下文的保守输入预留', default=32768, numeric=True, integer=True, minimum=1)
    cli_idle_timeout_s: int = config_field('CLI 无活动超时，秒', default=120, numeric=True, integer=True, minimum=1, maximum=3600, validation_order=5, limit_message='cli_idle_timeout_s 上限为 3600')
    cli_max_tool_errors: int = config_field('连续工具错误上限', default=3, numeric=True, integer=True, minimum=1, maximum=20, validation_order=6, limit_message='cli_max_tool_errors 上限为 20')
    cli_max_repeated_reads: int = config_field('重复读取上限', default=4, numeric=True, integer=True, minimum=1, maximum=20, validation_order=7, limit_message='cli_max_repeated_reads 上限为 20')


@dataclass
class AutoTune:
    enabled: bool = config_field('开启策略提案与选择', default=False)
    auto_apply: bool = config_field('自动选择需同时启用 autonomy 和 autotune', default=False)
    min_runs: int = config_field('最少同配置终态记录数', default=5, numeric=True, integer=True, minimum=1, constraint_minimum=5, validation_order=1, limit_message='自动调优至少需要 5 条同配置记录')
    max_batch_size: int = config_field('可自动选择的修复文件批量上限', default=2, numeric=True, integer=True, minimum=1, maximum=3, validation_order=2, limit_message='自动修复文件批量上限为 3')


@dataclass
class ReviewCoverage:
    diff_lines: int = config_field('累积 diff 注入行数上限', default=600, numeric=True, integer=True, minimum=1, maximum=10000)
    per_file_bytes: int = config_field('变更文件全文注入的单文件字节上限', default=24000, numeric=True, integer=True, minimum=1, maximum=2097152)
    total_bytes: int = config_field('变更文件全文注入总字节上限', default=180000, numeric=True, integer=True, minimum=1, maximum=8388608)
    max_files: int = config_field('全文注入文件数上限', default=60, numeric=True, integer=True, minimum=1, maximum=1000)
    existing_per_file_bytes: int = config_field('审查现有代码库的单文件注入字节上限', default=12000, numeric=True, integer=True, minimum=1, maximum=2097152)
    existing_total_bytes: int = config_field('审查现有代码库的总注入字节上限', default=120000, numeric=True, integer=True, minimum=1, maximum=8388608)
    incomplete: str = config_field('覆盖不完整时 manual 待审批、fail 失败、allow 显式接受风险；无人值守 manual 仍失败', default='manual')


@dataclass
class Config:
    task_workdir: Path = config_field('任务工作目录，相对配置文件目录解析')
    state_dir: Path = config_field('状态目录，相对配置文件目录解析')
    stages: list[StageSpec] = config_field('顺序执行的阶段数组，至少一个阶段')
    adapters: dict[str, AdapterConfig] = config_field('具名适配器表；纯 test 流程可为空', input_default={})
    serial: bool = config_field('当前只支持 true，阶段串行执行', default=True)
    allow_yolo: bool = config_field('兼容开关，不解除路径与审批约束', default=False)
    run_budget: RunBudget = config_field('整个运行的 token 与时长上限', default_factory=RunBudget)
    base_dir: Path = field(default_factory=Path.cwd)
    git_exe: str = config_field('可选 Git 可执行路径；为空时探测 PATH/安装位置', default='')
    watch_root: str = config_field('有限监视域；相对配置目录解析；空或 off 关闭', default='')
    # 目录快照默认只告警（启发式，父目录无关保存不该判阶段失败）。
    # 设 true 才把根外变更当失败；硬隔离请用容器/系统级作业对象。
    watch_enforce: bool = config_field('根外快照变化是否判失败，默认仅告警', default=False)
    source_path: Path | None = None
    source_hash: str = ""
    tune_proposal_id: str = config_field('已批准调优提案标识；空或 tune- 加 12 位十六进制', default='')
    autonomy: Autonomy = config_field('显式预授权的无人值守策略', default_factory=Autonomy)
    autotune: AutoTune = config_field('有界的后续运行策略选择', default_factory=AutoTune)
    review_coverage: ReviewCoverage = config_field('审查提示词覆盖上限与不完整处理策略', default_factory=ReviewCoverage)

    def stage(self, name: str) -> StageSpec:
        for s in self.stages:
            if s.name == name:
                return s
        raise ConfigError(f"未定义的阶段: {name}")

    def stage_index(self, name: str) -> int:
        for i, s in enumerate(self.stages):
            if s.name == name:
                return i
        raise ConfigError(f"未定义的阶段: {name}")


CONFIG_SECTIONS = {"root": Config, "stages": StageSpec, "run_budget": RunBudget,
                   "autonomy": Autonomy, "autotune": AutoTune, "review_coverage": ReviewCoverage}
_ROOT_KEYS = set(describe(Config))
_STAGE_KEYS = set(describe(StageSpec))
_BUDGET_KEYS = set(describe(RunBudget))


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    _require(path.is_file(), f"配置文件不存在: {path}")
    content = path.read_bytes()
    try:
        raw = tomllib.loads(content.decode("utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"TOML 解析失败: {e}") from e
    config = parse_config(raw, base_dir=path.parent)
    config.source_path = path.resolve()
    config.source_hash = hashlib.sha256(content).hexdigest()
    return config


def _numeric(value, label: str, *, integer=False, minimum=0):
    _require(type(value) in ((int,) if integer else (int, float))
             and math.isfinite(value) and value >= minimum,
             f"{label} 必须是 >= {minimum} 的{'整数' if integer else '数值'}")
    return value


def _schema_bounds(value, info, label):
    if info.get('maximum') is not None:
        _require(value <= info['maximum'], info['limit_message'] or f"{label} 超过上限")
    if info.get('constraint_minimum') is not None:
        _require(value >= info['constraint_minimum'], info['limit_message'] or f"{label} 低于下限")


def _schema_number(value, cls, key, label, *, bounds=True):
    info = field_metadata(cls, key)
    result = _numeric(value, label, integer=info['integer'], minimum=info['minimum'])
    if bounds:
        _schema_bounds(result, info, label)
    return result


def parse_config(raw: dict[str, Any], base_dir: Path | None = None) -> Config:
    base_dir = Path(base_dir) if base_dir else Path.cwd()
    _require(isinstance(raw, dict), "配置根必须是表")
    # 未知键必须明确拒绝：拼错 approval/watch_enforce/max_tokens 等安全或
    # 预算字段时，静默按默认值运行会让门禁/限额无声失效（P2-10）。
    _reject_unknown(raw, _ROOT_KEYS, "配置根")
    for key in ("autonomy", "autotune", "adapters", "run_budget", "review_coverage"):
        _require(isinstance(raw.get(key, {}), dict), f"{key} 必须是表")
    autonomy = _policy(raw.get("autonomy", {}), Autonomy)
    autotune = _policy(raw.get("autotune", {}), AutoTune)
    coverage_raw = raw.get('review_coverage', {})
    _reject_unknown(coverage_raw, set(describe(ReviewCoverage)), '[review_coverage]')
    coverage_values = {}
    for key, info in describe(ReviewCoverage).items():
        value = coverage_raw.get(key, default_value(ReviewCoverage, key))
        if info['numeric']:
            value = _schema_number(value, ReviewCoverage, key, 'review_coverage.'+key)
        coverage_values[key] = value
    _require(coverage_values['incomplete'] in ('manual', 'fail', 'allow'),
             'review_coverage.incomplete 必须为 manual、fail 或 allow')
    coverage = ReviewCoverage(**coverage_values)
    _require(not autotune.auto_apply or (autotune.enabled and autonomy.enabled),
             "自动应用调优必须同时启用 autonomy 和 autotune")

    task_workdir = raw.get("task_workdir")
    state_dir = raw.get("state_dir")
    _require(isinstance(task_workdir, str) and task_workdir, "缺少 task_workdir")
    _require(isinstance(state_dir, str) and state_dir, "缺少 state_dir")

    # 适配器
    adapters_raw = raw.get("adapters", default_value(Config, "adapters"))
    _require(isinstance(adapters_raw, dict), "[adapters] 配置必须是表")
    for key in ("serial", "allow_yolo", "watch_enforce"):
        if key in raw:
            _require(type(raw[key]) is bool, f"{key} 必须是布尔值")
    _require(raw.get("serial", default_value(Config, "serial")), "当前只支持 serial=true")
    if raw.get("tune_proposal_id"):
        _require(isinstance(raw["tune_proposal_id"], str)
                 and bool(re.fullmatch(r"tune-[0-9a-f]{12}", raw["tune_proposal_id"])),
                 "tune_proposal_id 必须是合法的提案标识")
    adapters: dict[str, AdapterConfig] = {}
    for name, opts in adapters_raw.items():
        _require(isinstance(opts, dict), f"适配器 {name} 配置必须是表")
        atype = opts.get("type")
        _require(isinstance(atype, str) and atype in KNOWN_ADAPTER_TYPES,
                 f"适配器 {name} 的 type 非法或缺失: {atype!r}（已知: {sorted(KNOWN_ADAPTER_TYPES)}）")
        options = {k: v for k, v in opts.items() if k != "type"}
        if atype == "openai_compat":
            _require(isinstance(options.get("base_url"), str) and options["base_url"],
                     f"适配器 {name} 缺少 base_url")
            _require(isinstance(options.get("model"), str) and options["model"],
                     f"适配器 {name} 缺少 model")
            _require(isinstance(options.get("api_key_env"), str) and options["api_key_env"],
                     f"适配器 {name} 缺少 api_key_env（只存环境变量名，不存密钥）")
        adapters[name] = AdapterConfig(name=name, type=atype, options=options)

    # 阶段
    stages_raw = raw.get("stages", [])
    _require(isinstance(stages_raw, list) and stages_raw, "缺少 [[stages]] 配置")
    stages: list[StageSpec] = []
    seen: set[str] = set()
    for i, s in enumerate(stages_raw):
        _require(isinstance(s, dict), f"第 {i} 个阶段必须是表")
        _reject_unknown(s, _STAGE_KEYS, f"第 {i} 个阶段")
        name = s.get("name")
        _require(isinstance(name, str) and name, f"第 {i} 个阶段缺少 name")
        _require(bool(re.fullmatch(r"[\w.-]+", name)) and ".." not in name
                 and name not in (".", ".."), f"非法阶段名: {name}")
        _require(name not in seen, f"阶段名重复: {name}")
        seen.add(name)
        kind = s.get("kind", default_value(StageSpec, "kind"))
        if kind == "verify":
            kind = "test"  # 兼容优化方案中的命名，不新增另一套执行路径。
        _require(kind in ("code", "report", "review", "test"),
                 f"阶段 {name} 的 kind 非法: {kind!r}")
        # test 阶段不调用模型：不需要 adapter/prompt，但必须有 test_cmd
        test_cmd_raw = s.get("test_cmd", default_value(StageSpec, "test_cmd"))
        if kind == "test":
            _require(isinstance(test_cmd_raw, list) and test_cmd_raw
                     and all(isinstance(x, str) and x for x in test_cmd_raw),
                     f"阶段 {name} (kind=test) 必须配置非空字符串数组 test_cmd")
            adapter = s.get("adapter") or ""
            prompt = s.get("prompt") or ""
        else:
            _require(not test_cmd_raw, f"阶段 {name} 只有 kind=test 才能设 test_cmd")
            adapter = s.get("adapter")
            _require(isinstance(adapter, str) and adapter in adapters,
                     f"阶段 {name} 引用了未定义的适配器: {adapter!r}")
            prompt = s.get("prompt")
            _require(isinstance(prompt, str) and prompt, f"阶段 {name} 缺少 prompt")
        feedback_to = s.get("feedback_to")
        max_rounds = _schema_number(s.get("max_rounds", default_value(StageSpec, "max_rounds")), StageSpec, "max_rounds", f"阶段 {name} max_rounds")
        _require(max_rounds >= 1, f"阶段 {name} 的 max_rounds 须 >= 1")
        if feedback_to is not None:
            _require(kind in ("review", "test"),
                     f"阶段 {name} 只有 kind=review/test 才能设 feedback_to")
            _require(isinstance(feedback_to, str) and feedback_to,
                     f"阶段 {name} 的 feedback_to 须为非空字符串")
        for key in ("max_tokens", "max_duration_s"):
            if s.get(key) is not None:
                _schema_number(s[key], StageSpec, key, f"阶段 {name} {key}")
        _require(type(s.get("approval", default_value(StageSpec, "approval"))) is bool, f"阶段 {name} approval 必须是布尔值")
        config_keys_raw = s.get("config_keys", default_value(StageSpec, "config_keys"))
        _require(isinstance(config_keys_raw, dict), f"阶段 {name} config_keys 必须是表")
        try:
            json.dumps(config_keys_raw, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as error:
            raise ConfigError(
                f"阶段 {name} config_keys 必须可 JSON 序列化（TOML datetime 等值不支持）: {error}"
            ) from error
        stages.append(StageSpec(
            name=name,
            adapter=adapter,
            prompt=prompt,
            timeout_s=_schema_number(s.get("timeout_s", default_value(StageSpec, "timeout_s")), StageSpec, "timeout_s", f"阶段 {name} timeout_s"),
            approval=bool(s.get("approval", default_value(StageSpec, "approval"))),
            max_attempts=_schema_number(s.get("max_attempts", default_value(StageSpec, "max_attempts")), StageSpec, "max_attempts", f"阶段 {name} max_attempts"),
            max_tokens=s.get("max_tokens"),
            max_duration_s=s.get("max_duration_s"),
            kind=kind,
            config_keys=dict(s.get("config_keys", default_value(StageSpec, "config_keys"))),
            feedback_to=feedback_to,
            max_rounds=max_rounds,
            test_cmd=list(test_cmd_raw) if kind == "test" else [],
            context_files=_paths(s.get("context_files", default_value(StageSpec, "context_files")), f"阶段 {name} context_files"),
            precheck_cmd=_command(s.get("precheck_cmd", default_value(StageSpec, "precheck_cmd")), f"阶段 {name} precheck_cmd"),
        ))
        _require(not stages[-1].precheck_cmd or kind == "code", "只有 code 阶段能配置 precheck_cmd")

    # 校验 feedback_to 指向的阶段存在，且位于审查阶段之前
    stage_names = [s.name for s in stages]
    for i, s in enumerate(stages):
        if s.feedback_to:
            _require(s.feedback_to in stage_names,
                     f"阶段 {s.name} 的 feedback_to 指向未定义阶段: {s.feedback_to!r}")
            _require(stage_names.index(s.feedback_to) < i,
                     f"阶段 {s.name} 的 feedback_to 须指向其之前的阶段")
            if autonomy.enabled:
                _require(stages[stage_names.index(s.feedback_to)].kind == "code",
                         "无人值守反馈目标必须是 code 阶段")
    if autonomy.enabled:
        _require(all(not s.approval or s.name in autonomy.auto_approve for s in stages),
                 "无人值守审批阶段必须在 autonomy.auto_approve 中预授权")
        _require(set(autonomy.auto_approve) <= set(stage_names), "auto_approve 含未定义阶段")
        _require(not any(s.kind == "code" for s in stages) or bool(autonomy.allowed_files),
                 "无人值守 code 阶段必须声明 autonomy.allowed_files")

    budget_raw = raw.get("run_budget", {})
    _require(isinstance(budget_raw, dict), "[run_budget] 必须是表")
    _reject_unknown(budget_raw, _BUDGET_KEYS, "[run_budget]")
    for key in ("max_tokens", "max_duration_s"):
        if budget_raw.get(key) is not None:
            _schema_number(budget_raw[key], RunBudget, key, f"run_budget.{key}")
    budget = RunBudget(
        max_tokens=budget_raw.get("max_tokens"),
        max_duration_s=budget_raw.get("max_duration_s"),
        unlimited_tokens=budget_raw.get('unlimited_tokens', False),
    )
    _require(type(budget.unlimited_tokens) is bool, 'run_budget.unlimited_tokens 必须是布尔值')
    if budget.unlimited_tokens:
        _require(budget.max_tokens is None and all(s.max_tokens is None for s in stages),
                 'unlimited_tokens=true 时请删除运行和阶段 max_tokens，避免歧义')
        _require(all(a.options.get('max_tokens') is None for a in adapters.values()),
                 'unlimited_tokens=true 时请删除适配器 max_tokens')
    if autonomy.enabled:
        _require((budget.unlimited_tokens or (budget.max_tokens is not None and budget.max_tokens > 0))
                 and budget.max_duration_s is not None and budget.max_duration_s > 0,
                 "无人值守必须配置正数 token/时长预算，或显式 unlimited_tokens 并保留正数时长")

    return Config(
        task_workdir=(base_dir / task_workdir).resolve(),
        state_dir=(base_dir / state_dir).resolve(),
        stages=stages,
        adapters=adapters,
        serial=bool(raw.get("serial", default_value(Config, "serial"))),
        allow_yolo=bool(raw.get("allow_yolo", default_value(Config, "allow_yolo"))),
        run_budget=budget,
        base_dir=base_dir.resolve(),
        git_exe=str(raw.get("git_exe", default_value(Config, "git_exe")) or ""),
        watch_root=str(raw.get("watch_root", default_value(Config, "watch_root")) or ""),
        watch_enforce=bool(raw.get("watch_enforce", default_value(Config, "watch_enforce"))),
        tune_proposal_id=str(raw.get("tune_proposal_id", default_value(Config, "tune_proposal_id"))),
        autonomy=autonomy,
        autotune=autotune,
        review_coverage=coverage,
    )


def _command(value, label):
    _require(isinstance(value, list) and all(isinstance(s, str) and s for s in value),
             f"{label} 必须是字符串数组")
    return list(value)


def _paths(value, label):
    from .safety import relative_target
    paths = _command(value, label)
    for name in paths:
        try:
            relative_target(Path.cwd(), name)
        except RuntimeError as error:
            raise ConfigError(f"{label}: {error}") from error
    return paths


def _policy(raw, cls):
    _require(isinstance(raw, dict), f"{cls.__name__} 必须是表")
    defaults = cls()
    _require(set(raw) <= set(vars(defaults)), f"{cls.__name__} 包含未知字段")
    values = dict(vars(defaults))
    for key, value in raw.items():
        original = values[key]
        if type(original) is bool:
            _require(type(value) is bool, f"{key} 必须是布尔值")
        elif isinstance(original, list):
            value = _paths(value, key) if key != "auto_approve" else _command(value, key)
        else:
            _schema_number(value, cls, key, key, bounds=False)
        values[key] = value
    constraints = [(key, info) for key, info in describe(cls).items() if info['validation_order']]
    for key, info in sorted(constraints, key=lambda row: row[1]['validation_order']):
        _schema_bounds(values[key], info, key)
    return cls(**values)
