"""Scheduling behaviour.

Four of these correspond directly to defects in the legacy ``run()``:

* :func:`test_a_second_tick_in_the_same_minute_does_nothing` -- double execution
* :func:`test_a_late_tick_still_runs_a_missed_task` -- silently dropped tasks
* :func:`test_an_unknown_user_does_not_stop_the_run` -- a KeyError killing the loop
* :func:`test_a_failing_task_refunds_its_quota` -- quota spent on work that failed
"""

from __future__ import annotations

import datetime as dt

from orbit_scheduler import ExecutionStatus, InMemoryRunLog

from .conftest import NOON, TOMORROW, make_task

# -- the happy path --------------------------------------------------------


def test_runs_every_due_task(make_scheduler, recorder):
    scheduler = make_scheduler([make_task("t1"), make_task("t2", user="bob")])
    report = scheduler.tick()

    assert recorder.call_count == 2
    assert len(report.succeeded) == 2
    assert report.counts == {"success": 2}


def test_a_task_scheduled_later_today_is_not_due(make_scheduler, recorder):
    scheduler = make_scheduler([make_task("t1", time="23:00")])
    assert scheduler.tick().results == ()
    assert recorder.call_count == 0


def test_a_disabled_task_never_runs(make_scheduler, recorder):
    scheduler = make_scheduler([make_task("t1", enabled=False)])
    assert scheduler.tick().results == ()
    assert recorder.call_count == 0


def test_one_user_can_hold_several_simultaneous_tasks(make_scheduler, recorder):
    """Required by the brief: "a single user can have multiple tasks simultaneously"."""
    scheduler = make_scheduler(
        [make_task("t1"), make_task("t2"), make_task("t3")]  # all alice, all 12:00
    )
    report = scheduler.tick()

    assert recorder.calls == ["t1", "t2", "t3"]
    assert len(report.succeeded) == 3, "alice's quota of 3 covers exactly these"


# -- idempotency and catch-up ----------------------------------------------


def test_a_second_tick_in_the_same_minute_does_nothing(make_scheduler, recorder, clock):
    """The legacy ``task['time'] == now`` check executed everything twice here."""
    scheduler = make_scheduler([make_task("t1")])
    scheduler.tick()
    clock.advance(seconds=30)
    second = scheduler.tick()

    assert recorder.call_count == 1, "the task must run once per day, not once per tick"
    assert second.results == ()


def test_a_late_tick_still_runs_a_missed_task(make_scheduler, recorder, clock):
    """The legacy equality check dropped the task forever if no tick hit its minute."""
    scheduler = make_scheduler([make_task("t1", time="12:00")])
    clock.set(NOON.replace(hour=12, minute=47))  # 47 minutes late
    report = scheduler.tick()

    assert recorder.call_count == 1
    assert len(report.succeeded) == 1


def test_a_task_past_the_catchup_window_waits_for_tomorrow(
    make_scheduler, recorder, clock
):
    scheduler = make_scheduler(
        [make_task("t1", time="12:00")], catchup_window=dt.timedelta(minutes=30)
    )
    clock.set(NOON.replace(hour=14))  # two hours late
    assert scheduler.tick().results == ()
    assert recorder.call_count == 0


def test_no_catchup_window_means_any_later_tick_today(make_scheduler, recorder, clock):
    scheduler = make_scheduler([make_task("t1", time="12:00")], catchup_window=None)
    clock.set(NOON.replace(hour=23, minute=59))
    assert len(scheduler.tick().succeeded) == 1


def test_the_task_runs_again_the_next_day(make_scheduler, recorder, clock):
    scheduler = make_scheduler([make_task("t1")])
    scheduler.tick()
    clock.set(TOMORROW)
    scheduler.tick()
    assert recorder.call_count == 2


# -- quota -----------------------------------------------------------------


def test_tasks_past_the_users_quota_are_refused(make_scheduler, recorder):
    """alice may run 3 a day; the fourth is refused with a clear reason."""
    tasks = [make_task(f"t{n}") for n in range(1, 5)]
    report = make_scheduler(tasks).tick()

    assert recorder.call_count == 3
    assert [r.status for r in report.results] == [
        ExecutionStatus.SUCCESS,
        ExecutionStatus.SUCCESS,
        ExecutionStatus.SUCCESS,
        ExecutionStatus.SKIPPED_QUOTA,
    ]
    assert "daily quota of 3" in report.results[-1].error


def test_quota_is_per_user(make_scheduler, recorder):
    tasks = [make_task(f"a{n}") for n in range(4)] + [make_task("b1", user="bob")]
    report = make_scheduler(tasks).tick()

    assert report.counts == {"success": 4, "skipped_quota": 1}
    assert "b1" in recorder.calls, "bob's own allowance is untouched by alice's"


def test_a_refused_task_is_retried_tomorrow(make_scheduler, recorder, clock):
    scheduler = make_scheduler([make_task(f"t{n}") for n in range(1, 5)])
    scheduler.tick()
    clock.set(TOMORROW)
    report = scheduler.tick()

    assert len(report.succeeded) == 3
    assert recorder.call_count == 6, "yesterday's refused task gets a fresh chance"


