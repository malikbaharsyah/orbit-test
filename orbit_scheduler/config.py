"""Configuration loading -- Refactoring Goal 2, "parameters configurable via dict input".

Turns a plain dictionary into validated domain objects. The dictionary can come from
anywhere: a JSON file, a YAML parser, an HTTP request body, a database row. Nothing in
here knows or cares.

Validation accumulates
----------------------
:func:`load_schedule` collects *every* problem it finds and raises once, rather than
failing on the first. A team with three typos in a config should learn about three typos
in one run, not fix-rerun-fix-rerun. The difference costs about ten lines here and saves
an afternoon across a growing team.

Validation happens early
------------------------
Each executor's
:meth:`~orbit_scheduler.executors.base.TaskExecutor.validate_params` runs at load time,
so a task whose parameters are wrong is rejected while somebody is watching the deploy --
not at 12:00 when it was supposed to run.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .clock import Clock
from .errors import ConfigError, InvalidParamsError, UnknownActionError
from .executors.base import ExecutorRegistry, default_registry
from .models import Task
from .scheduler import DEFAULT_CATCHUP_WINDOW, RunLog, Scheduler
from .users import QuotaManager, UserRegistry

__all__ = ["ScheduleConfig", "build_scheduler", "load_schedule", "load_schedule_file"]


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    """A validated schedule: who exists, what runs, and any extra settings."""

    users: UserRegistry
    tasks: tuple[Task, ...]
    settings: Mapping[str, Any]

    def __repr__(self) -> str:
        return f"ScheduleConfig({len(self.users)} users, {len(self.tasks)} tasks)"


def _default_task_id(raw: Mapping[str, Any], index: int) -> str:
    """Derive a stable id for a task that did not declare one.

    Content-derived rather than positional: reordering the config file must not change a
    task's identity, because the run log keys on it and a changed id means the task looks
    new and runs a second time the same day.
    """
    user = raw.get("user_id") or raw.get("user") or f"user{index}"
    action = raw.get("action") or f"action{index}"
    when = raw.get("scheduled_at", raw.get("time", index))
    return f"{user}:{action}:{when}"


def load_schedule(
    raw: Mapping[str, Any],
    *,
    registry: ExecutorRegistry | None = None,
) -> ScheduleConfig:
    """Build users and tasks from a dictionary, validating everything first.

    Expected shape (the ``users`` and ``tasks`` keys are both required)::

        {
          "users": {"alice": {"quota": 3}, "bob": {"quota": 5}},
          "tasks": [
            {"user": "alice", "time": "12:00", "action": "sync",
             "params": {"target": "/data/x"}}
          ],
          "settings": {"catchup_minutes": 60}
        }

    The legacy flat task shape (``target`` alongside ``action`` rather than inside
    ``params``) is accepted too -- see :meth:`~orbit_scheduler.models.Task.from_dict` --
    so an existing deployment can migrate its code before its configuration.

    :param registry: Used to check that every action exists and that its parameters are
        valid. Defaults to the process registry. Pass an explicit one to validate against
        a restricted set of actions.
    :raises ConfigError: carrying the complete list of problems found.
    """
    registry = registry if registry is not None else default_registry
    problems: list[str] = []

    if not isinstance(raw, Mapping):
        raise ConfigError([f"expected a configuration object, got {type(raw).__name__}"])

    # -- users ---------------------------------------------------------------
    users = UserRegistry()
    raw_users = raw.get("users")
    if raw_users is None:
        problems.append("missing 'users' section")
    elif not isinstance(raw_users, Mapping):
        problems.append(f"'users' must be an object, got {type(raw_users).__name__}")
    else:
        for user_id, spec in raw_users.items():
            try:
                users.add(_user_from(user_id, spec))
            except ValueError as exc:
                problems.append(f"users.{user_id}: {exc}")

    # -- tasks ---------------------------------------------------------------
    tasks: list[Task] = []
    seen_ids: dict[str, int] = {}
    raw_tasks = raw.get("tasks")
    if raw_tasks is None:
        problems.append("missing 'tasks' section")
    elif isinstance(raw_tasks, Mapping) or not isinstance(raw_tasks, Iterable):
        problems.append(f"'tasks' must be a list, got {type(raw_tasks).__name__}")
    else:
        for index, raw_task in enumerate(raw_tasks):
            label = f"tasks[{index}]"
            try:
                task = Task.from_dict(
                    raw_task, default_id=_default_task_id(raw_task, index)
                )
            except (ValueError, TypeError) as exc:
                problems.append(f"{label}: {exc}")
                continue

            if task.id in seen_ids:
                problems.append(
                    f"{label}: duplicate task id {task.id!r} (also tasks"
                    f"[{seen_ids[task.id]}]); give one of them an explicit 'id'"
                )
                continue
            seen_ids[task.id] = index

            problems.extend(_validate_task(task, label, users, registry, bool(raw_users)))
            tasks.append(task)

    if problems:
        raise ConfigError(problems)

    settings = raw.get("settings") or {}
    if not isinstance(settings, Mapping):
        raise ConfigError([f"'settings' must be an object, got {type(settings).__name__}"])

    return ScheduleConfig(users=users, tasks=tuple(tasks), settings=dict(settings))


def _user_from(user_id: str, spec: Any):
    """Thin wrapper so the import of User stays local to users.py."""
    from .users import User

    return User.from_dict(user_id, spec)


def _validate_task(
    task: Task,
    label: str,
    users: UserRegistry,
    registry: ExecutorRegistry,
    users_section_present: bool,
) -> list[str]:
    """Cross-check one task against the users and the executor registry."""
    problems: list[str] = []

    # Only meaningful if the users section parsed; otherwise every task would report an
    # unknown user and bury the real problem.
    if users_section_present and task.user_id not in users:
        problems.append(
            f"{label}: unknown user {task.user_id!r}"
            f" (known: {', '.join(users.ids()) or 'none'})"
        )

    try:
        executor = registry.get(task.action)
    except UnknownActionError as exc:
        problems.append(f"{label}: {exc}")
        return problems

    try:
        executor.validate_params(task.params)
    except InvalidParamsError as exc:
        problems.append(f"{label}: {exc}")

    return problems


def load_schedule_file(
    path: str | Path,
    *,
    registry: ExecutorRegistry | None = None,
) -> ScheduleConfig:
    """Load and validate a JSON schedule file.

    Deliberately a thin wrapper over :func:`load_schedule` -- the file is only a delivery
    mechanism, so the parsing and validation rules stay in exactly one place.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError([f"configuration file not found: {path}"]) from None
    except json.JSONDecodeError as exc:
        raise ConfigError([f"{path}: invalid JSON at line {exc.lineno}: {exc.msg}"]) from None
    return load_schedule(raw, registry=registry)


def build_scheduler(
    config: ScheduleConfig,
    *,
    registry: ExecutorRegistry | None = None,
    clock: Clock | None = None,
    run_log: RunLog | None = None,
    dry_run: bool = False,
) -> Scheduler:
    """Assemble a ready-to-run :class:`~orbit_scheduler.scheduler.Scheduler`.

    The wiring is five lines, but it is five lines that ``main.py``, the test suite and
    any future API server would otherwise each copy. ``settings.catchup_minutes``, if
    present, overrides the default catch-up window.
    """
    catchup_minutes = config.settings.get("catchup_minutes")
    catchup = (
        DEFAULT_CATCHUP_WINDOW
        if catchup_minutes is None
        else (None if catchup_minutes < 0 else dt.timedelta(minutes=catchup_minutes))
    )
    return Scheduler(
        tasks=config.tasks,
        quota=QuotaManager(config.users),
        registry=registry if registry is not None else default_registry,
        clock=clock,
        run_log=run_log,
        catchup_window=catchup,
        dry_run=dry_run,
    )
