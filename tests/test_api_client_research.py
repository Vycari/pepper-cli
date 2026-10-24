"""ApiClient research-project reads: the wire shape out, and the dataclasses back."""

from __future__ import annotations

import contextlib
import json

import httpx
import pytest

from pepper_cli import api_client
from pepper_cli.secret import SecretStr

PID = "00000000-0000-0000-0000-0000000000d1"
IID = "00000000-0000-0000-0000-0000000000e1"


def _client(handler) -> api_client.ApiClient:
    return api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _serve(body: object, seen: list[httpx.Request] | None = None, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=body)

    return handler


def test_list_projects_parses_nested_models_and_ignores_unknown_fields() -> None:
    seen: list[httpx.Request] = []
    page = _client(
        _serve(
            {
                "items": [
                    {
                        "id": PID,
                        "name": "robot-vacuums",
                        "status": "active",
                        "delivery_policy": "drip",
                        "brief_snippet": "b",
                        "skill": "web-research",
                        "scheduled_task_id": "t1",
                        "schedule": {"cron": "0 9 * * 0", "timezone": "UTC", "active": True},
                        "last_iteration": {
                            "id": "i1",
                            "status": "succeeded",
                            "started_at": "2026-10-19T16:00:00+00:00",
                            "finished_at": None,
                        },
                        "inflight_run": {
                            "id": "r1",
                            "status": "queued",
                            "requested_at": "2026-10-20T10:00:00+00:00",
                            "requested_by": {"user_id": "u1", "name": "Chris"},
                        },
                        "created_at": "2026-10-01T12:00:00+00:00",
                        "added_in_2027": True,
                    }
                ],
                "next_before": None,
            },
            seen,
        )
    ).list_research_projects(status="paused", limit=20)
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/v1/research-projects"
    assert dict(seen[0].url.params) == {"status": "paused", "limit": "20"}
    (project,) = page.items
    assert project.schedule == api_client.ResearchSchedule("0 9 * * 0", "UTC", True, None)
    assert project.last_iteration is not None and project.last_iteration.status == "succeeded"
    assert project.inflight_run is not None
    assert project.inflight_run.requested_by == api_client.ResearchPerson("u1", "Chris")
    assert page.next_before is None


def test_list_projects_without_a_status_sends_none() -> None:
    seen: list[httpx.Request] = []
    _client(_serve({"items": [], "next_before": None}, seen)).list_research_projects()
    assert "status" not in seen[0].url.params


def test_get_project_detail() -> None:
    detail = _client(
        _serve(
            {
                "id": PID,
                "name": "n",
                "status": "active",
                "delivery_policy": "drip",
                "brief_snippet": "",
                "skill": "s",
                "scheduled_task_id": None,
                "schedule": None,
                "last_iteration": None,
                "inflight_run": None,
                "created_at": "2026-10-01T12:00:00+00:00",
                "brief": "the brief",
                "source_url": None,
                "filters": [
                    {"text": "f", "author_user_id": None, "author_name": None, "set_at": None}
                ],
                "pending_steering_count": 2,
                "item_counts": {"accepted": 3},
                "report_artifact_id": None,
                "open_questions": [{"id": "q1", "text": "why?"}],
                "owner": {"kind": "user", "id": "u1"},
            }
        )
    ).get_research_project(PID)
    assert detail.brief == "the brief"
    assert detail.filters == [api_client.ResearchFilter("f", None, None, None)]
    assert detail.open_questions == [api_client.ResearchQuestion("q1", "why?")]
    assert detail.owner == api_client.ResearchOwner("user", "u1")
    assert detail.schedule is None and detail.inflight_run is None


