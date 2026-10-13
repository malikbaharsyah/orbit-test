"""The data model: immutability, parsing, and the legacy-shape migration path."""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from orbit_scheduler import ExecutionResult, ExecutionStatus, Task
from orbit_scheduler.models import parse_time

# -- time parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [("12:00", dt.time(12, 0)), ("00:00", dt.time(0, 0)), (" 23:59 ", dt.time(23, 59))],
)
def test_parses_valid_times(value, expected):
    assert parse_time(value) == expected


@pytest.mark.parametrize("value", ["noon", "25:00", "12:60", "12", "", "1200"])
def test_rejects_invalid_times(value):
    with pytest.raises(ValueError, match="HH:MM"):
        parse_time(value)


def test_a_time_object_passes_through():
    assert parse_time(dt.time(9, 30)) == dt.time(9, 30)


# -- Task ------------------------------------------------------------------


def test_a_task_is_immutable():
    """A task is a statement of intent; nothing downstream may rewrite it."""
    task = Task(id="t1", user_id="alice", action="sync", scheduled_at=dt.time(12, 0))
    with pytest.raises(dataclasses.FrozenInstanceError):
        task.action = "delete"  # type: ignore[misc]


def test_task_params_cannot_be_mutated_by_an_executor():
    task = Task(
        id="t1",
        user_id="alice",
        action="sync",
        scheduled_at=dt.time(12, 0),
        params={"target": "/data/x"},
    )
    with pytest.raises(TypeError):
        task.params["target"] = "/etc/passwd"  # type: ignore[index]


def test_params_are_copied_not_aliased():
    """Mutating the caller's dict afterwards must not change the task."""
    source = {"target": "/data/x"}
    task = Task(
        id="t1", user_id="alice", action="sync", scheduled_at=dt.time(12, 0), params=source
    )
    source["target"] = "/etc/passwd"
    assert task.params["target"] == "/data/x"


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"id": ""}, "task id"),
        ({"user_id": ""}, "user_id"),
        ({"action": ""}, "action"),
    ],
)
def test_a_task_requires_its_identifiers(kwargs, message):
    base = dict(id="t1", user_id="alice", action="sync", scheduled_at=dt.time(12, 0))
    with pytest.raises(ValueError, match=message):
        Task(**{**base, **kwargs})


def test_from_dict_accepts_the_legacy_flat_shape():
    """Exactly the dict shape the original script used."""
    task = Task.from_dict(
        {"user": "alice", "time": "12:00", "action": "sync", "target": "/data/x"},
        default_id="t1",
    )
    assert task.user_id == "alice"
    assert task.scheduled_at == dt.time(12, 0)
    assert task.params == {"target": "/data/x"}


def test_from_dict_accepts_the_nested_shape():
    task = Task.from_dict(
        {
            "id": "t1",
            "user_id": "alice",
            "action": "sync",
            "scheduled_at": "12:00",
            "params": {"target": "/data/x"},
        }
    )
    assert task.id == "t1"
    assert task.params["target"] == "/data/x"


def test_explicit_params_win_over_loose_keys():
    task = Task.from_dict(
        {
            "user": "alice",
            "time": "12:00",
            "action": "sync",
            "target": "/loose",
            "params": {"target": "/explicit"},
        },
        default_id="t1",
    )
    assert task.params["target"] == "/explicit"


def test_from_dict_requires_a_time():
    with pytest.raises(ValueError, match="time"):
        Task.from_dict({"user": "alice", "action": "sync"}, default_id="t1")


def test_to_dict_round_trips():
    original = Task.from_dict(
        {"id": "t1", "user": "alice", "time": "12:00", "action": "sync",
         "params": {"target": "/data/x"}}
    )
    assert Task.from_dict(original.to_dict()) == original


def test_due_at_combines_date_and_time():
    task = Task(id="t1", user_id="alice", action="sync", scheduled_at=dt.time(12, 0))
    assert task.due_at(dt.date(2026, 3, 2)) == dt.datetime(2026, 3, 2, 12, 0)


def test_describe_reads_as_a_sentence():
    task = Task.from_dict(
        {"user": "alice", "time": "12:00", "action": "sync", "target": "/data/x"},
        default_id="t1",
    )
    assert task.describe() == "sync on /data/x for alice at 12:00"


# -- ExecutionStatus and ExecutionResult -----------------------------------


@pytest.mark.parametrize(
    "status, is_skip",
    [
        (ExecutionStatus.SUCCESS, False),
        (ExecutionStatus.FAILED, False),
        (ExecutionStatus.SKIPPED_QUOTA, True),
        (ExecutionStatus.SKIPPED_UNKNOWN_USER, True),
    ],
)
def test_skip_statuses_are_distinguishable_from_failures(status, is_skip):
    """A refused task and a broken task are different things on a dashboard."""
    assert status.is_skip is is_skip


def test_statuses_serialise_as_plain_strings():
    assert ExecutionStatus.SUCCESS.value == "success"
    assert f"{ExecutionStatus.SKIPPED_QUOTA}" == "skipped_quota"


def test_only_success_counts_as_ok():
    def result(status):
        return ExecutionResult(task_id="t1", user_id="alice", action="sync", status=status)

    assert result(ExecutionStatus.SUCCESS).ok
    assert not result(ExecutionStatus.FAILED).ok
    assert not result(ExecutionStatus.SKIPPED_QUOTA).ok


def test_duration_is_none_when_untimed():
    assert ExecutionResult("t1", "alice", "sync", ExecutionStatus.SUCCESS).duration_ms is None


def test_duration_is_measured_when_timed():
    start = dt.datetime(2026, 3, 2, 12, 0, 0)
    result = ExecutionResult(
        "t1", "alice", "sync", ExecutionStatus.SUCCESS,
        started_at=start, finished_at=start + dt.timedelta(milliseconds=250),
    )
    assert result.duration_ms == 250.0


def test_a_result_serialises_for_metrics():
    payload = ExecutionResult(
        "t1", "alice", "sync", ExecutionStatus.SUCCESS, detail="synced"
    ).to_dict()
    assert payload["status"] == "success"
    assert payload["task_id"] == "t1"
