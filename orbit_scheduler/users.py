"""User management and quota control -- Refactoring Goal 1, first required module.

Responsibilities are split three ways so each piece can change independently:

:class:`UserRegistry`
    *Who exists and what are they allowed.* Pure lookup, no counting.

:class:`QuotaLedger`
    *How much has been used.* Pure counting, no policy.

:class:`QuotaManager`
    *May this task run right now?* The only object the scheduler talks to. It owns the
    policy that combines the other two, so the scheduler never learns how quota is stored.

Swapping the in-memory ledger for Redis later means replacing one class that has four
methods -- the registry, the manager's interface and the scheduler are all untouched.

The daily reset, which the legacy code got wrong
------------------------------------------------
The legacy counter ``users[user]['executed']`` only ever grew. The quota is daily, so by
day two every user was permanently locked out.

The fix here is structural rather than a scheduled reset job: consumption is keyed by
``(user_id, date)``. Tomorrow is simply a different key, so yesterday's usage stops
counting without anybody resetting anything. There is no reset to forget, no cron job to
fail, and no window during which the counter is briefly wrong.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .errors import QuotaExceededError, UnknownUserError, UserDisabledError

__all__ = ["QuotaLedger", "QuotaManager", "QuotaReservation", "User", "UserRegistry"]


@dataclass(frozen=True, slots=True)
class User:
    """A principal who owns tasks and has a daily execution allowance."""

    id: str
    daily_quota: int
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("user id must not be empty")
        if not isinstance(self.daily_quota, int) or isinstance(self.daily_quota, bool):
            raise ValueError(f"user {self.id!r}: daily_quota must be an integer")
        if self.daily_quota < 0:
            raise ValueError(f"user {self.id!r}: daily_quota must not be negative")

    @classmethod
    def from_dict(cls, user_id: str, raw: Mapping[str, Any] | int) -> User:
        """Build a user from config.

        Accepts the legacy shape ``{"quota": 3, "executed": 0}`` and the shorthand
        ``3``. ``executed`` is deliberately ignored: run-state is no longer stored
        alongside configuration, which is what made the legacy reset bug possible.
        """
        if isinstance(raw, int) and not isinstance(raw, bool):
            return cls(id=user_id, daily_quota=raw)
        if not isinstance(raw, Mapping):
            raise ValueError(
                f"user {user_id!r}: expected an object or an integer quota, "
                f"got {type(raw).__name__}"
            )
        if "quota" not in raw and "daily_quota" not in raw:
            raise ValueError(f"user {user_id!r}: missing 'quota'")
        return cls(
            id=user_id,
            daily_quota=raw.get("daily_quota", raw.get("quota")),
            enabled=bool(raw.get("enabled", True)),
        )


class UserRegistry:
    """Who exists, and what their daily allowance is.

    Lookup raises :class:`~orbit_scheduler.errors.UnknownUserError` rather than
    ``KeyError``, and the message lists the known users. The legacy ``users[user]``
    raised a bare ``KeyError`` that propagated out of the loop and stopped every
    remaining task from running.
    """

    def __init__(self, users: Iterable[User] = ()) -> None:
        self._users: dict[str, User] = {}
        for user in users:
            self.add(user)

    def add(self, user: User) -> User:
        """Register a user. Replaces any existing entry with the same id."""
        self._users[user.id] = user
        return user

    def remove(self, user_id: str) -> None:
        self._users.pop(user_id, None)

    def get(self, user_id: str) -> User:
        """Look up a user, or raise :class:`UnknownUserError` naming the valid ids."""
        try:
            return self._users[user_id]
        except KeyError:
            raise UnknownUserError(user_id, known=self._users) from None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> UserRegistry:
        """Build a registry from ``{"alice": {"quota": 3}, "bob": 5}``."""
        if not isinstance(raw, Mapping):
            raise ValueError(f"expected a users object, got {type(raw).__name__}")
        return cls(User.from_dict(uid, spec) for uid, spec in raw.items())

    def __contains__(self, user_id: object) -> bool:
        return user_id in self._users

    def __iter__(self) -> Iterator[User]:
        return iter(self._users.values())

    def __len__(self) -> int:
        return len(self._users)

    def ids(self) -> list[str]:
        return sorted(self._users)

    def __repr__(self) -> str:
        return f"UserRegistry({len(self._users)} users: {', '.join(self.ids())})"


class QuotaLedger:
    """Counts executions per user per calendar day.

    Keying on the date is the whole trick: a new day is a new key, so the daily reset
    happens by construction rather than by a job that has to remember to run.

    Thread-safe. ``try_consume`` is a single atomic check-and-increment -- checking and
    then incrementing as two steps lets two workers both pass the check and jointly blow
    through the limit.
    """

    def __init__(self) -> None:
        self._counts: defaultdict[tuple[str, dt.date], int] = defaultdict(int)
        self._lock = threading.Lock()

    def used(self, user_id: str, on_date: dt.date) -> int:
        """How many units this user has consumed on ``on_date``."""
        with self._lock:
            return self._counts[(user_id, on_date)]

    def try_consume(self, user_id: str, on_date: dt.date, limit: int) -> bool:
        """Atomically consume one unit if the limit allows. Returns success."""
        with self._lock:
            key = (user_id, on_date)
            if self._counts[key] >= limit:
                return False
            self._counts[key] += 1
            return True

    def refund(self, user_id: str, on_date: dt.date) -> None:
        """Return one consumed unit. Never drops below zero."""
        with self._lock:
            key = (user_id, on_date)
            if self._counts[key] > 0:
                self._counts[key] -= 1

    def snapshot(self, on_date: dt.date) -> dict[str, int]:
        """Usage for every user on one day. For reporting and assertions."""
        with self._lock:
            return {uid: n for (uid, day), n in self._counts.items() if day == on_date}

    def purge_before(self, cutoff: dt.date) -> int:
        """Drop entries older than ``cutoff`` and return how many were removed.

        A process that runs for months accumulates one entry per user per day. This keeps
        that bounded; a persistent ledger would use a TTL instead.
        """
        with self._lock:
            stale = [key for key in self._counts if key[1] < cutoff]
            for key in stale:
                del self._counts[key]
            return len(stale)

    def __repr__(self) -> str:
        return f"QuotaLedger({len(self._counts)} user-days tracked)"


class QuotaReservation:
    """A handle on one consumed quota unit.

    Held for the duration of an execution. If the work fails the unit is refunded, so a
    failed action does not burn the user's allowance -- the legacy code incremented the
    counter before it knew whether anything had succeeded.
    """

    __slots__ = ("_date", "_ledger", "_settled", "_user_id")

    def __init__(self, ledger: QuotaLedger, user_id: str, on_date: dt.date) -> None:
        self._ledger = ledger
        self._user_id = user_id
        self._date = on_date
        self._settled = False

    @property
    def settled(self) -> bool:
        return self._settled

    def commit(self) -> None:
        """Keep the consumed unit. Idempotent."""
        self._settled = True

    def refund(self) -> None:
        """Hand the consumed unit back. Idempotent."""
        if not self._settled:
            self._ledger.refund(self._user_id, self._date)
            self._settled = True

    def __repr__(self) -> str:
        state = "settled" if self._settled else "open"
        return f"QuotaReservation({self._user_id} {self._date.isoformat()} {state})"


class QuotaManager:
    """The scheduler's single point of contact for "may this run?".

    Combines a :class:`UserRegistry` (policy) with a :class:`QuotaLedger` (counting).
    The scheduler never touches either directly, so replacing the ledger with a
    Redis-backed implementation is a one-line change at construction.
    """

    def __init__(self, users: UserRegistry, ledger: QuotaLedger | None = None) -> None:
        self._users = users
        self._ledger = ledger if ledger is not None else QuotaLedger()

    @property
    def users(self) -> UserRegistry:
        return self._users

    @property
    def ledger(self) -> QuotaLedger:
        return self._ledger

    def remaining(self, user_id: str, on_date: dt.date) -> int:
        """How many more executions this user may perform on ``on_date``."""
        user = self._users.get(user_id)
        return max(0, user.daily_quota - self._ledger.used(user_id, on_date))

    @contextmanager
    def reserve(self, user_id: str, on_date: dt.date) -> Iterator[QuotaReservation]:
        """Consume one unit for the duration of the block.

        Refunds automatically if the block raises, keeps the unit if it completes::

            with quota.reserve("alice", today):
                executor.execute(task, ctx)     # raises -> unit refunded

        :raises UnknownUserError: the user does not exist.
        :raises UserDisabledError: the user exists but is switched off.
        :raises QuotaExceededError: the allowance for ``on_date`` is already spent.
        """
        user = self._users.get(user_id)
        if not user.enabled:
            raise UserDisabledError(user_id)
        if not self._ledger.try_consume(user_id, on_date, user.daily_quota):
            raise QuotaExceededError(user_id, user.daily_quota, on_date)

        reservation = QuotaReservation(self._ledger, user_id, on_date)
        try:
            yield reservation
        except BaseException:
            reservation.refund()
            raise
        else:
            reservation.commit()

    def __repr__(self) -> str:
        return f"QuotaManager({self._users!r}, {self._ledger!r})"
