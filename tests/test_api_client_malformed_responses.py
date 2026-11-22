"""Malformed successful responses raise ``ApiError`` (status 200), never ``KeyError`` or
``TypeError``, while older-server optional fields keep their defaults and unknown fields are
ignored (``/api/v1`` is additive-only)."""

from __future__ import annotations

import httpx
import pytest

from pepper_cli import api_client
from pepper_cli.api_client import ApiError
from pepper_cli.secret import SecretStr


def _api(body: object) -> api_client.ApiClient:
    return api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=body))
        ),
    )


def _task(**over: object) -> dict:
    row = {
        "id": "t1",
        "name": "n",
        "destinations": None,
        "active": True,
        "one_shot": False,
        "next_run_at": "2026-01-01T00:00:00Z",
    }
    return {**row, **over}


def _traj(**over: object) -> dict:
    body = {
        "trajectory": {
            "id": "tr1",
            "user_id": "u1",
            "channel": "web",
            "created_at": "2026-01-01T00:00:00Z",
        },
        "events": [
            {"id": "e1", "seq": 1, "event_type": "x", "created_at": "z", "payload": {"a": 1}}
        ],
    }
    return {**body, **over}


def _without(row: dict, key: str) -> dict:
    return {k: v for k, v in row.items() if k != key}


def test_valid_scheduled_tasks_keep_defaults_and_ignore_unknown_fields() -> None:
    with _api([_task(future_field={"x": 1})]) as api:
        (task,) = api.list_scheduled_tasks()
    assert (task.id, task.description, task.cron, task.destinations) == ("t1", None, None, None)


@pytest.mark.parametrize(
    "body",
    [
        [_without(_task(), "id")],
        [_without(_task(), "next_run_at")],
        [_task(id=7)],
        [_task(name=None)],
        [_task(active="yes")],
        [_task(one_shot=1)],
        [_task(destinations="email")],
        ["not a row"],
        [None],
    ],
    ids=[
        "no-id",
        "no-next-run",
        "int-id",
        "null-name",
        "str-bool",
        "int-bool",
        "str-dests",
        "str-row",
        "null-row",
    ],
)
def test_malformed_scheduled_task_rows_raise_api_error(body: list) -> None:
    with _api(body) as api, pytest.raises(ApiError) as raised:
        api.list_scheduled_tasks()
    assert raised.value.status == 200
    assert "malformed response" in str(raised.value)


def test_a_room_task_without_delivery_is_malformed() -> None:
    with _api([_task()]) as api, pytest.raises(ApiError):
        api.list_scheduled_tasks(group_id="g1")


@pytest.mark.parametrize("body", [[], "x", 3])
def test_a_scheduled_task_detail_that_is_not_an_object_is_malformed(body: object) -> None:
    with _api(body) as api, pytest.raises(ApiError):
        api.get_scheduled_task("t1")


def test_a_malformed_latest_run_is_malformed() -> None:
    with _api(_task(latest_run={"id": "r1"})) as api, pytest.raises(ApiError) as raised:
        api.get_scheduled_task("t1")
    assert "scheduled task run" in str(raised.value)


def test_valid_trajectory_detail_parses_with_defaults() -> None:
    body = _traj()
    body["trajectory"]["future"] = 1
    with _api(body) as api:
        detail = api.trajectory_get("tr1")
    assert detail.trajectory.meta == {}
    assert detail.trajectory.conversation_key is None
    assert [(e.seq, e.payload) for e in detail.events] == [(1, {"a": 1})]
    assert api_client._trajectory_detail_from_dict({"trajectory": body["trajectory"]}).events == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"trajectory": "x"},
        {"trajectory": None},
        _traj(events="x"),
        _traj(events=["x"]),
        _traj(events=[{"id": "e1"}]),
        _traj(events=[{"id": "e1", "seq": "1", "event_type": "x", "created_at": "z"}]),
        _traj(
            events=[{"id": "e1", "seq": 1, "event_type": "x", "created_at": "z", "payload": "x"}]
        ),
        _traj(trajectory={"id": "tr1"}),
        _traj(trajectory={"id": 1, "user_id": "u", "channel": "c", "created_at": "z"}),
        _traj(trajectory={"id": "t", "user_id": "u", "channel": "c", "created_at": "z", "meta": 3}),
        [],
    ],
)
def test_malformed_trajectory_details_raise_api_error(body: object) -> None:
    with _api(body) as api, pytest.raises(ApiError) as raised:
        api.trajectory_get("tr1")
    assert raised.value.status == 200
    assert len(raised.value.body) <= 200


def test_chat_history_malformed_rows_raise_api_error() -> None:
    with _api({"rows": []}) as api, pytest.raises(ApiError):
        api.chat_history()
    with _api([{"id": "a1", "role": "user"}]) as api, pytest.raises(ApiError):
        api.chat_history()
