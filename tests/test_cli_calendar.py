"""``pepper calendar`` — the API method, the ``--json`` path, and the agenda render."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from pepper_cli import api_client, cli, credentials, next_view
from pepper_cli.secret import SecretStr

_PAYLOAD = {
    "window": [
        {"date": "2026-10-02", "label": "Today"},
        {"date": "2026-10-03", "label": "Tomorrow", "date_label": "Sat, Oct 3"},
    ],
    "spanning": [
        {"summary": "Conference", "range_label": "Oct 3 - Oct 4", "ongoing": True},
        {"summary": "Fair", "range_label": "Oct 2 - Oct 5", "kind": "followed"},
    ],
    "days": [
        {
            "date": "2026-10-02",
            "label": "Today",
            "date_label": "Fri, Oct 2",
            "events": [
                {
                    "summary": "Later",
                    "start": "2026-10-02T15:00:00-07:00",
                    "start_label": "3pm",
                    "end_label": "4pm",
                },
                {
                    "summary": "Stand-up",
                    "start": "2026-10-02T09:00:00-07:00",
                    "start_label": "9am",
                    "end_label": "9:30am",
                    "is_next": True,
                    "conference": {"url": "https://meet", "name": "Google Meet"},
                    "my_response": "tentative",
                },
                {
                    "summary": "Game",
                    "kind": "followed",
                    "start": "2026-10-02T17:00:00-07:00",
                    "start_label": "5pm",
                    "end_label": "8pm",
                },
                {
                    "summary": "Recital",
                    "kind": "email",
                    "start": "2026-10-02T18:00:00-07:00",
                    "start_label": "6pm",
                    "end_label": "7pm",
                },
                {"summary": "Birthday", "all_day": True, "start": "2026-10-02T00:00:00-07:00"},
            ],
            "meals": [{"slot": "dinner", "name": "Tacos"}],
            "deadlines": [
                {
                    "title": "File taxes",
                    "time": "11am",
                    "sort_key": "2026-10-02T11:00:00-07:00",
                    "overdue": True,
                }
            ],
            "travel": [
                {"token": "tok", "to": "Dodger Stadium", "after": "2026-10-02T14:00:00-07:00"}
            ],
            "meal_windows": [
                {
                    "label": "Lunch",
                    "start_label": "12pm",
                    "end_label": "12:30pm",
                    "start": "2026-10-02T12:00:00-07:00",
                    "dish": None,
                }
            ],
        },
        {"date": "2026-10-04", "label": "Past window", "events": [{"summary": "Hidden"}]},
    ],
}


_ROOM = "11111111-1111-4111-8111-111111111111"


def test_calendar_range_and_planning_send_the_room_principal() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=_PAYLOAD)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with api_client.ApiClient(server_url="http://x", token=SecretStr("t"), client=client) as api:
        api.calendar_range(start="2026-10-02", days=1, principal=_ROOM)
        api.calendar_planning(start="2026-10-02", days=1, principal=_ROOM)
    assert seen == [
        f"http://x/api/v1/calendars/range?start=2026-10-02&days=1&principal={_ROOM}",
        f"http://x/api/v1/calendars/planning?start=2026-10-02&days=1&principal={_ROOM}",
    ]


def test_calendar_range_sends_start_and_days() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_PAYLOAD)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with api_client.ApiClient(server_url="http://x", token=SecretStr("t"), client=client) as api:
        assert api.calendar_range(start="2026-10-02", days=7) == _PAYLOAD
    assert seen["url"] == "http://x/api/v1/calendars/range?start=2026-10-02&days=7"


@pytest.fixture
def fake_api(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    calls = []
    plan: list[dict] = []

    class Fake:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def whoami(self):
            return SimpleNamespace(timezone="Asia/Tokyo")

        def list_groups(self, *, archived=False):
            groups = [] if archived else [SimpleNamespace(group_id=_ROOM, display_name="Family")]
            return SimpleNamespace(enabled=True, degraded=False, groups=groups)

        def calendar_range(self, *, start, days, principal=None):
            calls.append((start, days))
            if principal is not None:
                calls.append(("principal", principal))
            return copy.deepcopy(_PAYLOAD)

        def calendar_environment(self, *, dates, focus):
            return {
                "days": {
                    "2026-10-02": {
                        "weather": {"label": "cloudy", "low": 58, "high": 81},
                        "solar": {"sunrise_label": "Sunrise 7:05 AM"},
                    }
                },
                "precipitation": [{"label": "rain 2pm to 4pm"}],
            }

        def calendar_travel(self, token):
            return {"ok": True, "minutes": 23, "arrive_at": "3:23pm", "spare_minutes": -5}

        def calendar_schedule_plan(self):
            if not plan:
                raise api_client.ApiError(status=404, body="", detail="nf")
            return plan[0]

        def calendar_planning(self, *, start, days, principal=None):
            calls.append(("planning", start, days, principal))
            return {"cards": [{"kind": "setup", "title": "Add a home address"}], "acted_on": []}

    monkeypatch.setattr(api_client, "ApiClient", Fake)
    return SimpleNamespace(calls=calls, plan=plan)


def test_calendar_week_json_dumps_raw_response(fake_api, capsys) -> None:
    assert cli.main(["calendar", "week", "--start", "2026-10-02", "--json"]) == 0
    assert fake_api.calls == [("2026-10-02", 7)]
    out = json.loads(capsys.readouterr().out)
    assert (
        out["days"][0]["events"] == _PAYLOAD["days"][0]["events"]
        and out["environment"]["precipitation"]
    )


def test_calendar_defaults_to_today_in_the_account_zone_and_rejects_bad_days(
    fake_api, monkeypatch
) -> None:
    # 15:30 UTC is already Oct 3 in Tokyo; the host's date must not be the one asked for.
    monkeypatch.setattr(next_view, "_clock", datetime(2026, 10, 2, 15, 30, tzinfo=UTC).astimezone)
    assert cli.main(["calendar", "--json"]) == 0
    assert fake_api.calls[0] == ("2026-10-03", 1)
    assert cli.main(["calendar", "--days", "32"]) == 2


def test_calendar_renders_agenda(fake_api, capsys) -> None:
    assert cli.main(["calendar", "week"]) == 0
    out = capsys.readouterr().out
    assert out.index("Multi-day") < out.index("Today")
    assert "Oct 3 - Oct 4  Conference (ongoing)" in out
    # all-day first, then chronological; next event marked, tentative flagged
    assert out.index("Birthday") < out.index("Stand-up") < out.index("Later")
    assert "all day" in out
    assert "> 9am-9:30am" in out
    assert "[Google Meet]  (tentative)" in out
    assert "Tomorrow Sat, Oct 3\n  (nothing scheduled)" in out  # quiet day, from window only
    assert "Hidden" not in out
    # Calendar is only what is on the user's calendars.
    for imputed in ("Game", "Recital", "Fair", "File taxes", "Tacos", "meal window"):
        assert imputed not in out


def test_calendar_schedule_adds_todo_deadlines_and_meals(fake_api, capsys) -> None:
    assert cli.main(["calendar", "schedule", "--days", "2"]) == 0
    out = capsys.readouterr().out
    assert out.index("Stand-up") < out.index("File taxes") < out.index("Later")
    assert "File taxes" in out and "(late)" not in out and "Tacos" in out
    assert "Game" not in out


def test_calendar_planning_marks_imputed_items_and_lists_cards(fake_api, capsys) -> None:
    assert cli.main(["calendar", "week", "--planning"]) == 0
    out = capsys.readouterr().out
    assert "Game  (followed)" in out and "Fair  (followed)" in out
    assert "Recital  (suggested - from email)" in out
    assert "Lunch (meal window)" in out
    assert "[setup] Add a home address" in out


def test_calendar_planning_json_pairs_both_payloads_and_caps_days(fake_api, capsys) -> None:
    assert cli.main(["calendar", "week", "--planning", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (
        out["range"]["days"][0]["date"] == "2026-10-02"
        and out["planning"]["cards"][0]["kind"] == "setup"
    )
    assert cli.main(["calendar", "--planning", "--days", "8"]) == 2


def test_calendar_shows_conditions_and_commute_legs(fake_api, capsys) -> None:
    assert cli.main(["calendar", "week"]) == 0
    out = capsys.readouterr().out
    assert "cloudy, 58-81F | Sunrise 7:05 AM" in out
    assert "precipitation: rain 2pm to 4pm" in out
    assert "drive 23 min" in out and "to Dodger Stadium, arrive 3:23pm, 5 min late" in out


def test_calendar_schedule_merges_proposed_blocks_and_degrades_on_404(fake_api, capsys) -> None:
    assert cli.main(["calendar", "schedule"]) == 0  # server without the endpoint
    assert "proposed" not in capsys.readouterr().out
    fake_api.plan.append(
        {
            "date": "2026-10-02",
            "active": "urgent",
            "plans": [
                {
                    "philosophies": ["urgent"],
                    "blocks": [
                        {
                            "type": "todo",
                            "title": "Draft budget",
                            "start": "2026-10-02T13:00:00-07:00",
                            "start_label": "1pm",
                            "end_label": "2pm",
                            "reason": "Due today.",
                        },
                        {
                            "type": "todo",
                            "title": "Held",
                            "on_calendar": True,
                            "start": "2026-10-02T14:00:00-07:00",
                            "start_label": "2pm",
                            "end_label": "3pm",
                        },
                    ],
                }
            ],
        }
    )
    assert cli.main(["calendar", "schedule"]) == 0
    out = capsys.readouterr().out
    assert "Draft budget  (proposed todo block - Due today.)" in out
    assert "Held" not in out


def test_calendar_room_resolves_strictly_and_scopes_both_reads(fake_api, capsys) -> None:
    argv = ["calendar", "--planning", "--room", "fam", "--start", "2026-10-02", "--json"]
    assert cli.main(argv) == 0
    assert fake_api.calls == [
        ("2026-10-02", 1),
        ("principal", _ROOM),
        ("planning", "2026-10-02", 1, _ROOM),
    ]
    out = json.loads(capsys.readouterr().out)
    # A room view carries none of the caller's own weather or drive layers.
    assert "environment" not in out["range"]


def test_calendar_unknown_room_is_a_json_error(fake_api, capsys) -> None:
    assert cli.main(["calendar", "--room", "nope", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == "not_found"
    assert fake_api.calls == []


def test_calendar_room_rejects_the_schedule_view(fake_api, capsys) -> None:
    assert cli.main(["calendar", "schedule", "--room", "fam", "--json"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "usage"
