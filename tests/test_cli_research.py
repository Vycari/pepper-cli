"""``research-projects`` and ``research-items`` read commands against a faked server.

HTTP is mocked at the transport, so these cover the whole path: argument parsing, name and
number resolution, the request that goes out, the human and ``--json`` output, and the exit
code and J8 line for each failure in ``planning/research-projects.md`` §8.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import threading
import time

import httpx
import pytest

from pepper_cli import api_client, cli, credentials, research_view
from pepper_cli.secret import SecretStr

P1 = "00000000-0000-0000-0000-0000000000d1"
P2 = "00000000-0000-0000-0000-0000000000d2"
ME = "00000000-0000-0000-0000-0000000000aa"
CHRIS = "00000000-0000-0000-0000-0000000000bb"
BASE = "/api/v1/research-projects"


def _summary(pid: str, name: str, **over) -> dict:
    row = {
        "id": pid,
        "name": name,
        "status": "active",
        "delivery_policy": "digest",
        "brief_snippet": "snippet",
        "skill": "web-research",
        "scheduled_task_id": None,
        "schedule": None,
        "last_iteration": None,
        "inflight_run": None,
        "created_at": "2026-10-01T12:00:00+00:00",
    }
    row.update(over)
    return row


def _detail(pid: str = P1, name: str = "robot-vacuums", **over) -> dict:
    row = _summary(pid, name)
    row.update(
        brief="Robot vacuums under $500\nthat handle pet hair.",
        source_url=None,
        filters=[{"text": "nothing over $500", "author_user_id": None, "author_name": None}],
        pending_steering_count=1,
        item_counts={"candidate": 2, "accepted": 12, "weird": 1},
        report_artifact_id=None,
        open_questions=[{"id": "q1", "text": "Self-emptying base a must?"}],
        owner={"kind": "user", "id": ME},
        brand_new_server_field="ignored",
    )
    row.update(over)
    return row


def _item(n: int, **over) -> dict:
    row = {
        "id": f"{n:08x}-0000-0000-0000-00000000e000",
        "seq": 48000 + n,
        "title": f"Item {n}",
        "state": "candidate",
        "state_reason": None,
        "url": f"https://shop{n}.example.com/x",
        "summary": None,
        "first_seen_at": "2026-10-19T16:00:00+00:00",
        "state_changed_at": "2026-10-19T16:00:00+00:00",
    }
    row.update(over)
    return row


def _iteration(**over) -> dict:
    row = {
        "id": "00000000-0000-0000-0000-00000000f001",
        "occurrence_key": None,
        "trigger": "scheduled",
        "requested_by": None,
        "run_id": None,
        "status": "succeeded",
        "started_at": "2026-10-19T16:00:00+00:00",
        "finished_at": "2026-10-19T16:03:00+00:00",
        "error": None,
        "source_failures": None,
    }
    row.update(over)
    return row


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("PEPPER_JSON", raising=False)
    monkeypatch.chdir(tmp_path)
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    requests: list[httpx.Request] = []
    routes: dict[str, httpx.Response] = {
        BASE: httpx.Response(
            200,
            json={
                "items": [_summary(P1, "robot-vacuums"), _summary(P2, "robot-lawnmowers")],
                "next_before": None,
            },
        ),
        "/api/v1/whoami": httpx.Response(
            200, json={"user_id": ME, "display_name": "Me", "timezone": "UTC"}
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        # A write is keyed "POST /path" (a read by its bare path). A list is a script: each
        # request takes the next response, the last one repeating; a callable sees the request.
        for key in (f"{request.method} {request.url.path}", request.url.path):
            if key in routes:
                answer = routes[key]
                if isinstance(answer, list):
                    answer = answer.pop(0) if len(answer) > 1 else answer[0]
                return answer(request) if callable(answer) else answer
        return httpx.Response(404, json={"detail": "nope"})

    real = api_client.ApiClient

    def factory(**kwargs):
        return real(client=httpx.Client(transport=httpx.MockTransport(handler)), **kwargs)

    monkeypatch.setattr(api_client, "ApiClient", factory)
    return type("Server", (), {"requests": requests, "routes": routes})


def _run(argv: list[str], capsys) -> tuple[int, str, str]:
    rc = cli.main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _json(response_json: object) -> httpx.Response:
    return httpx.Response(200, json=response_json)


def _error(err: str) -> dict:
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1, err
    return json.loads(lines[0])["error"]


# ---------------------------------------------------------------------------
# research-projects list


def test_list_sends_the_default_status_and_prints_a_table(server, capsys) -> None:
    server.routes[BASE] = _json(
        {
            "items": [
                _summary(
                    P1,
                    "robot-vacuums",
                    schedule={
                        "cron": "0 9 * * 0",
                        "timezone": "America/Los_Angeles",
                        "active": True,
                    },
                    last_iteration={
                        "id": "i1",
                        "status": "succeeded",
                        "started_at": "2026-10-19T16:00:00+00:00",
                        "finished_at": None,
                    },
                    inflight_run={
                        "id": "r1",
                        "status": "queued",
                        "requested_at": "2026-10-20T10:00:00+00:00",
                        "requested_by": {"user_id": ME, "name": "Me"},
                    },
                ),
                _summary(P2, "horn-walk", status="paused", delivery_policy="drip"),
            ],
            "next_before": None,
        }
    )
    rc, out, _ = _run(["research-projects", "list"], capsys)
    assert rc == 0
    assert server.requests[-1].url.params["status"] == "active"
    header, first, second = out.splitlines()
    assert header.split() == ["NAME", "STATUS", "POLICY", "SCHEDULE", "LAST", "RUN"]
    assert "robot-vacuums" in first and "0 9 * * 0 (America/Los_Angeles)" in first
    assert "succeeded · queued run" in first
    assert "horn-walk" in second and "manual" in second and "never run" in second


def test_list_all_sends_no_status(server, capsys) -> None:
    rc, _, _ = _run(["research-projects", "list", "--status", "all"], capsys)
    assert rc == 0
    assert "status" not in server.requests[-1].url.params


def test_list_json_is_the_envelope_with_server_fields(server, capsys) -> None:
    rc, out, _ = _run(["research-projects", "list", "--json"], capsys)
    body = json.loads(out)
    assert rc == 0
    assert body["next_before"] is None
    assert [p["name"] for p in body["items"]] == ["robot-vacuums", "robot-lawnmowers"]
    assert body["items"][0]["brief_snippet"] == "snippet"


def test_list_empty(server, capsys) -> None:
    server.routes[BASE] = _json({"items": [], "next_before": None})
    rc, out, _ = _run(["research-projects", "list"], capsys)
    assert (rc, out.strip()) == (0, "(no research projects)")


def test_list_a_bad_status_is_a_usage_error(server, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["research-projects", "list", "--status", "stopped"])
    assert exc.value.code == 2
    assert not server.requests


# ---------------------------------------------------------------------------
# project references


@pytest.mark.parametrize("ref", ["robot-vacuums", "ROBOT-V", P1])
def test_a_project_resolves_by_name_prefix_or_uuid(server, capsys, ref) -> None:
    server.routes[f"{BASE}/{P1}"] = _json(_detail())
    rc, out, _ = _run(["research-projects", "show", ref], capsys)
    assert rc == 0
    assert server.requests[-1].url.path == f"{BASE}/{P1}"
    if ref == P1:  # a UUID goes straight through, with no lookup
        assert [r.url.path for r in server.requests] == [f"{BASE}/{P1}"]
    else:
        assert server.requests[0].url.path == BASE
        assert "status" not in server.requests[0].url.params  # every status is searched
    assert out.startswith("robot-vacuums")


def test_an_ambiguous_prefix_exits_2_and_lists_the_candidates(server, capsys) -> None:
    rc, out, err = _run(["research-projects", "show", "robot", "--json"], capsys)
    assert (rc, out) == (2, "")
    error = _error(err)
    assert error["code"] == "ambiguous"
    assert "robot-vacuums" in error["message"] and "robot-lawnmowers" in error["message"]


def test_an_unknown_name_exits_2_not_found(server, capsys) -> None:
    rc, _, err = _run(["research-projects", "show", "nothing-like-it"], capsys)
    assert rc == 2
    assert "no research project named 'nothing-like-it'" in err


def test_a_project_that_is_not_yours_is_the_servers_404(server, capsys) -> None:
    rc, out, err = _run(["research-projects", "show", P2, "--json"], capsys)
    assert (rc, out) == (1, "")
    assert _error(err)["code"] == "not_found"


# ---------------------------------------------------------------------------
# show


def test_show_prints_the_project(server, capsys) -> None:
    server.routes[f"{BASE}/{P1}"] = _json(
        _detail(
            inflight_run={
                "id": "7f3a-run",
                "status": "running",
                "requested_at": "2026-10-20T10:00:00+00:00",
                "requested_by": {"user_id": ME, "name": "Me"},
            },
            last_iteration={
                "id": "i1",
                "status": "error",
                "started_at": "2026-10-19T16:00:00+00:00",
                "finished_at": None,
            },
            schedule={"cron": "0 9 * * 0", "timezone": "UTC", "active": False},
        )
    )
    rc, out, _ = _run(["research-projects", "show", P1], capsys)
    assert rc == 0
    lines = out.splitlines()
    assert lines[0] == "robot-vacuums   active · digest · skill web-research"
    assert "Brief       Robot vacuums under $500" in lines
    assert "            that handle pet hair." in lines  # a multi-line brief stays aligned
    assert "Filters     1. nothing over $500" in lines
    assert "Schedule    0 9 * * 0 (UTC), off" in lines
    assert "Source      (none)" in lines
    assert any(ln.startswith("Last run    ") and ln.endswith("error") for ln in lines)
    assert "Run         running 7f3a-run (pepper research-projects show robot-vacuums)" in lines
    # The server's ledger order, then states the CLI does not know.
    assert "Ledger      2 candidate · 12 accepted · 1 weird" in lines
    assert "Steering    1 note queued for the next run" in lines
    assert "Open        q1  Self-emptying base a must?" in lines
    assert lines[-1] == "Report      pepper research-projects show robot-vacuums --report"


def test_show_a_quiet_project_leaves_out_the_optional_lines(server, capsys) -> None:
    server.routes[f"{BASE}/{P1}"] = _json(
        _detail(filters=[], pending_steering_count=0, item_counts={}, open_questions=[], brief="b")
    )
    _, out, _ = _run(["research-projects", "show", P1], capsys)
    assert "Filters" not in out and "Steering" not in out and "Open" not in out
    assert "Ledger      (empty)" in out
    assert "Last run    (never run)" in out


def test_show_json_is_the_detail(server, capsys) -> None:
    server.routes[f"{BASE}/{P1}"] = _json(_detail())
    rc, out, _ = _run(["research-projects", "show", P1, "--json"], capsys)
    body = json.loads(out)
    assert rc == 0
    assert body["item_counts"] == {"candidate": 2, "accepted": 12, "weird": 1}
    assert body["open_questions"] == [{"id": "q1", "text": "Self-emptying base a must?"}]


def test_show_neutralises_terminal_controls_in_server_text(server, capsys) -> None:
    server.routes[f"{BASE}/{P1}"] = _json(_detail(brief="clean\x1b[31mred\x07 [bold]x[/bold]"))
    _, out, _ = _run(["research-projects", "show", P1], capsys)
    assert "\x1b" not in out and "\x07" not in out
    assert "[bold]x[/bold]" in out  # brackets are text, not markup


REPORT = {
    "artifact_id": None,
    "body": "# robot-vacuums\n\n## Summary\nAll good.\n",
    "summary": "All good.",
    "current_recommendation": "",
    "open_questions": [],
    "changelog": [
        {"day": "2026-10-20", "text": "Two new", "evidence_ids": [], "iteration_id": None}
    ],
    "collapsed_changelog": 0,
    "updated_at": "2026-10-20T10:00:00+00:00",
}


def test_show_report_prints_the_body_byte_exact(server, capsys) -> None:
    server.routes[f"{BASE}/{P1}/report"] = _json(REPORT)
    rc, out, _ = _run(["research-projects", "show", P1, "--report"], capsys)
    assert (rc, out) == (0, REPORT["body"])


def test_show_report_json_is_the_report_object(server, capsys) -> None:
    server.routes[f"{BASE}/{P1}/report"] = _json(REPORT)
    rc, out, _ = _run(["research-projects", "show", P1, "--report", "--json"], capsys)
    body = json.loads(out)
    assert rc == 0
    assert body["body"] == REPORT["body"]
    assert body["changelog"][0]["text"] == "Two new"


# ---------------------------------------------------------------------------
# runs


def test_runs_shows_a_trigger_column_and_who_pressed_run(server, capsys) -> None:
    server.routes[f"{BASE}/{P1}/iterations"] = _json(
        {
            "items": [
                _iteration(),
                _iteration(
                    trigger="manual",
                    requested_by={"user_id": CHRIS, "name": "Chris"},
                    status="error",
                    error="page fetch failed (source_url 503)",
                ),
                _iteration(trigger="manual", requested_by={"user_id": ME, "name": "Me"}),
                _iteration(source_failures={"arxiv": {}, "hn": {}}),
            ],
            "next_before": "CUR",
        }
    )
    rc, out, _ = _run(["research-projects", "runs", P1], capsys)
    assert rc == 0
    lines = out.splitlines()
    assert lines[0].split() == ["STARTED", "TRIGGER", "STATUS", "NOTE"]
    assert "scheduled" in lines[1] and "succeeded" in lines[1]
    assert "manual · Chris" in lines[2] and "page fetch failed (source_url 503)" in lines[2]
    assert "manual · you" in lines[3]
    assert "sources failed: arxiv, hn" in lines[4]
    assert lines[-1] == "(more — pass --before CUR)"
    sent = next(r for r in server.requests if r.url.path.endswith("/iterations")).url.params
    assert sent["limit"] == "50" and "before" not in sent


def test_runs_pages_back_with_before(server, capsys) -> None:
    server.routes[f"{BASE}/{P1}/iterations"] = _json({"items": [_iteration()], "next_before": None})
    rc, _, _ = _run(["research-projects", "runs", P1, "--before", "CUR", "--limit", "5"], capsys)
    assert rc == 0
    sent = server.requests[-1].url.params
    assert (sent["before"], sent["limit"]) == ("CUR", "5")


def test_runs_json_is_the_envelope_and_never_asks_who_you_are(server, capsys) -> None:
    server.routes[f"{BASE}/{P1}/iterations"] = _json(
        {
            "items": [_iteration(trigger="manual", requested_by={"user_id": ME, "name": "Me"})],
            "next_before": "CUR",
        }
    )
    rc, out, _ = _run(["research-projects", "runs", P1, "--json"], capsys)
    body = json.loads(out)
    assert rc == 0
    assert body["next_before"] == "CUR"
    assert body["items"][0]["trigger"] == "manual"
    assert body["items"][0]["requested_by"] == {"user_id": ME, "name": "Me"}
    assert all(r.url.path != "/api/v1/whoami" for r in server.requests)


def test_runs_empty(server, capsys) -> None:
    server.routes[f"{BASE}/{P1}/iterations"] = _json({"items": [], "next_before": None})
    _, out, _ = _run(["research-projects", "runs", P1], capsys)
    assert out.strip() == "(no runs yet)"


@pytest.mark.parametrize("limit", ["0", "201"])
def test_runs_limit_out_of_range_is_a_usage_error(server, capsys, limit) -> None:
    rc, out, err = _run(["research-projects", "runs", P1, "--limit", limit, "--json"], capsys)
    assert (rc, out) == (2, "")
    assert _error(err)["code"] == "usage"
    assert not server.requests


# ---------------------------------------------------------------------------
# research-items list


def _items_route(server, items: list[dict], next_before: str | None = None) -> None:
    server.routes[f"{BASE}/{P1}/items"] = _json({"items": items, "next_before": next_before})


def test_items_list_numbers_the_rows_and_filters_by_state(server, capsys) -> None:
    _items_route(
        server,
        [_item(1), _item(2, state="accepted", title="Coway Airmega 150")],
        next_before="SEQ",
    )
    rc, out, _ = _run(
        [
            "research-items", "list", "--project", "robot-vacuums",
            "--state", "candidate", "--state", "accepted", "--limit", "2",
        ],
        capsys,
    )  # fmt: skip
    assert rc == 0
    sent = server.requests[-1].url.params
    assert sent.get_list("state") == ["candidate", "accepted"]
    assert sent["limit"] == "2"
    lines = out.splitlines()
    assert lines[0] == "robot-vacuums — 2 items (states: candidate, accepted)"
    assert lines[1].split()[:3] == ["#", "STATE", "TITLE"]
    assert lines[2].split()[:3] == ["1", "candidate", "Item"]
    assert "shop1.example.com" in lines[2] and "2026-10-19" in lines[2]
    assert lines[3].split()[:2] == ["2", "accepted"]
    assert "48001" not in out  # the global seq is never shown
    assert lines[-1] == "(more — pass --before SEQ)"


def test_items_list_json_numbers_and_keeps_the_envelope(server, capsys) -> None:
    _items_route(server, [_item(1), _item(2)], next_before="SEQ")
    rc, out, _ = _run(["research-items", "list", "--project", P1, "--json"], capsys)
    body = json.loads(out)
    assert rc == 0
    assert body["next_before"] == "SEQ"
    assert [(i["n"], i["title"]) for i in body["items"]] == [(1, "Item 1"), (2, "Item 2")]
    assert body["items"][0]["seq"] == 48001


def test_items_list_before_is_sent_as_given(server, capsys) -> None:
    _items_route(server, [_item(3)])
    rc, _, _ = _run(["research-items", "list", "--project", P1, "--before", "SEQ"], capsys)
    assert rc == 0
    assert server.requests[-1].url.params["before"] == "SEQ"


def test_items_list_requires_a_project(server) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["research-items", "list"])
    assert exc.value.code == 2


def test_items_list_rejects_an_unknown_state_before_any_request(server) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["research-items", "list", "--project", P1, "--state", "maybe"])
    assert exc.value.code == 2
    assert not server.requests


def test_items_list_empty_still_names_the_project(server, capsys) -> None:
    _items_route(server, [])
    _, out, _ = _run(["research-items", "list", "--project", P1], capsys)
    assert out.strip() == f"{P1} — 0 items (states: all)"


def test_items_list_saves_the_project_scope(server, capsys) -> None:
    _items_route(server, [_item(1)])
    _run(["research-items", "list", "--project", "robot-vacuums"], capsys)
    rows, meta = research_view.VIEW.load()  # ty: ignore[not-iterable]
    assert [r["title"] for r in rows] == ["Item 1"]
    assert meta == {"project_id": P1, "room_id": None}


# ---------------------------------------------------------------------------
# research-items show


def _show_route(server, item: dict) -> None:
    server.routes[f"{BASE}/{P1}/items/{item['id']}"] = _json(
        {
            **item,
            "source_type": "web",
            "creator": "Roborock",
            "year": 2026,
            "published_at": "2026-10-20T08:00:00+00:00",
            "citations": [
                {"title": "Review", "url": "https://rev.example.com"},
                "plain text cite",
                {"odd": 1},
            ],
            "superseded_by": None,
        }
    )


def test_items_show_by_number_after_a_list(server, capsys) -> None:
    items = [_item(1), _item(2, state="rejected", state_reason="over budget", summary="Pricey")]
    _items_route(server, items)
    _show_route(server, items[1])
    _run(["research-items", "list", "--project", "robot-vacuums"], capsys)
    rc, out, _ = _run(["research-items", "show", "--project", "robot-vacuums", "2"], capsys)
    assert rc == 0
    lines = out.splitlines()
    assert lines[0] == "Item 2"
    assert "State       rejected — over budget" in lines
    assert "Link        https://shop2.example.com/x" in lines
    assert "Summary     Pricey" in lines
    assert "Source      web · Roborock · 2026" in lines
    assert "Citations   Review — https://rev.example.com" in lines
    assert "            plain text cite" in lines
    assert '            {"odd": 1}' in lines
    assert any(ln.startswith("First seen  ") for ln in lines)
    assert any(ln.startswith("Delivered   ") for ln in lines)


def test_items_show_by_item_id_needs_no_listing(server, capsys) -> None:
    item = _item(7)
    _show_route(server, item)
    rc, out, _ = _run(["research-items", "show", "--project", P1, item["id"], "--json"], capsys)
    assert rc == 0
    assert json.loads(out)["id"] == item["id"]
    assert [r.url.path for r in server.requests] == [f"{BASE}/{P1}/items/{item['id']}"]


def test_items_show_by_id_prefix_of_a_listed_item(server, capsys) -> None:
    items = [_item(1), _item(2)]
    _items_route(server, items)
    _show_route(server, items[1])
    _run(["research-items", "list", "--project", P1], capsys)
    rc, out, _ = _run(["research-items", "show", "--project", P1, items[1]["id"][:8]], capsys)
    assert rc == 0
    assert out.startswith("Item 2")


@pytest.mark.parametrize(
    ("list_ref", "show_ref"), [("robot-vacuums", P1.upper()), (P1, P1.upper()), (P1.upper(), P1)]
)
def test_the_same_project_in_another_case_is_not_another_project(
    server, capsys, list_ref, show_ref
) -> None:
    items = [_item(1, id="0000000a-0000-0000-0000-00000000e000")]
    _items_route(server, items)
    _show_route(server, items[0])
    _run(["research-items", "list", "--project", list_ref], capsys)
    _, meta = research_view.VIEW.load()  # ty: ignore[not-iterable]
    assert meta["project_id"] == P1
    rc, out, _ = _run(["research-items", "show", "--project", show_ref, "1"], capsys)
    assert rc == 0
    assert out.startswith("Item 1")
    assert server.requests[-1].url.path == f"{BASE}/{P1}/items/{items[0]['id']}"


@pytest.mark.parametrize("ref", ["0000000A-0000-0000-0000-00000000E000", "0000000A"])
def test_an_item_id_or_prefix_matches_in_any_case(server, capsys, ref) -> None:
    items = [_item(1, id="0000000a-0000-0000-0000-00000000e000")]
    _items_route(server, items)
    _show_route(server, items[0])
    _run(["research-items", "list", "--project", P1], capsys)
    rc, out, _ = _run(["research-items", "show", "--project", P1, ref], capsys)
    assert rc == 0
    assert out.startswith("Item 1")


def test_a_number_from_another_projects_listing_is_refused(server, capsys) -> None:
    _items_route(server, [_item(1)])
    _run(["research-items", "list", "--project", P1], capsys)
    server.requests.clear()
    rc, out, err = _run(["research-items", "show", "--project", P2, "1", "--json"], capsys)
    assert (rc, out) == (2, "")
    error = _error(err)
    assert error["code"] == "usage"
    assert error["message"] == (
        "the last 'research-items list' was of another project — list this one first"
    )
    assert not server.requests  # nothing was fetched for the wrong ledger


def test_a_number_with_no_listing_says_to_list_first(server, capsys) -> None:
    rc, _, err = _run(["research-items", "show", "--project", P1, "1"], capsys)
    assert rc == 2
    assert "run 'pepper research-items list" in err


def test_a_number_past_the_listing_is_not_found(server, capsys) -> None:
    _items_route(server, [_item(1)])
    _run(["research-items", "list", "--project", P1], capsys)
    rc, _, err = _run(["research-items", "show", "--project", P1, "5"], capsys)
    assert rc == 2
    assert "no research item 5" in err


def test_something_that_is_not_a_number_or_an_id_is_not_found(server, capsys) -> None:
    rc, _, err = _run(["research-items", "show", "--project", P1, "coway"], capsys)
    assert rc == 2
    assert "no research item 'coway'" in err


def test_logout_forgets_the_listing(server, capsys) -> None:
    _items_route(server, [_item(1)])
    _run(["research-items", "list", "--project", P1], capsys)
    assert research_view.VIEW.state_path().exists()
    cli.main(["logout"])
    assert not research_view.VIEW.state_path().exists()


# ---------------------------------------------------------------------------
# errors (§8)


@pytest.mark.parametrize(
    ("argv", "path"),
    [
        (["research-projects", "list"], BASE),
        (["research-projects", "show", P1], f"{BASE}/{P1}"),
        (["research-projects", "runs", P1], f"{BASE}/{P1}/iterations"),
        (["research-items", "list", "--project", P1], f"{BASE}/{P1}/items"),
    ],
)
@pytest.mark.parametrize("as_json", [False, True])
def test_flag_off_is_forbidden_with_a_clear_message(server, capsys, argv, path, as_json) -> None:
    server.routes[path] = httpx.Response(
        403,
        json={"detail": "research projects are not enabled", "code": "research_projects_disabled"},
    )
    rc, out, err = _run([*argv, *(["--json"] if as_json else [])], capsys)
    assert (rc, out) == (1, "")
    if as_json:
        assert _error(err) == {
            "code": "forbidden",
            "status": 403,
            "message": "Research projects are not enabled on this Pepper server.",
        }
    else:
        assert "Research projects are not enabled on this Pepper server." in err
        assert "Server returned" not in err


def test_room_flag_off_has_its_own_message(server, capsys) -> None:
    server.routes[f"{BASE}/{P1}"] = httpx.Response(
        403, json={"detail": "off", "code": "research_group_projects_disabled"}
    )
    rc, _, err = _run(["research-projects", "show", P1], capsys)
    assert rc == 1
    assert "Research projects in rooms are not enabled on this Pepper server." in err


def test_a_403_without_a_flag_code_keeps_the_servers_detail(server, capsys) -> None:
    server.routes[f"{BASE}/{P1}"] = httpx.Response(403, json={"detail": "Only a room owner can."})
    rc, _, err = _run(["research-projects", "show", P1], capsys)
    assert rc == 1
    assert "Server returned 403: Only a room owner can." in err


def test_a_bad_cursor_is_the_servers_422(server, capsys) -> None:
    server.routes[f"{BASE}/{P1}/items"] = httpx.Response(422, json={"detail": "bad cursor"})
    rc, _, err = _run(
        ["research-items", "list", "--project", P1, "--before", "x", "--json"], capsys
    )
    assert rc == 1
    assert _error(err)["code"] == "validation"


def test_not_logged_in(tmp_home, capsys) -> None:
    rc, _, err = _run(["research-projects", "list"], capsys)
    assert rc == 1
    assert "pepper login" in err


def _without(row: dict, key: str) -> dict:
    return {k: v for k, v in row.items() if k != key}


@pytest.mark.parametrize(
    ("argv", "path", "body", "missing"),
    [
        (
            ["research-projects", "list"],
            BASE,
            {"items": [_without(_summary(P1, "robot-vacuums"), "name")]},
            "ResearchProjectSummary has no 'name'",
        ),
        (
            ["research-projects", "list"],
            BASE,
            {
                "items": [
                    _summary(
                        P1,
                        "robot-vacuums",
                        last_iteration={"id": "i1", "status": "succeeded", "started_at": None},
                    )
                ]
            },
            "ResearchLastIteration has no 'started_at'",
        ),
        (
            ["research-projects", "show", P1],
            f"{BASE}/{P1}",
            _without(_detail(), "status"),
            "ResearchProjectDetail has no 'status'",
        ),
        (
            ["research-projects", "runs", P1],
            f"{BASE}/{P1}/iterations",
            {"items": [_without(_iteration(), "started_at")]},
            "ResearchIteration has no 'started_at'",
        ),
        (
            ["research-items", "list", "--project", P1],
            f"{BASE}/{P1}/items",
            {"items": [_without(_item(1), "state")]},
            "ResearchItem has no 'state'",
        ),
        (
            ["research-items", "show", "--project", P1, _item(1)["id"]],
            f"{BASE}/{P1}/items/{_item(1)['id']}",
            {**_without(_item(1), "state"), "source_type": "web", "citations": []},
            "ResearchItemDetail has no 'state'",
        ),
    ],
)
@pytest.mark.parametrize("as_json", [False, True])
def test_a_missing_required_field_is_a_clear_error_not_a_crash(
    server, capsys, argv, path, body, missing, as_json
) -> None:
    server.routes[path] = _json(body)
    rc, out, err = _run([*argv, *(["--json"] if as_json else [])], capsys)
    assert (rc, out) == (1, "")
    assert "Traceback" not in err
    message = f"Server returned 200: malformed response: {missing}"
    if as_json:
        assert _error(err)["message"] == message
    else:
        assert message in " ".join(err.split())  # the terminal may wrap the line


# ---------------------------------------------------------------------------
# the write and run commands (personal)

RUN = "00000000-0000-0000-0000-0000000000c1"
OWN = {"user_id": ME, "name": "Me"}


def _post(path: str, status: int = 200, **body) -> httpx.Response:
    return httpx.Response(status, json=body)


def _receipt(status: str = "queued") -> dict:
    return {
        "run_id": RUN,
        "project_id": P1,
        "status": status,
        "requested_at": "2026-10-20T10:00:00+00:00",
    }


def _run_detail(status: str = "running", **over) -> dict:
    row = {
        "id": RUN,
        "status": status,
        "requested_by_user_id": ME,
        "requested_at": "2026-10-20T10:00:00+00:00",
        "started_at": "2026-10-20T10:00:05+00:00",
        "finished_at": None,
        "attempts": 1,
        "error": None,
        "iteration": None,
        "changelog": [],
    }
    row.update(over)
    return row


def _writes(server) -> list[httpx.Request]:
    return [r for r in server.requests if r.method != "GET"]


def _sent(request: httpx.Request) -> dict:
    return json.loads(request.content) if request.content else {}


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)


def _stdin(monkeypatch, text: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def _editor(monkeypatch, returns: str | None):
    seen: list[str] = []

    def fake(initial: str) -> str | None:
        seen.append(initial)
        return returns

    monkeypatch.setattr(cli, "_open_in_editor", fake)
    return seen


# --- create ---------------------------------------------------------------


def test_create_sends_the_brief_and_leaves_the_skill_to_the_server(
    server, capsys, tmp_path
) -> None:
    brief = tmp_path / "brief.md"
    brief.write_text("Robot vacuums under $500.\n", encoding="utf-8")
    server.routes[f"POST {BASE}"] = httpx.Response(201, json=_detail())
    rc, out, err = _run(
        ["research-projects", "create", "robot-vacuums", "--file", str(brief)], capsys
    )
    assert rc == 0
    assert _sent(_writes(server)[0]) == {
        "name": "robot-vacuums",
        "brief": "Robot vacuums under $500.\n",
        "delivery_policy": "drip",
    }
    assert "Created research project 'robot-vacuums'" in err
    assert out.strip() == P1


def test_create_sends_skill_policy_and_source_url_when_given(server, capsys, monkeypatch) -> None:
    _stdin(monkeypatch, "A brief")
    server.routes[f"POST {BASE}"] = httpx.Response(201, json=_detail())
    rc, out, _ = _run(
        [
            "research-projects",
            "create",
            "robot-vacuums",
            "--file",
            "-",
            "--skill",
            "product-research",
            "--policy",
            "digest",
            "--source-url",
            "https://example.com/x",
            "--json",
        ],
        capsys,
    )
    assert rc == 0
    assert _sent(_writes(server)[0]) == {
        "name": "robot-vacuums",
        "brief": "A brief",
        "delivery_policy": "digest",
        "skill_name": "product-research",
        "source_url": "https://example.com/x",
    }
    assert json.loads(out)["id"] == P1  # J5: the object as the server returned it


def test_create_takes_the_brief_from_the_editor_on_a_terminal(
    server, capsys, tty, monkeypatch
) -> None:
    seen = _editor(monkeypatch, "From the editor")
    server.routes[f"POST {BASE}"] = httpx.Response(201, json=_detail())
    rc, _, _ = _run(["research-projects", "create", "robot-vacuums"], capsys)
    assert rc == 0
    assert seen == [""]
    assert _sent(_writes(server)[0])["brief"] == "From the editor"


@pytest.mark.parametrize("as_json", [False, True])
def test_create_without_a_brief_and_no_editor_is_a_usage_error(server, capsys, as_json) -> None:
    argv = ["research-projects", "create", "robot-vacuums", *(["--json"] if as_json else [])]
    rc, out, err = _run(argv, capsys)
    assert (rc, out) == (2, "")
    assert "pass --file" in err
    assert not server.requests


def test_create_an_empty_brief_is_a_usage_error(server, capsys, monkeypatch) -> None:
    _stdin(monkeypatch, "  \n")
    rc, _, err = _run(["research-projects", "create", "x", "--file", "-", "--json"], capsys)
    assert rc == 2
    assert _error(err)["code"] == "usage"
    assert not server.requests


def test_create_a_bad_policy_is_a_usage_error_before_any_request(server) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["research-projects", "create", "x", "--policy", "alert"])
    assert exc.value.code == 2
    assert not server.requests


def test_create_a_taken_name_is_the_servers_conflict(server, capsys, monkeypatch) -> None:
    _stdin(monkeypatch, "brief")
    server.routes[f"POST {BASE}"] = httpx.Response(409, json={"detail": "name already taken"})
    rc, out, err = _run(["research-projects", "create", "x", "--file", "-", "--json"], capsys)
    assert (rc, out) == (1, "")
    assert _error(err) == {
        "code": "conflict",
        "status": 409,
        "message": "Server returned 409: name already taken",
    }


def test_create_yes_is_accepted_and_changes_nothing(server, capsys, monkeypatch) -> None:
    _stdin(monkeypatch, "brief")
    server.routes[f"POST {BASE}"] = httpx.Response(201, json=_detail())
    rc, _, _ = _run(["research-projects", "create", "x", "--file", "-", "--yes"], capsys)
    assert rc == 0


# --- edit -----------------------------------------------------------------


def test_edit_file_patches_the_brief_byte_exact(server, capsys, monkeypatch) -> None:
    _stdin(monkeypatch, "New brief\n\n")
    server.routes[f"PATCH {BASE}/{P1}"] = _json(_detail(brief="New brief\n\n"))
    rc, out, _ = _run(
        ["research-projects", "edit", "robot-vacuums", "--file", "-", "--json"], capsys
    )
    assert rc == 0
    (write,) = _writes(server)
    assert write.url.path == f"{BASE}/{P1}"
    assert _sent(write) == {"brief": "New brief\n\n"}
    assert json.loads(out)["brief"] == "New brief\n\n"


def test_edit_filters_replace_the_whole_list(server, capsys) -> None:
    server.routes[f"PUT {BASE}/{P1}/filters"] = _json(_detail())
    rc, _, err = _run(
        ["research-projects", "edit", P1, "--filter", "nothing over $500", "--filter", "no subs"],
        capsys,
    )
    assert rc == 0
    (write,) = _writes(server)
    assert _sent(write) == {"filters": [{"text": "nothing over $500"}, {"text": "no subs"}]}
    assert "Updated research project 'robot-vacuums'" in err


def test_edit_clear_filters_sends_an_empty_list(server, capsys) -> None:
    server.routes[f"PUT {BASE}/{P1}/filters"] = _json(_detail(filters=[]))
    rc, _, _ = _run(["research-projects", "edit", P1, "--clear", "filters"], capsys)
    assert rc == 0
    assert _sent(_writes(server)[0]) == {"filters": []}


def test_edit_brief_and_filters_send_both_and_answer_the_last(server, capsys, monkeypatch) -> None:
    _stdin(monkeypatch, "B")
    server.routes[f"PATCH {BASE}/{P1}"] = _json(_detail(brief="B"))
    server.routes[f"PUT {BASE}/{P1}/filters"] = _json(_detail(brief="B", name="after-filters"))
    rc, out, _ = _run(
        ["research-projects", "edit", P1, "--file", "-", "--filter", "f", "--json"], capsys
    )
    assert rc == 0
    assert [w.method for w in _writes(server)] == ["PATCH", "PUT"]
    assert json.loads(out)["name"] == "after-filters"


@pytest.mark.parametrize("as_json", [True, False])
def test_edit_brief_applied_but_filters_refused_says_which_part_applied(
    server, capsys, monkeypatch, as_json
) -> None:
    _stdin(monkeypatch, "B")
    server.routes[f"PATCH {BASE}/{P1}"] = _json(_detail(brief="B"))
    server.routes[f"PUT {BASE}/{P1}/filters"] = httpx.Response(422, json={"detail": "Too many."})
    argv = ["research-projects", "edit", P1, "--file", "-", "--filter", "f"]
    rc, out, err = _run([*argv, "--json"] if as_json else argv, capsys)
    assert (rc, out) == (1, "")
    message = _error(err)["message"] if as_json else " ".join(err.split())
    assert "brief was updated" in message
    assert "filters were not changed" in message
    assert "Too many." in message


def test_edit_brief_applied_but_filters_unreachable_says_which_part_applied(
    server, capsys, monkeypatch
) -> None:
    def dropped(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("reset", request=request)

    _stdin(monkeypatch, "B")
    server.routes[f"PATCH {BASE}/{P1}"] = _json(_detail(brief="B"))
    server.routes[f"PUT {BASE}/{P1}/filters"] = dropped
    rc, out, err = _run(
        ["research-projects", "edit", P1, "--file", "-", "--filter", "f", "--json"], capsys
    )
    assert (rc, out) == (1, "")
    error = _error(err)
    assert error["code"] == "network"
    assert "brief was updated" in error["message"]
    assert "filters were not changed" in error["message"]


@pytest.mark.parametrize(
    "failure",
    [
        lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=request)),
        lambda request: (_ for _ in ()).throw(httpx.RemoteProtocolError("eof", request=request)),
        lambda request: httpx.Response(504, json={"detail": "gateway timeout"}),
    ],
    ids=["read-timeout", "dropped-mid-response", "504"],
)
def test_edit_brief_applied_but_filters_unconfirmed_says_so(
    server, capsys, monkeypatch, failure
) -> None:
    # The PUT may have reached the server and been saved: "not changed" would be a guess.
    _stdin(monkeypatch, "B")
    server.routes[f"PATCH {BASE}/{P1}"] = _json(_detail(brief="B"))
    server.routes[f"PUT {BASE}/{P1}/filters"] = failure
    rc, out, err = _run(
        ["research-projects", "edit", P1, "--file", "-", "--filter", "f", "--json"], capsys
    )
    assert (rc, out) == (1, "")
    message = _error(err)["message"]
    assert "brief was updated" in message
    assert "filter change could not be confirmed" in message
    assert "not changed" not in message
    assert f"pepper research-projects show {P1}" in message


def test_edit_filter_with_clear_filters_is_a_usage_error(server, capsys) -> None:
    rc, out, err = _run(
        ["research-projects", "edit", P1, "--filter", "x", "--clear", "filters", "--json"], capsys
    )
    assert (rc, out) == (2, "")
    assert _error(err)["code"] == "usage"
    assert not server.requests


def test_edit_a_blank_filter_is_a_usage_error(server, capsys) -> None:
    rc, _, err = _run(["research-projects", "edit", P1, "--filter", " ", "--json"], capsys)
    assert rc == 2
    assert _error(err)["code"] == "usage"


def test_edit_with_nothing_to_change_opens_the_editor_on_the_brief(
    server, capsys, tty, monkeypatch
) -> None:
    seen = _editor(monkeypatch, "Edited brief")
    server.routes[f"{BASE}/{P1}"] = _json(_detail(brief="Old brief"))
    server.routes[f"PATCH {BASE}/{P1}"] = _json(_detail(brief="Edited brief"))
    rc, _, _ = _run(["research-projects", "edit", P1], capsys)
    assert rc == 0
    assert seen == ["Old brief"]
    assert _sent(_writes(server)[0]) == {"brief": "Edited brief"}


def test_edit_an_unchanged_editor_brief_sends_nothing(server, capsys, tty, monkeypatch) -> None:
    _editor(monkeypatch, "Old brief\n")  # an editor's trailing newline is not a change
    server.routes[f"{BASE}/{P1}"] = _json(_detail(brief="Old brief"))
    rc, _, err = _run(["research-projects", "edit", P1], capsys)
    assert rc == 0
    assert "No changes." in err
    assert not _writes(server)


@pytest.mark.parametrize("as_json", [False, True])
def test_edit_with_nothing_to_change_and_no_terminal_exits_2(server, capsys, as_json) -> None:
    rc, out, err = _run(["research-projects", "edit", P1, *(["--json"] if as_json else [])], capsys)
    assert (rc, out) == (2, "")
    assert "pass --file" in err
    assert not server.requests


def test_edit_a_422_prints_the_servers_sentence(server, capsys, monkeypatch) -> None:
    _stdin(monkeypatch, "same")
    server.routes[f"PATCH {BASE}/{P1}"] = httpx.Response(422, json={"detail": "Nothing changed."})
    rc, _, err = _run(["research-projects", "edit", P1, "--file", "-", "--json"], capsys)
    assert rc == 1
    assert _error(err) == {
        "code": "validation",
        "status": 422,
        "message": "Server returned 422: Nothing changed.",
    }


# --- pause and resume ------------------------------------------------------


@pytest.mark.parametrize(("verb", "said"), [("pause", "Paused"), ("resume", "Resumed")])
def test_pause_and_resume_answer_the_project(server, capsys, verb, said) -> None:
    server.routes[f"POST {BASE}/{P1}/{verb}"] = _json(_detail(status="paused"))
    rc, out, err = _run(["research-projects", verb, "robot-vacuums"], capsys)
    assert rc == 0
    assert said in err
    rc, out, _ = _run(["research-projects", verb, "robot-vacuums", "--json"], capsys)
    assert json.loads(out)["status"] == "paused"


def test_resume_says_it_does_not_run_the_project(server, capsys) -> None:
    server.routes[f"POST {BASE}/{P1}/resume"] = _json(_detail())
    _, _, err = _run(["research-projects", "resume", P1], capsys)
    assert "does not run it" in err


def test_pause_a_conflict_is_the_servers_refusal(server, capsys) -> None:
    server.routes[f"POST {BASE}/{P1}/pause"] = httpx.Response(409, json={"detail": "stopped"})
    rc, _, err = _run(["research-projects", "pause", P1, "--json"], capsys)
    assert rc == 1
    assert _error(err)["code"] == "conflict"


# --- steer -----------------------------------------------------------------


def _steering(**over) -> dict:
    row = {
        "id": "00000000-0000-0000-0000-0000000000e1",
        "text": "skip anything refurbished",
        "question_id": None,
        "author_user_id": ME,
        "author_name": "Me",
        "created_at": "2026-10-20T10:00:00+00:00",
        "consumed_at": None,
    }
    row.update(over)
    return row


def test_steer_joins_the_words_and_names_the_question(server, capsys) -> None:
    server.routes[f"POST {BASE}/{P1}/steering"] = httpx.Response(
        201, json=_steering(question_id="q2")
    )
    rc, _out, err = _run(
        ["research-projects", "steer", P1, "skip", "anything", "refurbished", "--question", "q2"],
        capsys,
    )
    assert rc == 0
    assert _sent(_writes(server)[0]) == {"text": "skip anything refurbished", "question_id": "q2"}
    assert "(answering q2)" in err


def test_steer_reads_stdin_for_a_dash_and_emits_the_note(server, capsys, monkeypatch) -> None:
    _stdin(monkeypatch, "  long guidance\nover lines\n")
    server.routes[f"POST {BASE}/{P1}/steering"] = httpx.Response(201, json=_steering())
    rc, out, _ = _run(["research-projects", "steer", P1, "-", "--json"], capsys)
    assert rc == 0
    assert _sent(_writes(server)[0]) == {"text": "long guidance\nover lines"}
    assert json.loads(out)["author_name"] == "Me"


def test_steer_without_text_is_a_usage_error(server, capsys) -> None:
    rc, out, err = _run(["research-projects", "steer", P1, "--json"], capsys)
    assert (rc, out) == (2, "")
    assert _error(err)["code"] == "usage"
    assert not server.requests


def test_steer_yes_is_accepted_and_changes_nothing(server, capsys) -> None:
    server.routes[f"POST {BASE}/{P1}/steering"] = httpx.Response(201, json=_steering())
    rc, _, _ = _run(["research-projects", "steer", P1, "go", "--yes"], capsys)
    assert rc == 0


# --- run, without --wait ---------------------------------------------------


def test_run_prints_the_receipt_and_does_not_wait(server, capsys) -> None:
    server.routes[f"POST {BASE}/{P1}/runs"] = httpx.Response(202, json=_receipt())
    rc, out, err = _run(["research-projects", "run", "robot-vacuums"], capsys)
    assert rc == 0
    assert out.strip() == RUN
    assert f"Queued run {RUN} for robot-vacuums." in err
    assert "pepper research-projects show robot-vacuums" in err
    assert [r.method for r in server.requests].count("POST") == 1


def test_run_json_is_the_receipt(server, capsys) -> None:
    server.routes[f"POST {BASE}/{P1}/runs"] = httpx.Response(202, json=_receipt())
    rc, out, err = _run(["research-projects", "run", P1, "--json"], capsys)
    assert (rc, err) == (0, "")
    assert json.loads(out) == _receipt()


def test_run_while_one_is_in_flight_prints_it_and_exits_0(server, capsys) -> None:
    server.routes[f"POST {BASE}/{P1}/runs"] = httpx.Response(
        409, json={"detail": "a run is already in flight", "run_id": RUN.upper()}
    )
    server.routes[f"{BASE}/{P1}/runs/{RUN}"] = _json(_run_detail("running"))
    rc, out, err = _run(["research-projects", "run", "robot-vacuums"], capsys)
    assert rc == 0
    assert f"A run of robot-vacuums is already running ({RUN}); not starting another." in err
    assert out.strip() == RUN
    rc, out, err = _run(["research-projects", "run", P1, "--json"], capsys)
    assert (rc, err) == (0, "")
    assert json.loads(out)["status"] == "running"
    assert [r.method for r in server.requests].count("POST") == 2  # one per invocation


def test_run_a_409_without_a_run_id_is_a_conflict(server, capsys) -> None:
    server.routes[f"POST {BASE}/{P1}/runs"] = httpx.Response(
        409, json={"detail": "the project is paused"}
    )
    rc, out, err = _run(["research-projects", "run", P1, "--json"], capsys)
    assert (rc, out) == (1, "")
    assert _error(err) == {
        "code": "conflict",
        "status": 409,
        "message": "Server returned 409: the project is paused",
    }


def test_run_yes_is_accepted_and_changes_nothing(server, capsys) -> None:
    server.routes[f"POST {BASE}/{P1}/runs"] = httpx.Response(202, json=_receipt())
    rc, _, _ = _run(["research-projects", "run", P1, "--yes"], capsys)
    assert rc == 0


def test_run_a_malformed_receipt_is_a_clear_error(server, capsys) -> None:
    server.routes[f"POST {BASE}/{P1}/runs"] = httpx.Response(
        202, json={k: v for k, v in _receipt().items() if k != "run_id"}
    )
    rc, out, err = _run(["research-projects", "run", P1, "--json"], capsys)
    assert (rc, out) == (1, "")
    assert "ResearchRunReceipt has no 'run_id'" in _error(err)["message"]


@pytest.mark.parametrize(
    "flags",
    [
        ["--timeout", "5"],
        ["--wait", "--timeout", "-1"],
        ["--wait", "--timeout", "nan"],
        ["--wait", "--timeout", "inf"],
        ["--wait", "--timeout", "Infinity"],
    ],
)
def test_run_timeout_needs_wait_and_a_sane_value(server, capsys, flags) -> None:
    rc, out, err = _run(["research-projects", "run", P1, *flags, "--json"], capsys)
    assert (rc, out) == (2, "")
    assert _error(err)["code"] == "usage"
    assert not server.requests


# --- run --wait ------------------------------------------------------------


class _Clock:
    """A fake clock: ``sleep`` advances it, and the gaps it was asked for are recorded."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(cli, "_sleep", c.sleep)
    monkeypatch.setattr(cli, "_monotonic", c.monotonic)
    return c


