"""从配置字段元数据生成 README 配置表及示例注释；--check 检查漂移。"""
from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from orchestrator.config import CONFIG_SECTIONS
from orchestrator.schema import describe, render_table
from orchestrator.adapters import registry
from orchestrator.adapters.spec import render_adapter_tables

START='<!-- CONFIG_SCHEMA:START -->'
END='<!-- CONFIG_SCHEMA:END -->'
ADAPTER_START='<!-- ADAPTER_SCHEMA:START -->'
ADAPTER_END='<!-- ADAPTER_SCHEMA:END -->'
PREFIX='# schema: '


def annotated_example(text):
    section='root'
    schemas={name:describe(cls) for name,cls in CONFIG_SECTIONS.items()}
    out=[]
    for line in text.splitlines():
        if line.startswith(PREFIX):continue
        stripped=line.strip()
        if stripped.startswith('[['):
            section=stripped[2:].split(']]',1)[0].strip()
        elif stripped.startswith('['):
            section=stripped[1:].split(']',1)[0].strip()
        match=re.match(r'^(\w+)\s*=',stripped)
        if match and match[1] in schemas.get(section,{}):
            info=schemas[section][match[1]]
            out.append(PREFIX+section+'.'+match[1]+'：'+info['description'])
        out.append(line)
    result='\n'.join(out)+'\n'
    if tomllib.loads(text)!=tomllib.loads(result):
        raise ValueError('生成注释不得改变示例配置含义')
    return result


def _replace_block(text, start, end, body, *, required):
    count=(text.count(start),text.count(end))
    if count==(0,0) and not required:
        return text
    if count!=(1,1):
        raise ValueError(f'README 必须含有唯一的 {start} 生成标记')
    before,rest=text.split(start)
    _,after=rest.split(end)
    return before+start+'\n'+body+end+after


def generated_files(root=ROOT):
    root=Path(root)
    path=root/'README.md';text=path.read_text(encoding='utf8')
    text=_replace_block(text,START,END,render_table(CONFIG_SECTIONS),required=True)
    # 适配器表标记可选：没有标记的 README（如测试临时目录）不生成该表。
    text=_replace_block(text,ADAPTER_START,ADAPTER_END,render_adapter_tables(registry.specs()),required=False)
    result={path:text}
    result.update({p:annotated_example(p.read_text(encoding='utf8')) for p in sorted(root.glob('*.example.toml'))})
    return result


def check_generated(root=ROOT):
    return [str(path) for path,text in generated_files(root).items() if path.read_text(encoding='utf8')!=text]


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args(argv)
    files=generated_files()
    changed=[str(path) for path,text in files.items() if path.read_text(encoding='utf8')!=text]
    if not args.check:
        for path,text in files.items():path.write_text(text,encoding='utf8')
    print(('生成文件存在漂移：' if args.check and changed else '配置文档同步：')+str(len(changed)))
    for name in changed:print(name)
    return 1 if args.check and changed else 0


if __name__=='__main__':raise SystemExit(main())
