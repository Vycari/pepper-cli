"""``pepper next``: events, todos in server order, fallbacks, --room/--source, --json shape."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from pepper_cli import cli, credentials, next_view
from pepper_cli.secret import SecretStr

NOW = datetime(2026, 10, 2, 16, 0, tzinfo=UTC)
TODAY = NOW.date()


def _at(minutes: int = 0, hours: int = 0, days: int = 0) -> str:
    return (NOW + timedelta(minutes=minutes, hours=hours, days=days)).isoformat()


def _day(days: int) -> str:
    return (NOW + timedelta(days=days)).date().isoformat()


def _todo(
    title: str,
    *,
    due: str | None = None,
    at: str | None = None,
    high: bool = False,
    remind: str | None = None,
    source: str = "user",
) -> dict:
    """A ``GET /api/v1/todos`` row. ``due`` is a day (date-only), ``at`` an instant."""
    raw = at or (f"{due}T00:00:00Z" if due else None)
    return {
        "id": title,
        "title": title,
        "details": None,
        "status": "open",
        "source": source,
        "priority": "high" if high else None,
        "due_date": raw,
        "due_is_date_only": None if raw is None else at is None,
        "created_at": "2026-10-01T00:00:00Z",
        "done_at": None,
        "source_ref": None,
        "remind_at": remind,
    }


def _event(title: str, start: int, end: int, detail: str | None = None) -> dict:
    return {
        "event_id": title,
        "title": title,
        "start": _at(start),
        "end": _at(end),
        "start_label": f"{start}m",
        "end_label": f"{end}m",
        "detail": detail,
    }


def _plan(events=()) -> dict:
    return {
        "now": NOW.isoformat(),
        "timezone": "UTC",
        "plans": [],
        "your_day": {
            "items": list(events),
            "tomorrow": {"items": [_event("Tomorrow", 1500, 1560)]},
        },
    }


@pytest.fixture(autouse=True)
def _creds(monkeypatch, tmp_path):
    monkeypatch.setattr(next_view, "_clock", NOW.astimezone)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))


class _Server:
    """Canned GETs by path; any request nobody registered (schedule-now, say) fails the test."""

    def __init__(self) -> None:
        self.routes: dict[str, httpx.Response] = {}
        self.seen: list[str] = []

    def add(self, path: str, body: object, status: int = 200) -> None:
        self.routes[path] = httpx.Response(status, json=body)

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", request
        self.seen.append(request.url.path)
        return self.routes[request.url.path]


@pytest.fixture
def server(monkeypatch) -> _Server:
    srv = _Server()
    srv.add("/api/v1/whoami", {"user_id": "u", "display_name": "U", "timezone": "UTC"})
    real = httpx.Client

    def make_client(**kw: Any) -> httpx.Client:
        return real(transport=httpx.MockTransport(srv.handle), **kw)

    monkeypatch.setattr(httpx, "Client", make_client)
    return srv


def _run(capsys, *argv: str) -> str:
    assert cli.main(["next", *argv]) == 0
    return capsys.readouterr().out


def _mock(server, todos=(), events=(), plan: bool = True) -> None:
    if plan:
        server.add("/api/v1/calendars/schedule-plan", _plan(events))
    else:
        server.add("/api/v1/calendars/schedule-plan", {"detail": "nope"}, 404)
    server.add("/api/v1/todos", list(todos))


def _json(server, capsys, *argv: str, todos=(), events=(), plan: bool = True) -> dict:
    _mock(server, todos, events, plan)
    return json.loads(_run(capsys, "--json", *argv))


def _titles(data: dict) -> list[str]:
    return [t["title"] for t in data["todos"]["items"]]


def test_live_event_is_now_with_time_left_and_the_next_event(server, capsys) -> None:
    events = [_event("Past", -90, -30), _event("Standup", -10, 20), _event("Dentist", 60, 120)]
    data = _json(server, capsys, events=events)
    assert data["source"] == "todos" and data["note"] is None
    assert data["now"]["title"] == "Standup" and data["now"]["minutes_left"] == 20
    assert data["next_event"]["title"] == "Dentist"
    assert data["next_event"]["in_minutes"] == 60
    assert [r["title"] for r in data["calendar"]] == ["Dentist"]  # tomorrow and past are not shown


def test_no_live_event_means_no_now_and_todos_carry_the_servers_fields(server, capsys) -> None:
    data = _json(server, capsys, todos=[_todo("A", due=_day(1), high=True, remind=_at(30))])
    assert data["now"] is None and data["next_event"] is None
    assert data["todos"]["items"] == [
        {
            "id": "A",
            "title": "A",
            "due": f"{_day(1)}T00:00:00Z",
            "due_is_date_only": True,
            "remind_at": _at(30),
            "priority": "high",
            "source": "user",
            "next_action": None,
            "next_action_suggestion": None,
        }
    ]


def test_todos_keep_the_servers_order_and_are_capped(server, capsys) -> None:
    """No reordering: an overdue item stays where the server put it."""
    todos = [
        _todo("far high", due=_day(90), high=True),
        _todo("late", due=_day(-3)),
        _todo("high nodue", high=True),
        _todo("today", due=_day(0)),
    ]
    todos += [_todo(f"filler{n}") for n in range(12)]
    data = _json(server, capsys, todos=todos)
    assert _titles(data)[:4] == ["far high", "late", "high nodue", "today"]
    everything = json.loads(_run(capsys, "--json", "--all"))
    assert _titles(everything) == [t["title"] for t in todos]
    assert data["todos"]["count"] == 16 and len(data["todos"]["items"]) == 10
    assert len(everything["todos"]["items"]) == 16


def test_empty_plans_are_not_needed(server, capsys) -> None:
    """The bug in #50: schedule-plan answers with no plans, and todos still show."""
    data = _json(server, capsys, todos=[_todo("A", due=_day(1))])
    assert _titles(data) == ["A"]


