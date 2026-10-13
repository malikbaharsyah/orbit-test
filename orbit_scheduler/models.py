"""The task data model -- Refactoring Goal 1, "Task data model".

Two types carry the whole domain:

:class:`Task`
    *What should happen.* Immutable, because a task is a statement of intent: once the
    schedule is loaded, nothing downstream has any business rewriting it. Immutability
    also makes a task safe to share across threads and safe to use as a dict key, which
    the legacy mutable dicts were not.

:class:`ExecutionResult`
    *What actually happened.* Keeping this separate from :class:`Task` is the single most
    important boundary in the package. The legacy code conflated the two by mutating
    ``users[user]['executed']`` mid-loop, which is why its state could not be inspected,
    reported on, or tested.

The extension point is :attr:`Task.params`. A new action type reads whatever keys it
needs out of that mapping; neither this module nor the scheduler ever learns their names.
Adding "compress tasks need a level" requires no change here.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

__all__ = ["TIME_FORMAT", "ExecutionResult", "ExecutionStatus", "Task", "parse_time"]

TIME_FORMAT = "%H:%M"

# Keys from_dict() understands directly. Anything else in the input dict is folded into
# Task.params, which is what lets the legacy flat task shape load unchanged.
_RESERVED_KEYS = frozenset(
    {"id", "user", "user_id", "time", "scheduled_at", "action", "enabled", "params"}
)


def parse_time(value: str | dt.time) -> dt.time:
    """Parse an ``HH:MM`` string into a :class:`datetime.time`.

    Parsing once, at load time, is what kills the legacy string-comparison bug: the
    scheduler then compares times, not text, and a malformed value is rejected at startup
    instead of silently never matching.
    """
    if isinstance(value, dt.time):
        return value
    if not isinstance(value, str):
        raise ValueError(
            f"expected an HH:MM string or datetime.time, got {type(value).__name__}"
        )
    try:
        return dt.datetime.strptime(value.strip(), TIME_FORMAT).time()
    except ValueError:
        raise ValueError(f"expected a time of the form HH:MM, got {value!r}") from None


class ExecutionStatus(str, Enum):
    """The outcome of attempting one task.

    Subclasses :class:`str` so statuses serialise straight to JSON and read naturally in
    logs, while still comparing by identity (``status is ExecutionStatus.SUCCESS``).
    """

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED_QUOTA = "skipped_quota"
    SKIPPED_DISABLED = "skipped_disabled"
    SKIPPED_UNKNOWN_USER = "skipped_unknown_user"
    SKIPPED_UNKNOWN_ACTION = "skipped_unknown_action"

    @property
    def is_skip(self) -> bool:
        """True when the task was deliberately not run, as opposed to run and failed."""
        return self.name.startswith("SKIPPED_")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Task:
    """One scheduled unit of work.

    :param id: Stable identity. Used to remember that this task already ran today, so it
        must not change between runs -- derive it from the config, never from list order.
    :param user_id: Owner. Their daily quota governs whether this runs.
    :param action: Which executor handles it. Resolved through the executor registry.
    :param scheduled_at: Local time of day the task becomes due.
    :param params: Free-form, executor-specific arguments. Read-only once constructed.
    :param enabled: ``False`` parks the task without deleting it from the config.
    """

    id: str
    user_id: str
    action: str
    scheduled_at: dt.time
    params: Mapping[str, Any] = field(default_factory=dict, hash=False)
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("task id must not be empty")
        if not self.user_id:
            raise ValueError(f"task {self.id!r}: user_id must not be empty")
        if not self.action:
            raise ValueError(f"task {self.id!r}: action must not be empty")
        # Freeze params so an executor cannot mutate shared state behind the scheduler's
        # back. Bypasses the frozen-dataclass guard deliberately.
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], *, default_id: str | None = None) -> Task:
        """Build a task from a plain dictionary.

        Accepts both the modern nested shape and the legacy flat one, so an existing
        system can migrate without rewriting its configuration first::

            Task.from_dict({"user": "alice", "time": "12:00",
                            "action": "sync", "target": "/data/x"})

        ``target`` is not a reserved key, so it is folded into ``params`` automatically.
        Explicit ``params`` and loose keys are merged, with explicit ``params`` winning.
        """
        if not isinstance(raw, Mapping):
            raise ValueError(f"expected a task object, got {type(raw).__name__}")

        extras = {k: v for k, v in raw.items() if k not in _RESERVED_KEYS}
        declared = raw.get("params") or {}
        if not isinstance(declared, Mapping):
            raise ValueError("'params' must be an object")

        user_id = raw.get("user_id") or raw.get("user") or ""
        when = raw.get("scheduled_at", raw.get("time"))
        if when is None:
            raise ValueError("missing 'time' (HH:MM)")

        return cls(
            id=str(raw.get("id") or default_id or ""),
            user_id=str(user_id),
            action=str(raw.get("action") or ""),
            scheduled_at=parse_time(when),
            params={**extras, **declared},
            enabled=bool(raw.get("enabled", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        """Round-trip back to a plain dictionary, for persistence or an API response."""
        return {
            "id": self.id,
            "user": self.user_id,
            "time": self.scheduled_at.strftime(TIME_FORMAT),
            "action": self.action,
            "params": dict(self.params),
            "enabled": self.enabled,
        }

    def due_at(self, on_date: dt.date) -> dt.datetime:
        """The exact moment this task becomes due on ``on_date``."""
        return dt.datetime.combine(on_date, self.scheduled_at)

    def describe(self) -> str:
        """A one-line human summary, used in logs and reports."""
        target = self.params.get("target")
        suffix = f" on {target}" if target else ""
        clock = self.scheduled_at.strftime(TIME_FORMAT)
        return f"{self.action}{suffix} for {self.user_id} at {clock}"


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """The record of one attempt at one task.

    Returned rather than printed, so callers can assert on it in tests, aggregate it into
    a :class:`~orbit_scheduler.scheduler.RunReport`, ship it to a metrics backend, or
    persist it -- none of which the legacy ``print`` allowed.
    """

    task_id: str
    user_id: str
    action: str
    status: ExecutionStatus
    detail: str = ""
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True only when the action actually ran and succeeded."""
        return self.status is ExecutionStatus.SUCCESS

    @property
    def duration_ms(self) -> float | None:
        """Wall-clock duration, or ``None`` if the attempt was not timed."""
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds() * 1000.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "user": self.user_id,
            "action": self.action,
            "status": self.status.value,
            "detail": self.detail,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }
