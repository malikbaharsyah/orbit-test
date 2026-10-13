"""ORBIT task scheduling and resource limitation.

A small, dependency-free scheduler that runs user-owned tasks once a day, within a daily
per-user quota, through an open set of pluggable actions.

Everything another team needs is re-exported here, so the common import is one line::

    from orbit_scheduler import load_schedule, build_scheduler, configure_logging

    configure_logging()
    config = load_schedule({
        "users": {"alice": {"quota": 3}},
        "tasks": [{"user": "alice", "time": "12:00", "action": "sync",
                   "params": {"target": "/data/x"}}],
    })
    report = build_scheduler(config).tick()
    print(report.summary())

Importing deeper modules stays supported and is the right thing when writing an executor
or substituting an implementation. This list is a convenience, not a fence.

Module map
----------
======================  =====================================================
``models``              ``Task``, ``ExecutionResult`` -- the data model
``users``               ``User``, quota control
``executors``           ``TaskExecutor`` and the registry -- the extension point
``scheduler``           ``Scheduler``, ``RunLog``, ``RunReport``
``config``              dict/JSON in, validated objects out
``clock``               injectable time, for deterministic tests
``logging_setup``       application logging and contextual adapters
``errors``              one exception hierarchy for the whole package
======================  =====================================================
"""

from __future__ import annotations

from .clock import Clock, FrozenClock, SystemClock
from .config import ScheduleConfig, build_scheduler, load_schedule, load_schedule_file
from .errors import (
    ConfigError,
    InvalidParamsError,
    OrbitSchedulerError,
    QuotaExceededError,
    TaskExecutionError,
    UnknownActionError,
    UnknownUserError,
    UserDisabledError,
)
from .executors import (
    ExecutionContext,
    ExecutorRegistry,
    TaskExecutor,
    default_registry,
)
from .logging_setup import TaskLogAdapter, configure_logging, get_logger
from .models import ExecutionResult, ExecutionStatus, Task
from .scheduler import InMemoryRunLog, RunLog, RunReport, Scheduler
from .users import QuotaLedger, QuotaManager, User, UserRegistry

__version__ = "1.0.0"

# Grouped by concern rather than sorted alphabetically: the grouping tells a reader
# which parts of the package belong together.
__all__ = [  # noqa: RUF022
    "__version__",
    # configuration and assembly -- the usual entry point
    "ScheduleConfig",
    "load_schedule",
    "load_schedule_file",
    "build_scheduler",
    # data model
    "Task",
    "ExecutionResult",
    "ExecutionStatus",
    # users and quota
    "User",
    "UserRegistry",
    "QuotaLedger",
    "QuotaManager",
    # execution -- the extension point
    "TaskExecutor",
    "ExecutorRegistry",
    "ExecutionContext",
    "default_registry",
    # scheduling
    "Scheduler",
    "RunReport",
    "RunLog",
    "InMemoryRunLog",
    # time
    "Clock",
    "SystemClock",
    "FrozenClock",
    # logging
    "configure_logging",
    "get_logger",
    "TaskLogAdapter",
    # errors
    "OrbitSchedulerError",
    "ConfigError",
    "InvalidParamsError",
    "UnknownUserError",
    "UserDisabledError",
    "UnknownActionError",
    "QuotaExceededError",
    "TaskExecutionError",
]
