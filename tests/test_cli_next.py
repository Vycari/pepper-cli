"""``pepper next``: plan ordering, left_out and 404 fallbacks, --room, --json shape."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from pepper_cli import cli, credentials, next_view
from pepper_cli.secret import SecretStr

NOW = datetime(2026, 10, 2, 16, 0, tzinfo=UTC)
DAY = NOW.date().isoformat()
NEXT_UP = {"title": "Standup", "start_label": "9am", "end_label": "9:30am", "countdown": "in 5 min"}


def _at(minutes: int = 0, hours: int = 0) -> str:
    return (NOW + timedelta(minutes=minutes, hours=hours)).isoformat()


def _todo(title: str, room: str, due: str | None = None) -> dict:
    return {
        "todo_id": title,
        "title": title,
        "source_label": room,
        "reason": f"why {title}",
        "estimate_label": "about 30 min",
        "due": {"label": f"Due {due}" if due else "No due date", "date": due, "at": None},
    }


def _block(title: str, room: str, start: int, end: int) -> dict:
    return {**_todo(title, room), "type": "todo", "start": _at(start), "end": _at(end)}


def _plan(blocks: list[dict], left_out: list[dict] | None = None) -> dict:
    tomorrow = (NOW + timedelta(days=1)).date().isoformat()
    return {
        "now": NOW.isoformat(),
        "active": "urgent",
        "next_up": NEXT_UP,
        "plans": [
            {
                "philosophies": ["urgent"],
                "summary": "Nothing fits in the time that is left today.",
                "blocks": blocks,
                "left_out": left_out or [],
            }
        ],
        "more": {"count": 7, "items": [_todo("Water plants", "Personal", tomorrow)]},
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


def _mock_plan(server, plan: dict, cards: list[dict] | None = None) -> None:
    server.add("/api/v1/calendars/schedule-plan", plan)
    server.add("/api/v1/calendars/planning", {"cards": cards or []})


def test_plan_keeps_order_drops_past_blocks_and_dedupes_due_soon(server, capsys) -> None:
    cards = [
        {"kind": "email_deadline", "title": "Water plants", "when": _at(hours=20)},
        {"kind": "email_deadline", "title": "File form", "when": _at(hours=5)},
    ]
    blocks = [_block("Past", "Pepper", -60, -30), _block("B", "Pepper", 10, 40)]
    _mock_plan(server, _plan([*blocks, _block("A", "Home", 40, 70)]), cards)
    data = json.loads(_run(capsys, "--json"))
    assert [i["title"] for i in data["do_next"]["items"]] == ["B", "A"]
    assert [i["title"] for i in data["due_soon"]] == ["Water plants", "File form"]  # once each
    assert data["later"] == {"count": 6, "items": []}
    assert data["source"] == "schedule-plan" and data["now"][0]["title"] == "Standup"


def test_nothing_ahead_shows_summary_then_left_out_in_order(server, capsys) -> None:
    left_out = [_todo("Z", "Pepper"), _todo("Y", "Pepper")]
    _mock_plan(server, _plan([_block("Past", "Pepper", -60, -30)], left_out))
    out = _run(capsys)
    assert out.index("Nothing fits") < out.index(" 1. Z") < out.index(" 2. Y")
    assert out.index("Now") < out.index("Do next")


def test_unplaced_left_out_lands_in_later_when_blocks_are_ahead(server, capsys) -> None:
    _mock_plan(server, _plan([_block("B", "Pepper", 10, 40)], [_todo("Unplaced", "Pepper")]))
    data = json.loads(_run(capsys, "--json"))
    assert data["later"]["count"] == 7
    assert [i["title"] for i in data["later"]["items"]] == ["Unplaced"]


def test_due_soon_window_is_48_elapsed_hours_across_dst() -> None:
    # 2026-11-01 02:00 is a fall-back in New York: 48 elapsed hours end at 11:00 on Nov 2, not noon.
    now = datetime(2026, 10, 31, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    assert next_view._in_window(None, "2026-11-02T10:30:00-05:00", now)
    assert not next_view._in_window(None, "2026-11-02T11:30:00-05:00", now)


def test_source_narrows_by_case_insensitive_prefix(server, capsys) -> None:
    _mock_plan(server, _plan([_block("B", "Pepper Design", 10, 40), _block("A", "Home", 40, 70)]))
    data = json.loads(_run(capsys, "--json", "--source", "pepper"))
    assert [i["title"] for i in data["do_next"]["items"]] == ["B"]
    assert data["due_soon"] == [] and data["later"]["count"] == 0


def test_404_falls_back_to_unranked_todos_and_never_calls_schedule_now(server, capsys) -> None:
    server.add("/api/v1/calendars/schedule-plan", {}, 404)
    event = {"summary": "Dentist", "start": _at(hours=1), "calendar_id": "me"}
    shared = {"summary": "Mrs Gee", "start": _at(minutes=-5), "end": _at(hours=2), "is_next": True}
    cals = [{"id": "me", "is_primary": True}, {"id": "sh", "writable": False, "shared": True}]
    free = {"summary": "Focus", "start": _at(minutes=10), "busy": False, "calendar_id": "me"}
    days = [{"date": DAY, "events": [{**shared, "calendar_id": "sh"}, free, event]}]
    server.add("/api/v1/calendars/range", {"calendars": cals, "days": days})
    server.add("/api/v1/calendars/planning", {}, 404)
    row = {"status": "open", "source": "user", "created_at": _at(), "due_date": None}
    server.add(
        "/api/v1/todos",
        [{**row, "id": "t1", "title": "First"}, {**row, "id": "t2", "title": "Second"}],
    )
    out = _run(capsys)
    assert "unranked: Pepper's Schedule ranking isn't available" in out
    assert "Dentist" in out and "Mrs Gee" not in out and "Focus" not in out
    assert out.index("First") < out.index("Second")
    assert "/api/v1/calendars/schedule-plan" in server.seen  # and nothing unregistered ran


def test_json_shape_is_stable(server, capsys) -> None:
    _mock_plan(server, _plan([_block("B", "Pepper", 10, 40)]))
    data = json.loads(_run(capsys, "--json"))
    assert list(data) == ["generated_at", "source", "plan", "now", "do_next", "due_soon", "later"]
    assert set(data["do_next"]) == {"note", "items"} and set(data["later"]) == {"count", "items"}


def _three(fast_blocks: list[dict], same: bool = False) -> dict:
    keys = {"urgent": "Urgent first", "important": "Important first", "fast": "Fast first"}
    urgent = [_block("U", "Pepper", 10, 40), _block("V", "Pepper", 40, 70)]
    plan = _plan(urgent)
    plan["philosophies"] = [{"key": k, "name": n, "subtitle": f"{n}."} for k, n in keys.items()]
    base = {"summary": "s", "left_out": [], "agree_note": "All three give the same plan today"}
    plan["plans"] = [
        {**base, "philosophies": ["urgent"], "same_as": None, "blocks": urgent},
        {**base, "philosophies": ["important"], "same_as": "urgent", "blocks": []},
        {
            **base,
            "philosophies": ["fast"],
            "same_as": "urgent" if same else None,
            "blocks": [] if same else fast_blocks,
        },
    ]
    return plan


def test_first_picks_plan_through_same_as(server, capsys) -> None:
    _mock_plan(server, _three([_block("F", "Pepper", 10, 40)]))
    data = json.loads(_run(capsys, "--first", "important", "--json"))
    assert [i["title"] for i in data["do_next"]["items"]] == ["U", "V"]  # via same_as
    assert data["plan"]["first"] == "important" and not data["plan"]["chosen_by_pepper"]
    fast = next(o for o in data["plan"]["others"] if o["first"] == "fast")
    assert (fast["first_block"], fast["blocks"]) == ("F", 1)
    default = json.loads(_run(capsys, "--json"))
    assert default["plan"]["first"] == "urgent" and default["plan"]["chosen_by_pepper"]


def test_comparison_line_when_plans_differ_and_agree_note_when_not(server, capsys) -> None:
    _mock_plan(server, _three([_block("F", "Pepper", 10, 40)]))
    out = _run(capsys, "--first", "fast")
    assert "Fast first" in out and "Urgent first starts with U and fits 2 blocks" in out
    _mock_plan(server, _three([], same=True))
    out = _run(capsys)
    assert "Urgent first, Pepper's choice today" in out
    assert "All three give the same plan today" in out


def test_first_on_fallback_notes_on_stderr_and_still_lists(server, capsys) -> None:
    for path in ("schedule-plan", "range", "planning"):
        server.add(f"/api/v1/calendars/{path}", {}, 404)
    server.add("/api/v1/todos", [])
    assert cli.main(["next", "--first", "fast"]) == 0
    cap = capsys.readouterr()
    assert "plans aren't available" in cap.err and "Do next" in cap.out


def test_room_items_carry_their_room_id_and_room_accepts_it(server, capsys) -> None:
    gid = _rooms(server, "Trip")["Trip"]
    room = {**_block("B", "Trip", 10, 40), "principal": gid}
    _mock_plan(server, _plan([room, _block("A", "Home", 40, 70)]))
    data = json.loads(_run(capsys, "--json", "--room", gid))
    assert [(i["title"], i["principal"]) for i in data["do_next"]["items"]] == [("B", gid)]
    assert f"room {gid}" in _run(capsys)


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


def test_room_resolves_a_name_to_its_id(server, capsys) -> None:
    ids = _rooms(server, "Trip", "Home")
    room = {**_block("B", "Trip", 10, 40), "principal": ids["Trip"]}
    other = {**_block("A", "Home", 40, 70), "principal": ids["Home"]}
    _mock_plan(server, _plan([room, other]))
    data = json.loads(_run(capsys, "--json", "--room", "trip"))
    assert [i["title"] for i in data["do_next"]["items"]] == ["B"]


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
    room = {**_block("B", "Trip", 10, 40), "principal": gid}
    _mock_plan(server, _plan([room, _block("A", "Home", 40, 70)]))
    data = json.loads(_run(capsys, "--json", "--room", gid.upper()))
    assert [i["title"] for i in data["do_next"]["items"]] == ["B"]


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
