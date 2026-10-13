"""The extensible task executor -- Refactoring Goal 1, third required module.

This is the module that makes the system worth refactoring. The legacy code had no
executor at all: the action was a string that got interpolated into a ``print``, so
adding a real action meant editing the scheduling loop and growing an ``if/elif`` chain
that every team would have to touch and re-test.

Here an action is a class. Adding one is::

    @default_registry.register
    class CompressExecutor(TaskExecutor):
        action = "compress"

        def execute(self, task, ctx):
            ...

and nothing else in the system changes -- not the scheduler, not the data model, not the
config loader. :class:`ExecutorRegistry` is the single source of truth for which actions
exist; no other module anywhere enumerates action names.

Two design choices are worth calling out for reviewers:

**Why a context object.** :meth:`TaskExecutor.execute` takes an
:class:`ExecutionContext` rather than a logger argument, a clock argument and a dry-run
flag. When the system later needs to hand executors something new -- a trace id, a
cancellation token, a metrics client -- it is added to the context and no executor
signature changes. Twenty executors across four teams do not need a coordinated edit.

**Why validate_params is separate from execute.** Validation runs at *configuration load
time*, not at execution time. A task with a mistyped parameter then fails when the
service starts, in front of the person deploying it, rather than at 12:00 in front of
nobody.
"""

from __future__ import annotations

import datetime as dt
import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from ..errors import UnknownActionError
from ..models import Task

__all__ = ["ExecutionContext", "ExecutorRegistry", "TaskExecutor", "default_registry"]


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Everything an executor needs from the world around it.

    :param logger: Already bound to this run, task and user -- an executor should log
        through it and never build its own, so every line is correlated for free.
    :param now: The scheduler's notion of now. Executors must prefer this over
        ``datetime.now()`` so that frozen-clock tests stay deterministic.
    :param run_id: Correlation id for the tick that triggered this execution.
    :param dry_run: When true, an executor must describe what it *would* do and change
        nothing.
    """

    logger: logging.Logger | logging.LoggerAdapter
    now: dt.datetime
    run_id: str
    dry_run: bool = False


class TaskExecutor(ABC):
    """Base class for every action the scheduler can perform.

    Subclasses declare the :attr:`action` they handle, implement :meth:`execute`, and
    optionally tighten :meth:`validate_params`.
    """

    #: The action name this executor handles. Must be unique across the registry.
    action: ClassVar[str] = ""

    #: One line, shown by ``ExecutorRegistry.describe()`` and in docs. Write it for
    #: somebody on another team who has never seen this code.
    description: ClassVar[str] = ""

    # B027: intentionally concrete and empty. Making it abstract would force every
    # executor to write a no-op override, which is exactly the boilerplate this
    # design exists to avoid.
    def validate_params(self, params: Mapping[str, Any]) -> None:  # noqa: B027
        """Check a task's parameters at configuration-load time.

        Raise :class:`~orbit_scheduler.errors.InvalidParamsError` to reject. The default
        accepts anything, so an executor with no required parameters writes nothing.
        """

    @abstractmethod
    def execute(self, task: Task, ctx: ExecutionContext) -> str:
        """Perform the action.

        :returns: A short human-readable detail recorded on the
            :class:`~orbit_scheduler.models.ExecutionResult`, e.g. ``"synced /data/x"``.
        :raises Exception: Any exception marks the attempt as failed. The scheduler logs
            it with a traceback, refunds the user's quota and carries on with the next
            task -- one bad executor can never stop a run.
        """

    def __repr__(self) -> str:
        return f"{type(self).__name__}(action={self.action!r})"


class ExecutorRegistry:
    """The single source of truth for which actions exist.

    Dispatch is a dictionary lookup, not an ``if/elif`` chain, so the set of supported
    actions is open for extension and closed for modification.
    """

    def __init__(self) -> None:
        self._executors: dict[str, TaskExecutor] = {}

    def register(self, executor_cls: type[TaskExecutor]) -> type[TaskExecutor]:
        """Register an executor class. Usable directly or as a decorator.

        Returns the class unchanged so ``@registry.register`` leaves the name bound to
        the class, not to an instance.

        :raises TypeError: ``executor_cls`` is not a :class:`TaskExecutor` subclass.
        :raises ValueError: the class declares no action, or an action another executor
            already claims. Catching the collision here, at import time, is much kinder
            than discovering at 12:00 that two teams both shipped a ``"backup"``.
        """
        if not (isinstance(executor_cls, type) and issubclass(executor_cls, TaskExecutor)):
            raise TypeError(
                f"{executor_cls!r} is not a TaskExecutor subclass; "
                f"executors must inherit from TaskExecutor"
            )
        action = getattr(executor_cls, "action", "")
        if not action:
            raise ValueError(
                f"{executor_cls.__name__} must declare a non-empty class attribute "
                f"'action' before it can be registered"
            )
        existing = self._executors.get(action)
        if existing is not None:
            raise ValueError(
                f"action {action!r} is already handled by {type(existing).__name__}; "
                f"{executor_cls.__name__} cannot claim it too"
            )
        self._executors[action] = executor_cls()
        return executor_cls

    def get(self, action: str) -> TaskExecutor:
        """Resolve an action to its executor.

        :raises UnknownActionError: with the list of actions that *are* registered, so
            the reader of the log can see the typo immediately.
        """
        try:
            return self._executors[action]
        except KeyError:
            raise UnknownActionError(action, available=self._executors) from None

    def available(self) -> list[str]:
        """Every registered action name, sorted."""
        return sorted(self._executors)

    def describe(self) -> str:
        """A readable catalogue of registered actions. Useful in ``--help`` output."""
        if not self._executors:
            return "(no executors registered)"
        width = max(len(a) for a in self._executors)
        return "\n".join(
            f"  {action:<{width}}  {self._executors[action].description or '-'}"
            for action in self.available()
        )

    def copy(self) -> ExecutorRegistry:
        """An independent registry holding the same executors.

        Tests use this to add a fixture executor without mutating the process-wide
        default and leaking that change into every later test.
        """
        clone = ExecutorRegistry()
        clone._executors = dict(self._executors)
        return clone

    def __contains__(self, action: object) -> bool:
        return action in self._executors

    def __iter__(self) -> Iterator[TaskExecutor]:
        return iter(self._executors.values())

    def __len__(self) -> int:
        return len(self._executors)

    def __repr__(self) -> str:
        return f"ExecutorRegistry({', '.join(self.available()) or 'empty'})"


#: The registry the built-in executors register into, and a sensible default for
#: applications. Tests and embedders should use ``default_registry.copy()`` or build
#: their own :class:`ExecutorRegistry` to stay isolated.
default_registry = ExecutorRegistry()
