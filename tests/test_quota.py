"""User management and quota control.

The headline test here is :func:`test_quota_resets_the_next_day`. The legacy
implementation fails it, and failing it is what made the legacy quota a lifetime limit
instead of a daily one.
"""

from __future__ import annotations

import datetime as dt

import pytest

from orbit_scheduler import (
    QuotaExceededError,
    QuotaLedger,
    QuotaManager,
    UnknownUserError,
    User,
    UserDisabledError,
    UserRegistry,
)

DAY_ONE = dt.date(2026, 3, 2)
DAY_TWO = dt.date(2026, 3, 3)


# -- the ledger ------------------------------------------------------------


def test_consumes_up_to_the_limit():
    ledger = QuotaLedger()
    assert [ledger.try_consume("alice", DAY_ONE, 3) for _ in range(3)] == [True] * 3
    assert ledger.used("alice", DAY_ONE) == 3


def test_refuses_beyond_the_limit():
    ledger = QuotaLedger()
    for _ in range(3):
        ledger.try_consume("alice", DAY_ONE, 3)
    assert ledger.try_consume("alice", DAY_ONE, 3) is False
    assert ledger.used("alice", DAY_ONE) == 3, "a refused attempt must not count"


def test_quota_resets_the_next_day():
    """The defect the legacy counter had: it only ever grew.

    Keying consumption on (user, date) makes the reset structural -- nothing has to run
    at midnight for this to hold.
    """
    ledger = QuotaLedger()
    for _ in range(3):
        ledger.try_consume("alice", DAY_ONE, 3)

    assert ledger.try_consume("alice", DAY_ONE, 3) is False, "spent for day one"
    assert ledger.try_consume("alice", DAY_TWO, 3) is True, "day two starts fresh"
    assert ledger.used("alice", DAY_ONE) == 3
    assert ledger.used("alice", DAY_TWO) == 1


def test_users_do_not_share_an_allowance():
    ledger = QuotaLedger()
    ledger.try_consume("alice", DAY_ONE, 1)
    assert ledger.try_consume("bob", DAY_ONE, 1) is True


def test_refund_returns_a_unit_and_never_goes_negative():
    ledger = QuotaLedger()
    ledger.try_consume("alice", DAY_ONE, 3)
    ledger.refund("alice", DAY_ONE)
    assert ledger.used("alice", DAY_ONE) == 0
    ledger.refund("alice", DAY_ONE)
    assert ledger.used("alice", DAY_ONE) == 0


def test_purge_drops_only_old_entries():
    ledger = QuotaLedger()
    ledger.try_consume("alice", DAY_ONE, 3)
    ledger.try_consume("alice", DAY_TWO, 3)
    assert ledger.purge_before(DAY_TWO) == 1
    assert ledger.used("alice", DAY_ONE) == 0
    assert ledger.used("alice", DAY_TWO) == 1


def test_snapshot_reports_one_day():
    ledger = QuotaLedger()
    ledger.try_consume("alice", DAY_ONE, 3)
    ledger.try_consume("bob", DAY_ONE, 3)
    ledger.try_consume("bob", DAY_TWO, 3)
    assert ledger.snapshot(DAY_ONE) == {"alice": 1, "bob": 1}


# -- the registry ----------------------------------------------------------


def test_unknown_user_names_the_known_ones(users):
    """A bare KeyError is what killed the legacy run loop. This is actionable instead."""
    with pytest.raises(UnknownUserError) as exc:
        users.get("ghost")
    assert "ghost" in str(exc.value)
    assert "alice" in str(exc.value) and "bob" in str(exc.value)


@pytest.mark.parametrize(
    "spec, expected",
    [
        ({"quota": 3}, 3),
        (5, 5),
        ({"daily_quota": 7}, 7),
        ({"quota": 2, "executed": 99}, 2),  # legacy 'executed' is ignored
    ],
)
def test_user_accepts_config_shapes(spec, expected):
    assert User.from_dict("alice", spec).daily_quota == expected


@pytest.mark.parametrize("bad", [{"quota": -1}, {"quota": "three"}, {}, "nope"])
def test_user_rejects_bad_config(bad):
    with pytest.raises(ValueError):
        User.from_dict("alice", bad)


# -- the manager -----------------------------------------------------------


def test_reserve_consumes_on_success(quota):
    with quota.reserve("alice", DAY_ONE):
        pass
    assert quota.remaining("alice", DAY_ONE) == 2


def test_reserve_refunds_when_the_block_raises(quota):
    """A failed action must not cost the user part of their daily allowance."""
    with pytest.raises(RuntimeError), quota.reserve("alice", DAY_ONE):
        raise RuntimeError("the action failed")
    assert quota.remaining("alice", DAY_ONE) == 3


def test_reserve_raises_once_the_allowance_is_spent(quota):
    for _ in range(3):
        with quota.reserve("alice", DAY_ONE):
            pass
    with pytest.raises(QuotaExceededError) as exc, quota.reserve("alice", DAY_ONE):
        pass
    assert "daily quota of 3" in str(exc.value)


def test_reserve_rejects_an_unknown_user(quota):
    with pytest.raises(UnknownUserError), quota.reserve("ghost", DAY_ONE):
        pass


def test_reserve_rejects_a_disabled_user():
    registry = UserRegistry([User(id="alice", daily_quota=3, enabled=False)])
    manager = QuotaManager(registry)
    with pytest.raises(UserDisabledError), manager.reserve("alice", DAY_ONE):
        pass


def test_explicit_refund_inside_the_block_is_respected(quota):
    with quota.reserve("alice", DAY_ONE) as reservation:
        reservation.refund()
    assert quota.remaining("alice", DAY_ONE) == 3, "refund must survive block exit"


def test_zero_quota_blocks_everything():
    manager = QuotaManager(UserRegistry([User(id="carol", daily_quota=0)]))
    with pytest.raises(QuotaExceededError), manager.reserve("carol", DAY_ONE):
        pass
