"""``pepper rooms watch`` — the agent member's event stream, as one line per event.

``ApiClient.watch_agent_events`` is replaced by a scripted fake: each call is
one connection, which yields its frames and then ends or raises. When the
script runs out the fake raises ``KeyboardInterrupt``, which is how a person
stops the real command, so every test terminates the way the command does.
The reconnect sleep is recorded instead of slept.
"""

from __future__ import annotations

import json
from pathlib import Path

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


def _event(event_id: int, kind: str = "message.created") -> FeedFrame:
    return FeedFrame(
        kind=kind,
        id=event_id,
        payload={
            "id": event_id,
            "kind": kind,
            "group_id": _GROUP,
            "person_id": _PERSON,
            "payload": {"topic_id": _TOPIC, "message_id": _MESSAGE},
            "created_at": "2026-10-01T12:00:00+00:00",
        },
    )


def _cursor(n: int) -> FeedFrame:
    return FeedFrame(kind="cursor", id=n, payload={"cursor": n})


def _error(slug: str, *, retry: bool) -> FeedFrame:
    return FeedFrame(kind="error", id=None, payload={"error": slug, "retry": retry})


class _Connection:
    def __init__(self, *frames: FeedFrame, then: Exception | None = None) -> None:
        self.frames = frames
        self.then = then


class _Watch:
    """The scripted ``watch_agent_events`` and what it was called with."""

    def __init__(self, *connections: _Connection | Exception) -> None:
        self.connections = list(connections)
        self.calls: list[dict[str, int | None]] = []
        self.sleeps: list[float] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _Watch:
        outer = self

        class FakeApi:
            def __init__(self, *, server_url, token, **_kw) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return None

            def watch_agent_events(self, *, since=0, last_event_id=None):
                outer.calls.append({"since": since, "last_event_id": last_event_id})
                if not outer.connections:
                    raise KeyboardInterrupt
                item = outer.connections.pop(0)
                if isinstance(item, Exception):
                    raise item
                yield from item.frames
                if item.then is not None:
                    raise item.then

        monkeypatch.setattr(api_client, "ApiClient", FakeApi)
        monkeypatch.setattr(cli.time, "sleep", self.sleeps.append)
        return self


def _watch(monkeypatch, *connections) -> _Watch:
    return _Watch(*connections).install(monkeypatch)


def _lines(capsys) -> list[str]:
    return capsys.readouterr().out.splitlines()


def test_json_prints_the_event_fields_plus_the_cursor(monkeypatch, capsys):
    _watch(monkeypatch, _Connection(_event(7)))

    assert cli.main(["rooms", "watch", "--json"]) == 0

    (line,) = _lines(capsys)
    assert json.loads(line) == {**_event(7).payload, "cursor": 7}


def test_the_human_line_is_short_and_names_the_ids_to_read(monkeypatch, capsys):
    _watch(monkeypatch, _Connection(_event(7)))

    assert cli.main(["rooms", "watch"]) == 0

    (line,) = _lines(capsys)
    assert line == (
        f"#7 message.created group={_GROUP} person={_PERSON} "
        f"topic_id={_TOPIC} message_id={_MESSAGE}"
    )


def test_a_cursor_frame_is_silent_by_default_and_a_json_line_under_json(monkeypatch, capsys):
    _watch(monkeypatch, _Connection(_cursor(20)))
    assert cli.main(["rooms", "watch"]) == 0
    assert _lines(capsys) == []

    _watch(monkeypatch, _Connection(_event(3), _cursor(20)))
    assert cli.main(["rooms", "watch", "--json"]) == 0
    event, cursor = (json.loads(line) for line in _lines(capsys))
    assert event["cursor"] == 3
    assert cursor == {"kind": "cursor", "cursor": 20}


def test_since_is_the_first_connects_resume_point(monkeypatch):
    watch = _watch(monkeypatch)

    assert cli.main(["rooms", "watch", "--since", "41"]) == 0

    assert watch.calls == [{"since": 41, "last_event_id": None}]


