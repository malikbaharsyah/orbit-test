"""Time as an injected dependency.

A scheduler that calls ``datetime.now()`` directly cannot be tested without sleeping,
and tests that sleep are slow and flaky. Passing a :class:`Clock` in means "what time is
it" becomes a seam: production gets :class:`SystemClock`, tests and reproducible demos
get :class:`FrozenClock`, and the scheduling logic is identical in both.

Timezones: this package works in naive local time, matching the brief's ``'%H:%M'``
strings. A real deployment serving users in multiple regions needs per-user timezones --
"daily" means a different 24 hours in Jakarta than in Berlin. That belongs in
:class:`~orbit_scheduler.users.User`; see README, "Known limitations".
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "FrozenClock", "SystemClock"]


@runtime_checkable
class Clock(Protocol):
    """Anything that can report the current time.

    A Protocol rather than a base class: callers can supply their own clock without
    importing ours.
    """

    def now(self) -> dt.datetime:
        """Return the current local time."""
        ...


class SystemClock:
    """Wall-clock time. The production implementation."""

    __slots__ = ()

    def now(self) -> dt.datetime:
        return dt.datetime.now()

    def __repr__(self) -> str:
        return "SystemClock()"


class FrozenClock:
    """A clock that only moves when you tell it to.

    Used by the test suite and by ``main.py`` so the demo prints the same thing on every
    machine, on any day of the year.
    """

    __slots__ = ("_moment",)

    def __init__(self, moment: dt.datetime) -> None:
        self._moment = moment

    def now(self) -> dt.datetime:
        return self._moment

    def set(self, moment: dt.datetime) -> dt.datetime:
        """Jump to an absolute moment."""
        self._moment = moment
        return self._moment

    def advance(self, **timedelta_kwargs: float) -> dt.datetime:
        """Move forward, e.g. ``clock.advance(days=1)`` or ``clock.advance(minutes=5)``."""
        self._moment += dt.timedelta(**timedelta_kwargs)
        return self._moment

    def __repr__(self) -> str:
        return f"FrozenClock({self._moment.isoformat()})"
