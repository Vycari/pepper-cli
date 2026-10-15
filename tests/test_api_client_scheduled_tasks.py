"""Tests for the ApiClient scheduled-task methods.

Same ``httpx.MockTransport`` style as ``test_api_client_todos.py`` —
assert the wire shape (method, URL, body) and that responses parse into
the dataclasses. This module is the contract the native clients
reproduce, so the three destination states get their own assertions:
``null`` / ``[]`` / a list are three different bodies and three different
behaviors, and a client that coalesced any two would silence a task.
"""

from __future__ import annotations

import json

import httpx
import pytest

from pepper_cli import api_client
from pepper_cli.secret import SecretStr

_TASK_ID = "00000000-0000-0000-0000-0000000000a1"


def _mock(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _client(handler) -> api_client.ApiClient:
    return api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    )


def _task_json(**over) -> dict:
    base = {
        "id": _TASK_ID,
        "name": "morning-weather",
        "description": None,
        "cron": "0 8 * * 1-5",
        "timezone": None,
        "destinations": None,
        "active": True,
        "one_shot": False,
        "last_run_at": None,
        "next_run_at": "2026-06-05T15:00:00+00:00",
    }
    base.update(over)
    return base


def _run_json(**over) -> dict:
    base = {
        "id": "00000000-0000-0000-0000-0000000000b1",
        "fired_at": "2026-06-04T15:00:00+00:00",
        "finished_at": "2026-06-04T15:00:09+00:00",
        "status": "succeeded",
        "error": None,
        "delivered_channels": ["main_chat"],
        "trajectory_id": None,
        "reply_preview": "Sunny, 21C.",
    }
    base.update(over)
    return base


