"""Configuration loading and validation.

The behaviour worth defending here is :func:`test_every_problem_is_reported_at_once`.
Fail-on-first validation turns one bad config into one deploy per typo.
"""

from __future__ import annotations

import datetime as dt

import pytest

from orbit_scheduler import (
    ConfigError,
    build_scheduler,
    load_schedule,
    load_schedule_file,
)

VALID = {
    "users": {"alice": {"quota": 3}, "bob": 5},
    "tasks": [
        {"user": "alice", "time": "12:00", "action": "sync", "params": {"target": "/data/x"}},
        {"user": "bob", "time": "13:30", "action": "backup", "params": {"target": "/srv/y"}},
    ],
}


def test_loads_a_valid_config():
    config = load_schedule(VALID)

    assert len(config.users) == 2
    assert config.users.get("bob").daily_quota == 5
    assert [t.action for t in config.tasks] == ["sync", "backup"]
    assert config.tasks[1].scheduled_at == dt.time(13, 30)


def test_the_legacy_flat_task_shape_still_loads():
    """An existing deployment can migrate its code before its configuration.

    ``target`` sits beside ``action`` rather than inside ``params``, exactly as in the
    original script's task dicts.
    """
    config = load_schedule(
        {
            "users": {"alice": {"quota": 3, "executed": 0}},
            "tasks": [
                {"user": "alice", "time": "12:00", "action": "sync", "target": "/data/x"}
            ],
        }
    )
    assert config.tasks[0].params["target"] == "/data/x"


def test_ids_are_derived_from_content_not_position():
    """A stable id matters: the run log keys on it, so a shifting id re-runs the task."""
    first = load_schedule(VALID).tasks
    reordered = dict(VALID, tasks=list(reversed(VALID["tasks"])))
    second = load_schedule(reordered).tasks

    assert {t.id for t in first} == {t.id for t in second}


def test_explicit_ids_win():
    config = load_schedule(
        dict(VALID, tasks=[dict(VALID["tasks"][0], id="nightly-sync")])
    )
    assert config.tasks[0].id == "nightly-sync"


# -- validation ------------------------------------------------------------


def test_every_problem_is_reported_at_once():
    """Three mistakes should cost one run, not three."""
    with pytest.raises(ConfigError) as exc:
        load_schedule(
            {
                "users": {"alice": {"quota": 3}},
                "tasks": [
                    {"user": "ghost", "time": "12:00", "action": "sync",
                     "params": {"target": "/data/x"}},
                    {"user": "alice", "time": "25:99", "action": "sync",
                     "params": {"target": "/data/y"}},
                    {"user": "alice", "time": "12:00", "action": "teleport",
                     "params": {"target": "/data/z"}},
                ],
            }
        )

    problems = exc.value.problems
    assert len(problems) == 3
    joined = " ".join(problems)
    assert "ghost" in joined and "25:99" in joined and "teleport" in joined


def test_an_unknown_user_is_rejected_with_the_known_ones():
    with pytest.raises(ConfigError, match="alice"):
        load_schedule(dict(VALID, tasks=[dict(VALID["tasks"][0], user="ghost")]))


def test_an_unknown_action_is_rejected():
    with pytest.raises(ConfigError, match="teleport"):
        load_schedule(dict(VALID, tasks=[dict(VALID["tasks"][0], action="teleport")]))


def test_bad_parameters_are_caught_at_load_not_at_noon():
    """validate_params runs here, so the deploy fails instead of the 12:00 run."""
    with pytest.raises(ConfigError, match="absolute"):
        load_schedule(
            dict(VALID, tasks=[dict(VALID["tasks"][0], params={"target": "relative"})])
        )


def test_a_delete_outside_the_allowed_roots_never_reaches_production():
    with pytest.raises(ConfigError, match="restricted to"):
        load_schedule(
            dict(
                VALID,
                tasks=[{"user": "alice", "time": "12:00", "action": "delete",
                        "params": {"target": "/etc/passwd"}}],
            )
        )


def test_duplicate_task_ids_are_rejected():
    task = dict(VALID["tasks"][0], id="same")
    with pytest.raises(ConfigError, match="duplicate task id"):
        load_schedule(dict(VALID, tasks=[task, dict(task)]))


@pytest.mark.parametrize(
    "config, expected",
    [
        ({"tasks": []}, "missing 'users'"),
        ({"users": {}}, "missing 'tasks'"),
        ({"users": [], "tasks": []}, "'users' must be an object"),
        ({"users": {}, "tasks": {}}, "'tasks' must be a list"),
    ],
)
def test_malformed_sections_are_named(config, expected):
    with pytest.raises(ConfigError, match=expected.replace("'", "'")):
        load_schedule(config)


def test_a_bad_quota_is_attributed_to_its_user():
    with pytest.raises(ConfigError, match=r"users\.alice"):
        load_schedule(dict(VALID, users={"alice": {"quota": -1}}))


def test_an_unparseable_time_is_rejected():
    with pytest.raises(ConfigError, match="HH:MM"):
        load_schedule(dict(VALID, tasks=[dict(VALID["tasks"][0], time="noon")]))


# -- files -----------------------------------------------------------------


def test_loads_the_shipped_example(tmp_path):
    config = load_schedule_file("config/schedule.example.json")
    assert len(config.tasks) == 3
    assert config.settings["catchup_minutes"] == 60


def test_a_missing_file_is_a_config_error():
    with pytest.raises(ConfigError, match="not found"):
        load_schedule_file("config/does-not-exist.json")


def test_invalid_json_names_the_line(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"users": {,}}', encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid JSON at line"):
        load_schedule_file(path)


# -- assembly --------------------------------------------------------------


def test_build_scheduler_wires_everything_together():
    scheduler = build_scheduler(load_schedule(VALID))
    assert len(scheduler.tasks) == 2
    assert scheduler.quota.remaining("alice", dt.date(2026, 3, 2)) == 3


def test_catchup_minutes_setting_is_honoured():
    scheduler = build_scheduler(load_schedule(dict(VALID, settings={"catchup_minutes": 5})))
    assert scheduler._catchup_window == dt.timedelta(minutes=5)


def test_a_negative_catchup_setting_disables_the_window():
    scheduler = build_scheduler(load_schedule(dict(VALID, settings={"catchup_minutes": -1})))
    assert scheduler._catchup_window is None
