"""配置加载与校验（评审 S4：零依赖，tomllib + 手写校验替代 pydantic）。"""
from __future__ import annotations

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
}

# 配置表允许的键集合：未知键直接报错，避免拼写错误让安全/预算字段静默失效。
_ROOT_KEYS = {
    "task_workdir", "state_dir", "serial", "allow_yolo", "watch_root",
    "watch_enforce", "git_exe", "tune_proposal_id", "adapters", "stages",
    "run_budget", "autonomy", "autotune",
}
_STAGE_KEYS = {
    "name", "kind", "adapter", "prompt", "timeout_s", "approval",
    "max_attempts", "max_tokens", "max_duration_s", "config_keys",
    "feedback_to", "max_rounds", "test_cmd", "context_files", "precheck_cmd",
}
_BUDGET_KEYS = {"max_tokens", "max_duration_s"}


def config_key_sets() -> dict[str, set[str]]:
    """诊断复用加载器的字段集合；不维护第二套配置 schema。"""
    return {"root": set(_ROOT_KEYS), "stages": set(_STAGE_KEYS),
            "run_budget": set(_BUDGET_KEYS), "autonomy": set(vars(Autonomy())),
            "autotune": set(vars(AutoTune()))}


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
    max_tokens: int | None = None
    max_duration_s: int | None = None


@dataclass
class Autonomy:
    enabled: bool = False
    auto_approve: list[str] = field(default_factory=list)
    allowed_files: list[str] = field(default_factory=list)
    baseline_paths: list[str] = field(default_factory=list)
    max_repair_rounds: int = 4
    max_stagnant_rounds: int = 2
    repair_batch_size: int = 1
    output_tokens: int = 2048
    context_chars: int = 32000
    cli_input_overhead_tokens: int = 32768
    cli_idle_timeout_s: int = 120
    cli_max_tool_errors: int = 3
    cli_max_repeated_reads: int = 4


@dataclass
class AutoTune:
    enabled: bool = False
    auto_apply: bool = False
    min_runs: int = 5
    max_batch_size: int = 2


@dataclass
class Config:
    task_workdir: Path
    state_dir: Path
    stages: list[StageSpec]
    adapters: dict[str, AdapterConfig]
    serial: bool = True
    allow_yolo: bool = False
    run_budget: RunBudget = field(default_factory=RunBudget)
    base_dir: Path = field(default_factory=Path.cwd)
    git_exe: str = ""                          # 可选：git 可执行文件绝对路径（G1）
    watch_root: str = ""                       # 可选：越界监视域（G2）；空=不扫描（默认关闭）
    # 目录快照默认只告警（启发式，父目录无关保存不该判阶段失败）。
    # 设 true 才把根外变更当失败；硬隔离请用容器/系统级作业对象。
    watch_enforce: bool = False
    source_path: Path | None = None
    source_hash: str = ""
    tune_proposal_id: str = ""
    autonomy: Autonomy = field(default_factory=Autonomy)
    autotune: AutoTune = field(default_factory=AutoTune)

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