def _capture(response: httpx.Response) -> tuple[dict, object]:
    """A handler that records the request and returns ``response``."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content) if request.content else None
        return response

    return seen, handler


def test_list_scheduled_tasks_parses_and_defaults_to_active_only() -> None:
    seen, handler = _capture(httpx.Response(200, json=[_task_json(destinations=["main_chat"])]))
    with _client(handler) as api:
        rows = api.list_scheduled_tasks()

    assert seen["method"] == "GET"
    assert seen["url"] == "http://example.com/api/v1/scheduled-tasks"
    assert isinstance(rows[0], api_client.ScheduledTaskSummary)
    assert rows[0].destinations == ["main_chat"]


def test_list_scheduled_tasks_include_inactive() -> None:
    seen, handler = _capture(httpx.Response(200, json=[]))
    with _client(handler) as api:
        api.list_scheduled_tasks(include_inactive=True)
    assert seen["url"] == "http://example.com/api/v1/scheduled-tasks?include_inactive=true"


def test_list_scheduled_tasks_rejects_a_non_list_body() -> None:
    _, handler = _capture(httpx.Response(200, json={"oops": True}))
    with _client(handler) as api, pytest.raises(api_client.ApiError):
        api.list_scheduled_tasks()


def test_get_scheduled_task_parses_the_latest_run() -> None:
    body = _task_json()
    body["latest_run"] = _run_json()
    seen, handler = _capture(httpx.Response(200, json=body))
    with _client(handler) as api:
        detail = api.get_scheduled_task(_TASK_ID)

    assert seen["url"] == f"http://example.com/api/v1/scheduled-tasks/{_TASK_ID}"
    assert isinstance(detail, api_client.ScheduledTaskDetail)
    assert detail.latest_run is not None
    assert detail.latest_run.status == "succeeded"


def test_get_scheduled_task_without_a_run() -> None:
    body = _task_json()
    body["latest_run"] = None
    _, handler = _capture(httpx.Response(200, json=body))
    with _client(handler) as api:
        assert api.get_scheduled_task(_TASK_ID).latest_run is None


def test_list_runs_and_thread_pass_their_limits() -> None:
    seen, handler = _capture(httpx.Response(200, json=[_run_json()]))
    with _client(handler) as api:
        runs = api.list_scheduled_task_runs(_TASK_ID, limit=5)
    assert seen["url"].endswith(f"/scheduled-tasks/{_TASK_ID}/runs?limit=5")
    assert runs[0].reply_preview == "Sunny, 21C."

    message = {
        "id": "m1",
        "role": "assistant",
        "text": "Sunny, 21C.",
        "channel": "client_api",
        "trajectory_id": None,
        "created_at": "2026-06-04T15:00:09+00:00",
    }
    seen, handler = _capture(httpx.Response(200, json=[message]))
    with _client(handler) as api:
        rows = api.get_scheduled_task_thread(_TASK_ID, limit=7)
    assert seen["url"].endswith(f"/scheduled-tasks/{_TASK_ID}/thread?limit=7")
    # A message with no attachments key still parses — the server omits
    # the field for text-only rows.
    assert rows[0].attachments == []


def test_create_scheduled_task_omits_unset_fields() -> None:
    """An omitted ``destinations`` key is what "inherit my notification
    preference" looks like on the wire — sending ``null`` explicitly would
    mean the same thing, but sending ``[]`` would not."""
    seen, handler = _capture(httpx.Response(201, json=_task_json()))
    with _client(handler) as api:
        api.create_scheduled_task(name="x", prompt="p", cron="0 8 * * *")

    assert seen["method"] == "POST"
    assert seen["url"] == "http://example.com/api/v1/scheduled-tasks"
    assert seen["body"] == {"name": "x", "prompt": "p", "cron": "0 8 * * *"}


def test_create_scheduled_task_sends_an_empty_destination_list() -> None:
    seen, handler = _capture(httpx.Response(201, json=_task_json(destinations=[])))
    with _client(handler) as api:
        task = api.create_scheduled_task(
            name="quiet", prompt="p", cron="0 8 * * *", destinations=[]
        )

    assert seen["body"]["destinations"] == []
    assert task.destinations == []


def test_create_scheduled_task_sends_run_at_for_a_one_shot() -> None:
    seen, handler = _capture(httpx.Response(201, json=_task_json(one_shot=True, cron=None)))
    with _client(handler) as api:
        api.create_scheduled_task(name="x", prompt="p", run_at="2099-01-01T09:00:00+00:00")
    assert seen["body"]["run_at"] == "2099-01-01T09:00:00+00:00"
    assert "cron" not in seen["body"]


def test_update_scheduled_task_passes_the_edits_verbatim() -> None:
    """The dict is the contract: whatever the caller put in it is what the
    server is told to write, and nothing else is mentioned."""
    seen, handler = _capture(httpx.Response(200, json=_task_json()))
    with _client(handler) as api:
        api.update_scheduled_task(_TASK_ID, edits={"description": None})

    assert seen["method"] == "PATCH"
    assert seen["url"] == f"http://example.com/api/v1/scheduled-tasks/{_TASK_ID}"
    assert seen["body"] == {"description": None}


def test_cancel_and_resume_are_posts() -> None:
    seen, handler = _capture(httpx.Response(200, json=_task_json(active=False)))
    with _client(handler) as api:
        assert api.cancel_scheduled_task(_TASK_ID).active is False
    assert seen["method"] == "POST"
    assert seen["url"].endswith(f"/scheduled-tasks/{_TASK_ID}/cancel")

    seen, handler = _capture(httpx.Response(200, json=_task_json(active=True)))
    with _client(handler) as api:
        assert api.resume_scheduled_task(_TASK_ID).active is True
    assert seen["url"].endswith(f"/scheduled-tasks/{_TASK_ID}/resume")


@pytest.mark.parametrize("value", [None, [], ["main_chat", "resend_email"]])
def test_set_destinations_always_sends_the_field(value) -> None:
    """All three states go on the wire under the same required key — an
    omission must never be readable as "make it silent"."""
    seen, handler = _capture(httpx.Response(200, json=_task_json(destinations=value)))
    with _client(handler) as api:
        api.set_scheduled_task_destinations(_TASK_ID, destinations=value)

    assert seen["method"] == "PUT"
    assert seen["url"].endswith(f"/scheduled-tasks/{_TASK_ID}/destinations")
    assert seen["body"] == {"destinations": value}


def test_errors_carry_the_servers_detail() -> None:
    _, handler = _capture(httpx.Response(409, json={"detail": "already exists"}))
    with _client(handler) as api, pytest.raises(api_client.ApiError) as excinfo:
        api.create_scheduled_task(name="x", prompt="p", cron="0 8 * * *")
    assert excinfo.value.status == 409
    assert excinfo.value.detail == "already exists"


def test_a_missing_destinations_key_is_a_malformed_response() -> None:
    """``.get`` would collapse it onto ``None`` — a real state meaning
    inherit — so a truncated payload would look like a valid task."""
    body = _task_json()
    del body["destinations"]
    _, handler = _capture(httpx.Response(200, json=[body]))
    with _client(handler) as api, pytest.raises(KeyError):
        api.list_scheduled_tasks()


# ---------------------------------------------------------------------------
# A room's tasks (``group_id=``) — ``/api/v1/groups/{id}/scheduled-tasks``
#
# The room's wire differs from the personal one in exactly the places a
# client could get wrong by analogy: pause and resume are one PATCH with a
# boolean, delivery is a nested object with a *required* list, and "the
# default" is a sentinel rather than ``null``.

_ROOM = "00000000-0000-0000-0000-0000000000c1"
_ROOM_TASKS = f"http://example.com/api/v1/groups/{_ROOM}/scheduled-tasks"


def _room_task_json(**over) -> dict:
    base = _task_json(delivery="Email to every member", last_run_status=None)
    base.update(over)
    return base


def test_room_list_hits_the_room_and_parses_the_room_fields() -> None:
    row = _room_task_json(last_run_status="succeeded")
    seen, handler = _capture(httpx.Response(200, json=[row]))
    with _client(handler) as api:
        rows = api.list_scheduled_tasks(include_inactive=True, group_id=_ROOM)

    assert seen["method"] == "GET"
    assert seen["url"] == f"{_ROOM_TASKS}?include_inactive=true"
    assert isinstance(rows[0], api_client.RoomScheduledTaskSummary)
    assert rows[0].delivery == "Email to every member"
    assert rows[0].last_run_status == "succeeded"


def test_room_list_defaults_to_active_only() -> None:
    seen, handler = _capture(httpx.Response(200, json=[]))
    with _client(handler) as api:
        assert api.list_scheduled_tasks(group_id=_ROOM) == []
    assert seen["url"] == _ROOM_TASKS


def test_room_create_omits_delivery_for_the_rooms_default() -> None:
    seen, handler = _capture(httpx.Response(201, json=_room_task_json()))
    with _client(handler) as api:
        task = api.create_scheduled_task(
            name="weekly-menu", prompt="Plan dinners.", cron="0 9 * * 0", group_id=_ROOM
        )

    assert seen["method"] == "POST"
    assert seen["url"] == _ROOM_TASKS
    # No ``delivery`` key at all: the server decides the room's delivery.
    assert seen["body"] == {"name": "weekly-menu", "prompt": "Plan dinners.", "cron": "0 9 * * 0"}
    assert isinstance(task, api_client.RoomScheduledTaskSummary)


def test_room_create_sends_delivery_timezone_and_topic() -> None:
    seen, handler = _capture(httpx.Response(201, json=_room_task_json()))
    with _client(handler) as api:
        api.create_scheduled_task(
            name="weekly-menu",
            prompt="Plan dinners.",
            cron="0 9 * * 0",
            timezone="Europe/Zurich",
            destinations=["groups_service"],
            topic="Dinner",
            group_id=_ROOM,
        )
    assert seen["body"] == {
        "name": "weekly-menu",
        "prompt": "Plan dinners.",
        "cron": "0 9 * * 0",
        "timezone": "Europe/Zurich",
        "delivery": {"destinations": ["groups_service"], "topic": "Dinner"},
    }


def test_room_create_never_sends_a_description() -> None:
    """A room task has no description; a caller that passes one by habit must
    not smuggle it onto the room's route."""
    seen, handler = _capture(httpx.Response(201, json=_room_task_json()))
    with _client(handler) as api:
        api.create_scheduled_task(
            name="t",
            prompt="p",
            cron="0 9 * * 0",
            description="d",
            destinations=[],
            group_id=_ROOM,
        )
    assert seen["body"] == {
        "name": "t",
        "prompt": "p",
        "cron": "0 9 * * 0",
        "delivery": {"destinations": []},
    }


