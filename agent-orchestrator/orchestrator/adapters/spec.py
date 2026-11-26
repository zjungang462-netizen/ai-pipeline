"""适配器参数格式与能力声明：每种适配器在一处声明，配置解析、工厂、诊断与工作台共用。

只依赖标准库。validate_options 不读环境变量、不探测文件系统；环境相关检查留在 doctor。
"""
from __future__ import annotations

import difflib
import math
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

MISSING: Any = type('Missing', (), {'__repr__': lambda self: 'MISSING'})()


@dataclass(frozen=True)
class OptionSpec:
    key: str
    type: type | tuple[type, ...]
    default: Any = MISSING                  # MISSING 表示必填
    doc: str = ''
    choices: tuple | None = None
    min: float | None = None                # 数值下限（含）
    positive: bool = False                  # 必须 > 0
    item_type: type | None = None           # list 元素类型
    check: Callable[[Any], str | None] | None = None  # 返回错误文本或 None
    secret_ref: bool = False                # 只存环境变量名
    overridable: bool = False               # 工作台本次运行可覆盖
    nonempty: bool = False                  # 字符串不可为空
    blank_none: bool = False                # 空字符串视为未设置（兼容旧的 `or None` 写法）

    @property
    def required(self) -> bool:
        return self.default is MISSING


@dataclass(frozen=True)
class Capabilities:
    model_select: bool = False
    reasoning_effort: tuple[str, ...] = ()  # 已验证可用的强度；空表示不支持
    thinking: tuple[str, ...] = ()
    file_tools: bool = False
    review_json_mode: bool = False
    streaming: bool = False
    session_resume: str = 'none'            # 'none' | 'replay' | 'reconnect'
    usage_reporting: str = 'exact'          # 'exact' | 'estimated' | 'unknown'
    stage_kinds: tuple[str, ...] = ('report', 'code', 'review')

    def __post_init__(self):
        if self.session_resume not in ('none', 'replay', 'reconnect'):
            raise ValueError(f'session_resume 非法: {self.session_resume!r}')
        if self.usage_reporting not in ('exact', 'estimated', 'unknown'):
            raise ValueError(f'usage_reporting 非法: {self.usage_reporting!r}')


@dataclass(frozen=True)
class AdapterSpec:
    type: str
    factory: Callable[..., Any]
    options: tuple[OptionSpec, ...]
    capabilities: Capabilities = Capabilities()
    verified_versions: tuple[str, ...] = ()  # 仅供展示；版本门禁仍由适配器自身执行
    kind_aware: bool = False                 # 构造时需要阶段 kind

    def __post_init__(self):
        keys = [o.key for o in self.options]
        if len(keys) != len(set(keys)):
            raise ValueError(f'{self.type} 的参数声明存在重复键')

    def option(self, key: str) -> OptionSpec | None:
        return next((o for o in self.options if o.key == key), None)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(o.key for o in self.options)

    @property
    def overridable_keys(self) -> tuple[str, ...]:
        return tuple(o.key for o in self.options if o.overridable)


@dataclass(frozen=True)
class Issue:
    level: str        # 'error' | 'warning'
    path: str         # 'adapters.coder.timeout_s'
    message: str
    hint: str = ''

    def __str__(self) -> str:
        return f'{self.path}: {self.message}' + (f'（{self.hint}）' if self.hint else '')


def check_http_url(value: str) -> str | None:
    try:
        parts = urlsplit(value)
    except ValueError:
        return '不是合法的 URL'
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return '需要 HTTP(S) 端点'
    if parts.username or parts.password:
        return '端点不得包含凭据'
    return None


# CLI 适配器共用参数；默认值与 CLIAdapter.__init__ 一致（由契约测试保证）。
COMMON_CLI: tuple[OptionSpec, ...] = (
    OptionSpec('exe', str, None, '可执行文件绝对路径或 PATH 命令名', blank_none=True),
    OptionSpec('extra_env', list, None, '透传给工具的环境变量名', item_type=str),
    OptionSpec('timeout_s', int, 300, '单次调用超时（秒）', positive=True),
)


def model_option() -> OptionSpec:
    return OptionSpec('model', str, None, '模型标识；空表示工具默认', blank_none=True, overridable=True)


def effort_option(choices: tuple[str, ...]) -> OptionSpec:
    return OptionSpec('reasoning_effort', str, None, '思考强度（仅列已验证取值）', choices=choices,
                      blank_none=True, overridable=True)


def proxy_option() -> OptionSpec:
    return OptionSpec('proxy_url', str, None, '不含凭据的 HTTP(S) 代理', blank_none=True, check=check_http_url)


def _type_name(t) -> str:
    ts = t if isinstance(t, tuple) else (t,)
    return '/'.join(x.__name__ for x in ts)