def test_source_narrows_todos_but_keeps_events(server, capsys) -> None:
    todos = [_todo("Mine"), _todo("Theirs", source="gmail-scan")]
    data = _json(server, capsys, "--source", "GMAIL", todos=todos, events=[_event("E", -5, 30)])
    assert _titles(data) == ["Theirs"]
    assert data["now"]["title"] == "E"


def test_room_reads_that_rooms_todo_list(server, capsys) -> None:
    gid = _rooms(server, "Trip")["Trip"]
    server.add("/api/v1/calendars/schedule-plan", _plan())
    server.add(f"/api/v1/groups/{gid}/todos", [_todo("Book hotel")])
    assert _titles(json.loads(_run(capsys, "--json", "--room", "trip"))) == ["Book hotel"]
    assert f"/api/v1/groups/{gid}/todos" in server.seen


def test_missing_plan_falls_back_to_the_calendar_range(server, capsys) -> None:
    server.add("/api/v1/whoami", {"user_id": "u", "display_name": "U", "timezone": "UTC"})
    server.add(
        "/api/v1/calendars/range",
        {
            "calendars": [{"id": "me", "is_primary": True}, {"id": "other"}],
            "days": [
                {
                    "date": TODAY.isoformat(),
                    "events": [
                        {
                            "id": "1",
                            "calendar_id": "me",
                            "title": "Lunch",
                            "start": _at(30),
                            "end": _at(90),
                        },
                        {
                            "id": "2",
                            "calendar_id": "other",
                            "title": "Not mine",
                            "start": _at(40),
                            "end": _at(50),
                        },
                        {
                            "id": "3",
                            "calendar_id": "me",
                            "title": "All day",
                            "all_day": True,
                            "start": _at(0),
                            "end": _at(0),
                        },
                        {
                            "id": "4",
                            "calendar_id": "me",
                            "title": "Gone",
                            "start": _at(-90),
                            "end": _at(-30),
                        },
                    ],
                }
            ],
        },
    )
    data = _json(server, capsys, todos=[_todo("A")], plan=False)
    assert [r["title"] for r in data["calendar"]] == ["Lunch"]
    # The range rows carry no labels, so the CLI formats them (the Windows crash path).
    assert data["calendar"][0]["start_label"] == "4:30pm"
    assert data["calendar"][0]["end_label"] == "5:30pm"
    assert data["note"] is None and _titles(data) == ["A"]
    assert "/api/v1/calendars/schedule-plan" in server.seen