def test_room_create_omits_what_was_not_given_never_null() -> None:
    """The server types prompt and cron as strings, so an unset one is left out
    of the body — ``null`` would be a 422."""
    seen, handler = _capture(httpx.Response(201, json=_room_task_json(one_shot=True, cron=None)))
    with _client(handler) as api:
        api.create_scheduled_task(
            name="t", skill="weekly-menu", run_at="2030-01-01T09:00:00+00:00", group_id=_ROOM
        )
    assert seen["method"] == "POST"
    assert seen["body"] == {
        "name": "t",
        "skill": "weekly-menu",
        "run_at": "2030-01-01T09:00:00+00:00",
    }


def test_personal_create_refuses_a_skill_or_a_missing_prompt() -> None:
    _, handler = _capture(httpx.Response(201, json=_task_json()))
    with _client(handler) as api:
        with pytest.raises(ValueError, match="needs a prompt"):
            api.create_scheduled_task(name="t", prompt="p", cron="0 9 * * *", skill="s")
        with pytest.raises(ValueError, match="needs a prompt"):
            api.create_scheduled_task(name="t", cron="0 9 * * *")


def _room_detail_json(**over) -> dict:
    base = _room_task_json(
        latest_run=None,
        group_name="Trip",
        group_members=["Ada", "Grace"],
        group_delivery="Posted to the room",
    )
    base.update(over)
    return base


