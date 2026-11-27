"""多智能体开发流水线编排器（零外部依赖，Python 3.11+ 标准库）。"""
import sys

if sys.version_info < (3, 11):
    _found = sys.version.split()[0]
    raise ImportError(f"需要 Python 3.11 或更高版本（当前 {_found}）/ "
                      f"Python 3.11+ is required (found {_found})")

from .contracts import (ErrorKind, PreflightReport, Result, RunContext,
                        StageSpec, StageStatus)
from .config import Config, ConfigError, load_config, parse_config
from .state import RunState, StageLock, compute_inputs_hash
from .engine import Engine, BudgetExceeded
from ._version import __version__

__all__ = [
    '__version__',
    "ErrorKind", "PreflightReport", "Result", "RunContext", "StageSpec",
    "StageStatus", "Config", "ConfigError", "load_config", "parse_config",
    "RunState", "StageLock", "compute_inputs_hash", "Engine", "BudgetExceeded",
]
