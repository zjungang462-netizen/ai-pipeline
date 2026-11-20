"""适配器与引擎共用的结构化等待回执。"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .state import atomic_write_json

_HEADER = re.compile(r"(?m)^```(?:json)?[ \t]*\r?\n# file: stage_result\.json[ \t]*\r?$")
_CLOSE = re.compile(r"(?m)^```[ \t]*\r?$")


def validate_deferred(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("stage_result.json 根必须是对象")
    allowed = {"outcome", "reason", "condition", "resume_action", "next_check_at"}
    if set(value) - allowed:
        raise ValueError("stage_result.json 含未知字段")
    outcome = value.get("outcome")
    if outcome not in ("waiting_external", "waiting_owner"):
        raise ValueError("outcome 只能是 waiting_external 或 waiting_owner")
    reason = value.get("reason")
    action = value.get("resume_action")
    condition = value.get("condition", "")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValueError("reason 必须是 1 到 2000 字符")
    if not isinstance(action, str) or not action.strip() or len(action) > 2000:
        raise ValueError("resume_action 必须是 1 到 2000 字符")
    if not isinstance(condition, str) or len(condition) > 2000:
        raise ValueError("condition 最长 2000 字符")
    if outcome == "waiting_external" and not condition.strip():
        raise ValueError("waiting_external 必须说明可核实的 condition")
    next_check = value.get("next_check_at")
    if next_check is not None and (type(next_check) not in (int, float)
                                   or not 0 <= next_check <= 253402300799):
        raise ValueError("next_check_at 必须是有效的非负 Unix 时间戳或 null")
    return {"outcome": outcome, "reason": reason.strip(),
            "condition": condition.strip(), "resume_action": action.strip(),
            "next_check_at": next_check}


def extract_deferred(text: str, attempt_dir: Path) -> dict | None:
    matches = list(_HEADER.finditer(text))
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("stage_result.json 只能出现一次")
    start = matches[0].end()
    close = _CLOSE.search(text, start, min(len(text), start + 32768))
    if close is None or text[close.end():].strip():
        raise ValueError("stage_result.json 必须是结尾处一个完整的回执块")
    try:
        value = json.loads(text[start:close.start()])
    except (ValueError, TypeError) as error:
        raise ValueError("stage_result.json 不是合法 JSON") from error
    result = validate_deferred(value)
    atomic_write_json(Path(attempt_dir) / "stage_result.json", result)
    return result