# -- failure isolation -----------------------------------------------------


def test_an_unknown_user_does_not_stop_the_run(make_scheduler, recorder):
    """The legacy ``users[user]`` raised KeyError and every later task was skipped."""
    scheduler = make_scheduler(
        [make_task("ghost", user="nobody"), make_task("after")]
    )
    report = scheduler.tick()

    assert recorder.calls == ["after"], "the task after the bad one still ran"
    assert report.results[0].status is ExecutionStatus.SKIPPED_UNKNOWN_USER
    assert "nobody" in report.results[0].error


def test_an_unknown_action_does_not_stop_the_run(make_scheduler, recorder):
    scheduler = make_scheduler(
        [make_task("odd", action="teleport"), make_task("after")]
    )
    report = scheduler.tick()

    assert recorder.calls == ["after"]
    assert report.results[0].status is ExecutionStatus.SKIPPED_UNKNOWN_ACTION
    assert "teleport" in report.results[0].error


def test_a_raising_executor_does_not_stop_the_run(make_scheduler, recorder, exploder):
    scheduler = make_scheduler(
        [make_task("bad", action="explode"), make_task("after")]
    )
    report = scheduler.tick()

    assert exploder.attempts == 1
    assert recorder.calls == ["after"]
    assert report.results[0].status is ExecutionStatus.FAILED
    assert report.results[0].error == "RuntimeError: boom"


def test_a_failing_task_refunds_its_quota(make_scheduler, quota):
    """A failure must not cost part of the user's daily allowance."""
    scheduler = make_scheduler([make_task("bad", action="explode")])
    scheduler.tick()
    assert quota.remaining("alice", NOON.date()) == 3


def test_a_failing_task_is_retried_on_the_next_tick(
    make_scheduler, exploder, clock
):
    """Not marking the run log on failure is what makes the retry possible."""
    scheduler = make_scheduler([make_task("bad", action="explode")])
    scheduler.tick()
    clock.advance(minutes=5)
    scheduler.tick()
    assert exploder.attempts == 2


def test_an_unroutable_action_costs_no_quota(make_scheduler, quota):
    """The executor is resolved before quota is consumed, so a typo is free."""
    scheduler = make_scheduler([make_task("odd", action="teleport")])
    scheduler.tick()
    assert quota.remaining("alice", NOON.date()) == 3


# -- dry run ---------------------------------------------------------------


def test_dry_run_leaves_no_trace(make_scheduler, recorder, quota):
    """A rehearsal must not spend quota or mark tasks as done.

    Otherwise the real run immediately afterwards would skip everything it previewed.
    """
    scheduler = make_scheduler([make_task("t1")], dry_run=True)
    report = scheduler.tick()

    assert len(report.succeeded) == 1
    assert quota.remaining("alice", NOON.date()) == 3
    assert scheduler.run_log.has_run("t1", NOON.date()) is False


# -- reporting and plumbing ------------------------------------------------


def test_the_report_describes_the_tick(make_scheduler):
    report = make_scheduler([make_task("t1"), make_task("t2")]).tick()

    assert len(report) == 2
    assert report.run_id
    assert "2 attempted" in report.summary()
    assert [r.task_id for r in report] == ["t1", "t2"]


def test_an_empty_tick_says_so(make_scheduler):
    assert make_scheduler([]).tick().summary() == "nothing due"


def test_executors_receive_the_schedulers_notion_of_now(make_scheduler, recorder):
    """Executors must use ctx.now, not datetime.now(), or frozen-clock tests lie."""
    make_scheduler([make_task("t1")]).tick()
    assert recorder.contexts[0].now == NOON


def test_duplicate_task_ids_are_refused(make_scheduler):
    scheduler = make_scheduler([make_task("t1")])
    try:
        scheduler.add_task(make_task("t1"))
    except ValueError as exc:
        assert "t1" in str(exc)
    else:
        raise AssertionError("a duplicate id must be refused")


def test_remove_task(make_scheduler, recorder):
    scheduler = make_scheduler([make_task("t1"), make_task("t2")])
    assert scheduler.remove_task("t1") is True
    assert scheduler.remove_task("nope") is False
    scheduler.tick()
    assert recorder.calls == ["t2"]


def test_run_forever_stops_after_max_ticks(make_scheduler):
    scheduler = make_scheduler([make_task("t1")])
    assert scheduler.run_forever(interval_seconds=0, max_ticks=3) == 3


def test_purge_history_clears_old_state(make_scheduler, clock):
    scheduler = make_scheduler([make_task("t1")])
    scheduler.tick()
    assert len(scheduler.run_log) == 1
    scheduler.purge_history(before=TOMORROW.date())
    assert len(scheduler.run_log) == 0


def test_the_run_log_is_swappable(make_scheduler, recorder):
    """The Protocol is what lets a team drop in a database-backed run log."""

    class AlwaysRan(InMemoryRunLog):
        def has_run(self, task_id, on_date):
            return True

    scheduler = make_scheduler([make_task("t1")], run_log=AlwaysRan())
    assert scheduler.tick().results == ()
    assert recorder.call_count == 0
