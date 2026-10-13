"""Runnable demonstration of the refactored scheduler.

    python main.py                 # the full walkthrough
    python main.py --dry-run       # executors describe their work, change nothing
    python main.py --log-level DEBUG

Five scenes, each one a defect from the legacy implementation that this design removes.
The clock is frozen so the output is identical on every machine, on any day -- which is
also how the test suite drives the scheduler.

This file is the *application*: it is the only place that configures logging, and the
only place that decides which clock, registry and run log to use. Everything it uses is
public API from :mod:`orbit_scheduler`.
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from orbit_scheduler import (
    ExecutionContext,
    FrozenClock,
    RunReport,
    Task,
    TaskExecutor,
    build_scheduler,
    configure_logging,
    default_registry,
    load_schedule_file,
)

CONFIG_PATH = Path(__file__).parent / "config" / "schedule.example.json"

DAY_ONE = dt.datetime(2026, 3, 2, 12, 0, 5)
DAY_TWO = DAY_ONE + dt.timedelta(days=1)
DAY_THREE = DAY_ONE + dt.timedelta(days=2)
DAY_FOUR = DAY_ONE + dt.timedelta(days=3)


class UnreliableExecutor(TaskExecutor):
    """An action that always fails, so the demo can show what happens when one does.

    Registering it is also the extension point in miniature: this class is defined in
    the application, outside the library, and the scheduler runs it without a single
    change anywhere in :mod:`orbit_scheduler`.
    """

    action = "flaky"
    description = "Always raises. Demonstrates failure isolation and quota refund."

    def execute(self, task: Task, ctx: ExecutionContext) -> str:
        raise ConnectionError("upstream storage refused the connection")


def scene(number: int, title: str, proves: str) -> None:
    print()
    print("=" * 78)
    print(f"  SCENE {number}  {title}")
    print(f"  proves: {proves}")
    print("=" * 78)


def show(report: RunReport) -> None:
    """Print a run report as a small table."""
    print(f"\n  -> {report.summary()}")
    if not report.results:
        return
    width = max(len(r.task_id) for r in report.results)
    for result in report.results:
        marker = "ok  " if result.ok else "SKIP" if result.status.is_skip else "FAIL"
        note = result.detail or result.error or ""
        print(f"     {marker}  {result.task_id:<{width}}  {result.status}  {note}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING, ...")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="executors describe their work instead of performing it",
    )
    args = parser.parse_args()

    # The application configures logging. The library never does.
    configure_logging(args.log_level.upper())

    # A copy, so registering the demo-only executor cannot leak into another process
    # that imports this package.
    registry = default_registry.copy()
    registry.register(UnreliableExecutor)

    print("Registered actions:")
    print(registry.describe())

    config = load_schedule_file(CONFIG_PATH, registry=registry)
    print(f"\nLoaded {CONFIG_PATH.name}: {config!r}")

    clock = FrozenClock(DAY_ONE)
    scheduler = build_scheduler(
        config, registry=registry, clock=clock, dry_run=args.dry_run
    )

    # ------------------------------------------------------------------ 1 --
    scene(1, "Day one, 12:00", "the happy path: three tasks, three executions")
    show(scheduler.tick())

    # ------------------------------------------------------------------ 2 --
    scene(
        2,
        "Day one, 12:00:35 -- a second tick in the same minute",
        "idempotency. The legacy '%H:%M' equality check ran everything twice here.",
    )
    clock.advance(seconds=30)
    show(scheduler.tick())

    # ------------------------------------------------------------------ 3 --
    scene(
        3,
        "Day two, 12:00",
        "the daily quota reset. The legacy counter never reset, so alice -- who used "
        "2 of 3 yesterday -- would be permanently locked out from here on.",
    )
    clock.set(DAY_TWO)
    show(scheduler.tick())
    print(f"\n     alice has {scheduler.quota.remaining('alice', DAY_TWO.date())} of 3 left today")

    # ------------------------------------------------------------------ 4 --
    scene(
        4,
        "Day three, with three broken tasks mixed in",
        "failure isolation. In the legacy loop the unknown user raised KeyError and "
        "every task after it silently never ran.",
    )
    clock.set(DAY_THREE)
    scheduler.add_task(
        Task(
            id="ghost-sync",
            user_id="ghost",
            action="sync",
            scheduled_at=dt.time(12, 0),
            params={"target": "/data/ghost"},
        )
    )
    scheduler.add_task(
        Task(
            id="alice-flaky",
            user_id="alice",
            action="flaky",
            scheduled_at=dt.time(12, 0),
            params={"target": "/data/unreliable"},
        )
    )
    scheduler.add_task(
        Task(
            id="bob-teleport",
            user_id="bob",
            action="teleport",
            scheduled_at=dt.time(12, 0),
            params={"target": "/srv/elsewhere"},
        )
    )
    show(scheduler.tick())
    remaining = scheduler.quota.remaining("alice", DAY_THREE.date())
    print(f"\n     alice has {remaining} of 3 left -- the failed task was refunded")

    # ------------------------------------------------------------------ 5 --
    scene(
        5,
        "Day four, alice over quota",
        "quota enforcement: the fourth task is refused, and says so clearly.",
    )
    clock.set(DAY_FOUR)
    scheduler.remove_task("ghost-sync")
    scheduler.remove_task("bob-teleport")
    scheduler.remove_task("alice-flaky")
    for n in range(1, 3):
        scheduler.add_task(
            Task(
                id=f"alice-extra-{n}",
                user_id="alice",
                action="sync",
                scheduled_at=dt.time(12, 0),
                params={"target": f"/data/extra{n}"},
            )
        )
    show(scheduler.tick())

    print()
    print("=" * 78)
    print("  Run `pytest -q` for the same behaviours asserted as tests.")
    print("  Run `python legacy/original.py` to compare against the starting point.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