@pytest.mark.parametrize(
    ("iso", "label"),
    [
        ("2026-10-02T00:05:00+00:00", "12:05am"),
        ("2026-10-02T09:30:00+00:00", "9:30am"),
        ("2026-10-02T12:00:00+00:00", "12:00pm"),
        ("2026-10-02T23:59:00+00:00", "11:59pm"),
    ],
)
def test_clock_label_has_no_platform_specific_strftime(iso: str, label: str) -> None:
    """``%-I`` raises on Windows; the label is built by hand and reads the same everywhere."""
    assert next_view._label(iso, UTC) == label


def test_calendar_down_still_shows_todos_with_a_note(server, capsys) -> None:
    server.add("/api/v1/whoami", {"user_id": "u", "display_name": "U", "timezone": "UTC"})
    server.add("/api/v1/calendars/range", {"detail": "boom"}, 500)
    data = _json(server, capsys, todos=[_todo("A")], plan=False)
    assert data["note"] == next_view.NO_CALENDAR_NOTE and data["calendar"] == []
    assert _titles(data) == ["A"]


def test_calendar_down_is_said_under_now_not_shown_as_a_free_slot(server, capsys) -> None:
    server.add("/api/v1/calendars/range", {"detail": "boom"}, 500)
    _mock(server, [_todo("A")], plan=False)
    out = _run(capsys)
    assert "(no event right now)" not in out
    assert out.index("Now") < out.index(next_view.NO_CALENDAR_NOTE) < out.index("Todos")


def test_a_non_json_plan_body_falls_back_instead_of_aborting(server, capsys) -> None:
    """A captive portal answers 200 with HTML; that is a failed read, not a crash."""
    server.add("/api/v1/calendars/range", {"days": [], "calendars": []})
    _mock(server, [_todo("A")])
    server.routes["/api/v1/calendars/schedule-plan"] = httpx.Response(
        200, text="<html>login</html>"
    )
    data = json.loads(_run(capsys, "--json"))
    assert _titles(data) == ["A"] and data["note"] is None
    assert "/api/v1/calendars/range" in server.seen


def test_plan_and_whoami_both_down_still_shows_the_todos(server, capsys) -> None:
    server.add("/api/v1/whoami", {"detail": "unavailable"}, 503)
    data = _json(server, capsys, todos=[_todo("A")], plan=False)
    assert data["note"] == next_view.NO_CALENDAR_NOTE and _titles(data) == ["A"]
    assert "/api/v1/calendars/range" not in server.seen


def test_next_never_writes(server, capsys) -> None:
    _json(server, capsys, todos=[_todo("A")], events=[_event("E", 5, 30)])
    # _Server asserts every request is a GET; schedule-now is not registered either.
    assert "/api/v1/calendars/schedule-now" not in server.seen


def test_first_flag_is_gone(server, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["next", "--first", "urgent"])
    assert exc.value.code == 2
    assert "--first" in capsys.readouterr().err


def test_todo_leads_with_its_next_action_and_shows_a_suggestion(server, capsys) -> None:
    acted = _todo("Vet") | {
        "next_action": {"text": "Email the vet", "by": "model", "updated_at": "2026-10-20T00:00Z"},
        "next_action_suggestion": {"text": "Book a slot", "updated_at": "2026-10-21T00:00Z"},
    }
    _mock(server, [acted, _todo("Plain")])
    lines = [ln for ln in _run(capsys).splitlines() if "Vet" in ln or "Plain" in ln or "Or:" in ln]
    assert lines == [
        " 1. Email the vet  for: Vet",
        "      Or: Book a slot",
        " 2. Plain",
    ]


def test_json_shape_is_stable(server, capsys) -> None:
    data = _json(server, capsys, todos=[_todo("A")], events=[_event("E", 5, 30)])
    assert list(data) == [
        "generated_at",
        "timezone",
        "source",
        "note",
        "now",
        "next_event",
        "calendar",
        "todos",
    ]
    assert set(data["todos"]) == {"count", "items"}
    assert list(data["next_event"]) == [
        "id",
        "title",
        "start",
        "end",
        "start_label",
        "end_label",
        "detail",
        "in_minutes",
    ]


