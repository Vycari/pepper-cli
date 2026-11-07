"""``pepper calendar feed`` — the client methods and each command, with the HTTP layer mocked."""

from __future__ import annotations

import json

import httpx
import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr

SECRET = "SECRETTOKEN"
_URL = "https://pepper.example/cal/feed/SECRETTOKEN.ics"
_WEBCAL = "webcal://pepper.example/cal/feed/SECRETTOKEN.ics"
_STATUS = {
    "enabled": True,
    "detail": "busy",
    "link_created_at": "2026-11-01T12:00:00Z",
    "calendars": [
        {"id": "work@x", "label": "Work", "included": True},
        {"id": "fam@x", "label": "Family", "included": False},
        {"id": "wine@x", "label": "Workout", "included": False},
    ],
    "follows": [
        {"id": "f1", "label": "Dodgers", "included": True},
        {"id": "f2", "label": "Lakers", "included": False},
    ],
    "meals": True,
    "workouts": False,
    "todos": False,
}
_CONSENT = {
    "detail": {
        "code": "consent_required",
        "message": "Adding a Google calendar publishes its events.",
        "disclosure": "DISCLOSURE: anyone with the link can read these events.",
    }
}


@pytest.fixture
def server(monkeypatch, tmp_path):
    """Route every ApiClient through a MockTransport; ``server.replies`` maps (method, path)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("PEPPER_JSON", raising=False)
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    seen: list[tuple[str, str, object]] = []
    replies: dict[tuple[str, str], list[httpx.Response]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        queue = replies[(request.method, request.url.path)]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    real = api_client.ApiClient

    def make(**kw):
        return real(**kw, client=httpx.Client(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(api_client, "ApiClient", make)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)

    class S:
        pass

    s = S()
    s.seen, s.replies = seen, replies  # type: ignore[attr-defined]
    s.reply = lambda method, path, status=200, body=None: replies.setdefault(  # type: ignore[attr-defined]
        (method, path), []
    ).append(httpx.Response(status, json=body if body is not None else _STATUS))
    return s


def test_show_prints_state_sources_and_the_rotate_hint(server, capsys) -> None:
    server.reply("GET", "/api/v1/calendars/feed")
    assert cli.main(["calendar", "feed"]) == 0
    out = capsys.readouterr().out
    assert "Calendar feed: on" in out and "detail: busy" in out
    assert (
        "[x] Work" in out and "[ ] Family" in out and "[x] Dodgers" in out and "[ ] Lakers" in out
    )
    assert "meals    yes" in out and "workouts no" in out
    assert "can't be shown again" in out and "rotate" in out
    assert SECRET not in out


def test_show_off_points_at_on(server, capsys) -> None:
    server.reply("GET", "/api/v1/calendars/feed", body={**_STATUS, "enabled": False})
    assert cli.main(["calendar", "feed", "show"]) == 0
    out = capsys.readouterr().out
    assert "Calendar feed: off" in out and "calendar feed on" in out


@pytest.mark.parametrize(
    "argv", [["calendar", "feed", "--json"], ["calendar", "feed", "show", "--json"]]
)
def test_show_json_is_the_server_status(server, capsys, argv) -> None:
    server.reply("GET", "/api/v1/calendars/feed")
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == _STATUS


def test_on_prints_both_links_once_on_stdout(server, capsys) -> None:
    server.reply(
        "POST",
        "/api/v1/calendars/feed/enable",
        body={**_STATUS, "url": _URL, "webcal_url": _WEBCAL},
    )
    assert cli.main(["calendar", "feed", "on"]) == 0
    cap = capsys.readouterr()
    assert _URL in cap.out and _WEBCAL in cap.out
    assert SECRET not in cap.err
    assert "can't be shown again" in cap.err


def test_on_json_carries_the_url(server, capsys) -> None:
    server.reply(
        "POST",
        "/api/v1/calendars/feed/enable",
        body={**_STATUS, "url": _URL, "webcal_url": _WEBCAL},
    )
    assert cli.main(["calendar", "feed", "on", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["url"] == _URL


def test_on_when_already_on_shows_the_server_reason(server, capsys) -> None:
    server.reply(
        "POST",
        "/api/v1/calendars/feed/enable",
        409,
        {"detail": "The feed is already on. rotate it."},
    )
    assert cli.main(["calendar", "feed", "on"]) == 1
    assert "already on" in capsys.readouterr().err


def test_off_posts_disable(server, capsys) -> None:
    server.reply("POST", "/api/v1/calendars/feed/disable", body={**_STATUS, "enabled": False})
    assert cli.main(["calendar", "feed", "off"]) == 0
    assert server.seen == [("POST", "/api/v1/calendars/feed/disable", None)]
    assert "off" in capsys.readouterr().err


def test_rotate_refuses_without_yes_when_there_is_no_terminal(server, capsys) -> None:
    assert cli.main(["calendar", "feed", "rotate"]) == 2
    assert server.seen == []
    assert "--yes" in capsys.readouterr().err


def test_rotate_asks_on_a_terminal_and_aborts_on_no(server, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    asked = []
    monkeypatch.setattr(cli, "_prompt", lambda q: asked.append(q) or "n")
    assert cli.main(["calendar", "feed", "rotate"]) == 0
    assert server.seen == [] and "stops working" in asked[0]


def test_rotate_with_yes_prints_the_new_links(server, capsys) -> None:
    server.reply(
        "POST",
        "/api/v1/calendars/feed/rotate",
        body={**_STATUS, "url": _URL, "webcal_url": _WEBCAL},
    )
    assert cli.main(["calendar", "feed", "rotate", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "rotated" in out and _URL in out and _WEBCAL in out


def test_rotate_when_off_is_the_servers_409(server, capsys) -> None:
    server.reply(
        "POST",
        "/api/v1/calendars/feed/rotate",
        409,
        {"detail": "The feed is off; turn it on first."},
    )
    assert cli.main(["calendar", "feed", "rotate", "--yes"]) == 1
    assert "turn it on first" in capsys.readouterr().err


def _set(server, *flags, status_body=None, patched=None):
    server.reply("GET", "/api/v1/calendars/feed", body=status_body)
    if patched is not False:
        server.reply("PATCH", "/api/v1/calendars/feed/sources", body=patched)
    return cli.main(["calendar", "feed", "set", *flags])


def test_set_sends_whole_calendar_list_resolved_from_labels(server) -> None:
    assert _set(server, "--add-calendar", "family", "--remove-calendar", "work") == 0
    assert server.seen[-1] == ("PATCH", "/api/v1/calendars/feed/sources", {"calendars": ["fam@x"]})


def test_set_keeps_the_other_included_calendars(server) -> None:
    assert _set(server, "--add-calendar", "fam@x") == 0
    assert server.seen[-1][2] == {"calendars": ["work@x", "fam@x"]}


def test_set_follows_off_is_the_whole_not_included_list(server) -> None:
    assert _set(server, "--remove-follow", "Dodgers", "--add-follow", "lakers") == 0
    assert server.seen[-1][2] == {"follows_off": ["f1"]}


def test_set_scalar_switches_send_only_what_was_given(server) -> None:
    assert _set(server, "--detail", "full", "--no-meals", "--workouts") == 0
    assert server.seen[-1][2] == {"detail": "full", "meals": False, "workouts": True}


def test_set_exact_label_beats_prefix_and_a_shared_prefix_is_ambiguous(server, capsys) -> None:
    assert (
        _set(server, "--add-calendar", "Work") == 0
    )  # "Work" is exact; "Workout" also starts with it
    assert server.seen[-1][2] == {"calendars": ["work@x"]}
    server.seen.clear()
    server.reply("GET", "/api/v1/calendars/feed")
    assert cli.main(["calendar", "feed", "set", "--add-calendar", "wor"]) == 2
    assert "matches several" in capsys.readouterr().err
    assert [m for m, *_ in server.seen] == ["GET"]


def test_set_unknown_name_is_a_usage_error_listing_yours(server, capsys) -> None:
    server.reply("GET", "/api/v1/calendars/feed")
    assert cli.main(["calendar", "feed", "set", "--add-follow", "Mets"]) == 2
    err = capsys.readouterr().err
    assert "Mets" in err and "Dodgers" in err
    assert [m for m, *_ in server.seen] == ["GET"]


def test_set_with_no_flags_is_a_usage_error(server, capsys) -> None:
    assert cli.main(["calendar", "feed", "set"]) == 2
    assert server.seen == [] and "nothing to change" in capsys.readouterr().err


def test_set_consent_flag_is_sent_when_given(server) -> None:
    assert _set(server, "--add-calendar", "family", "--consent") == 0
    assert server.seen[-1][2] == {"calendars": ["work@x", "fam@x"], "consent": True}


def test_set_consent_required_without_terminal_refuses_and_never_retries(server, capsys) -> None:
    server.reply("GET", "/api/v1/calendars/feed")
    server.reply("PATCH", "/api/v1/calendars/feed/sources", 409, _CONSENT)
    assert cli.main(["calendar", "feed", "set", "--add-calendar", "family"]) == 2
    err = capsys.readouterr().err
    assert "DISCLOSURE" in err and "--consent" in err
    assert [m for m, *_ in server.seen] == ["GET", "PATCH"]
    assert "consent" not in server.seen[-1][2]


def test_set_consent_required_json_error_carries_the_disclosure(server, capsys) -> None:
    server.reply("GET", "/api/v1/calendars/feed")
    server.reply("PATCH", "/api/v1/calendars/feed/sources", 409, _CONSENT)
    assert cli.main(["calendar", "feed", "set", "--add-calendar", "family", "--json"]) == 2
    err = json.loads(capsys.readouterr().err)["error"]
    assert err["code"] == "usage" and "DISCLOSURE" in err["message"]


def test_set_consent_required_on_a_terminal_asks_then_retries_with_consent(
    server, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda q: "y")
    server.reply("GET", "/api/v1/calendars/feed")
    server.reply("PATCH", "/api/v1/calendars/feed/sources", 409, _CONSENT)
    server.reply("PATCH", "/api/v1/calendars/feed/sources", 200, _STATUS)
    assert cli.main(["calendar", "feed", "set", "--add-calendar", "family"]) == 0
    assert server.seen[-1][2] == {"calendars": ["work@x", "fam@x"], "consent": True}
    cap = capsys.readouterr()
    assert "DISCLOSURE" in cap.err and "Calendar feed: on" in cap.out


def test_set_consent_declined_on_a_terminal_changes_nothing(server, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda q: "")
    server.reply("GET", "/api/v1/calendars/feed")
    server.reply("PATCH", "/api/v1/calendars/feed/sources", 409, _CONSENT)
    assert cli.main(["calendar", "feed", "set", "--add-calendar", "family"]) == 0
    assert [m for m, *_ in server.seen] == ["GET", "PATCH"]


def test_set_unknown_sources_422_names_them(server, capsys) -> None:
    server.reply("GET", "/api/v1/calendars/feed")
    server.reply(
        "PATCH",
        "/api/v1/calendars/feed/sources",
        422,
        {"detail": {"code": "unknown_sources", "calendars": ["gone@x"], "follows": []}},
    )
    assert cli.main(["calendar", "feed", "set", "--add-calendar", "family"]) == 1
    assert "gone@x" in capsys.readouterr().err


def test_set_json_prints_the_new_status(server, capsys) -> None:
    assert _set(server, "--todos", "--json") == 0
    assert json.loads(capsys.readouterr().out) == _STATUS


def test_calendar_agenda_commands_still_parse(server, capsys) -> None:
    # The feed verbs have their own parser; the window word on `calendar` is unchanged.
    with pytest.raises(SystemExit):
        cli.main(["calendar", "feeds"])


def test_the_link_is_never_written_under_the_config_dir(server, tmp_path) -> None:
    server.reply(
        "POST",
        "/api/v1/calendars/feed/enable",
        body={**_STATUS, "url": _URL, "webcal_url": _WEBCAL},
    )
    assert cli.main(["calendar", "feed", "on"]) == 0
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_text(errors="ignore"), path


def test_client_methods_hit_the_documented_routes() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content))
        return httpx.Response(200, json=_STATUS)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with api_client.ApiClient(server_url="http://x", token=SecretStr("t"), client=client) as api:
        api.calendar_feed()
        api.calendar_feed_enable()
        api.calendar_feed_rotate()
        api.calendar_feed_disable()
        api.calendar_feed_sources({"todos": True})
    assert [(m, p) for m, p, _ in seen] == [
        ("GET", "/api/v1/calendars/feed"),
        ("POST", "/api/v1/calendars/feed/enable"),
        ("POST", "/api/v1/calendars/feed/rotate"),
        ("POST", "/api/v1/calendars/feed/disable"),
        ("PATCH", "/api/v1/calendars/feed/sources"),
    ]
    assert json.loads(seen[-1][2]) == {"todos": True}
