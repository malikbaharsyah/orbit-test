"""Exception hierarchy for :mod:`orbit_scheduler`.

Every error this package raises derives from :class:`OrbitSchedulerError`. An
integrating team can therefore write one ``except OrbitSchedulerError`` and be certain
nothing from this library escapes uncaught -- without needing to know the names of our
individual failure modes.

Error messages are written to be actionable on their own: they name the offending value
*and* the valid alternatives, because the person reading the log at 03:00 is usually not
the person who wrote the code.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

__all__ = [
    "ConfigError",
    "InvalidParamsError",
    "OrbitSchedulerError",
    "QuotaExceededError",
    "TaskExecutionError",
    "UnknownActionError",
    "UnknownUserError",
    "UserDisabledError",
]


class OrbitSchedulerError(Exception):
    """Base class for every error raised by this package."""


class ConfigError(OrbitSchedulerError):
    """A schedule configuration is invalid.

    Carries *every* problem found during validation, not just the first one, so a team
    fixing a bad config learns about all of their mistakes in a single pass.
    """

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems: list[str] = list(problems)
        count = len(self.problems)
        bullets = "\n  - ".join(self.problems)
        super().__init__(f"{count} configuration problem(s) found:\n  - {bullets}")


class InvalidParamsError(OrbitSchedulerError):
    """An executor rejected a task's parameters.

    Raised from :meth:`~orbit_scheduler.executors.base.TaskExecutor.validate_params`,
    which runs at configuration-load time -- so a mistyped parameter fails at startup
    rather than at 12:00 when nobody is watching.
    """


class UnknownUserError(OrbitSchedulerError):
    """A task referenced a user that does not exist."""

    def __init__(self, user_id: str, known: Iterable[str] = ()) -> None:
        self.user_id = user_id
        self.known = sorted(known)
        hint = (
            f" Known users: {', '.join(self.known)}."
            if self.known
            else " No users are registered."
        )
        super().__init__(f"Unknown user {user_id!r}.{hint}")


class UserDisabledError(OrbitSchedulerError):
    """A task belongs to a user whose account is switched off."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        super().__init__(f"User {user_id!r} is disabled; their tasks will not run.")


class UnknownActionError(OrbitSchedulerError):
    """No executor is registered for a task's action."""

    def __init__(self, action: str, available: Iterable[str] = ()) -> None:
        self.action = action
        self.available = sorted(available)
        hint = (
            f" Registered actions: {', '.join(self.available)}."
            if self.available
            else " No executors are registered."
        )
        super().__init__(f"No executor registered for action {action!r}.{hint}")


class QuotaExceededError(OrbitSchedulerError):
    """A user has already used their whole allowance for the day."""

    def __init__(self, user_id: str, quota: int, on_date: dt.date) -> None:
        self.user_id = user_id
        self.quota = quota
        self.on_date = on_date
        super().__init__(
            f"User {user_id!r} has used their daily quota of {quota} on {on_date.isoformat()}."
        )


class TaskExecutionError(OrbitSchedulerError):
    """An executor failed to carry out its action.

    Executors signal failure by raising. Raising this type (rather than a bare
    ``Exception``) tells the scheduler the failure came from the action itself and not
    from the scheduling machinery around it.
    """

    def __init__(self, message: str, *, task_id: str | None = None) -> None:
        self.task_id = task_id
        super().__init__(f"[task={task_id}] {message}" if task_id else message)