def test_get_report_and_iterations_and_item_paths() -> None:
    seen: list[httpx.Request] = []
    client = _client(
        _serve(
            {
                "artifact_id": None,
                "body": "b",
                "summary": "",
                "current_recommendation": "",
                "open_questions": [],
                "changelog": [
                    {"day": "2026-10-20", "text": "t", "evidence_ids": [], "iteration_id": None}
                ],
                "collapsed_changelog": 0,
                "updated_at": None,
                "items": [],
                "next_before": "C",
            },
            seen,
        )
    )
    report = client.get_research_report(PID)
    assert report.changelog[0].text == "t"
    iterations = client.list_research_iterations(PID, limit=5, before="X")
    assert iterations.next_before == "C"
    client.list_research_items(PID, states=["candidate", "taken"], limit=7)
    paths = [r.url.path for r in seen]
    assert paths == [
        f"/api/v1/research-projects/{PID}/report",
        f"/api/v1/research-projects/{PID}/iterations",
        f"/api/v1/research-projects/{PID}/items",
    ]
    assert dict(seen[1].url.params) == {"limit": "5", "before": "X"}
    assert seen[2].url.params.get_list("state") == ["candidate", "taken"]


def test_get_item_detail_defaults_missing_lists() -> None:
    item = _client(
        _serve(
            {
                "id": IID,
                "seq": 9,
                "title": "t",
                "state": "taken",
                "state_reason": None,
                "url": None,
                "summary": None,
                "first_seen_at": "a",
                "state_changed_at": "b",
                "source_type": "web",
                "creator": None,
                "year": None,
                "published_at": None,
                "citations": None,
                "superseded_by": None,
            }
        )
    ).get_research_item(PID, IID)
    assert item.citations == []
    assert item.state == "taken"


@pytest.mark.parametrize("body", [[], {"items": "nope"}])
def test_a_malformed_page_is_an_api_error(body) -> None:
    with pytest.raises(api_client.ApiError, match="malformed response"):
        _client(_serve(body)).list_research_items(PID)


def test_http_errors_raise_with_the_code_readable() -> None:
    client = _client(
        _serve(
            {"detail": "research projects are not enabled", "code": "research_projects_disabled"},
            status=403,
        )
    )
    with pytest.raises(api_client.ApiError) as exc:
        client.list_research_projects()
    assert exc.value.status == 403
    assert api_client.error_code(exc.value) == "research_projects_disabled"


@pytest.mark.parametrize("body", ["not json", "[1]", '{"code": 3}', "{}"])
def test_error_code_is_none_unless_the_body_carries_a_string_code(body) -> None:
    assert api_client.error_code(api_client.ApiError(403, body)) is None


def test_a_missing_optional_field_reads_none_and_falsy_required_values_pass() -> None:
    item = _client(
        _serve(
            {
                "items": [
                    {
                        "id": IID,
                        "seq": 0,
                        "title": "",
                        "state": "candidate",
                        "first_seen_at": "a",
                        "state_changed_at": "b",
                    }
                ]
            }
        )
    ).list_research_items(PID)
    row = item.items[0]
    assert (row.seq, row.title, row.url, row.summary, row.state_reason) == (0, "", None, None, None)


@pytest.mark.parametrize("value", ["missing", None])
def test_a_missing_or_null_required_field_is_a_malformed_response(value) -> None:
    row = {
        "id": IID,
        "seq": 1,
        "title": "t",
        "first_seen_at": "a",
        "state_changed_at": "b",
    }
    if value is None:
        row["state"] = None
    with pytest.raises(api_client.ApiError) as exc:
        _client(_serve({"items": [row]})).list_research_items(PID)
    assert exc.value.status == 200
    assert exc.value.detail == "malformed response: ResearchItem has no 'state'"


def test_a_required_nested_field_is_checked_too() -> None:
    schedule = {"cron": "0 9 * * 0", "timezone": None}  # no "active"
    with pytest.raises(api_client.ApiError, match="ResearchSchedule has no 'active'"):
        _client(
            _serve(
                {
                    "items": [
                        {
                            "id": PID,
                            "name": "n",
                            "status": "active",
                            "delivery_policy": "drip",
                            "brief_snippet": "b",
                            "skill": "s",
                            "schedule": schedule,
                            "created_at": "c",
                        }
                    ]
                }
            )
        ).list_research_projects()


