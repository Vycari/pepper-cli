"""ApiClient research-project reads: the wire shape out, and the dataclasses back."""

from __future__ import annotations

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
    assert project.schedule == api_client.ResearchSchedule("0 9 * * 0", "UTC", True)
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
