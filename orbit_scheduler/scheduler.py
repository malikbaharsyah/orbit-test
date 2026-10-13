"""The scheduling system -- Refactoring Goal 1, fourth required module.

:class:`Scheduler` decides what is due, asks the quota manager for permission, delegates
the work to an executor, and records the outcome. It deliberately does none of those
jobs itself: every collaborator is injected, so each can be replaced without this file
changing.

    Scheduler(tasks=..., quota=QuotaManager(...), registry=ExecutorRegistry(),
              clock=SystemClock(), run_log=InMemoryRunLog())

Swap ``run_log`` for a database-backed one, ``clock`` for a frozen one in tests,
``quota`` for a Redis-backed manager when you outgrow a single process -- the logic below
is untouched in all three cases.

What this fixes from the legacy loop
------------------------------------
*Double execution.* ``if task['time'] == now`` fires on every tick inside the scheduled
minute. Two ticks a few seconds apart ran every task twice. :class:`RunLog` records that
a task has run today, so a tick is idempotent.

*Silently dropped tasks.* The same equality check means that if no tick lands inside the
scheduled minute -- a slow run, a restart, a GC pause -- the task never runs at all and
nothing says so. Here a task is due from its scheduled time onward, so a late tick still
catches it, bounded by ``catchup_window`` so a service that was down for a day does not
stampede on restart.

*A single bad task killing the run.* ``users[task['user']]`` raised ``KeyError`` straight
out of the loop, so one task naming a deleted user silently stopped every task after it
from running. Here each task is isolated: a failure is logged with its traceback,
recorded as a result, and the loop continues.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .clock import Clock, SystemClock
from .errors import (
    QuotaExceededError,
    UnknownActionError,
    UnknownUserError,
    UserDisabledError,
)
from .executors.base import ExecutionContext, ExecutorRegistry
from .logging_setup import TaskLogAdapter
from .models import ExecutionResult, ExecutionStatus, Task
from .users import QuotaManager

__all__ = ["DEFAULT_CATCHUP_WINDOW", "InMemoryRunLog", "RunLog", "RunReport", "Scheduler"]

logger = logging.getLogger(__name__)

#: How late a tick may be and still run a task it missed. One hour absorbs restarts and
#: slow runs without letting a day-long outage replay yesterday's whole schedule.
DEFAULT_CATCHUP_WINDOW = dt.timedelta(hours=1)


@runtime_checkable
class RunLog(Protocol):
    """Remembers which tasks have already run on which day.

    A Protocol, not a base class, so a team can back this with Postgres or Redis without
    importing anything from here. The persistent implementation is also where
    exactly-once semantics come from at scale: a unique constraint on
    ``(task_id, date)`` makes a double-dispatch a no-op rather than a duplicate run.
    """

    def has_run(self, task_id: str, on_date: dt.date) -> bool: ...

    def mark(self, task_id: str, on_date: dt.date) -> None: ...


class InMemoryRunLog:
    """Process-local run log. The default, and all a single-process service needs.

    Thread-safe. State is lost on restart, which means a restart may re-run a task that
    already ran today -- acceptable for the simple deployment this targets, and the
    reason the README recommends a persistent run log before running more than one
    scheduler process.
    """

    def __init__(self) -> None:
        self._seen: set[tuple[str, dt.date]] = set()
        self._lock = threading.Lock()

    def has_run(self, task_id: str, on_date: dt.date) -> bool:
        with self._lock:
            return (task_id, on_date) in self._seen

    def mark(self, task_id: str, on_date: dt.date) -> None:
        with self._lock:
            self._seen.add((task_id, on_date))

    def purge_before(self, cutoff: dt.date) -> int:
        """Drop entries older than ``cutoff``; returns how many were removed."""
        with self._lock:
            stale = {entry for entry in self._seen if entry[1] < cutoff}
            self._seen -= stale
            return len(stale)

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)

    def __repr__(self) -> str:
        return f"InMemoryRunLog({len(self)} entries)"


@dataclass(frozen=True, slots=True)
class RunReport:
    """Everything that happened during one tick.

    Returned by :meth:`Scheduler.tick` so a caller can assert on it, aggregate it, emit
    metrics from it, or persist it. The legacy loop returned nothing and printed instead,
    which is why it could not be tested.
    """

    run_id: str
    started_at: dt.datetime
    finished_at: dt.datetime
    results: tuple[ExecutionResult, ...] = field(default_factory=tuple)

    @property
    def counts(self) -> dict[str, int]:
        """Number of results per status value, for metrics."""
        tally: dict[str, int] = {}
        for result in self.results:
            tally[result.status.value] = tally.get(result.status.value, 0) + 1
        return tally

    @property
    def succeeded(self) -> tuple[ExecutionResult, ...]:
        return tuple(r for r in self.results if r.ok)

    @property
    def failed(self) -> tuple[ExecutionResult, ...]:
        return tuple(r for r in self.results if r.status is ExecutionStatus.FAILED)

    @property
    def skipped(self) -> tuple[ExecutionResult, ...]:
        return tuple(r for r in self.results if r.status.is_skip)

    def summary(self) -> str:
        """One line for the log: ``3 attempted: 2 success, 1 skipped_quota``."""
        if not self.results:
            return "nothing due"
        parts = ", ".join(f"{n} {status}" for status, n in sorted(self.counts.items()))
        return f"{len(self.results)} attempted: {parts}"

    def __iter__(self) -> Iterator[ExecutionResult]:
        return iter(self.results)

    def __len__(self) -> int:
        return len(self.results)


class Scheduler:
    """Runs due tasks, within quota, and reports what happened.

    :param tasks: The schedule. Tasks can also be added later with :meth:`add_task`.
    :param quota: Decides whether a user may run another task today.
    :param registry: Resolves a task's action to an executor.
    :param clock: Source of "now". Inject a
        :class:`~orbit_scheduler.clock.FrozenClock` to make tests deterministic.
    :param run_log: Remembers what already ran today, making :meth:`tick` idempotent.
    :param catchup_window: How late a tick may be and still run a missed task.
        ``None`` means any tick later the same day will run it.
    :param dry_run: Passed to every executor; nothing is actually performed.
    :param logger_name: Root logger name for this scheduler's output.
    """

    def __init__(
        self,
        *,
        tasks: Iterable[Task] = (),
        quota: QuotaManager,
        registry: ExecutorRegistry,
        clock: Clock | None = None,
        run_log: RunLog | None = None,
        catchup_window: dt.timedelta | None = DEFAULT_CATCHUP_WINDOW,
        dry_run: bool = False,
        logger_name: str = __name__,
    ) -> None:
        self._tasks: list[Task] = list(tasks)
        self._quota = quota
        self._registry = registry
        self._clock = clock if clock is not None else SystemClock()
        self._run_log = run_log if run_log is not None else InMemoryRunLog()
        self._catchup_window = catchup_window
        self._dry_run = dry_run
        self._log = TaskLogAdapter(logging.getLogger(logger_name), {})
        self._lock = threading.Lock()

    # -- schedule management ------------------------------------------------

    @property
    def quota(self) -> QuotaManager:
        """The quota manager, for asking how much allowance a user has left."""
        return self._quota

    @property
    def registry(self) -> ExecutorRegistry:
        """The executor registry backing this scheduler."""
        return self._registry

    @property
    def run_log(self) -> RunLog:
        """The run log recording which tasks have already run today."""
        return self._run_log

    @property
    def clock(self) -> Clock:
        """The clock this scheduler reads time from."""
        return self._clock

    @property
    def tasks(self) -> tuple[Task, ...]:
        with self._lock:
            return tuple(self._tasks)

    def add_task(self, task: Task) -> Task:
        """Add a task to the schedule.

        :raises ValueError: a task with the same id is already scheduled. Duplicate ids
            would share a run-log entry, so one of them would silently never run.
        """
        with self._lock:
            if any(existing.id == task.id for existing in self._tasks):
                raise ValueError(f"a task with id {task.id!r} is already scheduled")
            self._tasks.append(task)
        return task

    def remove_task(self, task_id: str) -> bool:
        """Remove a task by id. Returns whether anything was removed."""
        with self._lock:
            before = len(self._tasks)
            self._tasks = [t for t in self._tasks if t.id != task_id]
            return len(self._tasks) != before

    # -- scheduling ---------------------------------------------------------

    def due_tasks(self, now: dt.datetime) -> list[Task]:
        """The tasks that should run at ``now``, in schedule order.

        A task is due when it is enabled, its scheduled time has arrived, the tick is
        not later than ``catchup_window`` past it, and it has not already run today.
        """
        today = now.date()
        due: list[Task] = []
        for task in self.tasks:
            if not task.enabled:
                self._log.debug("Task %s is disabled; not due", task.id)
                continue
            scheduled = task.due_at(today)
            if now < scheduled:
                continue
            if self._catchup_window is not None and now - scheduled > self._catchup_window:
                self._log.debug(
                    "Task %s was due at %s, more than %s ago; outside the catch-up "
                    "window, skipping until tomorrow",
                    task.id,
                    scheduled.isoformat(timespec="minutes"),
                    self._catchup_window,
                )
                continue
            if self._run_log.has_run(task.id, today):
                continue
            due.append(task)
        return due

    def tick(self, now: dt.datetime | None = None) -> RunReport:
        """Run everything due at ``now`` (default: the clock's current time).

        Never raises on behalf of a task: every failure becomes an
        :class:`~orbit_scheduler.models.ExecutionResult` in the returned report.
        """
        started = now if now is not None else self._clock.now()
        run_id = uuid.uuid4().hex[:8]
        run_log = self._log.bind(run=run_id)

        due = self.due_tasks(started)
        run_log.info(
            "Tick at %s -- %d task(s) due%s",
            started.isoformat(timespec="seconds"),
            len(due),
            " [dry run]" if self._dry_run else "",
        )

        results = [self._execute_one(task, started, run_id, run_log) for task in due]
        report = RunReport(
            run_id=run_id,
            started_at=started,
            finished_at=self._clock.now(),
            results=tuple(results),
        )
        run_log.info("Tick complete -- %s", report.summary())
        return report

    def run_forever(
        self,
        interval_seconds: float = 30.0,
        *,
        stop: threading.Event | None = None,
        max_ticks: int | None = None,
    ) -> int:
        """Poll on an interval until ``stop`` is set. Returns the number of ticks run.

        A polling loop is the "simple implementation" the brief allows, and it is the
        right shape at this size: one process, a handful of tasks. The README's scaling
        section describes what replaces it when this becomes a dispatcher feeding a pool
        of workers.

        :param stop: Set it from another thread or a signal handler to shut down
            cleanly, rather than killing the process mid-task.
        :param max_ticks: Stop after this many ticks. For demos and tests.
        """
        ticks = 0
        while stop is None or not stop.is_set():
            self.tick()
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            if stop is not None:
                if stop.wait(interval_seconds):
                    break
            else:
                time.sleep(interval_seconds)
        return ticks

    # -- one task -----------------------------------------------------------

    def _execute_one(
        self,
        task: Task,
        now: dt.datetime,
        run_id: str,
        run_log: TaskLogAdapter,
    ) -> ExecutionResult:
        """Attempt one task. Always returns a result; never propagates an exception.

        The ordering matters: resolve the executor *before* consuming quota, so an
        unroutable action does not cost the user anything.
        """
        task_log = run_log.bind(task=task.id, user=task.user_id, action=task.action)
        today = now.date()
        started = self._clock.now()

        def outcome(
            status: ExecutionStatus, detail: str = "", error: str | None = None
        ) -> ExecutionResult:
            return ExecutionResult(
                task_id=task.id,
                user_id=task.user_id,
                action=task.action,
                status=status,
                detail=detail,
                started_at=started,
                finished_at=self._clock.now(),
                error=error,
            )

        try:
            executor = self._registry.get(task.action)
        except UnknownActionError as exc:
            task_log.error("%s", exc)
            return outcome(ExecutionStatus.SKIPPED_UNKNOWN_ACTION, error=str(exc))

        try:
            with self._quota.reserve(task.user_id, today) as reservation:
                ctx = ExecutionContext(
                    logger=task_log, now=now, run_id=run_id, dry_run=self._dry_run
                )
                detail = executor.execute(task, ctx)
                if self._dry_run:
                    # A rehearsal must leave no trace. Without this the dry run would
                    # spend the user's real allowance and mark the task as done, so the
                    # real run afterwards would skip everything it just previewed.
                    reservation.refund()
                else:
                    # Marked only after the action succeeded, so a failure is retried on
                    # the next tick rather than being recorded as done.
                    self._run_log.mark(task.id, today)
                task_log.info("Executed %s -- %s", task.describe(), detail)
                return outcome(ExecutionStatus.SUCCESS, detail=detail)

        except UnknownUserError as exc:
            task_log.error("Skipping task: %s", exc)
            return outcome(ExecutionStatus.SKIPPED_UNKNOWN_USER, error=str(exc))
        except UserDisabledError as exc:
            task_log.warning("Skipping task: %s", exc)
            return outcome(ExecutionStatus.SKIPPED_DISABLED, error=str(exc))
        except QuotaExceededError as exc:
            task_log.warning("Skipping task: %s", exc)
            return outcome(ExecutionStatus.SKIPPED_QUOTA, error=str(exc))
        except Exception as exc:
            # The quota reservation has already refunded itself on the way out, so a
            # failed action costs the user nothing. exc_info gives the on-call engineer
            # the traceback instead of just a message.
            task_log.error("Task failed: %s", exc, exc_info=True)
            return outcome(
                ExecutionStatus.FAILED, error=f"{type(exc).__name__}: {exc}"
            )

    # -- housekeeping -------------------------------------------------------

    def purge_history(self, before: dt.date) -> None:
        """Drop run-log and quota history older than ``before``.

        Both in-memory stores grow by one entry per task (or user) per day. A service
        that runs for months should call this daily; persistent backends would use a TTL.
        """
        for store in (self._run_log, self._quota.ledger):
            purge = getattr(store, "purge_before", None)
            if callable(purge):
                purge(before)

    def __repr__(self) -> str:
        return (
            f"Scheduler({len(self._tasks)} tasks, actions="
            f"{self._registry.available()}, clock={self._clock!r})"
        )
