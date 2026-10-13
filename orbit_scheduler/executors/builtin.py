"""The built-in actions: ``sync``, ``backup`` and ``delete``.

These are *simulations*, matching the brief ("Simulated Strategy-Based Task Scheduling").
They log what they would do and return a detail string rather than touching a real
filesystem. The seam where real I/O belongs is marked in each :meth:`execute`.

The DRY bit worth noticing: all three operate on a path, so the rule for what counts as a
valid ``target`` lives once, in :class:`PathTaskExecutor`. Written out three times it
would be three rules that drift apart the first time one of them is tightened.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, ClassVar

from ..errors import InvalidParamsError, TaskExecutionError
from ..models import Task
from .base import ExecutionContext, TaskExecutor, default_registry

__all__ = [
    "BUILTIN_EXECUTORS",
    "BackupExecutor",
    "DeleteExecutor",
    "PathTaskExecutor",
    "SyncExecutor",
]

# POSIX absolute (/srv/y) or Windows absolute (D:\srv\y). Accepting both keeps the
# example config portable across the machines this is demoed on.
_ABSOLUTE_PATH = re.compile(r"^(/|[A-Za-z]:[\\/])")


class PathTaskExecutor(TaskExecutor):
    """Shared base for actions that operate on a filesystem path.

    Not registered itself -- it declares no :attr:`action`, so
    :meth:`ExecutorRegistry.register` would reject it. It exists purely to hold the
    ``target`` contract in one place.
    """

    #: Parameters every subclass requires. Extend in a subclass to add more.
    required_params: ClassVar[tuple[str, ...]] = ("target",)

    def validate_params(self, params: Mapping[str, Any]) -> None:
        missing = [name for name in self.required_params if not params.get(name)]
        if missing:
            raise InvalidParamsError(
                f"action {self.action!r} requires {', '.join(repr(m) for m in missing)}"
            )
        target = str(params["target"])
        if not _ABSOLUTE_PATH.match(target):
            raise InvalidParamsError(
                f"action {self.action!r}: 'target' must be an absolute path, "
                f"got {target!r}"
            )

    @staticmethod
    def target_of(task: Task) -> str:
        """The validated target path for a task."""
        return str(task.params["target"])


@default_registry.register
class SyncExecutor(PathTaskExecutor):
    """Replicate a path to its configured mirror."""

    action = "sync"
    description = "Replicate the target path to its mirror."

    def execute(self, task: Task, ctx: ExecutionContext) -> str:
        target = self.target_of(task)
        if ctx.dry_run:
            ctx.logger.info("Dry run: would sync %s", target)
            return f"dry-run: would sync {target}"
        ctx.logger.info("Syncing %s", target)
        # Real implementation goes here (rsync, object-store copy, ...).
        return f"synced {target}"


@default_registry.register
class BackupExecutor(PathTaskExecutor):
    """Snapshot a path to the backup store."""

    action = "backup"
    description = "Snapshot the target path to the backup store."

    def execute(self, task: Task, ctx: ExecutionContext) -> str:
        target = self.target_of(task)
        retention = task.params.get("retention_days", 30)
        if ctx.dry_run:
            ctx.logger.info("Dry run: would back up %s", target)
            return f"dry-run: would back up {target}"
        ctx.logger.info("Backing up %s (retention %s days)", target, retention)
        # Real implementation goes here (snapshot, upload, register in catalogue).
        return f"backed up {target} (retention {retention}d)"


@default_registry.register
class DeleteExecutor(PathTaskExecutor):
    """Delete a path, refusing anything outside the allowed roots.

    The guard is the point. ``delete`` is the one built-in action that destroys data, and
    the legacy code would happily have run it against any path a config typo produced.
    Scheduling a delete outside :attr:`allowed_roots` fails at *load* time, so the bad
    config never reaches production rather than being caught at 12:00.
    """

    action = "delete"
    description = "Delete the target path (restricted to temporary roots)."

    #: Prefixes a delete may target. Deliberately narrow; widen it in config review, not
    #: in a hurry at 03:00.
    allowed_roots: ClassVar[tuple[str, ...]] = ("/tmp/", "/var/tmp/")

    def validate_params(self, params: Mapping[str, Any]) -> None:
        super().validate_params(params)
        target = str(params["target"])
        if not any(target.startswith(root) for root in self.allowed_roots):
            raise InvalidParamsError(
                f"action 'delete' refuses {target!r}: deletes are restricted to "
                f"{', '.join(self.allowed_roots)}"
            )

    def execute(self, task: Task, ctx: ExecutionContext) -> str:
        target = self.target_of(task)
        # Defence in depth: validate_params already ran at load time, but a task added
        # at runtime through add_task() or an API has not necessarily been through it.
        if not any(target.startswith(root) for root in self.allowed_roots):
            raise TaskExecutionError(
                f"refusing to delete {target!r} outside {self.allowed_roots}",
                task_id=task.id,
            )
        if ctx.dry_run:
            ctx.logger.info("Dry run: would delete %s", target)
            return f"dry-run: would delete {target}"
        ctx.logger.info("Deleting %s", target)
        # Real implementation goes here (unlink, recursive remove, tombstone).
        return f"deleted {target}"


#: Every executor this module ships, for documentation and tests.
BUILTIN_EXECUTORS: tuple[type[TaskExecutor], ...] = (
    SyncExecutor,
    BackupExecutor,
    DeleteExecutor,
)
