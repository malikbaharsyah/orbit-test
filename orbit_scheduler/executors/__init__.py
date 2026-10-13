"""Task executors: the open set of actions the scheduler can perform.

Importing this package registers the built-in executors into
:data:`~orbit_scheduler.executors.base.default_registry` as a side effect -- that import
is what populates ``sync``, ``backup`` and ``delete``.

To add an action, see "Add a new task type" in the README. The short version::

    from orbit_scheduler.executors import TaskExecutor, default_registry

    @default_registry.register
    class CompressExecutor(TaskExecutor):
        action = "compress"
        description = "Compress the target path in place."

        def execute(self, task, ctx):
            ctx.logger.info("Compressing %s", task.params["target"])
            return f"compressed {task.params['target']}"
"""

from .base import ExecutionContext, ExecutorRegistry, TaskExecutor, default_registry
from .builtin import (
    BUILTIN_EXECUTORS,
    BackupExecutor,
    DeleteExecutor,
    PathTaskExecutor,
    SyncExecutor,
)

__all__ = [
    "BUILTIN_EXECUTORS",
    "BackupExecutor",
    "DeleteExecutor",
    "ExecutionContext",
    "ExecutorRegistry",
    "PathTaskExecutor",
    "SyncExecutor",
    "TaskExecutor",
    "default_registry",
]
