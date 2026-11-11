"""``pepper calendar publish`` — the client methods and each command, with the HTTP layer mocked."""

from __future__ import annotations

import json

import httpx
import pytest

from pepper_cli import api_client, cli, credentials, render
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
        "calendars": [{"id": "work@x", "label": "Work"}, {"id": "fam@x", "label": "Family"}],
    }
}
_ENABLE = "/api/v1/calendars/feed/enable"
_LINKS = {**_STATUS, "url": _URL, "webcal_url": _WEBCAL}


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
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, Exception):
            raise reply
        return reply

    real = api_client.ApiClient

    def make(**kw):
        return real(**kw, client=httpx.Client(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(api_client, "ApiClient", make)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)

    class S:
        pass

    s = S()
    s.seen, s.replies = seen, replies  # type: ignore[attr-defined]
    s.fail = lambda method, path, exc: replies.setdefault((method, path), []).append(exc)  # type: ignore[attr-defined]
    s.reply = lambda method, path, status=200, body=None: replies.setdefault(  # type: ignore[attr-defined]
        (method, path), []
    ).append(httpx.Response(status, json=body if body is not None else _STATUS))
    return s


def test_show_prints_state_sources_and_the_rotate_hint(server, capsys) -> None:
    server.reply("GET", "/api/v1/calendars/feed")
    assert cli.main(["calendar", "publish"]) == 0
    out = capsys.readouterr().out
    assert "Publishing: on" in out and "detail: busy" in out
    assert (
        "[x] Work" in out and "[ ] Family" in out and "[x] Dodgers" in out and "[ ] Lakers" in out
    )
    assert "meals    yes" in out and "workouts no" in out
    assert "can't be shown again" in out and "rotate" in out
    assert "feed" not in out.lower() and SECRET not in out


def test_show_off_points_at_on(server, capsys) -> None:
    server.reply("GET", "/api/v1/calendars/feed", body={**_STATUS, "enabled": False})
    assert cli.main(["calendar", "publish", "show"]) == 0
    out = capsys.readouterr().out
    assert "Publishing: off" in out and "calendar publish on" in out


@pytest.mark.parametrize(
    "argv", [["calendar", "publish", "--json"], ["calendar", "publish", "show", "--json"]]
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
    assert cli.main(["calendar", "publish", "on"]) == 0
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
    assert cli.main(["calendar", "publish", "on", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["url"] == _URL


def test_on_when_already_on_shows_the_server_reason(server, capsys) -> None:
    server.reply(
        "POST",
        "/api/v1/calendars/feed/enable",
        409,
        {"detail": "The feed is already on. rotate it."},
    )
    assert cli.main(["calendar", "publish", "on"]) == 1
    assert "already on" in capsys.readouterr().err


def _bodies(server) -> list[object]:
    return [b for m, path, b in server.seen if path == _ENABLE]


def test_enable_sends_no_body_without_consent_and_the_flag_with_it(server) -> None:
    server.reply("POST", _ENABLE, body=_LINKS)
    with api_client.ApiClient(server_url="http://example.com", token=SecretStr("t")) as api:
        api.calendar_feed_enable()
        api.calendar_feed_enable(consent_to=["work@x"])
    assert _bodies(server) == [None, {"consent": True, "calendars": ["work@x"]}]


def test_on_consent_required_without_terminal_refuses_and_never_retries(server, capsys) -> None:
    server.reply("POST", _ENABLE, 409, _CONSENT)
    assert cli.main(["calendar", "publish", "on"]) == 2
    err = capsys.readouterr().err
    assert "DISCLOSURE" in err and "--consent" in err and "Work, Family" in err
    assert _bodies(server) == [None]


def test_on_consent_required_json_error_carries_the_disclosure(server, capsys) -> None:
    server.reply("POST", _ENABLE, 409, _CONSENT)
    assert cli.main(["calendar", "publish", "on", "--json"]) == 2
    err = json.loads(capsys.readouterr().err)["error"]
    assert err["code"] == "usage" and "DISCLOSURE" in err["message"]
    assert _bodies(server) == [None]


def test_on_consent_flag_agrees_to_the_calendars_the_server_names(server, capsys) -> None:
    server.reply("POST", _ENABLE, 409, _CONSENT)
    server.reply("POST", _ENABLE, 200, _LINKS)
    assert cli.main(["calendar", "publish", "on", "--consent"]) == 0
    assert _bodies(server) == [None, {"consent": True, "calendars": ["work@x", "fam@x"]}]
    assert _URL in capsys.readouterr().out


def test_on_with_nothing_to_consent_to_publishes_on_the_first_post(server, capsys) -> None:
    server.reply("POST", _ENABLE, body=_LINKS)
    assert cli.main(["calendar", "publish", "on", "--consent"]) == 0
    assert _bodies(server) == [None]
    assert _URL in capsys.readouterr().out


def test_on_a_lost_answer_after_consent_says_it_may_be_on_and_how_to_recover(
    server, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda q: "y")
    server.reply("POST", _ENABLE, 409, _CONSENT)
    server.fail("POST", _ENABLE, httpx.ReadTimeout("timed out"))
    assert cli.main(["calendar", "publish", "on"]) == 1
    cap = capsys.readouterr()
    assert "may have started" in cap.err and "publish show" in cap.err and "rotate" in cap.err
    assert _URL not in cap.out


def test_on_yes_does_not_imply_consent(server) -> None:
    server.reply("POST", _ENABLE, 409, _CONSENT)
    with pytest.raises(SystemExit):
        cli.main(["calendar", "publish", "on", "--yes"])
    assert _bodies(server) == []


def test_on_consent_accepted_on_a_terminal_retries_and_prints_the_address(
    server, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda q: "y")
    server.reply("POST", _ENABLE, 409, _CONSENT)
    server.reply("POST", _ENABLE, 200, _LINKS)
    assert cli.main(["calendar", "publish", "on"]) == 0
    assert _bodies(server) == [None, {"consent": True, "calendars": ["work@x", "fam@x"]}]
    cap = capsys.readouterr()
    assert "DISCLOSURE" in cap.err and "- Work" in cap.err and "- Family" in cap.err
    assert _URL in cap.out and _WEBCAL in cap.out


def test_on_consent_declined_on_a_terminal_publishes_nothing(server, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda q: "")
    server.reply("POST", _ENABLE, 409, _CONSENT)
    assert cli.main(["calendar", "publish", "on"]) == 1
    assert _bodies(server) == [None]
    cap = capsys.readouterr()
    assert "Nothing was published" in cap.err and _URL not in cap.out


def test_off_with_yes_posts_disable(server, capsys) -> None:
    server.reply("POST", "/api/v1/calendars/feed/disable", body={**_STATUS, "enabled": False})
    assert cli.main(["calendar", "publish", "off", "--yes"]) == 0
    assert server.seen == [("POST", "/api/v1/calendars/feed/disable", None)]
    assert "Publishing is off" in capsys.readouterr().err


def test_off_refuses_without_yes_when_there_is_no_terminal(server, capsys) -> None:
    assert cli.main(["calendar", "publish", "off"]) == 2
    assert server.seen == []
    assert "--yes" in capsys.readouterr().err


def test_off_json_without_yes_refuses_even_on_a_terminal(server, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    assert cli.main(["calendar", "publish", "off", "--json"]) == 2
    assert server.seen == []


def test_json_before_the_verb_is_kept(server, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda q: pytest.fail("JSON mode never prompts"))
    assert cli.main(["calendar", "publish", "--json", "off"]) == 2
    assert server.seen == []


def test_pepper_json_reaches_bare_publish(server, capsys, monkeypatch) -> None:
    server.reply("GET", "/api/v1/calendars/feed", body=_STATUS)
    monkeypatch.setenv("PEPPER_JSON", "1")
    assert cli.main(["calendar", "publish"]) == 0
    assert json.loads(capsys.readouterr().out)["enabled"] == _STATUS["enabled"]


def test_off_asks_on_a_terminal_and_aborts_on_no(server, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    asked = []
    monkeypatch.setattr(cli, "_prompt", lambda q: asked.append(q) or "n")
    assert cli.main(["calendar", "publish", "off"]) == 1
    assert server.seen == []
    assert "Stop publishing?" in asked[0] and "stops working for good" in asked[0]
    assert "[y/N]" in asked[0]


def test_off_asks_on_a_terminal_and_stops_on_yes(server, monkeypatch) -> None:
    server.reply("POST", "/api/v1/calendars/feed/disable", body={**_STATUS, "enabled": False})
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda q: "y")
    assert cli.main(["calendar", "publish", "off"]) == 0
    assert server.seen == [("POST", "/api/v1/calendars/feed/disable", None)]


def test_off_yes_skips_the_prompt(server, monkeypatch) -> None:
    server.reply("POST", "/api/v1/calendars/feed/disable", body={**_STATUS, "enabled": False})
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)

    def no_prompt(q):
        raise AssertionError("--yes must not prompt")

    monkeypatch.setattr(cli, "_prompt", no_prompt)
    assert cli.main(["calendar", "publish", "off", "--yes"]) == 0


def test_rotate_refuses_without_yes_when_there_is_no_terminal(server, capsys) -> None:
    assert cli.main(["calendar", "publish", "rotate"]) == 2
    assert server.seen == []
    assert "--yes" in capsys.readouterr().err


def test_rotate_asks_on_a_terminal_and_aborts_on_no(server, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    asked = []
    monkeypatch.setattr(cli, "_prompt", lambda q: asked.append(q) or "n")
    assert cli.main(["calendar", "publish", "rotate"]) == 1
    assert server.seen == [] and "stops working" in asked[0]


def test_rotate_with_yes_prints_the_new_links(server, capsys) -> None:
    server.reply(
        "POST",
        "/api/v1/calendars/feed/rotate",
        body={**_STATUS, "url": _URL, "webcal_url": _WEBCAL},
    )
    assert cli.main(["calendar", "publish", "rotate", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "replaced" in out and _URL in out and _WEBCAL in out


def test_a_long_address_is_printed_whole_on_a_narrow_console(server, capsys, monkeypatch) -> None:
    long_url = "https://pepper.example/cal/" + "T" * 120 + ".ics"
    server.reply(
        "POST",
        "/api/v1/calendars/feed/rotate",
        body={**_STATUS, "url": long_url, "webcal_url": "webcal" + long_url[5:]},
    )
    monkeypatch.setattr(render._stdout, "_width", 40)
    assert cli.main(["calendar", "publish", "rotate", "--yes"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert f"url:    {long_url}" in lines
    assert "webcal: webcal" + long_url[5:] in lines


def test_rotate_when_off_is_the_servers_409(server, capsys) -> None:
    server.reply(
        "POST",
        "/api/v1/calendars/feed/rotate",
        409,
        {"detail": "The feed is off; turn it on first."},
    )
    assert cli.main(["calendar", "publish", "rotate", "--yes"]) == 1
    assert "turn it on first" in capsys.readouterr().err


def _set(server, *flags, status_body=None, patched=None):
    server.reply("GET", "/api/v1/calendars/feed", body=status_body)
    if patched is not False:
        server.reply("PATCH", "/api/v1/calendars/feed/sources", body=patched)
    return cli.main(["calendar", "publish", "set", *flags])


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
    assert cli.main(["calendar", "publish", "set", "--add-calendar", "wor"]) == 2
    assert "matches several" in capsys.readouterr().err
    assert [m for m, *_ in server.seen] == ["GET"]


def test_set_unknown_name_is_a_usage_error_listing_yours(server, capsys) -> None:
    server.reply("GET", "/api/v1/calendars/feed")
    assert cli.main(["calendar", "publish", "set", "--add-follow", "Mets"]) == 2
    err = capsys.readouterr().err
    assert "Mets" in err and "Dodgers" in err
    assert [m for m, *_ in server.seen] == ["GET"]


def test_set_with_no_flags_is_a_usage_error(server, capsys) -> None:
    assert cli.main(["calendar", "publish", "set"]) == 2
    assert server.seen == [] and "nothing to change" in capsys.readouterr().err


def test_set_consent_flag_is_sent_when_given(server) -> None:
    assert _set(server, "--add-calendar", "family", "--consent") == 0
    assert server.seen[-1][2] == {"calendars": ["work@x", "fam@x"], "consent": True}


def test_set_consent_required_without_terminal_refuses_and_never_retries(server, capsys) -> None:
    server.reply("GET", "/api/v1/calendars/feed")
    server.reply("PATCH", "/api/v1/calendars/feed/sources", 409, _CONSENT)
    assert cli.main(["calendar", "publish", "set", "--add-calendar", "family"]) == 2
    err = capsys.readouterr().err
    assert "DISCLOSURE" in err and "--consent" in err
    assert [m for m, *_ in server.seen] == ["GET", "PATCH"]
    assert "consent" not in server.seen[-1][2]


def test_set_consent_required_json_error_carries_the_disclosure(server, capsys) -> None:
    server.reply("GET", "/api/v1/calendars/feed")
    server.reply("PATCH", "/api/v1/calendars/feed/sources", 409, _CONSENT)
    assert cli.main(["calendar", "publish", "set", "--add-calendar", "family", "--json"]) == 2
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
    assert cli.main(["calendar", "publish", "set", "--add-calendar", "family"]) == 0
    assert server.seen[-1][2] == {"calendars": ["work@x", "fam@x"], "consent": True}
    cap = capsys.readouterr()
    assert "DISCLOSURE" in cap.err and "Publishing: on" in cap.out
    assert "- Work" in cap.err and "- Family" in cap.err


def test_set_after_consent_rebuilds_its_lists_from_a_fresh_read(server, monkeypatch) -> None:
    """VR-28a942d3: a change made on the page while the prompt was open is kept."""
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda q: "y")
    server.reply("GET", "/api/v1/calendars/feed")
    changed = {
        **_STATUS,
        "calendars": [{**c, "included": False} for c in _STATUS["calendars"]],
    }
    server.reply("GET", "/api/v1/calendars/feed", body=changed)
    server.reply("PATCH", "/api/v1/calendars/feed/sources", 409, _CONSENT)
    server.reply("PATCH", "/api/v1/calendars/feed/sources", 200, _STATUS)
    assert cli.main(["calendar", "publish", "set", "--add-calendar", "family"]) == 0
    assert server.seen[-1][2] == {"calendars": ["fam@x"], "consent": True}


def test_set_consent_declined_on_a_terminal_changes_nothing(server, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda q: "")
    server.reply("GET", "/api/v1/calendars/feed")
    server.reply("PATCH", "/api/v1/calendars/feed/sources", 409, _CONSENT)
    assert cli.main(["calendar", "publish", "set", "--add-calendar", "family"]) == 1
    assert [m for m, *_ in server.seen] == ["GET", "PATCH"]


def test_set_unknown_sources_422_names_them(server, capsys) -> None:
    server.reply("GET", "/api/v1/calendars/feed")
    server.reply(
        "PATCH",
        "/api/v1/calendars/feed/sources",
        422,
        {"detail": {"code": "unknown_sources", "calendars": ["gone@x"], "follows": []}},
    )
    assert cli.main(["calendar", "publish", "set", "--add-calendar", "family"]) == 1
    assert "gone@x" in capsys.readouterr().err


def test_set_json_prints_the_new_status(server, capsys) -> None:
    assert _set(server, "--todos", "--json") == 0
    assert json.loads(capsys.readouterr().out) == _STATUS


def test_calendar_feed_is_no_longer_a_command(server, capsys) -> None:
    # Renamed to `publish` with no alias: `feed` is not a window word either.
    with pytest.raises(SystemExit) as exc:
        cli.main(["calendar", "feed"])
    assert exc.value.code == 2
    assert server.seen == []


def test_calendar_agenda_commands_still_parse(server, capsys) -> None:
    # The publish verbs have their own parser; the window word on `calendar` is unchanged.
    with pytest.raises(SystemExit):
        cli.main(["calendar", "feeds"])


def test_the_link_is_never_written_under_the_config_dir(server, tmp_path) -> None:
    server.reply(
        "POST",
        "/api/v1/calendars/feed/enable",
        body={**_STATUS, "url": _URL, "webcal_url": _WEBCAL},
    )
    assert cli.main(["calendar", "publish", "on"]) == 0
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


_SHARED = {
    **_STATUS,
    "calendars": [
        {"id": "work@x", "label": "Work", "included": True, "shared": False},
        {
            "id": "pal@x",
            "label": "Pal",
            "included": True,
            "shared": True,
            "busy_only": False,
            "titles": True,
            "titles_switch": True,
        },
        {
            "id": "boss@x",
            "label": "Boss",
            "included": True,
            "shared": True,
            "busy_only": False,
            "titles": False,
            "titles_switch": True,
        },
        {
            "id": "busy@x",
            "label": "Busy",
            "included": True,
            "shared": True,
            "busy_only": True,
            "titles": False,
            "titles_switch": False,
        },
    ],
}


def test_show_marks_titles_only_where_the_switch_is_offered(server, capsys) -> None:
    server.reply("GET", "/api/v1/calendars/feed", body=_SHARED)
    assert cli.main(["calendar", "publish"]) == 0
    lines = {ln.split()[1]: ln for ln in capsys.readouterr().out.splitlines() if "[x]" in ln}
    assert "shared with you · titles shown" in lines["Pal"]
    assert "shared with you · titles hidden" in lines["Boss"]
    assert "titles" not in lines["Work"] and "titles" not in lines["Busy"]


def test_set_titles_off_sends_whole_list_including_already_hidden(server) -> None:
    assert _set(server, "--titles-off", "pal", status_body=_SHARED) == 0
    assert server.seen[-1] == (
        "PATCH",
        "/api/v1/calendars/feed/sources",
        {"titles_off": ["boss@x", "pal@x"]},
    )


def test_set_titles_on_removes_from_the_list(server) -> None:
    assert _set(server, "--titles-on", "Boss", status_body=_SHARED) == 0
    assert server.seen[-1][2] == {"titles_off": []}


def test_set_titles_unknown_label_is_a_usage_error(server, capsys) -> None:
    assert _set(server, "--titles-off", "nobody", status_body=_SHARED, patched=False) == 2
    assert "no calendar named 'nobody'" in capsys.readouterr().err
    assert all(m == "GET" for m, _, _ in server.seen)


def test_set_titles_on_for_a_calendar_without_the_switch_is_a_usage_error(server, capsys) -> None:
    assert _set(server, "--titles-on", "busy", status_body=_SHARED, patched=False) == 2
    assert "'busy' has no titles switch" in capsys.readouterr().err
    assert all(m == "GET" for m, _, _ in server.seen)


def test_set_titles_422_names_the_calendars_without_a_switch(server, capsys) -> None:
    server.reply("GET", "/api/v1/calendars/feed", body=_SHARED)
    server.reply(
        "PATCH",
        "/api/v1/calendars/feed/sources",
        422,
        {
            "detail": {
                "code": "unknown_sources",
                "calendars": [],
                "follows": [],
                "titles_off": ["busy@x"],
            }
        },
    )
    assert cli.main(["calendar", "publish", "set", "--titles-off", "busy"]) == 1
    err = capsys.readouterr().err
    assert "busy@x" in err and "no titles switch" in err