def test_text_view_reads_in_sections_and_labels_the_servers_fields(server, capsys) -> None:
    _mock(
        server,
        [
            _todo("Scan", source="gmail-scan"),
            _todo("Pay tax", due=_day(-2), high=True),
            _todo("Timed", at=_at(minutes=-30)),
            _todo("Later", due=_day(3)),
        ],
        [_event("Standup", -10, 20, "Room 1, 2 Main St"), _event("Later event", 60, 90)],
    )
    out = _run(capsys)
    assert out.index("Now") < out.index("Today's calendar") < out.index("Todos")
    assert "Standup" in out and "20m left" in out and "@ Room 1" in out
    assert "next: Later event at" in out
    assert out.index("Scan") < out.index("Pay tax")  # server order, not priority order
    assert f"due {_day(-2)} (overdue)" in out and "priority high" in out
    assert "from gmail-scan" in out
    lines = {
        ln.split(".", 1)[1].split("  ")[0].strip(): ln for ln in out.splitlines() if ". " in ln
    }
    assert "(overdue)" in lines["Timed"] and "(overdue)" not in lines["Later"]


def test_timed_due_reads_on_the_accounts_clock_not_the_machines(
    server, capsys, monkeypatch
) -> None:
    """Events are labelled in the account's zone; a timed due beside them must be too."""
    from pepper_cli import render

    monkeypatch.setattr(render, "_local_tz", lambda: UTC)  # the machine says UTC
    server.add("/api/v1/calendars/schedule-plan", {**_plan(), "timezone": "America/Los_Angeles"})
    server.add("/api/v1/todos", [_todo("Call", at="2026-10-02T18:00:00+00:00")])
    out = _run(capsys)
    assert "due 2026-10-02 11:00" in out and "18:00" not in out


def test_timed_due_across_a_dst_change_uses_the_zones_rules_not_todays_offset(
    server, capsys
) -> None:
    """Today is PDT (-07:00); Nov 2 is PST (-08:00). 17:00Z that day is 09:00, not 10:00."""
    server.add("/api/v1/calendars/schedule-plan", {**_plan(), "timezone": "America/Los_Angeles"})
    server.add("/api/v1/todos", [_todo("Renew", at="2026-11-02T17:00:00+00:00")])
    data = json.loads(_run(capsys, "--json"))
    assert data["timezone"] == "America/Los_Angeles"
    assert "due 2026-11-02 09:00" in _run(capsys)


def test_plan_without_a_timezone_takes_the_zone_from_whoami(server, capsys) -> None:
    plan = _plan()
    del plan["timezone"]
    server.add("/api/v1/calendars/schedule-plan", plan)
    server.add("/api/v1/todos", [])
    server.add("/api/v1/whoami", {"user_id": "u", "display_name": "U", "timezone": "Europe/Paris"})
    data = json.loads(_run(capsys, "--json"))
    assert data["timezone"] == "Europe/Paris"
    assert data["generated_at"].endswith("+02:00")


def test_no_account_zone_shows_utc_labelled_and_never_overdue(server, capsys) -> None:
    """Plan and whoami both down: no guessing from the machine's zone."""
    server.add("/api/v1/whoami", {"detail": "unavailable"}, 503)
    _mock(
        server,
        [_todo("Late", due=_day(-3)), _todo("Timed", at="2026-10-01T09:30:00+00:00")],
        plan=False,
    )
    out = _run(capsys)
    assert "(overdue)" not in out
    assert f"due {_day(-3)}" in out and "due 2026-10-01 09:30 UTC" in out
    data = json.loads(_run(capsys, "--json"))
    assert data["timezone"] is None and data["note"] == next_view.NO_CALENDAR_NOTE


@pytest.mark.parametrize(
    "bad",
    [
        {"title": "X", "start": _at(10), "end": "not-a-time"},
        {"title": "X", "start": "2026-10-02T17:00:00", "end": "2026-10-02T18:00:00"},  # naive
    ],
)
def test_a_calendar_that_cant_be_parsed_is_a_calendar_that_couldnt_be_read(
    server, capsys, bad
) -> None:
    _mock(server, [_todo("A")], [bad])
    data = json.loads(_run(capsys, "--json"))
    assert data["note"] == next_view.NO_CALENDAR_NOTE and _titles(data) == ["A"]
    assert data["now"] is None and data["calendar"] == []
    assert next_view.NO_CALENDAR_NOTE in _run(capsys)