def _wait_routes(server, *polls: httpx.Response) -> None:
    server.routes[f"POST {BASE}/{P1}/runs"] = httpx.Response(202, json=_receipt())
    server.routes[f"{BASE}/{P1}/runs/{RUN}"] = list(polls)


def _poll(status: str, **over) -> httpx.Response:
    return _json(_run_detail(status, **over))


def test_wait_polls_to_success_and_prints_one_json_document(server, capsys, clock) -> None:
    done = {"finished_at": "2026-10-20T10:03:17+00:00"}
    _wait_routes(server, _poll("queued"), _poll("running"), _poll("succeeded", **done))
    rc, out, err = _run(["research-projects", "run", P1, "--wait", "--json"], capsys)
    assert (rc, err) == (0, "")
    body = json.loads(out)  # exactly one document, nothing before it
    assert body["status"] == "succeeded"
    assert [r.method for r in server.requests].count("POST") == 1


def test_wait_polls_at_2s_growing_by_half_to_15s_and_resets_on_a_change(
    server, capsys, clock
) -> None:
    statuses = ["queued"] * 6 + ["running"] * 2 + ["succeeded"]
    _wait_routes(server, *[_poll(s) for s in statuses])
    rc, _, _ = _run(["research-projects", "run", P1, "--wait", "--json"], capsys)
    assert rc == 0
    # Six polls still queued: 2, then x1.5 each time to the 15 s cap. The seventh answers
    # "running" (that change resets the gap), so the next two sleeps are 2 and 3.
    assert clock.slept == pytest.approx([2, 3, 4.5, 6.75, 10.125, 15, 15, 2, 3])