TASK = "00000000-0000-0000-0000-0000000000c1"


def _project(**over) -> dict:
    row = {
        "id": PID,
        "name": "n",
        "status": "active",
        "delivery_policy": "drip",
        "brief_snippet": "b",
        "skill": "s",
        "scheduled_task_id": None,
        "schedule": None,
        "last_iteration": None,
        "inflight_run": None,
        "created_at": "c",
        "brief": "b",
        "source_url": None,
        "filters": [],
        "pending_steering_count": 0,
        "item_counts": {},
        "report_artifact_id": None,
        "open_questions": [],
        "owner": None,
    }
    row.update(over)
    return row


def test_enable_schedule_posts_the_cron_and_leaves_the_zone_to_the_server() -> None:
    seen: list[httpx.Request] = []
    detail = _client(_serve(_project(), seen, status=201)).enable_research_schedule(
        PID, cron="0 9 * * 0"
    )
    (request,) = seen
    assert (request.method, request.url.path) == (
        "POST",
        f"/api/v1/research-projects/{PID}/schedule",
    )
    assert json.loads(request.content) == {"cron": "0 9 * * 0"}
    assert detail.id == PID


def test_enable_schedule_sends_the_zone_when_given() -> None:
    seen: list[httpx.Request] = []
    _client(_serve(_project(), seen, status=201)).enable_research_schedule(
        PID, cron="0 9 * * 0", timezone="Europe/Paris"
    )
    assert json.loads(seen[0].content) == {"cron": "0 9 * * 0", "timezone": "Europe/Paris"}


def test_enable_schedule_409_with_a_task_id_is_the_already_scheduled_error() -> None:
    body = {"detail": "already has one", "scheduled_task_id": TASK.upper()}
    with pytest.raises(api_client.ResearchAlreadyScheduledError) as exc:
        _client(_serve(body, status=409)).enable_research_schedule(PID, cron="x")
    assert exc.value.scheduled_task_id == TASK  # canonical lowercase
    assert exc.value.status == 409
    assert exc.value.detail == "already has one"


@pytest.mark.parametrize(
    "body",
    [{"detail": "paused"}, {"detail": "x", "scheduled_task_id": "not-a-uuid"}],
    ids=["no-id", "bad-id"],
)
def test_enable_schedule_409_without_a_usable_task_id_stays_a_plain_error(body) -> None:
    with pytest.raises(api_client.ApiError) as exc:
        _client(_serve(body, status=409)).enable_research_schedule(PID, cron="x")
    assert not isinstance(exc.value, api_client.ResearchAlreadyScheduledError)


def test_detach_schedule_deletes_and_parses_the_project() -> None:
    seen: list[httpx.Request] = []
    detail = _client(_serve(_project(), seen)).detach_research_schedule(PID)
    assert (seen[0].method, seen[0].url.path) == (
        "DELETE",
        f"/api/v1/research-projects/{PID}/schedule",
    )
    assert seen[0].content == b""
    assert detail.scheduled_task_id is None


def test_change_schedule_patches_the_task_with_only_the_keys_given() -> None:
    seen: list[httpx.Request] = []
    client = _client(_serve({"id": TASK}, seen))
    client.change_research_schedule(TASK, cron="0 7 * * 1")
    client.change_research_schedule(TASK, cron="0 7 * * 1", timezone="UTC")
    assert [(r.method, r.url.path) for r in seen] == [
        ("PATCH", f"/api/v1/scheduled-tasks/{TASK}")
    ] * 2
    assert [json.loads(r.content) for r in seen] == [
        {"cron": "0 7 * * 1"},
        {"cron": "0 7 * * 1", "timezone": "UTC"},
    ]


# ---------------------------------------------------------------------------
# the room twins

GID = "00000000-0000-0000-0000-0000000000a1"
ROOM = f"/api/v1/groups/{GID}/research-projects"
PERSONAL = "/api/v1/research-projects"


