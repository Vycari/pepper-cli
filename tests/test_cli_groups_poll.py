"""``pepper rooms poll`` (one page of the agent feed, then exit) and ``rooms watch --for``.

``ApiClient.get_agent_events_page`` is replaced by a scripted fake that records its arguments;
the ``--for`` tests drive ``watch_agent_events`` / ``watch_scoped_events`` the way
``test_cli_groups_watch.py`` does, with the clock under the test's control.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.api_client import FeedFrame
from pepper_cli.secret import SecretStr

_GROUP = "00000000-0000-0000-0000-0000000000a1"
_PERSON = "00000000-0000-0000-0000-0000000000d1"
_TOPIC = "00000000-0000-0000-0000-0000000000b1"
_MESSAGE = "00000000-0000-0000-0000-0000000000c1"


@pytest.fixture(autouse=True)
def _logged_in(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    credentials.save(server_url="http://example.com", token=SecretStr("pep_agt_abc"))


def _event(event_id: int, kind: str = "message.created") -> dict[str, Any]:
    return {
        "id": event_id,
        "kind": kind,
        "group_id": _GROUP,
        "person_id": _PERSON,
        "payload": {"topic_id": _TOPIC, "message_id": _MESSAGE},
        "created_at": "2026-10-01T12:00:00+00:00",
    }


class _Poll:
    """The scripted ``get_agent_events_page`` and what it was called with."""

    def __init__(self, *pages: dict[str, Any] | Exception) -> None:
        self.pages = list(pages)
        self.calls: list[dict[str, int]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _Poll:
        outer = self

        class FakeApi:
            def __init__(self, *, server_url, token, **_kw) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return None

            def get_agent_events_page(self, *, after_id=0, limit=100, wait=0):
                outer.calls.append({"after_id": after_id, "wait": wait})
                item = outer.pages.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        monkeypatch.setattr(api_client, "ApiClient", FakeApi)
        return self


def _poll(monkeypatch, *pages) -> _Poll:
    return _Poll(*pages).install(monkeypatch)


def _lines(capsys) -> list[str]:
    return capsys.readouterr().out.splitlines()


def test_prints_the_events_then_the_cursor_and_exits_zero(monkeypatch, capsys):
    poll = _poll(monkeypatch, {"events": [_event(7), _event(9)], "next_after_id": 12})

    assert cli.main(["rooms", "poll"]) == 0

    first, second, cursor = _lines(capsys)
    assert first == (
        f"#7 message.created group={_GROUP} person={_PERSON} "
        f"topic_id={_TOPIC} message_id={_MESSAGE}"
    )
    assert second.startswith("#9 message.created")
    assert cursor == "cursor=12"
    assert poll.calls == [{"after_id": 0, "wait": 0}]


def test_json_lines_match_watch_and_end_with_a_cursor_line(monkeypatch, capsys):
    _poll(monkeypatch, {"events": [_event(7)], "next_after_id": 7})

    assert cli.main(["rooms", "poll", "--json"]) == 0

    event, cursor = (json.loads(line) for line in _lines(capsys))
    assert event == {**_event(7), "cursor": 7}
    assert cursor == {"kind": "cursor", "cursor": 7}


def test_pepper_json_turns_json_on(monkeypatch, capsys):
    monkeypatch.setenv("PEPPER_JSON", "1")
    _poll(monkeypatch, {"events": [], "next_after_id": 3})

    assert cli.main(["rooms", "poll"]) == 0

    assert [json.loads(line) for line in _lines(capsys)] == [{"kind": "cursor", "cursor": 3}]


def test_an_empty_page_prints_only_the_cursor(monkeypatch, capsys):
    _poll(monkeypatch, {"events": [], "next_after_id": 0})

    assert cli.main(["rooms", "poll", "--since", "0"]) == 0

    assert _lines(capsys) == ["cursor=0"]


def test_the_cursor_is_next_after_id_even_past_the_last_event(monkeypatch, capsys, tmp_path):
    cursor_file = tmp_path / "cursor"
    _poll(monkeypatch, {"events": [_event(7)], "next_after_id": 40})

    assert cli.main(["rooms", "poll", "--cursor-file", str(cursor_file)]) == 0

    assert _lines(capsys)[-1] == "cursor=40"
    assert cursor_file.read_text(encoding="utf-8").strip() == "40"


def test_since_and_wait_reach_the_request(monkeypatch, capsys):
    poll = _poll(monkeypatch, {"events": [], "next_after_id": 55})

    assert cli.main(["rooms", "poll", "--since", "55", "--wait", "25"]) == 0

    assert poll.calls == [{"after_id": 55, "wait": 25}]


def test_the_cursor_file_is_the_default_start_and_since_overrides_it(monkeypatch, capsys, tmp_path):
    cursor_file = tmp_path / "cursor"
    cursor_file.write_text("31\n", encoding="utf-8")
    poll = _poll(
        monkeypatch,
        {"events": [], "next_after_id": 31},
        {"events": [], "next_after_id": 80},
    )

    assert cli.main(["rooms", "poll", "--cursor-file", str(cursor_file)]) == 0
    assert cli.main(["rooms", "poll", "--cursor-file", str(cursor_file), "--since", "80"]) == 0

    assert [c["after_id"] for c in poll.calls] == [31, 80]
    assert cursor_file.read_text(encoding="utf-8").strip() == "80"


def test_a_cursor_file_is_not_moved_backwards(monkeypatch, capsys, tmp_path):
    cursor_file = tmp_path / "cursor"
    cursor_file.write_text("50\n", encoding="utf-8")
    _poll(monkeypatch, {"events": [], "next_after_id": 10})

    assert cli.main(["rooms", "poll", "--cursor-file", str(cursor_file)]) == 0

    assert _lines(capsys) == ["cursor=50"]
    assert cursor_file.read_text(encoding="utf-8").strip() == "50"


def test_a_scoped_watchs_cursor_file_is_refused(monkeypatch, capsys, tmp_path):
    cursor_file = tmp_path / "cursor"
    scoped = cli._ScopedCursor(
        server="http://example.com",
        agent_person_id=_PERSON,
        room_id=_GROUP,
        topic_id=None,
        cursor=9,
    )
    cursor_file.write_text(scoped.to_line() + "\n", encoding="utf-8")
    poll = _poll(monkeypatch)

    # Even with --since: the run would overwrite another watch's position.
    for extra in ([], ["--since", "3"]):
        rc = cli.main(["rooms", "poll", "--cursor-file", str(cursor_file), *extra])
        assert rc == 2
        err = capsys.readouterr().err
        assert "scoped watch" in err
        assert "Nothing was read" in err

    assert poll.calls == []
    assert cursor_file.read_text(encoding="utf-8").strip() == scoped.to_line()


def test_a_garbage_cursor_file_is_refused_unless_since_is_given(monkeypatch, capsys, tmp_path):
    cursor_file = tmp_path / "cursor"
    cursor_file.write_text("not a cursor\n", encoding="utf-8")
    poll = _poll(monkeypatch, {"events": [], "next_after_id": 4})

    assert cli.main(["rooms", "poll", "--cursor-file", str(cursor_file)]) == 2
    assert "does not hold an integer cursor" in capsys.readouterr().err
    assert poll.calls == []

    assert cli.main(["rooms", "poll", "--cursor-file", str(cursor_file), "--since", "4"]) == 0
    assert cursor_file.read_text(encoding="utf-8").strip() == "4"


def test_kinds_and_ignore_kinds_filter_the_output_but_the_cursor_moves(monkeypatch, capsys):
    page = {
        "events": [
            _event(1, "message.created"),
            _event(2, "mention.created"),
            _event(3, "message.link_cards_updated"),
        ],
        "next_after_id": 3,
    }
    _poll(monkeypatch, page, page)

    assert cli.main(["rooms", "poll", "--kinds", "mention.created"]) == 0
    only, cursor = _lines(capsys)
    assert only.startswith("#2 mention.created")
    assert cursor == "cursor=3"

    assert cli.main(["rooms", "poll", "--ignore-kinds", "message.link_cards_updated"]) == 0
    kept = _lines(capsys)
    assert [line.split()[1] for line in kept[:-1]] == ["message.created", "mention.created"]
    assert kept[-1] == "cursor=3"


def test_a_filtered_page_still_saves_the_cursor(monkeypatch, capsys, tmp_path):
    cursor_file = tmp_path / "cursor"
    _poll(monkeypatch, {"events": [_event(5, "message.created")], "next_after_id": 5})

    assert (
        cli.main(["rooms", "poll", "--kinds", "mention.created", "--cursor-file", str(cursor_file)])
        == 0
    )

    assert _lines(capsys) == ["cursor=5"]
    assert cursor_file.read_text(encoding="utf-8").strip() == "5"


@pytest.mark.parametrize("value", ["26", "-1", "soon", "1.5"])
def test_wait_outside_zero_to_twenty_five_is_an_argument_error(capsys, value):
    with pytest.raises(SystemExit) as stop:
        cli.main(["rooms", "poll", "--wait", value])
    assert stop.value.code == 2
    assert "0 to 25" in capsys.readouterr().err


def test_a_negative_since_is_a_usage_error(monkeypatch, capsys):
    poll = _poll(monkeypatch)

    assert cli.main(["rooms", "poll", "--since", "-1"]) == 2
    assert "non-negative" in capsys.readouterr().err
    assert poll.calls == []


@pytest.mark.parametrize(
    ("status", "needle"),
    [(401, "Authentication failed"), (403, "403"), (404, "404")],
)
def test_a_refused_token_or_missing_route_exits_one(monkeypatch, capsys, status, needle):
    _poll(monkeypatch, api_client.ApiError(status=status, body="", detail="nope"))

    assert cli.main(["rooms", "poll"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert needle in captured.err


def test_errors_are_json_lines_under_json(monkeypatch, capsys):
    _poll(monkeypatch, api_client.ApiError(status=403, body="", detail="agent tokens only"))

    assert cli.main(["rooms", "poll", "--json"]) == 1

    err = json.loads(capsys.readouterr().err)
    assert err["error"]["code"] == "forbidden"
    assert err["error"]["status"] == 403


def test_a_network_failure_exits_one(monkeypatch, capsys):
    _poll(monkeypatch, httpx.ConnectError("boom"))

    assert cli.main(["rooms", "poll"]) == 1
    assert "Couldn't reach http://example.com" in capsys.readouterr().err


def test_a_server_error_exits_one_without_retrying(monkeypatch, capsys):
    poll = _poll(monkeypatch, api_client.ApiError(status=503, body="", detail="down"))

    assert cli.main(["rooms", "poll"]) == 1
    assert len(poll.calls) == 1


def test_an_unwritable_cursor_file_exits_one(monkeypatch, capsys, tmp_path):
    _poll(monkeypatch, {"events": [], "next_after_id": 5})

    rc = cli.main(["rooms", "poll", "--cursor-file", str(tmp_path / "missing-dir" / "cursor")])

    assert rc == 1
    assert "Couldn't write the cursor file" in capsys.readouterr().err


def test_a_page_missing_its_fields_keeps_the_cursor(monkeypatch, capsys):
    _poll(monkeypatch, {})

    assert cli.main(["rooms", "poll", "--since", "6"]) == 0

    assert _lines(capsys) == ["cursor=6"]


def test_events_without_an_id_are_skipped_and_ids_past_next_after_id_win(monkeypatch, capsys):
    _poll(
        monkeypatch,
        {"events": [{"kind": "x"}, "junk", _event(9)], "next_after_id": 4},
    )

    assert cli.main(["rooms", "poll"]) == 0

    lines = _lines(capsys)
    assert len(lines) == 2
    assert lines[0].startswith("#9 ")
    assert lines[1] == "cursor=9"


def test_without_credentials_it_exits_one(tmp_path, monkeypatch, capsys):
    (tmp_path / ".config").mkdir(exist_ok=True)
    credentials.delete()

    assert cli.main(["rooms", "poll"]) == 1
    assert "login" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# ApiClient.get_agent_events_page


def test_the_client_sends_after_id_limit_and_wait_with_the_bearer(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"events": [_event(2)], "next_after_id": 2})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with api_client.ApiClient(
        server_url="http://example.com/", token=SecretStr("pep_agt_abc"), client=client
    ) as api:
        body = api.get_agent_events_page(after_id=1, limit=50, wait=10)

    assert body["next_after_id"] == 2
    (request,) = seen
    assert request.url.path == "/api/v1/groups/events"
    assert dict(request.url.params) == {"after_id": "1", "limit": "50", "wait": "10"}
    assert request.headers["authorization"] == "Bearer pep_agt_abc"


def test_the_client_holds_the_request_longer_than_the_wait():
    timeouts: list[dict[str, float | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, json={"events": [], "next_after_id": 0})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with api_client.ApiClient(
        server_url="http://example.com", token=SecretStr("pep_agt_abc"), client=client
    ) as api:
        api.get_agent_events_page(wait=25)

    assert timeouts[0]["read"] > 25


def test_the_client_raises_api_error_on_a_403():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(403, json={"detail": "no"}))
    )
    with (
        api_client.ApiClient(
            server_url="http://example.com", token=SecretStr("pep_pat_abc"), client=client
        ) as api,
        pytest.raises(api_client.ApiError) as raised,
    ):
        api.get_agent_events_page()
    assert raised.value.status == 403


def test_a_non_object_body_is_an_empty_page():
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=[1])))
    with api_client.ApiClient(
        server_url="http://example.com", token=SecretStr("pep_agt_abc"), client=client
    ) as api:
        assert api.get_agent_events_page() == {}


# ---------------------------------------------------------------------------
# --for


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("45s", 45.0), ("2m", 120.0), ("1h", 3600.0), ("90", 90.0), ("1.5m", 90.0), ("2M", 120.0)],
)
def test_duration_forms(text, seconds):
    assert cli._duration_seconds(text) == seconds


@pytest.mark.parametrize("text", ["0", "0s", "", "5x", "m", "1d", "1m30s"])
def test_bad_durations_are_argument_errors(text, capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(["rooms", "watch", "--for", text])
    assert stop.value.code == 2
    assert "duration" in capsys.readouterr().err


class _Clock:
    """``time.monotonic`` and ``time.sleep`` under the test's control."""

    def __init__(self, monkeypatch) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        monkeypatch.setattr(cli.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(cli.time, "sleep", self._sleep)

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _FeedApi:
    """A fake client whose stream calls are scripted by ``script(call_index, kwargs)``."""

    calls: list[dict[str, Any]] = []
    script: Any = None

    def __init__(self, *, server_url, token, **_kw) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def _stream(self, **kwargs):
        _FeedApi.calls.append(kwargs)
        return _FeedApi.script(len(_FeedApi.calls) - 1, kwargs)

    def watch_agent_events(self, **kwargs):
        return self._stream(**kwargs)

    def watch_scoped_events(self, room_id, **kwargs):
        return self._stream(room_id=room_id, **kwargs)


def _frame(event_id: int) -> FeedFrame:
    return FeedFrame(kind="message.created", id=event_id, payload=_event(event_id))


def _install_feed(monkeypatch, script) -> None:
    _FeedApi.calls = []
    _FeedApi.script = staticmethod(script)
    monkeypatch.setattr(api_client, "ApiClient", _FeedApi)


def test_watch_for_exits_zero_when_the_stream_goes_quiet_past_the_deadline(
    monkeypatch, capsys, tmp_path
):
    clock = _Clock(monkeypatch)
    cursor_file = tmp_path / "cursor"

    def script(_index, kwargs):
        # The quiet stream times out at the read budget, as httpx would.
        assert kwargs["read_timeout"] == pytest.approx(30.0)
        yield _frame(4)
        clock.now += 30.0
        raise httpx.ReadTimeout("quiet")

    _install_feed(monkeypatch, script)

    rc = cli.main(["rooms", "watch", "--for", "30s", "--cursor-file", str(cursor_file)])

    assert rc == 0
    assert len(_FeedApi.calls) == 1
    assert [line.split()[0] for line in _lines(capsys)] == ["#4"]
    assert cursor_file.read_text(encoding="utf-8").strip() == "4"
    assert capsys.readouterr().err == ""


def test_watch_for_stops_mid_stream_without_consuming_the_late_frame(monkeypatch, capsys, tmp_path):
    clock = _Clock(monkeypatch)
    cursor_file = tmp_path / "cursor"

    def script(_index, _kwargs):
        yield _frame(1)
        clock.now += 61.0
        yield _frame(2)

    _install_feed(monkeypatch, script)

    rc = cli.main(["rooms", "watch", "--for", "1m", "--cursor-file", str(cursor_file)])

    assert rc == 0
    assert [line.split()[0] for line in _lines(capsys)] == ["#1"]
    assert cursor_file.read_text(encoding="utf-8").strip() == "1"


def test_watch_for_does_not_outlast_a_reconnect_backoff(monkeypatch, capsys):
    clock = _Clock(monkeypatch)

    def script(index, kwargs):
        if index == 0:
            clock.now += 5.0
            raise httpx.ConnectError("down")
        raise AssertionError("must not reconnect after the deadline")
        yield  # pragma: no cover

    _install_feed(monkeypatch, script)

    rc = cli.main(["rooms", "watch", "--for", "5s"])

    assert rc == 0
    assert len(_FeedApi.calls) == 1
    assert clock.sleeps == []


def test_watch_for_caps_the_backoff_sleep_at_the_remaining_time(monkeypatch, capsys):
    clock = _Clock(monkeypatch)

    def script(index, kwargs):
        if index == 0:
            clock.now += 9.5
            raise httpx.ConnectError("down")
        raise AssertionError("must not reconnect after the deadline")
        yield  # pragma: no cover

    _install_feed(monkeypatch, script)

    # The first failure backs off 1 s, but only 0.5 s of the run is left.
    rc = cli.main(["rooms", "watch", "--for", "10s"])

    assert rc == 0
    assert clock.sleeps == [pytest.approx(0.5)]


def test_watch_without_for_passes_no_read_budget(monkeypatch, capsys):
    _Clock(monkeypatch)

    def script(_index, kwargs):
        assert "read_timeout" not in kwargs
        raise KeyboardInterrupt
        yield  # pragma: no cover

    _install_feed(monkeypatch, script)

    assert cli.main(["rooms", "watch"]) == 0


def test_scoped_watch_for_exits_zero_at_the_deadline(monkeypatch, capsys):
    clock = _Clock(monkeypatch)
    monkeypatch.setattr(
        cli, "_scoped_watch_target", lambda _api, _room, _topic, _args: (_GROUP, None)
    )

    def script(_index, kwargs):
        assert kwargs["room_id"] == _GROUP
        assert kwargs["read_timeout"] == pytest.approx(20.0)
        clock.now += 20.0
        raise httpx.ReadTimeout("quiet")
        yield  # pragma: no cover

    _install_feed(monkeypatch, script)

    rc = cli.main(["rooms", "watch", "--room", _GROUP, "--for", "20s"])

    assert rc == 0
    assert len(_FeedApi.calls) == 1


def test_scoped_watch_for_stops_before_consuming_a_late_frame(monkeypatch, capsys):
    clock = _Clock(monkeypatch)
    monkeypatch.setattr(
        cli, "_scoped_watch_target", lambda _api, _room, _topic, _args: (_GROUP, None)
    )

    def script(_index, _kwargs):
        clock.now += 21.0
        yield _frame(1)

    _install_feed(monkeypatch, script)

    rc = cli.main(["rooms", "watch", "--room", _GROUP, "--for", "20s"])

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_the_client_shortens_but_never_lengthens_the_stream_read_timeout():
    read_timeouts: list[float | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        read_timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(
            200, text=": connected\n\n", headers={"content-type": "text/event-stream"}
        )

    for requested, expected in (
        (5.0, 5.0),
        (None, api_client.FEED_STREAM_READ_TIMEOUT),
        (999.0, api_client.FEED_STREAM_READ_TIMEOUT),
    ):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with api_client.ApiClient(
            server_url="http://example.com", token=SecretStr("pep_agt_abc"), client=client
        ) as api:
            list(api.watch_agent_events(read_timeout=requested))
        assert read_timeouts[-1] == expected