def test_a_second_event_running_now_is_marked_in_progress(server, capsys) -> None:
    _mock(
        server,
        events=[_event("Standup", -20, 10), _event("Review", -20, 70), _event("Lunch", 60, 90)],
    )
    cal = _run(capsys).split("Today's calendar")[1].split("Todos")[0].splitlines()
    review = next(ln for ln in cal if "Review" in ln)
    lunch = next(ln for ln in cal if "Lunch" in ln)
    assert "(in progress)" in review and "(in progress)" not in lunch


def test_text_view_when_empty(server, capsys) -> None:
    _mock(server)
    out = _run(capsys)
    assert "(no event right now)" in out and "(nothing open)" in out


def test_text_view_hint_when_more_than_shown(server, capsys) -> None:
    _mock(server, [_todo(f"t{n}") for n in range(12)])
    assert "... 2 more (--all)" in _run(capsys)


def _rooms(server, *names: str) -> dict[str, str]:
    ids = {n: f"00000000-0000-0000-0000-{i:012d}" for i, n in enumerate(names, start=1)}
    rows = [
        {
            "group_id": gid,
            "display_name": n,
            "color": "#000",
            "sub": "",
            "member_count": 2,
            "archived": False,
        }
        for n, gid in ids.items()
    ]
    server.add(
        "/api/v1/groups",
        {
            "enabled": True,
            "degraded": False,
            "total": len(rows),
            "archived_total": 0,
            "groups": rows,
        },
    )
    return ids


def test_room_that_matches_nothing_is_exit_2_not_an_empty_list(server, capsys) -> None:
    _rooms(server, "Trip")
    assert cli.main(["next", "--room", "nope"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "no room named 'nope'" in captured.err


def test_room_that_matches_two_rooms_is_ambiguous(server, capsys) -> None:
    _rooms(server, "Trip A", "Trip B")
    assert cli.main(["next", "--json", "--room", "trip"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    err = json.loads(captured.err)["error"]
    assert err["code"] == "ambiguous" and "Trip A" in err["message"]


def test_room_miss_under_json_is_a_j8_not_found(server, capsys) -> None:
    _rooms(server, "Trip")
    assert cli.main(["next", "--json", "--room", "zzz"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "not_found"


def test_room_uuid_that_is_not_one_of_yours_is_not_found(server, capsys) -> None:
    _rooms(server, "Trip")
    stranger = "00000000-0000-0000-0000-0000000000ff"
    assert cli.main(["next", "--json", "--room", stranger]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    err = json.loads(captured.err)["error"]
    assert err["code"] == "not_found" and stranger in err["message"]


def test_room_uuid_matches_case_insensitively_and_uses_the_canonical_id(server, capsys) -> None:
    gid = _rooms(server, "Trip")["Trip"]
    server.add("/api/v1/calendars/schedule-plan", _plan())
    server.add(f"/api/v1/groups/{gid}/todos", [_todo("B")])
    data = json.loads(_run(capsys, "--json", "--room", gid.upper()))
    assert _titles(data) == ["B"]


@pytest.mark.parametrize(
    ("flags", "code", "said"),
    [
        ({"enabled": False, "degraded": False}, "forbidden", "Rooms are not enabled"),
        ({"enabled": True, "degraded": True}, "server", "could not read your rooms"),
    ],
)
def test_room_lookup_when_the_rooms_list_is_unavailable_is_not_a_miss(
    server, capsys, flags, code, said
) -> None:
    server.add("/api/v1/groups", {**flags, "total": 0, "archived_total": 0, "groups": []})
    assert cli.main(["next", "--json", "--room", "trip"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    err = json.loads(captured.err)["error"]
    assert err["code"] == code and said in err["message"]
    assert cli.main(["next", "--room", "trip"]) == 1
    assert said in capsys.readouterr().err