def parse_config(raw: dict[str, Any], base_dir: Path | None = None) -> Config:
    base_dir = Path(base_dir) if base_dir else Path.cwd()
    _require(isinstance(raw, dict), "配置根必须是表")
    # 未知键必须明确拒绝：拼错 approval/watch_enforce/max_tokens 等安全或
    # 预算字段时，静默按默认值运行会让门禁/限额无声失效（P2-10）。
    _reject_unknown(raw, _ROOT_KEYS, "配置根")
    for key in ("autonomy", "autotune", "adapters", "run_budget"):
        _require(isinstance(raw.get(key, {}), dict), f"{key} 必须是表")
    autonomy = _policy(raw.get("autonomy", {}), Autonomy)
    autotune = _policy(raw.get("autotune", {}), AutoTune)
    _require(not autotune.auto_apply or (autotune.enabled and autonomy.enabled),
             "自动应用调优必须同时启用 autonomy 和 autotune")

    task_workdir = raw.get("task_workdir")
    state_dir = raw.get("state_dir")
    _require(isinstance(task_workdir, str) and task_workdir, "缺少 task_workdir")
    _require(isinstance(state_dir, str) and state_dir, "缺少 state_dir")

    # 适配器
    adapters_raw = raw.get("adapters", {})
    _require(isinstance(adapters_raw, dict), "[adapters] 配置必须是表")
    for key in ("serial", "allow_yolo", "watch_enforce"):
        if key in raw:
            _require(type(raw[key]) is bool, f"{key} 必须是布尔值")
    _require(raw.get("serial", True), "当前只支持 serial=true")
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
        kind = s.get("kind", "code")
        if kind == "verify":
            kind = "test"  # 兼容优化方案中的命名，不新增另一套执行路径。
        _require(kind in ("code", "report", "review", "test"),
                 f"阶段 {name} 的 kind 非法: {kind!r}")
        # test 阶段不调用模型：不需要 adapter/prompt，但必须有 test_cmd
        test_cmd_raw = s.get("test_cmd", [])
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
        max_rounds = _numeric(s.get("max_rounds", 1), f"阶段 {name} max_rounds",
                              integer=True, minimum=1)
        _require(max_rounds >= 1, f"阶段 {name} 的 max_rounds 须 >= 1")
        if feedback_to is not None:
            _require(kind in ("review", "test"),
                     f"阶段 {name} 只有 kind=review/test 才能设 feedback_to")
            _require(isinstance(feedback_to, str) and feedback_to,
                     f"阶段 {name} 的 feedback_to 须为非空字符串")
        for key in ("max_tokens", "max_duration_s"):
            if s.get(key) is not None:
                _numeric(s[key], f"阶段 {name} {key}", integer=key == "max_tokens")
        _require(type(s.get("approval", False)) is bool, f"阶段 {name} approval 必须是布尔值")
        config_keys_raw = s.get("config_keys", {})
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
            timeout_s=_numeric(s.get("timeout_s", 300), f"阶段 {name} timeout_s", minimum=0.001),
            approval=bool(s.get("approval", False)),
            max_attempts=_numeric(s.get("max_attempts", 2), f"阶段 {name} max_attempts", integer=True, minimum=1),
            max_tokens=s.get("max_tokens"),
            max_duration_s=s.get("max_duration_s"),
            kind=kind,
            config_keys=dict(s.get("config_keys", {})),
            feedback_to=feedback_to,
            max_rounds=max_rounds,
            test_cmd=list(test_cmd_raw) if kind == "test" else [],
            context_files=_paths(s.get("context_files", []), f"阶段 {name} context_files"),
            precheck_cmd=_command(s.get("precheck_cmd", []), f"阶段 {name} precheck_cmd"),
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
            _numeric(budget_raw[key], f"run_budget.{key}", integer=key == "max_tokens")
    budget = RunBudget(
        max_tokens=budget_raw.get("max_tokens"),
        max_duration_s=budget_raw.get("max_duration_s"),
    )
    if autonomy.enabled:
        _require(budget.max_tokens is not None and budget.max_tokens > 0
                 and budget.max_duration_s is not None and budget.max_duration_s > 0,
                 "无人值守必须配置正数 run_budget.max_tokens 和 max_duration_s")

    return Config(
        task_workdir=(base_dir / task_workdir).resolve(),
        state_dir=(base_dir / state_dir).resolve(),
        stages=stages,
        adapters=adapters,
        serial=bool(raw.get("serial", True)),
        allow_yolo=bool(raw.get("allow_yolo", False)),
        run_budget=budget,
        base_dir=base_dir.resolve(),
        git_exe=str(raw.get("git_exe", "") or ""),
        watch_root=str(raw.get("watch_root", "") or ""),
        watch_enforce=bool(raw.get("watch_enforce", False)),
        tune_proposal_id=str(raw.get("tune_proposal_id", "")),
        autonomy=autonomy,
        autotune=autotune,
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
            _numeric(value, key, integer=True, minimum=1)
        values[key] = value
    if cls is Autonomy:
        _require(values['repair_batch_size'] <= 3, "repair_batch_size 上限为 3")
        _require(values['max_repair_rounds'] <= 20, "max_repair_rounds 上限为 20")
        _require(values['output_tokens'] <= 16384, "output_tokens 上限为 16384")
        _require(values['context_chars'] <= 128000, "context_chars 上限为 128000")
        _require(values['cli_idle_timeout_s'] <= 3600, "cli_idle_timeout_s 上限为 3600")
        _require(values['cli_max_tool_errors'] <= 20, "cli_max_tool_errors 上限为 20")
        _require(values['cli_max_repeated_reads'] <= 20, "cli_max_repeated_reads 上限为 20")
    else:
        _require(values['min_runs'] >= 5, "自动调优至少需要 5 条同配置记录")
        _require(values['max_batch_size'] <= 3, "自动修复文件批量上限为 3")
    return cls(**values)