def _room_calls(api: api_client.ApiClient, group_id: str | None):
    return [
        ("GET", "", lambda: api.list_research_projects(group_id=group_id)),
        ("GET", f"/{PID}", lambda: api.get_research_project(PID, group_id=group_id)),
        ("GET", f"/{PID}/report", lambda: api.get_research_report(PID, group_id=group_id)),
        ("GET", f"/{PID}/iterations", lambda: api.list_research_iterations(PID, group_id=group_id)),
        ("GET", f"/{PID}/items", lambda: api.list_research_items(PID, group_id=group_id)),
        ("GET", f"/{PID}/items/{IID}", lambda: api.get_research_item(PID, IID, group_id=group_id)),
        ("GET", f"/{PID}/runs/{IID}", lambda: api.get_research_run(PID, IID, group_id=group_id)),
        (
            "POST",
            "",
            lambda: api.create_research_project(
                name="n", brief="b", delivery_policy="drip", group_id=group_id
            ),
        ),
        ("PATCH", f"/{PID}", lambda: api.edit_research_project(PID, brief="b", group_id=group_id)),
        ("PUT", f"/{PID}/filters", lambda: api.set_research_filters(PID, ["f"], group_id=group_id)),
        ("POST", f"/{PID}/runs", lambda: api.run_research_project(PID, group_id=group_id)),
        (
            "POST",
            f"/{PID}/schedule",
            lambda: api.enable_research_schedule(PID, cron="0 9 * * 0", group_id=group_id),
        ),
        (
            "DELETE",
            f"/{PID}/schedule",
            lambda: api.detach_research_schedule(PID, group_id=group_id),
        ),
        (
            "POST",
            f"/{PID}/steering",
            lambda: api.steer_research_project(PID, text="t", group_id=group_id),
        ),
        ("POST", f"/{PID}/pause", lambda: api.pause_research_project(PID, group_id=group_id)),
        ("POST", f"/{PID}/resume", lambda: api.resume_research_project(PID, group_id=group_id)),
        (
            "POST",
            f"/{PID}/items/{IID}/take",
            lambda: api.take_research_item(PID, IID, group_id=group_id),
        ),
        (
            "POST",
            f"/{PID}/items/{IID}/reconsider",
            lambda: api.reconsider_research_item(PID, IID, reason="r", group_id=group_id),
        ),
    ]


@pytest.mark.parametrize("group_id", [None, GID])
def test_every_research_call_goes_to_the_personal_or_the_room_route(group_id) -> None:
    base = PERSONAL if group_id is None else ROOM
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    api = _client(handler)
    for method, suffix, call in _room_calls(api, group_id):
        seen.clear()
        with contextlib.suppress(
            Exception
        ):  # an empty body does not parse; only the request counts
            call()
        assert [(r.method, r.url.path) for r in seen] == [(method, base + suffix)]


def test_a_room_schedule_carries_the_topic_and_a_change_goes_to_the_room_task() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    api = _client(handler)
    with contextlib.suppress(Exception):  # the empty answer does not parse; the request is sent
        api.enable_research_schedule(PID, cron="0 9 * * 0", topic_id="T1", group_id=GID)
    assert json.loads(seen[-1].content) == {"cron": "0 9 * * 0", "topic_id": "T1"}
    api.change_research_schedule("TASK", cron="0 7 * * 1", topic_id="T1", group_id=GID)
    assert (seen[-1].method, seen[-1].url.path) == (
        "PATCH",
        f"/api/v1/groups/{GID}/scheduled-tasks/TASK",
    )
    assert json.loads(seen[-1].content) == {
        "cron": "0 7 * * 1",
        "delivery": {"destinations": ["groups_service"], "topic": "T1"},
    }
    api.change_research_schedule("TASK", cron="0 7 * * 1")
    assert seen[-1].url.path == "/api/v1/scheduled-tasks/TASK"
    assert json.loads(seen[-1].content) == {"cron": "0 7 * * 1"}
