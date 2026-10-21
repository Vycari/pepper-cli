"""``research-projects`` and ``research-items`` read commands against a faked server.

HTTP is mocked at the transport, so these cover the whole path: argument parsing, name and
number resolution, the request that goes out, the human and ``--json`` output, and the exit
code and J8 line for each failure in ``planning/research-projects.md`` §8.
"""

from __future__ import annotations

import json

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
        return routes.get(request.url.path, httpx.Response(404, json={"detail": "nope"}))

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
