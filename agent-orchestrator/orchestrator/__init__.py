"""多智能体开发流水线编排器（零外部依赖，Python 3.11+ 标准库）。"""
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