def test_wait_a_retry_after_overrides_the_gap(server, capsys, clock) -> None:
    _wait_routes(
        server,
        httpx.Response(200, json=_run_detail("running"), headers={"Retry-After": "7"}),
        _poll("succeeded"),
    )
    _run(["research-projects", "run", P1, "--wait", "--json"], capsys)
    assert clock.slept == [2, 7]


def test_wait_a_retry_after_date_is_the_delay_until_then(
    server, capsys, clock, monkeypatch
) -> None:
    now = dt.datetime(2026, 10, 20, 10, 0, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(api_client, "_utcnow", lambda: now)
    _wait_routes(
        server,
        httpx.Response(
            503,
            json={"detail": "busy"},
            headers={"Retry-After": "Tue, 20 Oct 2026 10:00:30 GMT"},
        ),
        _poll("succeeded"),
    )
    rc, _, _ = _run(["research-projects", "run", P1, "--wait", "--json"], capsys)
    assert rc == 0
    assert clock.slept == [2, 30]


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("7", 7.0),
        ("0", 0.0),
        ("Tue, 20 Oct 2026 10:01:00 GMT", 60.0),
        ("Tuesday, 20-Oct-26 10:01:00 GMT", 60.0),  # RFC 850, still valid to receive
        ("Tue Oct 20 10:01:00 2026", 60.0),  # asctime
        ("Tue, 20 Oct 2026 09:59:00 GMT", 0.0),  # already past: retry now, never negative
        ("-3", None),
        ("nan", None),
        ("inf", None),
        ("soon", None),
        ("", None),
    ],
)
def test_retry_after_reads_seconds_and_http_dates(monkeypatch, header, seconds) -> None:
    now = dt.datetime(2026, 10, 20, 10, 0, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(api_client, "_utcnow", lambda: now)
    headers = {"Retry-After": header} if header else {}
    assert api_client._retry_after(httpx.Response(503, headers=headers)) == seconds


def test_wait_human_output_has_progress_on_stderr_and_whats_new_on_stdout(
    server, capsys, clock
) -> None:
    iteration_id = "00000000-0000-0000-0000-00000000f001"
    _wait_routes(
        server,
        _poll("running"),
        _poll(
            "succeeded",
            finished_at="2026-10-20T10:03:17+00:00",
            changelog=[
                {
                    "day": "2026-10-20",
                    "text": "Two new candidates under $450",
                    "evidence_ids": [],
                    "iteration_id": iteration_id,
                }
            ],
        ),
    )
    rc, out, err = _run(["research-projects", "run", "robot-vacuums", "--wait"], capsys)
    assert rc == 0
    assert out.splitlines() == [
        f"Run {RUN} succeeded in 3m12s.",
        "What's new",
        "  2026-10-20  Two new candidates under $450",
    ]
    lines = err.splitlines()
    assert lines[0].startswith("Queued run")
    assert [ln.split(" ")[0] for ln in lines[1:]] == ["queued", "running"]  # one line per change


def test_wait_without_changelog_entries_points_at_the_report(server, capsys, clock) -> None:
    _wait_routes(server, _poll("succeeded", finished_at="2026-10-20T10:00:45+00:00"))
    _, out, _ = _run(["research-projects", "run", "robot-vacuums", "--wait"], capsys)
    assert out.splitlines() == [
        f"Run {RUN} succeeded in 40s.",
        "Read the report: pepper research-projects show robot-vacuums --report",
    ]


@pytest.mark.parametrize("status", ["error", "interrupted"])
def test_wait_a_failed_run_is_run_failed_with_the_receipt_on_stdout(
    server, capsys, clock, status
) -> None:
    _wait_routes(server, _poll("running"), _poll(status, error="page fetch failed"))
    rc, out, err = _run(["research-projects", "run", P1, "--wait", "--json"], capsys)
    assert rc == 1
    assert json.loads(out)["status"] == status  # the same document as a success
    assert _error(err) == {
        "code": "run_failed",
        "status": None,
        "message": f"run {RUN} ended {status}: page fetch failed",
    }


def test_wait_times_out_with_exit_4_and_the_last_receipt(server, capsys, clock) -> None:
    _wait_routes(server, _poll("queued"), _poll("running"))
    rc, out, err = _run(
        ["research-projects", "run", "robot-vacuums", "--wait", "--timeout", "1", "--json"], capsys
    )
    assert rc == 4
    body = json.loads(out)
    assert (body["id"], body["status"]) == (RUN, "queued")  # the last receipt seen
    error = _error(err)
    assert error["code"] == "timeout"
    assert RUN in error["message"]
    assert "pepper research-projects show robot-vacuums" in error["message"]
    assert "runs robot-vacuums" not in error["message"]  # never `runs` while it is unfinished


def test_wait_timeout_keeps_the_last_status_seen(server, capsys, clock) -> None:
    _wait_routes(server, _poll("running"))
    rc, out, _err = _run(
        ["research-projects", "run", P1, "--wait", "--timeout", "5", "--json"], capsys
    )
    assert rc == 4
    assert json.loads(out)["status"] == "running"
    assert clock.now == 1005.0  # the last sleep was cut to the time left


def test_wait_timeout_in_human_mode(server, capsys, clock) -> None:
    _wait_routes(server, _poll("running"))
    rc, out, err = _run(["research-projects", "run", P1, "--wait", "--timeout", "3"], capsys)
    assert rc == 4
    assert out.strip() == f"Run {RUN} is still running."
    assert "stopped waiting after 3s" in err


def test_wait_timeout_0_has_no_bound(server, capsys, clock) -> None:
    _wait_routes(server, *([_poll("running")] * 400), _poll("succeeded"))
    rc, _, _ = _run(["research-projects", "run", P1, "--wait", "--timeout", "0", "--json"], capsys)
    assert rc == 0
    assert sum(clock.slept) > 900  # past the default bound


def test_wait_ctrl_c_stops_waiting_without_cancelling(server, capsys, monkeypatch) -> None:
    _wait_routes(server, _poll("running"))

    def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_sleep", interrupt)
    rc, out, err = _run(["research-projects", "run", "robot-vacuums", "--wait"], capsys)
    assert (rc, out) == (130, "")
    assert "still running — check 'pepper research-projects show robot-vacuums'" in err
    assert [r.method for r in server.requests].count("POST") == 1
    assert not [r for r in server.requests if r.method in {"DELETE", "PUT", "PATCH"}]


def test_wait_ctrl_c_in_json_mode_is_a_timeout_line_and_empty_stdout(
    server, capsys, monkeypatch
) -> None:
    _wait_routes(server, _poll("running"))
    monkeypatch.setattr(cli, "_sleep", lambda _s: (_ for _ in ()).throw(KeyboardInterrupt))
    rc, out, err = _run(["research-projects", "run", P1, "--wait", "--json"], capsys)
    assert (rc, out) == (130, "")
    assert _error(err)["code"] == "timeout"


def test_wait_retries_a_5xx_and_a_dropped_connection(server, capsys, clock) -> None:
    def dropped(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("reset", request=request)

    _wait_routes(server, httpx.Response(503, json={"detail": "busy"}), dropped, _poll("succeeded"))
    rc, out, _ = _run(["research-projects", "run", P1, "--wait", "--json"], capsys)
    assert rc == 0
    assert json.loads(out)["status"] == "succeeded"


@pytest.mark.parametrize("status", [401, 403, 404])
def test_wait_a_client_error_while_polling_ends_through_the_normal_path(
    server, capsys, clock, status
) -> None:
    _wait_routes(server, httpx.Response(status, json={"detail": "nope"}))
    rc, out, err = _run(["research-projects", "run", P1, "--wait", "--json"], capsys)
    assert (rc, out) == (1, "")
    assert _error(err)["status"] == status


def test_wait_attaches_to_a_run_in_flight_without_a_second_post(server, capsys, clock) -> None:
    server.routes[f"POST {BASE}/{P1}/runs"] = httpx.Response(
        409, json={"detail": "in flight", "run_id": RUN}
    )
    server.routes[f"{BASE}/{P1}/runs/{RUN}"] = [
        _poll("running"),  # the lookup after the 409
        _poll("running"),
        _poll("succeeded"),
    ]
    rc, _out, err = _run(["research-projects", "run", "robot-vacuums", "--wait"], capsys)
    assert rc == 0
    assert (
        f"A run of robot-vacuums is already in flight ({RUN}); waiting on it instead of starting "
        "another." in " ".join(err.split())
    )
    assert [r.method for r in server.requests].count("POST") == 1


def test_wait_on_an_attached_run_that_failed_exits_1(server, capsys, clock) -> None:
    server.routes[f"POST {BASE}/{P1}/runs"] = httpx.Response(
        409, json={"detail": "in flight", "run_id": RUN}
    )
    server.routes[f"{BASE}/{P1}/runs/{RUN}"] = _poll("error", error="boom")
    rc, out, err = _run(["research-projects", "run", P1, "--wait", "--json"], capsys)
    assert rc == 1
    assert json.loads(out)["status"] == "error"
    assert _error(err)["code"] == "run_failed"


def _attach_routes(server, *polls) -> None:
    server.routes[f"POST {BASE}/{P1}/runs"] = httpx.Response(
        409, json={"detail": "in flight", "run_id": RUN}
    )
    server.routes[f"{BASE}/{P1}/runs/{RUN}"] = list(polls)


def _dropped(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("reset", request=request)


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(503, json={"detail": "busy"}),
        httpx.Response(429, json={"detail": "slow down"}),
        _dropped,
    ],
    ids=["503", "429", "dropped"],
)
def test_wait_retries_the_lookup_of_an_attached_run(server, capsys, clock, failure) -> None:
    _attach_routes(server, failure, _poll("running"), _poll("succeeded"))
    rc, out, err = _run(["research-projects", "run", P1, "--wait", "--json"], capsys)
    assert (rc, err) == (0, "")
    assert json.loads(out)["status"] == "succeeded"
    assert [r.method for r in server.requests].count("POST") == 1


def test_wait_honours_retry_after_on_the_lookup_of_an_attached_run(server, capsys, clock) -> None:
    _attach_routes(
        server,
        httpx.Response(200, json=_run_detail("running"), headers={"Retry-After": "7"}),
        _poll("succeeded"),
    )
    rc, _, _ = _run(["research-projects", "run", P1, "--wait", "--json"], capsys)
    assert rc == 0
    assert clock.slept == [7]  # the lookup is not slept before, and its Retry-After wins


def test_wait_an_attached_run_that_never_answers_times_out_with_empty_stdout(
    server, capsys, clock
) -> None:
    _attach_routes(server, httpx.Response(503, json={"detail": "busy"}))
    rc, out, err = _run(
        ["research-projects", "run", P1, "--wait", "--timeout", "5", "--json"], capsys
    )
    assert (rc, out) == (4, "")  # no receipt was ever seen, so there is none to print
    error = _error(err)
    assert error["code"] == "timeout"
    assert RUN in error["message"]


def _timeouts(server) -> list[float]:
    return [
        r.extensions["timeout"]["read"]
        for r in server.requests
        if r.method == "GET" and r.url.path.endswith(f"/runs/{RUN}")
    ]


def test_wait_bounds_each_poll_request_by_the_time_left(server, capsys, clock) -> None:
    _wait_routes(server, _poll("running"))
    rc, _, _ = _run(["research-projects", "run", P1, "--wait", "--timeout", "10", "--json"], capsys)
    assert rc == 4
    # Polls at 2 s, 4 s ("queued" -> "running" reset the gap) and 7 s, then one at the deadline:
    # each request may take only the time left, and the one at the deadline gets the floor.
    assert _timeouts(server) == pytest.approx([8, 6, 3, cli._WAIT_MIN_REQUEST_TIMEOUT])


def test_wait_without_a_bound_polls_with_the_normal_request_timeout(server, capsys, clock) -> None:
    _wait_routes(server, _poll("running"), _poll("succeeded"))
    rc, _, _ = _run(["research-projects", "run", P1, "--wait", "--timeout", "0", "--json"], capsys)
    assert rc == 0
    assert _timeouts(server) == [5.0, 5.0]  # the test client's own (httpx's default)


def test_wait_a_poll_that_times_out_at_the_deadline_is_a_wait_timeout(
    server, capsys, clock
) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        clock.now += request.extensions["timeout"]["read"]
        raise httpx.ReadTimeout("slow", request=request)

    _wait_routes(server, _poll("running"), slow)
    rc, out, err = _run(
        ["research-projects", "run", P1, "--wait", "--timeout", "3", "--json"], capsys
    )
    assert rc == 4
    assert json.loads(out)["status"] == "running"
    assert _error(err)["code"] == "timeout"
    assert clock.now <= 1003.0 + cli._WAIT_MIN_REQUEST_TIMEOUT


def test_wait_bounds_the_whole_poll_not_just_each_phase(server, capsys, clock, monkeypatch) -> None:
    # httpx's timeouts bound each phase (and not DNS at all), so a poll that keeps trickling
    # could outlast them; the wait gives up on the whole call once the time left is gone.
    monkeypatch.setattr(cli, "_WAIT_MIN_REQUEST_TIMEOUT", 0.05)
    release = threading.Event()

    def hung(request: httpx.Request) -> httpx.Response:
        release.wait(10)
        return _json(_run_detail("succeeded"))

    _wait_routes(server, _poll("running"), hung)
    began = time.monotonic()
    try:
        rc, out, err = _run(
            ["research-projects", "run", P1, "--wait", "--timeout", "3", "--json"], capsys
        )
    finally:
        release.set()
    assert time.monotonic() - began < 5  # nowhere near the hung request's 10 s
    assert rc == 4
    assert json.loads(out)["status"] == "running"
    assert _error(err)["code"] == "timeout"


# --- research-items take and reconsider ------------------------------------


def _marked(server, verb: str, item: dict, **over) -> None:
    server.routes[f"POST {BASE}/{P1}/items/{item['id']}/{verb}"] = _json({**item, **over})


def test_take_by_number_marks_the_item(server, capsys) -> None:
    items = [_item(1), _item(2, state="delivered")]
    _items_route(server, items)
    _marked(server, "take", items[1], state="taken")
    _run(["research-items", "list", "--project", P1], capsys)
    rc, out, err = _run(["research-items", "take", "--project", P1, "2"], capsys)
    assert rc == 0
    assert "Marked 'Item 2' taken." in err
    assert _writes(server)[0].url.path == f"{BASE}/{P1}/items/{items[1]['id']}/take"
    rc, out, _ = _run(["research-items", "take", "--project", P1, "2", "--json"], capsys)
    assert json.loads(out)["state"] == "taken"  # J5


def test_reconsider_sends_the_reason_as_given(server, capsys) -> None:
    item = _item(1, state="rejected")
    _items_route(server, [item])
    _marked(server, "reconsider", item, state="candidate")
    _run(["research-items", "list", "--project", P1], capsys)
    rc, _, err = _run(
        ["research-items", "reconsider", "--project", P1, "1", "--reason", "price dropped"], capsys
    )
    assert rc == 0
    assert "'Item 1' is a candidate again." in err
    assert _sent(_writes(server)[0]) == {"reason": "price dropped"}


def test_reconsider_without_a_reason_is_the_servers_refusal(server, capsys) -> None:
    item = _item(1, state="rejected")
    server.routes[f"POST {BASE}/{P1}/items/{item['id']}/reconsider"] = httpx.Response(
        422, json={"detail": "reconsidering a rejection needs a `reason`."}
    )
    rc, out, err = _run(
        ["research-items", "reconsider", "--project", P1, item["id"], "--json"], capsys
    )
    assert (rc, out) == (1, "")
    assert _sent(_writes(server)[0]) == {"reason": ""}
    assert _error(err)["code"] == "validation"


def test_take_a_refused_transition_prints_the_servers_sentence(server, capsys) -> None:
    item = _item(1)
    server.routes[f"POST {BASE}/{P1}/items/{item['id']}/take"] = httpx.Response(
        409, json={"detail": "only a delivered item can be taken"}
    )
    rc, _, err = _run(["research-items", "take", "--project", P1, item["id"], "--json"], capsys)
    assert rc == 1
    assert _error(err)["message"] == "Server returned 409: only a delivered item can be taken"


@pytest.mark.parametrize("verb", ["take", "reconsider"])
def test_a_number_from_another_projects_listing_is_refused_by_the_writes(
    server, capsys, verb
) -> None:
    _items_route(server, [_item(1)])
    _run(["research-items", "list", "--project", P1], capsys)
    server.requests.clear()
    rc, out, err = _run(["research-items", verb, "--project", P2, "1", "--json"], capsys)
    assert (rc, out) == (2, "")
    assert _error(err)["message"] == research_view.ANOTHER_PROJECT
    assert not server.requests  # nothing was written to the wrong ledger


@pytest.mark.parametrize("verb", ["take", "reconsider"])
def test_an_item_id_in_any_case_passes_straight_through_lowercased(server, capsys, verb) -> None:
    item = _item(1, id="0000000a-0000-0000-0000-00000000e000")
    _marked(server, verb, item)
    rc, _, _ = _run(
        ["research-items", verb, "--project", P1.upper(), item["id"].upper(), "--json"], capsys
    )
    assert rc == 0
    assert _writes(server)[0].url.path == f"{BASE}/{P1}/items/{item['id']}/{verb}"


# --- errors shared by the writes -------------------------------------------


_WRITES = [
    (["research-projects", "create", "x", "--file", "-"], "POST", BASE),
    (["research-projects", "edit", P1, "--file", "-"], "PATCH", f"{BASE}/{P1}"),
    (["research-projects", "edit", P1, "--filter", "f"], "PUT", f"{BASE}/{P1}/filters"),
    (["research-projects", "edit", P1, "--clear", "schedule"], "DELETE", f"{BASE}/{P1}/schedule"),
    (["research-projects", "run", P1], "POST", f"{BASE}/{P1}/runs"),
    (["research-projects", "steer", P1, "go"], "POST", f"{BASE}/{P1}/steering"),
    (["research-projects", "pause", P1], "POST", f"{BASE}/{P1}/pause"),
    (["research-projects", "resume", P1], "POST", f"{BASE}/{P1}/resume"),
    (
        ["research-items", "take", "--project", P1, _item(1)["id"]],
        "POST",
        f"{BASE}/{P1}/items/{_item(1)['id']}/take",
    ),
    (
        ["research-items", "reconsider", "--project", P1, _item(1)["id"]],
        "POST",
        f"{BASE}/{P1}/items/{_item(1)['id']}/reconsider",
    ),
]


@pytest.mark.parametrize(("argv", "method", "path"), _WRITES)
@pytest.mark.parametrize("as_json", [False, True])
def test_every_write_says_when_research_projects_are_switched_off(
    server, capsys, monkeypatch, argv, method, path, as_json
) -> None:
    _stdin(monkeypatch, "body")
    server.routes[f"{method} {path}"] = httpx.Response(
        403, json={"detail": "off", "code": "research_projects_disabled"}
    )
    rc, out, err = _run([*argv, *(["--json"] if as_json else [])], capsys)
    assert (rc, out) == (1, "")
    message = "Research projects are not enabled on this Pepper server."
    if as_json:
        assert _error(err) == {"code": "forbidden", "status": 403, "message": message}
    else:
        assert message in err


@pytest.mark.parametrize(("argv", "method", "path"), _WRITES)
def test_every_write_reports_a_project_that_is_not_yours_as_not_found(
    server, capsys, monkeypatch, argv, method, path
) -> None:
    _stdin(monkeypatch, "body")
    server.routes[f"{method} {path}"] = httpx.Response(
        404, json={"detail": "no such research project"}
    )
    rc, out, err = _run([*argv, "--json"], capsys)
    assert (rc, out) == (1, "")
    assert _error(err)["code"] == "not_found"


@pytest.mark.parametrize("argv", [w[0] for w in _WRITES if P1 in w[0] or "--project" in w[0]])
def test_the_writes_resolve_a_project_by_name_prefix_and_say_so_when_it_is_unknown(
    server, capsys, monkeypatch, argv
) -> None:
    _stdin(monkeypatch, "body")
    argv = [("nothing-like-it" if a == P1 else a) for a in argv]
    rc, out, err = _run([*argv, "--json"], capsys)
    assert (rc, out) == (2, "")
    assert _error(err)["code"] == "not_found"
    assert not _writes(server)


# ---------------------------------------------------------------------------
# edit: schedules (planning/research-projects.md section 7)

SCHEDULE = f"{BASE}/{P1}/schedule"
TASK = "00000000-0000-0000-0000-0000000000c1"
TASK_PATH = f"/api/v1/scheduled-tasks/{TASK}"
CRON = "0 9 * * 0"


def _scheduled(**over) -> dict:
    schedule = {"cron": CRON, "timezone": "America/Los_Angeles", "active": True}
    return _detail(**{"scheduled_task_id": TASK, "schedule": schedule, **over})


def _manual_only(server) -> None:
    server.routes[f"{BASE}/{P1}"] = _json(_detail())


def _no_editor(monkeypatch) -> None:
    def boom(initial: str) -> str | None:
        raise AssertionError("the editor must not open for a schedule edit")

    monkeypatch.setattr(cli, "_open_in_editor", boom)


def test_enable_confirms_with_the_cron_and_the_zone_then_posts(
    server, capsys, tty, monkeypatch
) -> None:
    _no_editor(monkeypatch)
    _manual_only(server)
    server.routes[f"POST {SCHEDULE}"] = httpx.Response(201, json=_scheduled())
    asked: list[str] = []
    monkeypatch.setattr(cli, "_prompt", lambda text: asked.append(text) or "y")
    rc, _, err = _run(
        [
            "research-projects",
            "edit",
            "robot-vacuums",
            "--cron",
            CRON,
            "--timezone",
            "Europe/Paris",
        ],
        capsys,
    )
    assert rc == 0
    (question,) = asked
    assert "robot-vacuums" in question
    assert f"'{CRON}'" in question
    assert "(Europe/Paris)" in question
    assert question.rstrip().endswith("[y/N]")
    (write,) = _writes(server)
    assert write.url.path == SCHEDULE
    assert _sent(write) == {"cron": CRON, "timezone": "Europe/Paris"}
    # What the server echoes back is what the person is told.
    assert f"'{CRON}' (America/Los_Angeles)" in err


def test_enable_without_a_zone_says_it_is_the_profile_zone_and_sends_none(
    server, capsys, tty, monkeypatch
) -> None:
    _manual_only(server)
    server.routes[f"POST {SCHEDULE}"] = httpx.Response(201, json=_scheduled())
    asked: list[str] = []
    monkeypatch.setattr(cli, "_prompt", lambda text: asked.append(text) or "yes")
    rc, _, _ = _run(["research-projects", "edit", P1, "--cron", CRON], capsys)
    assert rc == 0
    assert "(your profile timezone)" in asked[0]
    assert _sent(_writes(server)[0]) == {"cron": CRON}


def test_enable_declined_sends_nothing(server, capsys, tty, monkeypatch) -> None:
    _manual_only(server)
    monkeypatch.setattr(cli, "_prompt", lambda text: "n")
    rc, _, err = _run(["research-projects", "edit", P1, "--cron", CRON], capsys)
    assert rc == 0
    assert "Aborted." in err
    assert not _writes(server)


def test_a_declined_enable_does_not_apply_the_rest_of_the_edit(
    server, capsys, tty, monkeypatch
) -> None:
    _manual_only(server)
    _stdin(monkeypatch, "New brief")
    monkeypatch.setattr(cli, "_prompt", lambda text: "n")
    rc, _, _ = _run(["research-projects", "edit", P1, "--file", "-", "--cron", CRON], capsys)
    assert rc == 0
    assert not _writes(server)


@pytest.mark.parametrize("as_json", [False, True])
def test_enable_without_a_terminal_or_yes_is_a_usage_error_with_no_write(
    server, capsys, monkeypatch, as_json
) -> None:
    _manual_only(server)
    _stdin(monkeypatch, "New brief")
    argv = ["research-projects", "edit", P1, "--file", "-", "--cron", CRON]
    rc, out, err = _run([*argv, "--json"] if as_json else argv, capsys)
    assert (rc, out) == (2, "")
    assert "pass --yes" in (_error(err)["message"] if as_json else err)
    if as_json:
        assert _error(err)["code"] == "usage"
    assert not _writes(server)


def test_enable_json_never_prompts_even_on_a_terminal(server, capsys, tty, monkeypatch) -> None:
    _manual_only(server)

    def boom(text: str) -> str:
        raise AssertionError("JSON mode never prompts")

    monkeypatch.setattr(cli, "_prompt", boom)
    rc, out, err = _run(["research-projects", "edit", P1, "--cron", CRON, "--json"], capsys)
    assert (rc, out) == (2, "")
    assert _error(err)["code"] == "usage"
    assert not _writes(server)


def test_enable_with_yes_posts_without_a_terminal_and_answers_the_project(
    server, capsys, monkeypatch
) -> None:
    _no_editor(monkeypatch)
    _manual_only(server)
    server.routes[f"POST {SCHEDULE}"] = httpx.Response(201, json=_scheduled())
    rc, out, _ = _run(["research-projects", "edit", P1, "--cron", CRON, "--yes", "--json"], capsys)
    assert rc == 0
    assert json.loads(out)["schedule"]["timezone"] == "America/Los_Angeles"
    assert [w.method for w in _writes(server)] == ["POST"]


def test_changing_a_schedule_patches_the_task_without_a_prompt(server, capsys, monkeypatch) -> None:
    server.routes[f"{BASE}/{P1}"] = [
        _json(_scheduled()),
        _json(_scheduled(schedule={"cron": "0 7 * * 1", "timezone": "UTC", "active": True})),
    ]
    server.routes[f"PATCH {TASK_PATH}"] = _json({"id": TASK})
    rc, out, _ = _run(
        ["research-projects", "edit", P1, "--cron", "0 7 * * 1", "--timezone", "UTC", "--json"],
        capsys,
    )
    assert rc == 0  # no terminal and no --yes: a change does not prompt (decision D)
    (write,) = _writes(server)
    assert write.url.path == TASK_PATH
    assert _sent(write) == {"cron": "0 7 * * 1", "timezone": "UTC"}
    assert json.loads(out)["schedule"]["cron"] == "0 7 * * 1"  # the project, read back


def test_changing_only_the_cron_sends_no_timezone(server, capsys) -> None:
    server.routes[f"{BASE}/{P1}"] = _json(_scheduled())
    server.routes[f"PATCH {TASK_PATH}"] = _json({"id": TASK})
    rc, _, err = _run(["research-projects", "edit", P1, "--cron", "0 7 * * 1"], capsys)
    assert rc == 0
    assert _sent(_writes(server)[0]) == {"cron": "0 7 * * 1"}
    assert "Changed the schedule" in err


def test_a_refused_cron_is_the_servers_sentence(server, capsys) -> None:
    server.routes[f"{BASE}/{P1}"] = _json(_scheduled())
    server.routes[f"PATCH {TASK_PATH}"] = httpx.Response(422, json={"detail": "bad cron: nope"})
    rc, out, err = _run(["research-projects", "edit", P1, "--cron", "nope", "--json"], capsys)
    assert (rc, out) == (1, "")
    assert _error(err)["code"] == "validation"
    assert "bad cron: nope" in _error(err)["message"]


def test_a_lost_race_to_enable_follows_the_pointer_to_the_task(server, capsys, monkeypatch) -> None:
    # Another client scheduled it between the read and the POST: the 409 names the task.
    _manual_only(server)
    server.routes[f"POST {SCHEDULE}"] = httpx.Response(
        409, json={"detail": "already has a schedule", "scheduled_task_id": TASK}
    )
    server.routes[f"PATCH {TASK_PATH}"] = _json({"id": TASK})
    rc, _, _ = _run(["research-projects", "edit", P1, "--cron", CRON, "--yes"], capsys)
    assert rc == 0
    assert [(w.method, w.url.path) for w in _writes(server)] == [
        ("POST", SCHEDULE),
        ("PATCH", TASK_PATH),
    ]
    assert _sent(_writes(server)[1]) == {"cron": CRON}


def test_an_already_scheduled_409_without_a_task_id_is_a_conflict(server, capsys) -> None:
    _manual_only(server)
    server.routes[f"POST {SCHEDULE}"] = httpx.Response(409, json={"detail": "project is paused"})
    rc, out, err = _run(
        ["research-projects", "edit", P1, "--cron", CRON, "--yes", "--json"], capsys
    )
    assert (rc, out) == (1, "")
    assert _error(err)["code"] == "conflict"
    assert "project is paused" in _error(err)["message"]


def test_clear_schedule_deletes_without_a_prompt_and_answers_the_project(
    server, capsys, monkeypatch
) -> None:
    _no_editor(monkeypatch)
    server.routes[f"DELETE {SCHEDULE}"] = _json(_detail())
    rc, out, _ = _run(["research-projects", "edit", P1, "--clear", "schedule", "--json"], capsys)
    assert rc == 0  # stops spend and can be undone: no terminal, no --yes, still fine
    (write,) = _writes(server)
    assert (write.method, write.url.path) == ("DELETE", SCHEDULE)
    assert json.loads(out)["scheduled_task_id"] is None
    assert not [r for r in server.requests if r.method == "GET" and r.url.path == f"{BASE}/{P1}"]


def test_clear_schedule_says_the_history_is_kept(server, capsys) -> None:
    server.routes[f"DELETE {SCHEDULE}"] = _json(_detail())
    rc, _, err = _run(["research-projects", "edit", P1, "--clear", "schedule"], capsys)
    assert rc == 0
    assert "Turned off the schedule" in err
    assert "history is kept" in err


@pytest.mark.parametrize(
    "detail",
    [
        "Nothing changed: a scheduled run is in progress; try again when it finishes.",
        (
            "Nothing changed: the schedule of research project 'robot-vacuums' changed while it "
            "was being detached; read the project again and retry."
        ),
        "Nothing changed: research project 'robot-vacuums' has no schedule.",
    ],
    ids=["lease-running", "changed-while-detaching", "no-schedule"],
)
@pytest.mark.parametrize("as_json", [False, True])
def test_clear_schedule_409s_print_the_servers_sentence(server, capsys, detail, as_json) -> None:
    server.routes[f"DELETE {SCHEDULE}"] = httpx.Response(409, json={"detail": detail})
    argv = ["research-projects", "edit", P1, "--clear", "schedule"]
    rc, out, err = _run([*argv, "--json"] if as_json else argv, capsys)
    assert (rc, out) == (1, "")
    if as_json:
        error = _error(err)
        assert (error["code"], error["status"]) == ("conflict", 409)
        assert detail in error["message"]
    else:
        assert detail in " ".join(err.split())


def test_clear_schedule_with_a_brief_and_filters_is_three_writes_in_order(
    server, capsys, monkeypatch
) -> None:
    _stdin(monkeypatch, "B")
    server.routes[f"PATCH {BASE}/{P1}"] = _json(_detail(brief="B"))
    server.routes[f"PUT {BASE}/{P1}/filters"] = _json(_detail(brief="B"))
    server.routes[f"DELETE {SCHEDULE}"] = _json(_detail(brief="B", name="after-clear"))
    rc, out, _ = _run(
        [
            "research-projects",
            "edit",
            P1,
            "--file",
            "-",
            "--filter",
            "f",
            "--clear",
            "schedule",
            "--json",
        ],
        capsys,
    )
    assert rc == 0
    assert [w.method for w in _writes(server)] == ["PATCH", "PUT", "DELETE"]
    assert json.loads(out)["name"] == "after-clear"


@pytest.mark.parametrize(
    ("argv", "fragment"),
    [
        (["--timezone", "UTC"], "--timezone needs --cron"),
        (["--cron", CRON, "--clear", "schedule"], "contradict"),
        (["--cron", " "], "--cron needs"),
        (["--cron", CRON, "--timezone", " "], "--timezone needs"),
    ],
    ids=["timezone-alone", "cron-and-clear", "blank-cron", "blank-timezone"],
)
def test_schedule_flag_misuse_is_a_usage_error_before_any_request(
    server, capsys, argv, fragment
) -> None:
    rc, out, err = _run(["research-projects", "edit", P1, *argv, "--yes", "--json"], capsys)
    assert (rc, out) == (2, "")
    assert _error(err)["code"] == "usage"
    assert fragment in _error(err)["message"]
    assert not server.requests


def test_filter_with_clear_schedule_is_not_a_contradiction(server, capsys) -> None:
    server.routes[f"PUT {BASE}/{P1}/filters"] = _json(_detail())
    server.routes[f"DELETE {SCHEDULE}"] = _json(_detail())
    rc, _, _ = _run(
        ["research-projects", "edit", P1, "--filter", "f", "--clear", "schedule"], capsys
    )
    assert rc == 0


@pytest.mark.parametrize("as_json", [True, False])
def test_brief_applied_but_schedule_refused_says_which_part_applied(
    server, capsys, monkeypatch, as_json
) -> None:
    _stdin(monkeypatch, "B")
    _manual_only(server)
    server.routes[f"PATCH {BASE}/{P1}"] = _json(_detail(brief="B"))
    server.routes[f"POST {SCHEDULE}"] = httpx.Response(422, json={"detail": "bad cron: x"})
    argv = ["research-projects", "edit", P1, "--file", "-", "--cron", "x", "--yes"]
    rc, out, err = _run([*argv, "--json"] if as_json else argv, capsys)
    assert (rc, out) == (1, "")
    message = _error(err)["message"] if as_json else " ".join(err.split())
    assert "brief was updated" in message
    assert "schedule was not changed" in message
    assert "bad cron: x" in message


def test_brief_and_filters_applied_but_schedule_clear_refused_names_both(
    server, capsys, monkeypatch
) -> None:
    _stdin(monkeypatch, "B")
    server.routes[f"PATCH {BASE}/{P1}"] = _json(_detail(brief="B"))
    server.routes[f"PUT {BASE}/{P1}/filters"] = _json(_detail(brief="B"))
    server.routes[f"DELETE {SCHEDULE}"] = httpx.Response(
        409, json={"detail": "Nothing changed: a scheduled run is in progress; try again."}
    )
    rc, out, err = _run(
        [
            "research-projects",
            "edit",
            P1,
            "--file",
            "-",
            "--filter",
            "f",
            "--clear",
            "schedule",
            "--json",
        ],
        capsys,
    )
    assert (rc, out) == (1, "")
    error = _error(err)
    assert error["code"] == "conflict"
    assert "brief and filters were updated" in error["message"]
    assert "schedule was not changed" in error["message"]
    assert "a scheduled run is in progress" in error["message"]


@pytest.mark.parametrize(
    "failure",
    [
        lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=request)),
        lambda request: httpx.Response(504, json={"detail": "gateway timeout"}),
    ],
    ids=["read-timeout", "504"],
)
def test_brief_applied_but_schedule_unconfirmed_says_so(
    server, capsys, monkeypatch, failure
) -> None:
    _stdin(monkeypatch, "B")
    server.routes[f"PATCH {BASE}/{P1}"] = _json(_detail(brief="B"))
    server.routes[f"DELETE {SCHEDULE}"] = failure
    rc, out, err = _run(
        ["research-projects", "edit", P1, "--file", "-", "--clear", "schedule", "--json"], capsys
    )
    assert (rc, out) == (1, "")
    message = _error(err)["message"]
    assert "brief was updated" in message
    assert "schedule change could not be confirmed" in message
    assert "not changed" not in message
    assert f"pepper research-projects show {P1}" in message


