"""本机 CLI 路径、版本与解析支持盘点；不登录、不调用模型。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator.adapters.cli_registry import build
from orchestrator.config import AdapterConfig
from orchestrator.state import atomic_write_json


def inventory(qoder_exe=None):
    rows = []
    for name in ('codex', 'qwen', 'dsh', 'agy', 'qoder', 'claude', 'doubao'):
        options = {'exe': qoder_exe} if name == 'qoder' and qoder_exe else {}
        adapter = build(AdapterConfig(name=name, type='cli_' + name, options=options))
        report = adapter.preflight()
        known = (adapter._version_known() if hasattr(adapter, '_version_known')
                 else (name == 'dsh' and report.tool_version == '0.2.2-rc.3')
                 or (name == 'qoder' and report.tool_version == '1.1.89'))
        rows.append(dict(tool=name, installed=bool(report.exe_path), executable=report.exe_path,
                         version=report.tool_version, preflight_ok=report.ok,
                         parser_supported=bool(report.ok and known),
                         authentication='not_probed', file_tools='not_probed'))
    grok = shutil.which('grok')
    version = None
    if grok:
        try:
            process = subprocess.run([grok, '--version'], capture_output=True, text=True,
                                     encoding='utf8', errors='replace', timeout=15)
            if process.returncode == 0:
                version = process.stdout.strip().splitlines()[0]
        except (OSError, subprocess.TimeoutExpired, IndexError):
            pass
    rows.append(dict(tool='grok', installed=bool(grok), executable=grok, version=version,
                     parser_supported=False, adapter_registered=False,
                     authentication='not_probed', file_tools='not_probed'))
    return dict(checked_at=time.time(), model_calls=0, tools=rows,
                scope='local_version_and_parser_support_only')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--qoder-exe')
    args = parser.parse_args(argv)
    output = Path(args.output).absolute()
    if output.exists():
        parser.error('输出文件已存在')
    report = inventory(args.qoder_exe)
    atomic_write_json(output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
