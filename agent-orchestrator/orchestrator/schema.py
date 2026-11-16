"""配置字段元数据工具；dataclass 是字段、默认值与约束的唯一声明处。"""
from __future__ import annotations

import copy
from dataclasses import MISSING, asdict, field, fields, is_dataclass
from functools import lru_cache


def config_field(description, *, default=MISSING, default_factory=MISSING,
                 numeric=False, integer=False, minimum=0, maximum=None,
                 constraint_minimum=None, limit_message='', validation_order=0,
                 input_default=MISSING, required=None):
    metadata=dict(config=True, description=description, numeric=numeric, integer=integer,
                  minimum=minimum, maximum=maximum, constraint_minimum=constraint_minimum,
                  limit_message=limit_message, validation_order=validation_order, required=required)
    if input_default is not MISSING:metadata['input_default']=input_default
    return field(default=default, default_factory=default_factory, metadata=metadata)


@lru_cache(maxsize=None)
def _field_map(cls):
    return {f.name:f for f in fields(cls)}


def default_value(cls, name):
    f=_field_map(cls)[name]
    if 'input_default' in f.metadata:
        return copy.deepcopy(f.metadata['input_default'])
    if f.default is not MISSING:
        return copy.deepcopy(f.default)
    if f.default_factory is not MISSING:
        return f.default_factory()
    raise KeyError(f'{cls.__name__}.{name} 没有默认值')


def field_metadata(cls, name):
    return dict(_field_map(cls)[name].metadata)


def describe(cls):
    result={}
    for name,f in _field_map(cls).items():
        if not f.metadata.get('config'):
            continue
        required=(f.default is MISSING and f.default_factory is MISSING
                  and 'input_default' not in f.metadata)
        if f.metadata.get('required') is not None:required=f.metadata['required']
        value=None if required else default_value(cls,name)
        if is_dataclass(value):value=asdict(value)
        result[name]={**dict(f.metadata), 'type':str(f.type), 'default':value, 'required':required}
    return result


def render_table(sections):
    lines=['| 配置表 | 字段 | 类型 | 默认值 | 范围/说明 |',
           '|---|---|---|---|---|']
    import json
    nested_types={cls.__name__ for cls in sections.values()}
    for section,cls in sections.items():
        for name,info in describe(cls).items():
            default=('必填' if info['required'] else '{}' if info['type'] in nested_types
                     else json.dumps(info['default'],ensure_ascii=False,separators=(',',':')))
            bounds=''
            if info['numeric']:
                low=info['constraint_minimum'] if info['constraint_minimum'] is not None else info['minimum']
                bounds=f'>= {low}'+(f'，<= {info["maximum"]}' if info['maximum'] is not None else '')+'；'
            detail=(bounds+info['description']).replace('|','\\|').replace('\n',' ')
            lines.append(f'| {section} | `{name}` | `{info["type"]}` | `{default}` | {detail} |')
    return '\n'.join(lines)+'\n'
