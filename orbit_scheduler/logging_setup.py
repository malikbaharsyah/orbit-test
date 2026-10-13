"""Logging configuration and contextual logging.

Two rules this module exists to enforce:

**Libraries do not configure logging.** :func:`configure_logging` is called by the
*application* entrypoint (``main.py``) and nowhere else. A library that calls
``logging.basicConfig`` at import time silently hijacks the log output of every program
that imports it -- a bug that is miserable to track down. Every module inside this
package only ever does ``logger = logging.getLogger(__name__)``, which makes the source
of each line visible (``orbit_scheduler.scheduler``, ``orbit_scheduler.executors.builtin``)
and lets an operator tune verbosity per module.

**Context is bound once, not retyped.** Without :class:`TaskLogAdapter` every call site
has to re-interpolate the same identifiers::

    logger.info("[run=%s task=%s user=%s] Executing...", run_id, task.id, task.user_id)

which is four chances to get it wrong and guarantees the day someone forgets. Binding the
context once means every subsequent line carries it automatically.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import IO, Any

__all__ = ["DEFAULT_FORMAT", "TaskLogAdapter", "configure_logging", "get_logger"]

DEFAULT_FORMAT = "%(asctime)s  %(levelname)-7s  %(name)-32s  %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(
    level: int | str = logging.INFO,
    *,
    stream: IO[str] | None = None,
    fmt: str = DEFAULT_FORMAT,
    datefmt: str = DEFAULT_DATE_FORMAT,
) -> logging.Logger:
    """Install a single stdout handler on the root logger and return it.

    Call this once, from your entrypoint, before building a scheduler. Safe to call
    again (handlers are replaced, not stacked, so repeated calls in a REPL or a test
    session do not produce duplicated lines).
    """
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    return root


class TaskLogAdapter(logging.LoggerAdapter):
    """A logger that prefixes every record with the context bound to it.

    >>> log = TaskLogAdapter(logging.getLogger("demo"), {"run": "a1b2c3d4"})
    >>> task_log = log.bind(task="t-001", user="alice")
    >>> task_log.info("Executed")           # doctest: +SKIP
    ... # emits: [run=a1b2c3d4 task=t-001 user=alice] Executed
    """

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        context = " ".join(f"{k}={v}" for k, v in self.extra.items() if v is not None)
        return (f"[{context}] {msg}" if context else msg), kwargs

    def bind(self, **extra: Any) -> TaskLogAdapter:
        """Return a new adapter with additional context merged in.

        Returns a *new* adapter rather than mutating this one, so a per-task logger can
        never leak its task id back into the surrounding per-run logger.
        """
        return TaskLogAdapter(self.logger, {**self.extra, **extra})


def get_logger(name: str, **context: Any) -> TaskLogAdapter:
    """Convenience: a context-bound adapter over ``logging.getLogger(name)``."""
    return TaskLogAdapter(logging.getLogger(name), dict(context))
