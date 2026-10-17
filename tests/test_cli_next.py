"""``pepper next``: events, ranked todos, deadlines, fallbacks, --room/--source, --json shape."""

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


def test_live_event_is_now_with_time_left_and_the_next_commitment(server, capsys) -> None:
    events = [_event("Past", -90, -30), _event("Standup", -10, 20), _event("Dentist", 60, 120)]
    data = _json(server, capsys, events=events)
    assert data["source"] == "todos" and data["note"] is None
    assert data["now"]["title"] == "Standup" and data["now"]["minutes_left"] == 20
    assert data["next_commitment"]["title"] == "Dentist"
    assert data["next_commitment"]["in_minutes"] == 60
    assert [r["title"] for r in data["calendar"]] == ["Dentist"]  # tomorrow and past are not shown


def test_no_live_event_means_no_now_and_todos_get_no_times(server, capsys) -> None:
    data = _json(server, capsys, todos=[_todo("A")])
    assert data["now"] is None and data["next_commitment"] is None
    assert set(data["todos"]["items"][0]) == {
        "id",
        "title",
        "due",
        "remind_at",
        "source",
        "high_priority",
        "score",
        "reasons",
        "passed",
        "source_time",
    }


def test_todos_are_ranked_overdue_then_today_then_priority_and_capped(server, capsys) -> None:
    todos = [
        _todo("far high", due=_day(90), high=True),
        _todo("soon high", due=_day(2), high=True),
        _todo("today", due=_day(0)),
        _todo("late", due=_day(-3)),
        _todo("high nodue", high=True),
    ]
    todos += [_todo(f"filler{n}") for n in range(12)]
    data = _json(server, capsys, todos=todos)
    assert _titles(data)[:4] == ["late", "today", "soon high", "high nodue"]
    everything = json.loads(_run(capsys, "--json", "--all"))
    assert _titles(everything)[4] == "far high"
    assert data["todos"]["count"] == 17 and len(data["todos"]["items"]) == 10
    assert len(everything["todos"]["items"]) == 17


def test_empty_plans_are_not_needed(server, capsys) -> None:
    """The bug in #50: schedule-plan answers with no plans, and todos still show."""
    data = _json(server, capsys, todos=[_todo("A", due=_day(1))])
    assert _titles(data) == ["A"]
    assert data["todos"]["items"][0]["reasons"] == ["due tomorrow"]


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
    assert data["note"] is None and _titles(data) == ["A"]
    assert "/api/v1/calendars/schedule-plan" in server.seen


def test_calendar_down_still_shows_todos_with_a_note(server, capsys) -> None:
    server.add("/api/v1/whoami", {"user_id": "u", "display_name": "U", "timezone": "UTC"})
    server.add("/api/v1/calendars/range", {"detail": "boom"}, 500)
    data = _json(server, capsys, todos=[_todo("A")], plan=False)
    assert data["note"] == next_view.NO_CALENDAR_NOTE and data["calendar"] == []
    assert _titles(data) == ["A"]


def test_next_never_writes(server, capsys) -> None:
    _json(server, capsys, todos=[_todo("A")], events=[_event("E", 5, 30)])
    # _Server asserts every request is a GET; schedule-now is not registered either.
    assert "/api/v1/calendars/schedule-now" not in server.seen


def test_first_flag_is_gone(server, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["next", "--first", "urgent"])
    assert exc.value.code == 2
    assert "--first" in capsys.readouterr().err


def test_json_shape_is_stable(server, capsys) -> None:
    data = _json(server, capsys, todos=[_todo("A")], events=[_event("E", 5, 30)])
    assert list(data) == [
        "generated_at",
        "source",
        "note",
        "now",
        "next_commitment",
        "due_today",
        "calendar",
        "todos",
    ]
    assert set(data["todos"]) == {"count", "items"}


def test_text_view_reads_in_sections(server, capsys) -> None:
    _mock(
        server,
        [_todo("Pay tax", due=_day(-2), high=True), _todo("Scan", source="gmail-scan")],
        [_event("Standup", -10, 20, "Room 1, 2 Main St"), _event("Later", 60, 90)],
    )
    out = _run(capsys)
    assert out.index("Now") < out.index("Today's calendar") < out.index("Todos, prioritized")
    assert "Standup" in out and "20m left" in out and "@ Room 1" in out
    assert "Pay tax" in out and "overdue 2d, high priority" in out and "(gmail-scan)" in out
    assert "next: Later at" in out


def test_text_view_when_empty(server, capsys) -> None:
    _mock(server)
    out = _run(capsys)
    assert "(no event right now)" in out and "(nothing open)" in out


def test_text_view_hint_when_more_than_shown(server, capsys) -> None:
    _mock(server, [_todo(f"t{n}") for n in range(12)])
    assert "... 2 more (--all)" in _run(capsys)


# --- rank -------------------------------------------------------------------------------


def _r(**kw: Any) -> dict:
    return {"title": kw.pop("title", "x"), "due": kw.pop("due", {}), **kw}


def _d(offset: int) -> dict:
    return {"date": (TODAY + timedelta(days=offset)).isoformat()}