def test_the_cursor_file_supplies_the_start_and_since_overrides_it(monkeypatch, tmp_path):
    cursor_file = tmp_path / "cursor"
    cursor_file.write_text("17\n")

    watch = _watch(monkeypatch)
    assert cli.main(["rooms", "watch", "--cursor-file", str(cursor_file)]) == 0
    assert watch.calls == [{"since": 17, "last_event_id": None}]

    watch = _watch(monkeypatch)
    assert cli.main(["rooms", "watch", "--cursor-file", str(cursor_file), "--since", "5"]) == 0
    assert watch.calls == [{"since": 5, "last_event_id": None}]


def test_a_missing_cursor_file_starts_from_the_beginning(monkeypatch, tmp_path):
    watch = _watch(monkeypatch)

    cli.main(["rooms", "watch", "--cursor-file", str(tmp_path / "nope")])

    assert watch.calls == [{"since": 0, "last_event_id": None}]


def test_a_corrupt_cursor_file_is_an_error_not_a_silent_restart(monkeypatch, tmp_path, capsys):
    cursor_file = tmp_path / "cursor"
    cursor_file.write_text("garbage")
    watch = _watch(monkeypatch)

    assert cli.main(["rooms", "watch", "--cursor-file", str(cursor_file)]) == 2

    assert watch.calls == []
    assert "cursor file" in capsys.readouterr().err


def test_the_cursor_file_follows_each_event_and_each_cursor_frame(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    cursor_file = state / "cursor"
    seen_at_print: list[str] = []

    original = cli._emit_line

    def spy(line: str) -> None:
        # What the file held at the moment the event was printed: the cursor
        # moves only after the line is out, so a crash replays, never loses.
        seen_at_print.append(cursor_file.read_text().strip() if cursor_file.exists() else "")
        original(line)

    monkeypatch.setattr(cli, "_emit_line", spy)
    _watch(monkeypatch, _Connection(_event(3), _event(4), _cursor(9)))

    assert cli.main(["rooms", "watch", "--cursor-file", str(cursor_file)]) == 0

    assert seen_at_print == ["", "3"]
    assert cursor_file.read_text() == "9\n"
    # No temp file left behind.
    assert [p.name for p in state.iterdir()] == ["cursor"]


def test_the_cursor_file_is_replaced_not_truncated(monkeypatch, tmp_path):
    cursor_file = tmp_path / "cursor"
    cursor_file.write_text("1\n")
    inode_before = cursor_file.stat().st_ino
    _watch(monkeypatch, _Connection(_event(2)))

    cli.main(["rooms", "watch", "--cursor-file", str(cursor_file)])

    assert cursor_file.read_text() == "2\n"
    assert cursor_file.stat().st_ino != inode_before


def test_a_dropped_stream_reconnects_from_the_last_cursor_with_last_event_id(monkeypatch, capsys):
    watch = _watch(
        monkeypatch,
        _Connection(_event(3), then=httpx.ReadError("dropped")),
        _Connection(_event(4), _cursor(12)),
        _Connection(),
    )

    assert cli.main(["rooms", "watch", "--json"]) == 0

    assert watch.calls == [
        {"since": 0, "last_event_id": None},
        {"since": 3, "last_event_id": 3},
        {"since": 12, "last_event_id": 12},
        {"since": 12, "last_event_id": 12},
    ]
    assert [json.loads(line).get("cursor") for line in _lines(capsys)] == [3, 4, 12]


def test_a_stream_the_server_closed_cleanly_reconnects_too(monkeypatch):
    watch = _watch(monkeypatch, _Connection(_event(3)), _Connection())

    assert cli.main(["rooms", "watch"]) == 0

    assert [c["last_event_id"] for c in watch.calls] == [None, 3, 3]


def test_reconnects_back_off_and_a_frame_resets_the_backoff(monkeypatch):
    watch = _watch(
        monkeypatch,
        httpx.ConnectError("down"),
        httpx.ConnectError("down"),
        httpx.ConnectError("down"),
        _Connection(_event(1)),
        httpx.ConnectError("down"),
    )

    cli.main(["rooms", "watch"])

    assert watch.sleeps == [1.0, 2.0, 4.0, 1.0, 2.0]


def test_a_heartbeat_only_connection_resets_the_backoff(monkeypatch):
    """After an outage drives the delay to its cap, a connection that only ever
    sends ``: ping`` is healthy; its next drop waits 1 s, not 30 s."""
    connected = FeedFrame(kind="comment", id=None, payload={"comment": "connected"})
    ping = FeedFrame(kind="comment", id=None, payload={"comment": "ping"})
    watch = _watch(
        monkeypatch,
        *[httpx.ConnectError("down")] * 6,
        _Connection(connected, ping, ping),
        httpx.ConnectError("down"),
    )

    cli.main(["rooms", "watch"])

    assert watch.sleeps[:6] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]
    assert watch.sleeps[6:] == [1.0, 2.0]


