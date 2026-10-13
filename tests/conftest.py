"""Shared fixtures and test doubles.

Every test drives the scheduler through a :class:`~orbit_scheduler.clock.FrozenClock`,
so nothing here sleeps, nothing is timing-dependent, and the whole suite is deterministic
whatever time of day it runs at. That is the payoff of injecting the clock rather than
calling ``datetime.now()`` inside the scheduler.

Each test also gets its own registry (via ``default_registry.copy()``) and its own run
log, so registering a double in one test can never leak into another.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from orbit_scheduler import (
    ExecutionContext,
    ExecutorRegistry,
    FrozenClock,
    InMemoryRunLog,
    QuotaManager,
    Scheduler,
    Task,
    TaskExecutor,
    UserRegistry,
    default_registry,
)
from orbit_scheduler.models import parse_time

#: The moment every test starts at: five seconds past noon, so "the scheduled minute has
#: begun" is true without the test having to think about it.
NOON = dt.datetime(2026, 3, 2, 12, 0, 5)
TOMORROW = NOON + dt.timedelta(days=1)


class RecordingExecutor(TaskExecutor):
    """Succeeds, and remembers every task it was handed."""

    action = "record"
    description = "Test double: records calls and succeeds."

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.contexts: list[ExecutionContext] = []

    def execute(self, task: Task, ctx: ExecutionContext) -> str:
        self.calls.append(task.id)
        self.contexts.append(ctx)
        return f"recorded {task.id}"

    @property
    def call_count(self) -> int:
        return len(self.calls)


class ExplodingExecutor(TaskExecutor):
    """Always raises, for testing failure isolation and quota refunds."""

    action = "explode"
    description = "Test double: always raises."

    def __init__(self) -> None:
        self.attempts = 0

    def execute(self, task: Task, ctx: ExecutionContext) -> str:
        self.attempts += 1
        raise RuntimeError("boom")


def make_task(
    task_id: str,
    *,
    user: str = "alice",
    action: str = "record",
    time: str = "12:00",
    enabled: bool = True,
    **params: Any,
) -> Task:
    """Build a task without the ceremony, so tests read as intent."""
    return Task(
        id=task_id,
        user_id=user,
        action=action,
        scheduled_at=parse_time(time),
        params=params,
        enabled=enabled,
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOON)


@pytest.fixture
def registry() -> ExecutorRegistry:
    """An isolated registry holding the built-ins plus the two test doubles."""
    reg = default_registry.copy()
    reg.register(RecordingExecutor)
    reg.register(ExplodingExecutor)
    return reg


@pytest.fixture
def recorder(registry: ExecutorRegistry) -> RecordingExecutor:
    return registry.get("record")  # type: ignore[return-value]


@pytest.fixture
def exploder(registry: ExecutorRegistry) -> ExplodingExecutor:
    return registry.get("explode")  # type: ignore[return-value]


@pytest.fixture
def users() -> UserRegistry:
    return UserRegistry.from_dict({"alice": {"quota": 3}, "bob": {"quota": 5}})


@pytest.fixture
def quota(users: UserRegistry) -> QuotaManager:
    return QuotaManager(users)


@pytest.fixture
def make_scheduler(registry, clock, quota):
    """Factory so each test states only the part of the setup it cares about."""

    def _make(tasks=(), **overrides) -> Scheduler:
        kwargs: dict[str, Any] = {
            "tasks": tasks,
            "quota": quota,
            "registry": registry,
            "clock": clock,
            "run_log": InMemoryRunLog(),
        }
        kwargs.update(overrides)
        return Scheduler(**kwargs)

    return _make