def test_brief_applied_but_schedule_unreachable_says_which_part_applied(
    server, capsys, monkeypatch
) -> None:
    def dropped(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("reset", request=request)

    _stdin(monkeypatch, "B")
    server.routes[f"PATCH {BASE}/{P1}"] = _json(_detail(brief="B"))
    server.routes[f"DELETE {SCHEDULE}"] = dropped
    rc, _, err = _run(
        ["research-projects", "edit", P1, "--file", "-", "--clear", "schedule", "--json"], capsys
    )
    assert rc == 1
    assert _error(err)["code"] == "network"
    assert "schedule was not changed" in _error(err)["message"]


def test_a_changed_schedule_whose_read_back_fails_says_it_was_changed(server, capsys) -> None:
    server.routes[f"{BASE}/{P1}"] = [
        _json(_scheduled()),
        httpx.Response(503, json={"detail": "down"}),
    ]
    server.routes[f"PATCH {TASK_PATH}"] = _json({"id": TASK})
    rc, out, err = _run(["research-projects", "edit", P1, "--cron", "0 7 * * 1", "--json"], capsys)
    assert (rc, out) == (1, "")
    message = _error(err)["message"]
    assert "The schedule was updated, but reading the project back failed" in message
    assert f"pepper research-projects show {P1}" in message


@pytest.mark.parametrize("as_json", [True, False])
def test_a_combined_edit_whose_read_back_fails_names_every_saved_part(
    server, capsys, monkeypatch, as_json
) -> None:
    _stdin(monkeypatch, "B")
    server.routes[f"{BASE}/{P1}"] = [
        _json(_scheduled()),
        httpx.Response(503, json={"detail": "down"}),
    ]
    server.routes[f"PATCH {BASE}/{P1}"] = _json(_detail(brief="B"))
    server.routes[f"PUT {BASE}/{P1}/filters"] = _json(_detail(brief="B"))
    server.routes[f"PATCH {TASK_PATH}"] = _json({"id": TASK})
    argv = ["research-projects", "edit", P1, "--file", "-", "--filter", "f", "--cron", "0 7 * * 1"]
    rc, out, err = _run([*argv, "--json"] if as_json else argv, capsys)
    assert (rc, out) == (1, "")
    message = _error(err)["message"] if as_json else " ".join(err.split())
    assert "brief, filters and schedule were updated" in message
    assert "reading the project back failed" in message
    assert "down" in message
    assert f"pepper research-projects show {P1}" in message


@pytest.mark.parametrize("as_json", [False, True])
def test_schedule_writes_say_when_research_projects_are_switched_off(
    server, capsys, as_json
) -> None:
    off = httpx.Response(403, json={"detail": "off", "code": "research_projects_disabled"})
    server.routes[f"DELETE {SCHEDULE}"] = off
    server.routes[f"POST {SCHEDULE}"] = off
    _manual_only(server)
    for flags in (["--clear", "schedule"], ["--cron", CRON, "--yes"]):
        rc, out, err = _run(
            ["research-projects", "edit", P1, *flags, *(["--json"] if as_json else [])], capsys
        )
        assert (rc, out) == (1, "")
        assert "Research projects are not enabled on this Pepper server." in err