def test_room_show_hits_the_room_route_and_parses_the_group_fields() -> None:
    seen, handler = _capture(httpx.Response(200, json=_room_detail_json()))
    with _client(handler) as api:
        detail = api.get_scheduled_task(_TASK_ID, group_id=_ROOM)
    assert (seen["method"], seen["url"]) == ("GET", f"{_ROOM_TASKS}/{_TASK_ID}")
    assert detail.group_name == "Trip"
    assert detail.group_members == ["Ada", "Grace"]
    assert detail.group_delivery == "Posted to the room"
    assert detail.latest_run is None


def test_personal_show_leaves_the_group_fields_empty() -> None:
    seen, handler = _capture(httpx.Response(200, json=_task_json(latest_run=None)))
    with _client(handler) as api:
        detail = api.get_scheduled_task(_TASK_ID)
    assert seen["url"] == f"http://example.com/api/v1/scheduled-tasks/{_TASK_ID}"
    assert (detail.group_name, detail.group_members, detail.group_delivery) == (None, [], None)


def test_room_runs_and_thread_use_the_room_routes_with_the_same_cursor() -> None:
    seen, handler = _capture(httpx.Response(200, json=[]))
    with _client(handler) as api:
        api.list_scheduled_task_runs(
            _TASK_ID, limit=5, before="2026-06-04T15:00:00+00:00", before_id="r1", group_id=_ROOM
        )
        runs_url = seen["url"]
        api.get_scheduled_task_thread(_TASK_ID, limit=7, group_id=_ROOM)
        thread_url = seen["url"]
    assert runs_url.startswith(f"{_ROOM_TASKS}/{_TASK_ID}/runs?")
    assert "limit=5" in runs_url and "before_id=r1" in runs_url
    assert thread_url == f"{_ROOM_TASKS}/{_TASK_ID}/thread?limit=7"


def test_room_renew_is_a_post_to_the_room_route_and_parses_a_room_row() -> None:
    seen, handler = _capture(httpx.Response(200, json=_room_task_json()))
    with _client(handler) as api:
        row = api.renew_scheduled_task(_TASK_ID, group_id=_ROOM)
    assert (seen["method"], seen["url"]) == ("POST", f"{_ROOM_TASKS}/{_TASK_ID}/renew")
    assert isinstance(row, api_client.RoomScheduledTaskSummary)


def test_room_update_sends_fields_and_delivery_in_one_patch() -> None:
    seen, handler = _capture(httpx.Response(200, json=_room_task_json()))
    with _client(handler) as api:
        api.update_room_scheduled_task(
            _ROOM,
            _TASK_ID,
            edits={"prompt": "New.", "description": None},
            destinations=["groups_service"],
            topic="Dinner",
        )
    assert (seen["method"], seen["url"]) == ("PATCH", f"{_ROOM_TASKS}/{_TASK_ID}")
    assert seen["body"] == {
        "prompt": "New.",
        "description": None,
        "delivery": {"destinations": ["groups_service"], "topic": "Dinner"},
    }


def test_room_update_without_destinations_sends_no_delivery_key() -> None:
    """A room has no preference to inherit, so ``None`` means leave it alone."""
    seen, handler = _capture(httpx.Response(200, json=_room_task_json()))
    with _client(handler) as api:
        api.update_room_scheduled_task(_ROOM, _TASK_ID, edits={"cron": "0 9 * * 1"})
        assert seen["body"] == {"cron": "0 9 * * 1"}
        with pytest.raises(ValueError, match="topic needs"):
            api.update_room_scheduled_task(_ROOM, _TASK_ID, edits={}, topic="Dinner")


