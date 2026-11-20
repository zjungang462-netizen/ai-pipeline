"""只描述已采样的 init/result 协议；成功判定、权限与计量留在各适配器。"""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class ResultStream:
    version_field: str
    accepted_versions: frozenset[str]
    terminal_type: str = 'result'
    allow_array: bool = False
    allow_result_object: bool = False
    protocol_version: str | None = None

    def parse(self, stdout: str, version: str) -> tuple[dict, dict]:
        if version not in self.accepted_versions:
            raise ValueError('未验证的流式版本')
        single = False
        if self.allow_result_object:
            try:
                events = [json.loads(stdout)]
                single = True
            except json.JSONDecodeError:
                events = self._lines(stdout)
        elif self.allow_array and stdout.lstrip().startswith('['):
            events = json.loads(stdout)
        else:
            events = self._lines(stdout)
        if not isinstance(events, list) or not events or not all(isinstance(e, dict) for e in events):
            raise ValueError('非法事件流')
        init = {}
        if not single:
            init = next((e for e in events if e.get('type') == 'system' and e.get('subtype') == 'init'), {})
            if (init.get(self.version_field) != version
                    or self.protocol_version is not None and init.get('protocol_version') != self.protocol_version):
                raise ValueError('初始化版本或协议不匹配')
        if events[-1].get('type') != self.terminal_type or any(
                event.get('type') == self.terminal_type for event in events[:-1]):
            raise ValueError('需要唯一末尾终态')
        return init, events[-1]

    @staticmethod
    def _lines(stdout: str) -> list:
        return [json.loads(line) for line in stdout.splitlines() if line.strip()]


RESULT_STREAMS = {
    'cli_qwen': ResultStream('qwen_code_version', frozenset({'0.26.0'}), allow_array=True),
    'cli_qoder': ResultStream('qodercli_version', frozenset({'1.1.89'}),
                              allow_result_object=True, protocol_version='1.5.0'),
    'cli_claude': ResultStream('claude_code_version', frozenset({'2.1.325', '2.1.328'})),
}


def jsonl_objects(stdout: str):
    """仅用于已允许混合诊断行的协议；严格协议继续逐行校验。"""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event
