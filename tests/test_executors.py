"""The executor registry: the extension point, and the guardrails around it.

The test that matters most for the brief's "extensible" requirement is
:func:`test_a_new_action_needs_no_change_to_the_system` -- it adds an action from
outside the package and runs it end to end.
"""

from __future__ import annotations

import pytest

from orbit_scheduler import (
    ExecutionContext,
    ExecutorRegistry,
    InvalidParamsError,
    Task,
    TaskExecutor,
    UnknownActionError,
    default_registry,
)
from orbit_scheduler.executors import BUILTIN_EXECUTORS, PathTaskExecutor

from .conftest import make_task

# -- registration ----------------------------------------------------------


def test_the_builtins_are_registered():
    assert set(default_registry.available()) >= {"sync", "backup", "delete"}
    assert len(BUILTIN_EXECUTORS) == 3


def test_registering_returns_the_class_so_it_works_as_a_decorator():
    registry = ExecutorRegistry()

    @registry.register
    class Thing(TaskExecutor):
        action = "thing"

        def execute(self, task, ctx):
            return "done"

    assert isinstance(Thing, type), "the name must stay bound to the class"
    assert registry.get("thing").action == "thing"


def test_an_executor_without_an_action_is_refused():
    registry = ExecutorRegistry()

    class Nameless(TaskExecutor):
        def execute(self, task, ctx):
            return ""

    with pytest.raises(ValueError, match="action"):
        registry.register(Nameless)


def test_two_executors_cannot_claim_the_same_action():
    """Caught at import time, rather than at 12:00 when both teams' tasks are due."""
    registry = ExecutorRegistry()

    class First(TaskExecutor):
        action = "clash"

        def execute(self, task, ctx):
            return ""

    class Second(TaskExecutor):
        action = "clash"

        def execute(self, task, ctx):
            return ""

    registry.register(First)
    with pytest.raises(ValueError, match="already handled by First"):
        registry.register(Second)


def test_registering_a_non_executor_is_refused():
    with pytest.raises(TypeError):
        ExecutorRegistry().register(dict)  # type: ignore[arg-type]


def test_an_unknown_action_lists_the_known_ones():
    with pytest.raises(UnknownActionError) as exc:
        default_registry.get("teleport")
    message = str(exc.value)
    assert "teleport" in message and "sync" in message


def test_copy_isolates_a_registry():
    clone = default_registry.copy()

    @clone.register
    class Local(TaskExecutor):
        action = "local-only"

        def execute(self, task, ctx):
            return ""

    assert "local-only" in clone
    assert "local-only" not in default_registry, "the process registry must be untouched"


def test_describe_lists_actions_with_descriptions():
    text = default_registry.describe()
    assert "sync" in text and "Replicate" in text


# -- parameter validation, at load time ------------------------------------


def test_a_path_action_requires_a_target():
    with pytest.raises(InvalidParamsError, match="target"):
        default_registry.get("sync").validate_params({})


def test_a_path_action_requires_an_absolute_target():
    with pytest.raises(InvalidParamsError, match="absolute"):
        default_registry.get("sync").validate_params({"target": "relative/path"})


@pytest.mark.parametrize("target", ["/data/x", "D:\\data\\x", "C:/data/x"])
def test_absolute_paths_are_accepted(target):
    default_registry.get("sync").validate_params({"target": target})


def test_delete_refuses_a_target_outside_the_allowed_roots():
    """delete is the one built-in that destroys data, so it is fenced in."""
    with pytest.raises(InvalidParamsError, match="restricted to"):
        default_registry.get("delete").validate_params({"target": "/etc/passwd"})


def test_delete_accepts_a_temporary_path():
    default_registry.get("delete").validate_params({"target": "/tmp/z"})


def test_delete_refuses_at_execution_time_too(make_scheduler):
    """Defence in depth: a task added at runtime never went through load validation."""
    scheduler = make_scheduler(
        [make_task("danger", action="delete", target="/etc/passwd")]
    )
    result = scheduler.tick().results[0]
    assert result.status.value == "failed"
    assert "refusing to delete" in result.error


def test_the_shared_path_rule_lives_in_one_place():
    """DRY: sync, backup and delete all inherit the same target contract."""
    for action in ("sync", "backup", "delete"):
        assert isinstance(default_registry.get(action), PathTaskExecutor)


# -- behaviour -------------------------------------------------------------


def test_backup_honours_its_own_parameter():
    """Executor-specific params need no change to Task or the scheduler."""
    executor = default_registry.get("backup")
    task = make_task("b1", action="backup", target="/srv/y", retention_days=7)
    ctx = ExecutionContext(logger=_NullLogger(), now=None, run_id="test")
    assert "retention 7d" in executor.execute(task, ctx)


def test_dry_run_describes_instead_of_doing():
    executor = default_registry.get("delete")
    task = make_task("d1", action="delete", target="/tmp/z")
    ctx = ExecutionContext(logger=_NullLogger(), now=None, run_id="test", dry_run=True)
    assert executor.execute(task, ctx).startswith("dry-run")


def test_a_new_action_needs_no_change_to_the_system(make_scheduler, registry):
    """The whole point of the refactor, in one test.

    ``compress`` is defined here, in a test file, outside the package. Nothing in
    models, scheduler, config or the registry was edited to support it.
    """

    @registry.register
    class CompressExecutor(PathTaskExecutor):
        action = "compress"
        description = "Compress the target path in place."

        def execute(self, task: Task, ctx: ExecutionContext) -> str:
            return f"compressed {self.target_of(task)} at level {task.params['level']}"

    scheduler = make_scheduler(
        [make_task("c1", action="compress", target="/data/big", level=9)]
    )
    result = scheduler.tick().results[0]

    assert result.ok
    assert result.detail == "compressed /data/big at level 9"


class _NullLogger:
    """Swallows log calls, for executor unit tests that do not care about output."""

    def info(self, *args, **kwargs):
        pass

    debug = warning = error = info