def test_a_groups_outage_backs_off_instead_of_retrying_every_second(monkeypatch):
    """During a groups outage every connection is accepted (``: connected``) and
    then answered with a retryable error. Neither proves the stream healthy, so
    the delay keeps doubling rather than resetting to 1 s on each attempt."""
    connected = FeedFrame(kind="comment", id=None, payload={"comment": "connected"})
    outage = _Connection(connected, _error("groups_unavailable", retry=True))
    watch = _watch(monkeypatch, *[outage] * 5)

    cli.main(["rooms", "watch"])

    assert watch.sleeps == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_the_backoff_is_capped(monkeypatch):
    watch = _watch(monkeypatch, *[httpx.ConnectError("down")] * 8)

    cli.main(["rooms", "watch"])

    assert max(watch.sleeps) == cli._WATCH_BACKOFF_CAP
    assert watch.sleeps[:6] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]


def test_a_retryable_error_frame_reconnects(monkeypatch):
    watch = _watch(
        monkeypatch,
        _Connection(_event(3), _error("groups_unavailable", retry=True)),
        _Connection(),
    )

    assert cli.main(["rooms", "watch"]) == 0

    assert [c["last_event_id"] for c in watch.calls] == [None, 3, 3]


def test_a_non_retryable_error_frame_exits_nonzero(monkeypatch, capsys):
    watch = _watch(
        monkeypatch, _Connection(_error("agent_unavailable", retry=False)), _Connection()
    )

    assert cli.main(["rooms", "watch"]) == 1

    assert len(watch.calls) == 1
    assert "agent_unavailable" in capsys.readouterr().err


def test_a_revoked_token_exits_nonzero_without_reconnecting(monkeypatch, capsys):
    watch = _watch(
        monkeypatch, api_client.ApiError(status=401, body="", detail="invalid bearer token")
    )

    assert cli.main(["rooms", "watch"]) == 1

    assert len(watch.calls) == 1
    assert watch.sleeps == []
    assert "401" in capsys.readouterr().err


@pytest.mark.parametrize("status", [403, 404])
def test_a_token_that_cannot_watch_exits_nonzero(monkeypatch, status):
    watch = _watch(monkeypatch, api_client.ApiError(status=status, body="", detail="no"))

    assert cli.main(["rooms", "watch"]) == 1

    assert len(watch.calls) == 1


def test_a_server_error_is_waited_out(monkeypatch):
    watch = _watch(
        monkeypatch,
        api_client.ApiError(status=503, body="", detail="Groups service is unavailable."),
        _Connection(),
    )

    assert cli.main(["rooms", "watch"]) == 0

    assert len(watch.calls) == 3
    assert watch.sleeps[0] == 1.0


def test_a_negative_since_is_a_usage_error(monkeypatch):
    watch = _watch(monkeypatch)

    assert cli.main(["rooms", "watch", "--since", "-1"]) == 2

    assert watch.calls == []


# --- the client end of the wire ----------------------------------------------------


def _api(handler) -> api_client.ApiClient:
    return api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_agt_abc"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_the_client_sends_since_and_last_event_id_and_parses_ids():
    seen: dict[str, httpx.Request] = {}
    body = (
        ": connected\n\n"
        'id: 7\nevent: message.created\ndata: {"id":7,"kind":"message.created"}\n\n'
        ": ping\n\n"
        'id: 20\nevent: cursor\ndata: {"cursor":20}\n\n'
        'event: error\ndata: {"error":"groups_unavailable","retry":true}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    with _api(handler) as api:
        frames = list(api.watch_agent_events(since=3, last_event_id=5))

    request = seen["request"]
    assert request.url.path == "/api/v1/groups/events/stream"
    assert dict(request.url.params) == {"since": "3"}
    assert request.headers["last-event-id"] == "5"
    assert request.headers["authorization"] == "Bearer pep_agt_abc"
    assert request.headers["accept"] == "text/event-stream"
    assert [(f.kind, f.id) for f in frames] == [
        ("comment", None),
        ("message.created", 7),
        ("comment", None),
        ("cursor", 20),
        ("error", None),
    ]
    assert frames[3].payload == {"cursor": 20}


def test_a_first_connect_sends_no_last_event_id():
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, text="")

    with _api(handler) as api:
        assert list(api.watch_agent_events(since=0)) == []

    assert "last-event-id" not in seen["request"].headers