def test_delete_scheduled_task_is_a_delete_and_tolerates_204() -> None:
    seen, handler = _capture(httpx.Response(204))
    with _client(handler) as api:
        assert api.delete_scheduled_task(_TASK_ID) is None
    assert (seen["method"], seen["url"]) == (
        "DELETE",
        f"http://example.com/api/v1/scheduled-tasks/{_TASK_ID}",
    )


def test_trajectory_list_sends_the_principal_only_when_given() -> None:
    seen, handler = _capture(httpx.Response(200, json=[]))
    with _client(handler) as api:
        api.trajectory_list(limit=5, offset=10)
        plain = seen["url"]
        api.trajectory_list(limit=5, offset=10, principal=_ROOM)
        scoped = seen["url"]
    assert "principal" not in plain and "limit=5" in plain and "offset=10" in plain
    assert f"principal={_ROOM}" in scoped


@pytest.mark.parametrize(("method", "active"), [("cancel", False), ("resume", True)])
def test_room_pause_and_resume_are_one_patch(method, active) -> None:
    seen, handler = _capture(httpx.Response(200, json=_room_task_json(active=active)))
    with _client(handler) as api:
        call = api.resume_scheduled_task if active else api.cancel_scheduled_task
        row = call(_TASK_ID, group_id=_ROOM)

    assert seen["method"] == "PATCH", method
    assert seen["url"] == f"{_ROOM_TASKS}/{_TASK_ID}"
    assert seen["body"] == {"active": active}
    assert row.active is active


@pytest.mark.parametrize(
    ("destinations", "topic", "wire"),
    [
        (["resend_email"], None, {"destinations": ["resend_email"]}),
        (["groups_service"], "Dinner", {"destinations": ["groups_service"], "topic": "Dinner"}),
        ([], None, {"destinations": []}),
    ],
)
def test_room_destinations_send_the_delivery_object(destinations, topic, wire) -> None:
    seen, handler = _capture(httpx.Response(200, json=_room_task_json()))
    with _client(handler) as api:
        api.set_scheduled_task_destinations(
            _TASK_ID, destinations=destinations, topic=topic, group_id=_ROOM
        )

    assert seen["method"] == "PATCH"
    assert seen["url"] == f"{_ROOM_TASKS}/{_TASK_ID}"
    assert seen["body"] == {"delivery": wire}


def test_room_delivery_is_never_sent_implicitly() -> None:
    """``None`` means "inherit my preference" on a personal task; a room has
    none, so the client refuses rather than choose a wire value for it — and
    a topic without destinations is refused rather than dropped."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no request expected, got {request.method} {request.url}")

    with _client(handler) as api:
        with pytest.raises(ValueError, match="explicit"):
            api.set_scheduled_task_destinations(_TASK_ID, destinations=None, group_id=_ROOM)
        with pytest.raises(ValueError, match="topic"):
            api.create_scheduled_task(
                name="t", prompt="p", cron="0 9 * * 0", topic="Dinner", group_id=_ROOM
            )


def test_room_refusal_surfaces_as_an_api_error_with_the_status() -> None:
    body = {"detail": "Only an owner of this room can change its scheduled tasks."}
    _, handler = _capture(httpx.Response(403, json=body))
    with _client(handler) as api, pytest.raises(api_client.ApiError) as raised:
        api.cancel_scheduled_task(_TASK_ID, group_id=_ROOM)
    assert raised.value.status == 403


def test_personal_paths_are_unchanged_without_a_group() -> None:
    """``group_id=None`` is the personal route, byte for byte — the room
    switch must not move a personal write."""
    seen, handler = _capture(httpx.Response(200, json=_task_json()))
    with _client(handler) as api:
        api.cancel_scheduled_task(_TASK_ID)
        assert seen["url"] == f"http://example.com/api/v1/scheduled-tasks/{_TASK_ID}/cancel"
        api.set_scheduled_task_destinations(_TASK_ID, destinations=None)
        assert seen["method"] == "PUT"
        assert seen["body"] == {"destinations": None}