def test_rank_reasons_name_the_factors() -> None:
    ranked = next_view.rank(
        [
            _r(title="a", due=_d(-2), high_priority=True),
            _r(title="b", due=_d(0)),
            _r(title="c", due=_d(5)),
        ],
        TODAY,
    )
    assert {t["title"]: t["reasons"] for t in ranked} == {
        "a": ["overdue 2d", "high priority"],
        "b": ["due today"],
        "c": [f"due {(TODAY + timedelta(days=5)):%b} {(TODAY + timedelta(days=5)).day}"],
    }


def test_rank_is_stable_on_ties() -> None:
    ranked = next_view.rank([_r(title=t) for t in "abc"], TODAY)
    assert [t["title"] for t in ranked] == ["a", "b", "c"]


def test_high_priority_far_out_ranks_below_work_due_this_week() -> None:
    ranked = next_view.rank(
        [_r(title="far", due=_d(90), high_priority=True), _r(title="week", due=_d(3))], TODAY
    )
    assert [t["title"] for t in ranked] == ["week", "far"]


def test_due_boosts_halve_every_half_life_and_overdue_and_today_keep_the_top() -> None:
    half = int(next_view.HALF_LIFE_DAYS)
    today_row, later_row = next_view.rank(
        [_r(due=_d(0), high_priority=True), _r(due=_d(half), high_priority=True)], TODAY
    )
    assert today_row["score"] == next_view.W_DUE_TODAY + next_view.W_HIGH_PRIORITY
    assert later_row["score"] == pytest.approx(
        (next_view.W_HIGH_PRIORITY + next_view.W_DUE_SOON) / 2, abs=0.1
    )


def test_high_priority_without_a_due_date_keeps_a_moderate_flat_boost() -> None:
    [row] = next_view.rank([_r(high_priority=True)], TODAY)
    assert row["score"] == next_view.W_HIGH_NO_DUE


# --- timed deadlines and reminders ------------------------------------------------------


def _timed(title: str, minutes: int, **kw: Any) -> dict:
    return _todo(title, at=_at(minutes), **kw)


def test_timed_deadlines_join_the_calendar_and_passed_ones_get_a_grace_period(
    server, capsys
) -> None:
    todos = [_timed("soon", 30), _timed("missed", -60), _todo("noon", due=_day(0))]
    data = _json(server, capsys, todos=todos, events=[_event("Later event", 120, 150)])
    cal = {r["title"]: r for r in data["calendar"]}
    assert cal["soon"]["kind"] == "deadline" and cal["soon"]["source_time"] == "due"
    assert cal["soon"]["passed"] is False and cal["missed"]["passed"] is True
    assert [r["title"] for r in data["calendar"]] == ["missed", "soon", "Later event"]
    assert data["due_today"] == [{"id": "noon", "title": "noon"}]
    items = {t["title"]: t for t in data["todos"]["items"]}
    assert items["missed"]["passed"] and "was due" in items["missed"]["reasons"][0]
    assert items["missed"]["score"] == 0  # no boost for a passed deadline
    assert data["next_commitment"]["title"] == "soon"  # never the passed one


def test_passed_deadline_drops_off_once_the_grace_period_is_over(server, capsys) -> None:
    gone = _timed("gone", -(next_view.DEADLINE_GRACE_HOURS * 60 + 1))
    data = _json(server, capsys, todos=[gone, _timed("kept", -(23 * 60))])
    assert _titles(data) == ["kept"]
    assert data["calendar"] == []  # yesterday's deadline is not on today's calendar


def test_a_future_reminder_is_the_next_commitment_before_a_later_event(server, capsys) -> None:
    todos = [_todo("Call mum", remind=_at(20))]
    data = _json(server, capsys, todos=todos, events=[_event("Event", 90, 120)])
    nxt = data["next_commitment"]
    assert nxt["title"] == "Call mum" and nxt["source_time"] == "remind"
    assert data["todos"]["items"][0]["reasons"][0].startswith("reminder")


def test_a_reminded_todo_in_the_grace_period_reads_reminded_and_is_not_boosted(
    server, capsys
) -> None:
    data = _json(server, capsys, todos=[_todo("Call mum", remind=_at(-30))])
    [item] = data["todos"]["items"]
    assert item["passed"] and item["reasons"] == ["reminded 3:30pm"] and item["score"] == 0
    assert data["next_commitment"] is None


def test_a_reminder_older_than_the_grace_period_is_gone(server, capsys) -> None:
    data = _json(server, capsys, todos=[_todo("Old", remind=_at(hours=-25))])
    assert data["todos"]["count"] == 0


def test_a_timed_due_wins_over_a_reminder(server, capsys) -> None:
    data = _json(server, capsys, todos=[_todo("X", at=_at(60), remind=_at(10))])
    [row] = data["calendar"]
    assert row["source_time"] == "due" and row["start"] == _at(60)


def test_text_view_marks_due_remind_and_passed_rows(server, capsys) -> None:
    _mock(
        server,
        [_timed("deadline", 30), _todo("ping", remind=_at(45)), _timed("missed", -60)],
    )
    out = _run(capsys)
    assert "due: deadline" in out and "remind: ping" in out and "due: missed" in out
    assert "next: due: deadline at" in out


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