def test_a_refusal_is_an_api_error_before_any_frame():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid bearer token"})

    with _api(handler) as api, pytest.raises(api_client.ApiError) as raised:
        list(api.watch_agent_events())

    assert raised.value.status == 401


def test_malformed_frames_are_dropped_not_fatal():
    body = (
        'id: nope\nevent: x\ndata: {"a":1}\n\n'
        "event: y\ndata: not json\n\n"
        "event: z\ndata: [1]\n\n"
        'id: 4\nevent: message.created\ndata: {"id":4}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    with _api(handler) as api:
        frames = list(api.watch_agent_events())

    assert [(f.kind, f.id) for f in frames] == [("x", None), ("message.created", 4)]


def test_the_cursor_file_helpers_round_trip(tmp_path: Path):
    path = str(tmp_path / "c")
    assert cli._read_cursor_state(path) is None
    cli._write_cursor_file(path, 42)
    assert cli._read_cursor_state(path) == 42


def test_the_cursor_write_does_not_follow_a_planted_symlink(tmp_path):
    """The temp sibling is created exclusively under an unpredictable name, so a
    symlink planted at the old predictable path cannot redirect the write."""
    import os

    state = tmp_path / "state"
    state.mkdir()
    victim = state / "victim"
    victim.write_text("keep me\n")
    cursor_file = state / "cursor"
    (state / f".cursor.{os.getpid()}.tmp").symlink_to(victim)

    cli._write_cursor_file(str(cursor_file), 7)

    assert victim.read_text() == "keep me\n"
    assert cursor_file.read_text() == "7\n"
    assert sorted(p.name for p in state.iterdir() if not p.is_symlink()) == [
        "cursor",
        "victim",
    ]


def test_a_closed_reader_ends_the_watch_quietly(monkeypatch, capsys):
    """``pepper rooms watch | head -1``: the reader going away is not a
    cursor-file failure and is not reported as one."""
    _watch(monkeypatch, _Connection(_event(1), _event(2)))
    printed: list[str] = []

    def emit(line: str) -> None:
        if printed:
            raise BrokenPipeError
        printed.append(line)

    monkeypatch.setattr(cli, "_emit_line", emit)

    assert cli.main(["rooms", "watch", "--json"]) == 0
    assert "cursor file" not in capsys.readouterr().err


def test_a_retry_says_why_on_stderr_and_keeps_stdout_clean(monkeypatch, capsys):
    """A run of failures is retried, not fatal, but the operator is told what is
    happening once it is no longer a routine drop; stdout stays one line per
    event for the pipe."""
    _watch(
        monkeypatch,
        api_client.ApiError(503, "unavailable"),
        _Connection(_error("groups_unavailable", retry=True)),
        _Connection(_error("groups_unavailable", retry=True)),
    )

    assert cli.main(["rooms", "watch", "--json"]) == 0

    out, err = capsys.readouterr()
    assert out == ""
    # Only the third consecutive failure speaks.
    assert err.count("Feed unavailable") == 1
    assert "groups_unavailable" in err
    assert "HTTP 503" not in err


def test_one_dropped_connection_that_reconnects_prints_nothing_on_stderr(monkeypatch, capsys):
    watch = _watch(
        monkeypatch,
        _Connection(_event(3), then=httpx.RemoteProtocolError("peer closed connection")),
        _Connection(_event(4)),
    )

    assert cli.main(["rooms", "watch", "--json"]) == 0

    out, err = capsys.readouterr()
    assert err == ""
    assert [json.loads(line)["id"] for line in out.splitlines()] == [3, 4]
    assert watch.sleeps == [1.0, 1.0]


def test_two_failures_in_a_row_are_still_quiet(monkeypatch, capsys):
    _watch(monkeypatch, httpx.ConnectError("down"), httpx.ConnectError("down"), _Connection())

    assert cli.main(["rooms", "watch"]) == 0

    assert capsys.readouterr().err == ""


def test_consecutive_failures_print_the_notice_from_the_nth_and_keep_printing(monkeypatch, capsys):
    n = cli._WATCH_QUIET_FAILURES
    _watch(monkeypatch, *[httpx.ConnectError("down")] * (n + 2))

    cli.main(["rooms", "watch"])

    notices = [line for line in capsys.readouterr().err.splitlines() if line]
    assert len(notices) == 3
    assert all(
        line.startswith("error: Feed unavailable (ConnectError); retrying in ") for line in notices
    )
    assert notices[0].endswith("retrying in 4s.")


def test_a_sign_of_life_restarts_the_quiet_count(monkeypatch, capsys):
    """Failures separated by a healthy frame are not one run."""
    ping = FeedFrame(kind="comment", id=None, payload={"comment": "ping"})
    down = httpx.ConnectError("down")
    _watch(monkeypatch, down, down, _Connection(ping, then=down), down, _Connection())

    assert cli.main(["rooms", "watch"]) == 0

    assert capsys.readouterr().err == ""


def test_a_connection_that_only_says_connected_does_not_restart_the_count(monkeypatch, capsys):
    connected = FeedFrame(kind="comment", id=None, payload={"comment": "connected"})
    down = httpx.ConnectError("down")
    _watch(monkeypatch, down, _Connection(connected, then=down), down)

    cli.main(["rooms", "watch"])

    assert capsys.readouterr().err.count("Feed unavailable") == 1


def test_a_server_restarting_frame_resumes_quietly_and_at_once_from_the_cursor(monkeypatch, capsys):
    watch = _watch(
        monkeypatch,
        _Connection(_event(3), _cursor(5), _error("server_restarting", retry=True)),
        _Connection(_event(6), _event(7)),
    )

    assert cli.main(["rooms", "watch", "--json"]) == 0

    out, err = capsys.readouterr()
    assert err == ""
    assert [json.loads(line)["cursor"] for line in out.splitlines()] == [3, 5, 6, 7]
    assert watch.calls[:2] == [
        {"since": 0, "last_event_id": None},
        {"since": 5, "last_event_id": 5},
    ]
    # The only sleep is the one after the second connection ends cleanly; the
    # reconnect after the restarting frame did not wait.
    assert watch.sleeps == [1.0]


def test_repeated_server_restarting_backs_off_after_the_first_immediate_retry(monkeypatch, capsys):
    restarting = _Connection(_error("server_restarting", retry=True))
    watch = _watch(monkeypatch, *[restarting] * 4)

    cli.main(["rooms", "watch"])

    assert watch.sleeps == [1.0, 2.0, 4.0]
    assert capsys.readouterr().err.count("Feed unavailable (server_restarting)") == 2


def test_a_non_retryable_frame_and_fatal_statuses_still_speak_at_once(monkeypatch, capsys):
    _watch(monkeypatch, _Connection(_error("unauthorized", retry=False)))
    assert cli.main(["rooms", "watch"]) == 1
    assert capsys.readouterr().err.startswith("error: The server closed the stream")

    _watch(monkeypatch, api_client.ApiError(status=401, body="", detail="invalid bearer token"))
    assert cli.main(["rooms", "watch"]) == 1
    assert "401" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# --kinds / --ignore-kinds


_CARDS = "message.link_cards_updated"


def _mixed() -> _Connection:
    return _Connection(
        _event(1, "message.created"),
        _event(2, _CARDS),
        _event(3, "mention.created"),
        _event(4, _CARDS),
        _event(5, "member.joined"),
    )


def _printed_ids(capsys) -> list[int]:
    return [json.loads(line)["id"] for line in _lines(capsys)]


def test_no_filter_prints_every_kind(monkeypatch, capsys):
    _watch(monkeypatch, _mixed())
    assert cli.main(["rooms", "watch", "--json"]) == 0
    assert _printed_ids(capsys) == [1, 2, 3, 4, 5]


def test_ignore_kinds_skips_only_the_named_kinds(monkeypatch, capsys):
    _watch(monkeypatch, _mixed())
    assert cli.main(["rooms", "watch", "--json", "--ignore-kinds", _CARDS]) == 0
    assert _printed_ids(capsys) == [1, 3, 5]


def test_kinds_is_an_allow_list(monkeypatch, capsys):
    _watch(monkeypatch, _mixed())
    assert cli.main(["rooms", "watch", "--json", "--kinds", "message.created,mention.created"]) == 0
    assert _printed_ids(capsys) == [1, 3]


def test_ignore_kinds_wins_when_combined_with_kinds(monkeypatch, capsys):
    _watch(monkeypatch, _mixed())
    argv = ["rooms", "watch", "--json", "--kinds", f"message.created,{_CARDS}"]
    argv += ["--ignore-kinds", _CARDS]
    assert cli.main(argv) == 0
    assert _printed_ids(capsys) == [1]


def test_the_filter_applies_to_the_human_output_too(monkeypatch, capsys):
    _watch(monkeypatch, _mixed())
    assert cli.main(["rooms", "watch", "--ignore-kinds", _CARDS]) == 0
    assert [line.split()[:2] for line in _lines(capsys)] == [
        ["#1", "message.created"],
        ["#3", "mention.created"],
        ["#5", "member.joined"],
    ]


def test_an_unknown_kind_is_accepted_and_matches_what_the_service_sends_later(monkeypatch, capsys):
    _watch(monkeypatch, _Connection(_event(1, "thing.invented_next_year"), _event(2)))
    assert cli.main(["rooms", "watch", "--json", "--kinds", "thing.invented_next_year"]) == 0
    assert _printed_ids(capsys) == [1]

    _watch(monkeypatch, _Connection(_event(1, "thing.invented_next_year"), _event(2)))
    assert cli.main(["rooms", "watch", "--json", "--ignore-kinds", "not.a.kind"]) == 0
    assert _printed_ids(capsys) == [1, 2]


def test_a_filtered_event_still_advances_the_cursor_file(monkeypatch, tmp_path, capsys):
    """The last event is a filtered one: a restart must resume after it, not replay it."""
    cursor_file = tmp_path / "cursor"
    _watch(monkeypatch, _Connection(_event(1), _event(2, _CARDS), _event(3, _CARDS)))
    argv = ["rooms", "watch", "--json", "--cursor-file", str(cursor_file)]
    assert cli.main([*argv, "--ignore-kinds", _CARDS]) == 0

    assert _printed_ids(capsys) == [1]
    assert cursor_file.read_text() == "3\n"

    watch = _watch(monkeypatch, _Connection(_event(4)))
    assert cli.main([*argv, "--ignore-kinds", _CARDS]) == 0
    assert watch.calls[0]["since"] == 3


def test_a_filtered_event_advances_the_cursor_the_reconnect_resumes_from(monkeypatch, capsys):
    watch = _watch(
        monkeypatch,
        _Connection(_event(1), _event(2, _CARDS), then=httpx.ReadError("dropped")),
        _Connection(_event(3)),
    )
    assert cli.main(["rooms", "watch", "--json", "--ignore-kinds", _CARDS]) == 0
    assert _printed_ids(capsys) == [1, 3]
    assert [c["since"] for c in watch.calls[:2]] == [0, 2]


def test_a_filtered_event_prints_nothing_not_even_a_cursor_line(monkeypatch, capsys):
    _watch(monkeypatch, _Connection(_event(2, _CARDS)))
    assert cli.main(["rooms", "watch", "--json", "--ignore-kinds", _CARDS]) == 0
    assert _lines(capsys) == []


def test_cursor_frames_are_not_subject_to_the_kind_filter(monkeypatch, capsys):
    _watch(monkeypatch, _Connection(_event(1), _cursor(20)))
    assert cli.main(["rooms", "watch", "--json", "--kinds", "mention.created"]) == 0
    assert [json.loads(line) for line in _lines(capsys)] == [{"kind": "cursor", "cursor": 20}]


@pytest.mark.parametrize("flag", ["--kinds", "--ignore-kinds"])
@pytest.mark.parametrize("value", ["", "a,,b", "a,", ","])
def test_an_empty_kind_entry_is_a_usage_error(monkeypatch, capsys, flag, value):
    watch = _watch(monkeypatch, _mixed())
    with pytest.raises(SystemExit) as exc:
        cli.main(["rooms", "watch", flag, value])
    assert exc.value.code == 2
    assert watch.calls == []