def _type_ok(value, expected) -> bool:
    ts = expected if isinstance(expected, tuple) else (expected,)
    if type(value) is bool:                 # bool 不能当 int/float
        return bool in ts
    if type(value) is int and float in ts:  # int 可当 float
        return True
    return isinstance(value, ts)


def _check_value(o: OptionSpec, value) -> str | None:
    if not _type_ok(value, o.type):
        return f'类型应为 {_type_name(o.type)}，实际为 {type(value).__name__}'
    if isinstance(value, (int, float)) and type(value) is not bool:
        if not math.isfinite(value):
            return '需要有限数值'
        if o.positive and value <= 0:
            return '必须为正数'
        if o.min is not None and value < o.min:
            return f'不得小于 {o.min}'
    if isinstance(value, str) and o.nonempty and not value.strip():
        return '不能为空'
    if o.choices is not None and value not in o.choices:
        return f'取值应为 {list(o.choices)} 之一，实际为 {value!r}'
    if isinstance(value, list) and o.item_type is not None:
        if not all(_type_ok(v, o.item_type) and (not isinstance(v, str) or v) for v in value):
            return f'列表元素须为非空 {_type_name(o.item_type)}'
    if o.check is not None:
        return o.check(value)
    return None


def validate_options(spec: AdapterSpec, name: str, raw: dict, *,
                     strict_unknown: bool = False) -> tuple[dict, list[Issue]]:
    """返回 (填充默认值后的 options, 问题列表)。不修改 raw。

    值为 None 的可选字段视为未设置。未知键默认 warning（0.7.0 起改为 error）。
    """
    issues: list[Issue] = []
    out: dict[str, Any] = {}
    prefix = f'adapters.{name}'
    known = set(spec.keys)
    for key in sorted(k for k in raw if k != 'type' and k not in known):
        close = difflib.get_close_matches(key, spec.keys, n=1)
        issues.append(Issue('error' if strict_unknown else 'warning', f'{prefix}.{key}',
                            f'{spec.type} 不支持该参数，将被忽略',
                            f'是否想写 {close[0]}？' if close else f'支持: {", ".join(spec.keys)}'))
    for o in spec.options:
        value = raw.get(o.key)
        if o.blank_none and value == '':
            value = None
        if value is None:
            if o.required:
                issues.append(Issue('error', f'{prefix}.{o.key}', f'缺少必填参数（{o.doc}）' if o.doc else '缺少必填参数'))
            else:
                out[o.key] = list(o.default) if isinstance(o.default, tuple) and o.type is list else o.default
            continue
        problem = _check_value(o, value)
        if problem:
            issues.append(Issue('error', f'{prefix}.{o.key}', problem))
        else:
            out[o.key] = value
    return out, issues


def _cell(text) -> str:
    return str(text).replace('|', '\\|').replace('\n', ' ')


def _default_text(o: OptionSpec) -> str:
    if o.required:
        return '必填'
    if o.default is None:
        return '—'
    import json
    return '`' + json.dumps(o.default, ensure_ascii=False) + '`'


def render_adapter_tables(specs) -> str:
    """README 的适配器参数表与能力表；由 scripts/generate_config_docs.py 写入并做漂移检查。"""
    lines = ['| 适配器 | 参数 | 类型 | 默认值 | 工作台可覆盖 | 范围/说明 |', '|---|---|---|---|---|---|']
    for t, spec in specs.items():
        for o in spec.options:
            rule = []
            if o.choices:
                rule.append('取值 ' + '/'.join(map(str, o.choices)))
            if o.positive:
                rule.append('> 0')
            elif o.min is not None:
                rule.append(f'>= {o.min:g}')
            if o.secret_ref:
                rule.append('只填环境变量名')
            detail = '；'.join(rule + ([o.doc] if o.doc else []))
            lines.append(f'| `{t}` | `{o.key}` | `{_type_name(o.type)}` | {_default_text(o)} | '
                         f'{"是" if o.overridable else ""} | {_cell(detail)} |')
    # usage_reporting 尚未逐个核实，暂不写入文档。
    lines += ['', '| 适配器 | 已验证版本 | 选模型 | 思考强度 | 思考模式 | 文件工具 |',
              '|---|---|---|---|---|---|']
    for t, spec in specs.items():
        c = spec.capabilities
        lines.append('| `{}` | {} | {} | {} | {} | {} |'.format(
            t, _cell(', '.join(spec.verified_versions) or '—'), '是' if c.model_select else '',
            _cell('/'.join(c.reasoning_effort) or '—'), _cell('/'.join(c.thinking) or '—'),
            '是' if c.file_tools else ''))
    return '\n'.join(lines) + '\n'
