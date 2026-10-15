"""``pepper rooms watch --room ROOM [--topic TOPIC]`` — the server-scoped stream.

These run the real :class:`api_client.ApiClient` over an ``httpx.MockTransport``
that plays the server, so the routes, query, headers and SSE parsing are the
real ones. Every request is recorded: the fail-closed tests assert that nothing
reached the roster-wide stream, that nothing was printed, and that the cursor
file was left as it was. When the scripted streams run out the server raises
``KeyboardInterrupt``, which is how a person stops the real command.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.api_client import FeedFrame
from pepper_cli.secret import SecretStr

_SERVER = "http://example.com"
_ROOM = "00000000-0000-0000-0000-0000000000a1"
_SURVIVOR = "00000000-0000-0000-0000-0000000000a2"
_OTHER_ROOM = "00000000-0000-0000-0000-0000000000a3"
_SGROUP = "00000000-0000-0000-0000-0000000000e1"
_OTHER_SGROUP = "00000000-0000-0000-0000-0000000000e2"
_TOPIC = "00000000-0000-0000-0000-0000000000b1"
_OTHER_TOPIC = "00000000-0000-0000-0000-0000000000b2"
_AGENT = "00000000-0000-0000-0000-0000000000d1"
_OTHER_AGENT = "00000000-0000-0000-0000-0000000000d2"
_PERSON = "00000000-0000-0000-0000-0000000000d3"
_MESSAGE = "00000000-0000-0000-0000-0000000000c1"

_UNSCOPED_PATH = "/api/v1/groups/events/stream"
_ROOM_PATH = f"/api/v1/groups/{_ROOM}/events/stream"
_TOPIC_PATH = f"/api/v1/groups/{_ROOM}/topics/{_TOPIC}/events/stream"

_CONNECTED = ": connected\n\n"


@pytest.fixture(autouse=True)
def _logged_in(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    credentials.save(server_url=_SERVER, token=SecretStr("pep_agt_abc"))


def _frame(event: str, data: dict, event_id: int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _scope(
    *,
    room: str = _ROOM,
    requested: str = _ROOM,
    topic: str | None = None,
    agent: str = _AGENT,
    group: str = _SGROUP,
    v: int = 1,
) -> str:
    return _frame(
        "scope",
        {
            "v": v,
            "room_id": room,
            "requested_room_id": requested,
            "service_group_id": group,
            "topic_id": topic,
            "agent_person_id": agent,
        },
    )


def _event_data(
    event_id: int,
    *,
    kind: str = "message.created",
    room: str = _ROOM,
    group: str = _SGROUP,
    topic: str | None = _TOPIC,
) -> dict:
    payload = {"message_id": _MESSAGE}
    if topic is not None:
        payload["topic_id"] = topic
    return {
        "id": event_id,
        "kind": kind,
        "group_id": group,
        "room_id": room,
        "person_id": _PERSON,
        "payload": payload,
        "created_at": "2026-10-01T12:00:00+00:00",
    }


def _event(event_id: int, **kw) -> str:
    data = _event_data(event_id, **kw)
    return _frame(data["kind"], data, event_id)


def _cursor(n: int) -> str:
    return _frame("cursor", {"cursor": n}, n)


def _error(slug: str, *, retry: bool, **extra) -> str:
    return _frame("error", {"error": slug, "retry": retry, **extra})


class _Server:
    """Plays the server: the rooms list, a room's topics, and a script of
    stream answers (an SSE body, or an HTTP status)."""

    def __init__(
        self,
        *streams: str | int,
        features: tuple[str, ...] = ("watch_scope",),
        enabled: bool = True,
    ) -> None:
        self.streams = list(streams)
        self.features = features
        self.enabled = enabled
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/v1/groups":
            archived = request.url.params.get("archived") == "1"
            rooms = [] if archived else [_room_row(_ROOM, "Kitchen"), _room_row(_OTHER_ROOM, "Den")]
            return httpx.Response(
                200,
                json={
                    "enabled": self.enabled,
                    "degraded": False,
                    "total": len(rooms),
                    "archived_total": 0,
                    "groups": rooms,
                    "features": list(self.features),
                },
            )
        if path == f"/api/v1/groups/{_ROOM}/topics":
            return httpx.Response(
                200,
                json={"topics": [_topic_row(_TOPIC, "Dinner"), _topic_row(_OTHER_TOPIC, "Chores")]},
            )
        if path.endswith("/events/stream"):
            if not self.streams:
                raise KeyboardInterrupt
            item = self.streams.pop(0)
            if isinstance(item, int):
                return httpx.Response(item, json={"detail": "No such group."})
            return httpx.Response(200, text=item, headers={"content-type": "text/event-stream"})
        return httpx.Response(500, json={"detail": f"unexpected {path}"})

    def stream_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/events/stream")]

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def _room_row(group_id: str, name: str) -> dict:
    return {
        "group_id": group_id,
        "display_name": name,
        "color": "#000000",
        "sub": "",
        "member_count": 2,
        "archived": False,
        "topics": [],
    }


def _topic_row(topic_id: str, name: str) -> dict:
    return {"topic_id": topic_id, "name": name, "last_activity_at": None}


def _serve(monkeypatch, server: _Server) -> list[float]:
    real = api_client.ApiClient

    def make(**kw):
        return real(**kw, client=httpx.Client(transport=httpx.MockTransport(server)))

    monkeypatch.setattr(api_client, "ApiClient", make)
    sleeps: list[float] = []
    monkeypatch.setattr(cli.time, "sleep", sleeps.append)
    return sleeps


def _scoped_file(
    path: Path,
    *,
    cursor: int = 5,
    room: str = _ROOM,
    topic: str | None = None,
    agent: str = _AGENT,
    server: str = _SERVER,
) -> str:
    text = (
        json.dumps(
            {
                "v": 1,
                "server": server,
                "agent_person_id": agent,
                "room_id": room,
                "topic_id": topic,
                "cursor": cursor,
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    path.write_text(text)
    return text


def _assert_nothing_leaked(server: _Server, capsys) -> str:
    out, err = capsys.readouterr()
    assert out == ""
    assert _UNSCOPED_PATH not in server.paths()
    return err


# --- the happy paths -------------------------------------------------------


def test_a_room_watch_streams_the_room_route_and_prints_json_byte_exact(monkeypatch, capsys):
    server = _Server(_CONNECTED + _scope() + _event(7) + _cursor(9))
    _serve(monkeypatch, server)

    assert cli.main(["rooms", "watch", "--room", _ROOM, "--json"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == json.dumps({**_event_data(7), "cursor": 7}, separators=(",", ":"))
    assert lines[1] == '{"kind":"cursor","cursor":9}'
    assert len(lines) == 2, "the scope frame is not printed"
    first = server.stream_requests()[0]
    assert first.url.path == _ROOM_PATH
    assert first.url.params["since"] == "0"
    assert "last-event-id" not in first.headers
    assert _UNSCOPED_PATH not in server.paths()
    # No topic was asked for, so no topic was looked up (not even the default).
    assert f"/api/v1/groups/{_ROOM}/topics" not in server.paths()


def test_a_room_name_resolves_and_a_topic_name_resolves_within_it(monkeypatch, capsys):
    server = _Server(_CONNECTED + _scope(topic=_TOPIC) + _event(7))
    _serve(monkeypatch, server)

    assert cli.main(["rooms", "watch", "--room", "kitch", "--topic", "dinner"]) == 0

    assert server.stream_requests()[0].url.path == _TOPIC_PATH
    (line,) = capsys.readouterr().out.splitlines()
    assert line.startswith("#7 message.created ")
    # One rooms-list fetch serves both the capability check and the name.
    assert server.paths().count("/api/v1/groups") == 1


def test_an_upper_case_room_id_is_sent_and_compared_canonically(monkeypatch, capsys):
    server = _Server(_CONNECTED + _scope() + _event(7))
    _serve(monkeypatch, server)

    assert cli.main(["rooms", "watch", "--room", _ROOM.upper(), "--json"]) == 0

    assert server.stream_requests()[0].url.path == _ROOM_PATH
    assert len(capsys.readouterr().out.splitlines()) == 1


def test_kinds_filters_still_apply_to_a_scoped_watch(monkeypatch, capsys, tmp_path):
    server = _Server(_CONNECTED + _scope() + _event(7, kind="message.edited") + _event(8))
    _serve(monkeypatch, server)
    cursor_file = tmp_path / "c"

    argv = ["rooms", "watch", "--room", _ROOM, "--json", "--kinds", "message.created"]
    assert cli.main([*argv, "--cursor-file", str(cursor_file)]) == 0

    (line,) = capsys.readouterr().out.splitlines()
    assert json.loads(line)["id"] == 8
    assert json.loads(cursor_file.read_text())["cursor"] == 8


# --- arguments and capability, before any stream ---------------------------


def test_topic_without_room_is_a_usage_error_before_any_request(monkeypatch, capsys):
    server = _Server()
    _serve(monkeypatch, server)

    assert cli.main(["rooms", "watch", "--topic", _TOPIC]) == 2

    assert server.requests == []
    assert "--topic needs --room" in capsys.readouterr().err


def test_a_server_without_the_capability_is_refused_not_widened(monkeypatch, capsys):
    server = _Server(_CONNECTED + _event(1), features=())
    _serve(monkeypatch, server)

    assert cli.main(["rooms", "watch", "--room", _ROOM]) == 1

    err = _assert_nothing_leaked(server, capsys)
    assert "does not support a scoped watch" in err
    assert server.stream_requests() == []


def test_groups_switched_off_is_reported_and_nothing_streams(monkeypatch, capsys):
    server = _Server(enabled=False)
    _serve(monkeypatch, server)

    assert cli.main(["rooms", "watch", "--room", _ROOM]) == 1

    assert "Rooms are not enabled" in _assert_nothing_leaked(server, capsys)
    assert server.stream_requests() == []


@pytest.mark.parametrize("room", ["nope", ""], ids=["unknown", "ambiguous"])
def test_a_room_that_does_not_resolve_exits_2(monkeypatch, capsys, room):
    server = _Server(_CONNECTED + _scope() + _event(1))
    _serve(monkeypatch, server)

    assert cli.main(["rooms", "watch", "--room", room]) == 2

    _assert_nothing_leaked(server, capsys)
    assert server.stream_requests() == []


def test_a_topic_that_does_not_resolve_exits_2(monkeypatch, capsys):
    server = _Server()
    _serve(monkeypatch, server)

    assert cli.main(["rooms", "watch", "--room", _ROOM, "--topic", "nope"]) == 2

    assert "no topic named" in _assert_nothing_leaked(server, capsys)
    assert server.stream_requests() == []


@pytest.mark.parametrize("status", [404, 422])
def test_a_refused_scope_is_not_retried(monkeypatch, capsys, status):
    server = _Server(status, _CONNECTED + _scope() + _event(1))
    sleeps = _serve(monkeypatch, server)

    code = cli.main(["rooms", "watch", "--room", _ROOM])

    assert code == (2 if status == 404 else 1)
    err = _assert_nothing_leaked(server, capsys)
    if status == 404:
        assert "not a member of that room" in err
    assert len(server.stream_requests()) == 1
    assert sleeps == []


def test_a_503_at_connect_is_retried_on_the_scoped_route(monkeypatch, capsys):
    server = _Server(503, _CONNECTED + _scope() + _event(4))
    sleeps = _serve(monkeypatch, server)

    assert cli.main(["rooms", "watch", "--room", _ROOM, "--since", "3"]) == 0

    assert [r.url.path for r in server.stream_requests()] == [_ROOM_PATH] * 3
    assert server.stream_requests()[1].headers["last-event-id"] == "3"
    # One wait after the 503, one after the healthy stream closes.
    assert sleeps == [1.0, 1.0]
    assert len(capsys.readouterr().out.splitlines()) == 1


# --- the scope frame -------------------------------------------------------


@pytest.mark.parametrize(
    "first",
    [
        _event(1),
        _cursor(1),
        _scope(requested=_OTHER_ROOM),
        _scope(topic=_TOPIC),
        _scope(v=2),
        _frame("scope", {"v": 1, "room_id": _ROOM}),
        "event: scope\ndata: not json\n\n",
    ],
    ids=[
        "event-first",
        "cursor-first",
        "other-room",
        "topic-when-room-asked",
        "unknown-version",
        "missing-ids",
        "unreadable",
    ],
)
def test_an_unconfirmed_scope_stops_without_printing(monkeypatch, capsys, tmp_path, first):
    server = _Server(_CONNECTED + first + _event(1) + _event(2))
    _serve(monkeypatch, server)
    cursor_file = tmp_path / "c"
    before = _scoped_file(cursor_file, cursor=0)

    argv = ["rooms", "watch", "--room", _ROOM, "--json", "--cursor-file", str(cursor_file)]
    assert cli.main(argv) == 1

    assert "did not confirm the requested scope" in _assert_nothing_leaked(server, capsys)
    assert cursor_file.read_text() == before
    assert len(server.stream_requests()) == 1


def test_a_topic_watch_needs_the_topic_confirmed(monkeypatch, capsys):
    server = _Server(_CONNECTED + _scope(topic=_OTHER_TOPIC) + _event(1, topic=_OTHER_TOPIC))
    _serve(monkeypatch, server)

    assert cli.main(["rooms", "watch", "--room", _ROOM, "--topic", _TOPIC]) == 1

    _assert_nothing_leaked(server, capsys)


def test_a_merged_room_is_followed_and_said_once(monkeypatch, capsys, tmp_path):
    survivor = {"room": _SURVIVOR, "requested": _ROOM}
    server = _Server(
        _CONNECTED + _scope(**survivor) + _event(1, room=_SURVIVOR),
        _CONNECTED + _scope(**survivor) + _event(2, room=_SURVIVOR),
    )
    _serve(monkeypatch, server)
    cursor_file = tmp_path / "c"

    argv = ["rooms", "watch", "--room", _ROOM, "--json", "--cursor-file", str(cursor_file)]
    assert cli.main(argv) == 0

    out, err = capsys.readouterr()
    assert [json.loads(line)["id"] for line in out.splitlines()] == [1, 2]
    assert err.count("was merged into room") == 1
    assert _SURVIVOR in err
    assert json.loads(cursor_file.read_text())["room_id"] == _SURVIVOR


def test_a_reconnect_that_names_another_scope_stops_the_run(monkeypatch, capsys):
    server = _Server(
        _CONNECTED + _scope() + _event(1),
        _CONNECTED + _scope(room=_SURVIVOR) + _event(2, room=_SURVIVOR),
    )
    _serve(monkeypatch, server)

    assert cli.main(["rooms", "watch", "--room", _ROOM, "--json"]) == 1

    out, err = capsys.readouterr()
    assert [json.loads(line)["id"] for line in out.splitlines()] == [1]
    assert "now serving another scope" in err


def test_a_second_scope_frame_on_one_connection_is_checked_too(monkeypatch, capsys):
    server = _Server(_CONNECTED + _scope() + _event(1) + _scope(agent=_OTHER_AGENT) + _event(2))
    _serve(monkeypatch, server)

    assert cli.main(["rooms", "watch", "--room", _ROOM, "--json"]) == 1

    assert [json.loads(x)["id"] for x in capsys.readouterr().out.splitlines()] == [1]


def test_the_scope_frame_does_not_reset_the_backoff(monkeypatch, capsys):
    """During an outage every connection is accepted, scoped, then refused with
    a retryable error: the scope frame must not count as a sign of health."""
    outage = _CONNECTED + _scope() + _error("groups_unavailable", retry=True)
    server = _Server(outage, outage, outage)
    sleeps = _serve(monkeypatch, server)

    assert cli.main(["rooms", "watch", "--room", _ROOM]) == 0

    assert sleeps == [1.0, 2.0, 4.0]
    assert all(r.url.path == _ROOM_PATH for r in server.stream_requests())


# --- events outside the scope ----------------------------------------------


@pytest.mark.parametrize(
    ("argv", "scope", "stray"),
    [
        ([], _scope(), _event(2, room=_OTHER_ROOM)),
        ([], _scope(), _event(2, group=_OTHER_SGROUP)),
        ([], _scope(), _frame("x", {**_event_data(2), "room_id": None}, 2)),
        (["--topic", _TOPIC], _scope(topic=_TOPIC), _event(2, topic=_OTHER_TOPIC)),
        (["--topic", _TOPIC], _scope(topic=_TOPIC), _event(2, kind="member.joined", topic=None)),
        (
            [],
            _scope(),
            _frame(
                "message.created",
                {**_event_data(2), "payload": {"group_id": _OTHER_SGROUP}},
                2,
            ),
        ),
    ],
    ids=["other-room", "other-group", "no-room", "other-topic", "topicless", "payload-group"],
)
def test_an_event_outside_the_scope_is_a_hard_stop(
    monkeypatch, capsys, tmp_path, argv, scope, stray
):
    good = _event(1, topic=_TOPIC)
    server = _Server(_CONNECTED + scope + good + stray + _event(3))
    _serve(monkeypatch, server)
    cursor_file = tmp_path / "c"

    full = ["rooms", "watch", "--room", _ROOM, *argv, "--json", "--cursor-file", str(cursor_file)]
    assert cli.main(full) == 1

    out, err = capsys.readouterr()
    assert [json.loads(line)["id"] for line in out.splitlines()] == [1]
    assert "outside the requested scope" in err
    assert json.loads(cursor_file.read_text())["cursor"] == 1


# --- error frames ----------------------------------------------------------


def test_history_expired_exits_3_and_names_the_gap(monkeypatch, capsys, tmp_path):
    server = _Server(
        _CONNECTED
        + _scope(topic=_TOPIC)
        + _error("history_expired", retry=False, since=5, floor=40)
    )
    _serve(monkeypatch, server)
    cursor_file = tmp_path / "c"
    before = _scoped_file(cursor_file, cursor=5, topic=_TOPIC)

    argv = ["rooms", "watch", "--room", _ROOM, "--topic", _TOPIC, "--cursor-file", str(cursor_file)]
    assert cli.main(argv) == 3

    err = " ".join(_assert_nothing_leaked(server, capsys).split())
    assert "before position 40 is no longer available" in err
    assert "between 5 and 40" in err
    assert f"pepper messages list --room {_ROOM} --topic {_TOPIC}" in err
    assert "--since 40" in err
    assert cursor_file.read_text() == before


def test_history_expired_on_a_room_watch_points_at_each_topic(monkeypatch, capsys):
    server = _Server(
        _CONNECTED + _scope() + _error("history_expired", retry=False, since=0, floor=9)
    )
    _serve(monkeypatch, server)

    assert cli.main(["rooms", "watch", "--room", _ROOM]) == 3

    err = " ".join(capsys.readouterr().err.split())
    assert f"pepper messages list --room {_ROOM} --topic TOPIC' for each topic" in err


@pytest.mark.parametrize(
    ("slug", "words"),
    [
        ("access_revoked", "no longer a member"),
        ("room_deleted", "room was deleted"),
        ("room_superseded", "merged into another room"),
        ("agent_suspended", "will not resume it: agent_suspended"),
    ],
)
def test_terminal_error_frames_end_the_run(monkeypatch, capsys, slug, words):
    server = _Server(_CONNECTED + _scope() + _event(1) + _error(slug, retry=False))
    sleeps = _serve(monkeypatch, server)

    assert cli.main(["rooms", "watch", "--room", _ROOM]) == 1

    out, err = capsys.readouterr()
    assert len(out.splitlines()) == 1
    assert words in " ".join(err.split())
    assert len(server.stream_requests()) == 1
    assert sleeps == []


def test_a_retryable_error_reconnects_to_the_same_scope_from_the_cursor(monkeypatch, capsys):
    server = _Server(
        _CONNECTED + _scope(topic=_TOPIC) + _event(4) + _error("server_restarting", retry=True),
        _CONNECTED + _scope(topic=_TOPIC) + _event(5),
    )
    sleeps = _serve(monkeypatch, server)

    assert cli.main(["rooms", "watch", "--room", _ROOM, "--topic", _TOPIC, "--json"]) == 0

    requests = server.stream_requests()
    assert [r.url.path for r in requests] == [_TOPIC_PATH] * 3
    assert requests[1].headers["last-event-id"] == "4"
    assert sleeps == [1.0]  # the restart retry was immediate; the end-of-script one was not
    assert [json.loads(x)["id"] for x in capsys.readouterr().out.splitlines()] == [4, 5]


# --- the cursor file -------------------------------------------------------


def test_a_new_cursor_file_starts_at_since_and_records_the_scope(monkeypatch, capsys, tmp_path):
    server = _Server(_CONNECTED + _scope(topic=_TOPIC) + _event(12))
    _serve(monkeypatch, server)
    cursor_file = tmp_path / "c"

    argv = ["rooms", "watch", "--room", _ROOM, "--topic", _TOPIC, "--since", "11"]
    assert cli.main([*argv, "--cursor-file", str(cursor_file)]) == 0

    assert server.stream_requests()[0].url.params["since"] == "11"
    assert json.loads(cursor_file.read_text()) == {
        "v": 1,
        "server": _SERVER,
        "agent_person_id": _AGENT,
        "room_id": _ROOM,
        "topic_id": _TOPIC,
        "cursor": 12,
    }


def test_a_matching_cursor_file_resumes_from_its_position(monkeypatch, capsys, tmp_path):
    server = _Server(_CONNECTED + _scope() + _event(6))
    _serve(monkeypatch, server)
    cursor_file = tmp_path / "c"
    _scoped_file(cursor_file, cursor=5)

    assert cli.main(["rooms", "watch", "--room", _ROOM, "--cursor-file", str(cursor_file)]) == 0

    assert server.stream_requests()[0].url.params["since"] == "5"
    assert json.loads(cursor_file.read_text())["cursor"] == 6


def test_since_moves_the_start_of_a_matching_file(monkeypatch, capsys, tmp_path):
    server = _Server(_CONNECTED + _scope() + _event(9))
    _serve(monkeypatch, server)
    cursor_file = tmp_path / "c"
    _scoped_file(cursor_file, cursor=5)

    argv = ["rooms", "watch", "--room", _ROOM, "--since", "8", "--cursor-file", str(cursor_file)]
    assert cli.main(argv) == 0

    assert server.stream_requests()[0].url.params["since"] == "8"


@pytest.mark.parametrize(
    "kw",
    [{"room": _OTHER_ROOM}, {"topic": _TOPIC}, {"agent": _OTHER_AGENT}],
    ids=["other-room", "other-topic", "other-agent"],
)
@pytest.mark.parametrize("since", [None, "3"])
def test_a_cursor_file_from_another_scope_is_refused(monkeypatch, capsys, tmp_path, kw, since):
    server = _Server(_CONNECTED + _scope() + _event(6))
    _serve(monkeypatch, server)
    cursor_file = tmp_path / "c"
    before = _scoped_file(cursor_file, cursor=5, **kw)

    argv = ["rooms", "watch", "--room", _ROOM, "--cursor-file", str(cursor_file)]
    if since is not None:
        argv += ["--since", since]
    assert cli.main(argv) == 2

    err = " ".join(_assert_nothing_leaked(server, capsys).split())
    assert "belongs to a different watch" in err
    assert "pass --since 5 with a new file" in err
    assert cursor_file.read_text() == before


def test_a_cursor_file_for_another_server_is_refused_before_connecting(
    monkeypatch, capsys, tmp_path
):
    server = _Server()
    _serve(monkeypatch, server)
    cursor_file = tmp_path / "c"
    before = _scoped_file(cursor_file, server="http://elsewhere.example")

    assert cli.main(["rooms", "watch", "--room", _ROOM, "--cursor-file", str(cursor_file)]) == 2

    assert "another server" in capsys.readouterr().err
    assert server.requests == []
    assert cursor_file.read_text() == before


@pytest.mark.parametrize("since", [None, "3"])
def test_an_unscoped_cursor_file_is_refused_by_a_scoped_watch(monkeypatch, capsys, tmp_path, since):
    server = _Server()
    _serve(monkeypatch, server)
    cursor_file = tmp_path / "c"
    cursor_file.write_text("17\n")

    argv = ["rooms", "watch", "--room", _ROOM, "--cursor-file", str(cursor_file)]
    if since is not None:
        argv += ["--since", since]
    assert cli.main(argv) == 2

    err = " ".join(capsys.readouterr().err.split())
    assert "holds the unscoped watch's cursor" in err
    assert "--since 17" in err
    assert server.requests == []
    assert cursor_file.read_text() == "17\n"


@pytest.mark.parametrize("since", [None, "3"])
def test_a_scoped_cursor_file_is_refused_by_the_unscoped_watch(
    monkeypatch, capsys, tmp_path, since
):
    server = _Server()
    _serve(monkeypatch, server)
    cursor_file = tmp_path / "c"
    before = _scoped_file(cursor_file)

    argv = ["rooms", "watch", "--cursor-file", str(cursor_file)]
    if since is not None:
        argv += ["--since", since]
    assert cli.main(argv) == 2

    assert "belongs to a scoped watch" in capsys.readouterr().err
    assert server.requests == []
    assert cursor_file.read_text() == before


@pytest.mark.parametrize(
    "text",
    [
        "garbage\n",
        '{"v":2,"server":"http://example.com"}\n',
        (
            '{"v":1,"server":"http://example.com","agent_person_id":"a","room_id":"r",'
            '"topic_id":null,"cursor":-1}\n'
        ),
        (
            '{"v":1,"server":"http://example.com","agent_person_id":"a","room_id":"r",'
            '"topic_id":"","cursor":1}\n'
        ),
        (
            '{"v":1,"server":"http://example.com","agent_person_id":"a","room_id":"r",'
            '"topic_id":null,"cursor":true}\n'
        ),
        "[1]\n",
    ],
)
def test_a_corrupt_cursor_file_is_refused(monkeypatch, capsys, tmp_path, text):
    server = _Server()
    _serve(monkeypatch, server)
    cursor_file = tmp_path / "c"
    cursor_file.write_text(text)

    assert cli.main(["rooms", "watch", "--room", _ROOM, "--cursor-file", str(cursor_file)]) == 2

    assert server.requests == []
    assert cursor_file.read_text() == text


def test_the_scoped_cursor_file_round_trips(tmp_path):
    path = str(tmp_path / "c")
    stored = cli._ScopedCursor(
        server=_SERVER, agent_person_id=_AGENT, room_id=_ROOM, topic_id=None, cursor=3
    )
    cli._write_cursor_file(path, stored)
    assert cli._read_cursor_state(path) == stored


def test_a_cursor_file_write_failure_is_reported(monkeypatch, capsys, tmp_path):
    server = _Server(_CONNECTED + _scope() + _event(1))
    _serve(monkeypatch, server)

    argv = ["rooms", "watch", "--room", _ROOM, "--cursor-file", str(tmp_path / "no" / "c")]
    assert cli.main(argv) == 1

    assert "Couldn't write the cursor file" in capsys.readouterr().err


def test_a_closed_reader_ends_a_scoped_watch_quietly(monkeypatch, capsys):
    server = _Server(_CONNECTED + _scope() + _event(1) + _event(2))
    _serve(monkeypatch, server)
    printed: list[str] = []

    def emit(line: str) -> None:
        if printed:
            raise BrokenPipeError
        printed.append(line)

    monkeypatch.setattr(cli, "_emit_line", emit)

    assert cli.main(["rooms", "watch", "--room", _ROOM, "--json"]) == 0
    assert len(printed) == 1


def test_an_unreachable_server_is_reported(monkeypatch, capsys):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    real = api_client.ApiClient
    monkeypatch.setattr(
        api_client,
        "ApiClient",
        lambda **kw: real(**kw, client=httpx.Client(transport=httpx.MockTransport(down))),
    )

    assert cli.main(["rooms", "watch", "--room", _ROOM]) == 1
    assert "Couldn't reach" in capsys.readouterr().err


# --- the client pieces -----------------------------------------------------


def test_the_rooms_list_reads_features_and_tolerates_their_absence():
    def answer(features):
        def handler(request: httpx.Request) -> httpx.Response:
            body = {"enabled": True, "degraded": False, "total": 0, "archived_total": 0}
            if features is not None:
                body["features"] = features
            return httpx.Response(200, json=body)

        return handler

    for features, expected in [
        (None, ()),
        (["watch_scope", 3, None, "x"], ("watch_scope", "x")),
        (None, ()),
    ]:
        with api_client.ApiClient(
            server_url=_SERVER,
            token=SecretStr("pep_agt_abc"),
            client=httpx.Client(transport=httpx.MockTransport(answer(features))),
        ) as api:
            assert api.list_groups().features == expected


@pytest.mark.parametrize(
    ("payload", "ok"),
    [
        ({}, True),
        ({"topic_id": _TOPIC}, True),
        ({"v": 2}, False),
        ({"room_id": ""}, False),
        ({"agent_person_id": 7}, False),
        ({"topic_id": ""}, False),
        ({"service_group_id": None}, False),
    ],
)
def test_watch_scope_from_frame(payload, ok):
    base = {
        "v": 1,
        "room_id": _ROOM,
        "requested_room_id": _ROOM,
        "service_group_id": _SGROUP,
        "topic_id": None,
        "agent_person_id": _AGENT,
    }
    scope = api_client.watch_scope_from_frame(
        FeedFrame(kind="scope", id=None, payload={**base, **payload})
    )
    assert (scope is not None) is ok
    assert api_client.watch_scope_from_frame(FeedFrame(kind="cursor", id=1, payload=base)) is None


def test_a_negative_since_is_a_usage_error(monkeypatch, capsys):
    server = _Server()
    _serve(monkeypatch, server)

    assert cli.main(["rooms", "watch", "--room", _ROOM, "--since", "-1"]) == 2
    assert server.requests == []


def test_a_refused_token_on_the_rooms_list_is_reported(monkeypatch, capsys):
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "nope"})

    real = api_client.ApiClient
    monkeypatch.setattr(
        api_client,
        "ApiClient",
        lambda **kw: real(**kw, client=httpx.Client(transport=httpx.MockTransport(refuse))),
    )

    assert cli.main(["rooms", "watch", "--room", _ROOM]) == 1
    assert "Authentication failed (401)" in capsys.readouterr().err


def test_a_dropped_connection_reconnects_to_the_scope(monkeypatch, capsys):
    server = _Server(_CONNECTED + _scope() + _event(1), _CONNECTED + _scope() + _event(2))
    real_call = server.__call__
    dropped: list[bool] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.path == _ROOM_PATH and not dropped:
            dropped.append(True)
            raise httpx.ReadTimeout("stalled", request=request)
        return real_call(request)

    real = api_client.ApiClient
    monkeypatch.setattr(
        api_client,
        "ApiClient",
        lambda **kw: real(**kw, client=httpx.Client(transport=httpx.MockTransport(flaky))),
    )
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)

    assert cli.main(["rooms", "watch", "--room", _ROOM, "--json"]) == 0
    assert [json.loads(x)["id"] for x in capsys.readouterr().out.splitlines()] == [1, 2]


def test_ids_compare_as_uuids_and_other_values_exactly():
    assert cli._same_id(_ROOM.upper(), _ROOM)
    assert cli._same_id("not-a-uuid", "not-a-uuid")
    assert not cli._same_id("not-a-uuid", _ROOM)
    assert not cli._same_id(7, _ROOM)
    assert not cli._same_id(None, _ROOM)
    assert cli._same_id(None, None)


def test_history_expired_after_a_merge_points_at_the_surviving_room(monkeypatch, capsys):
    server = _Server(
        _CONNECTED
        + _scope(room=_SURVIVOR, requested=_ROOM)
        + _error("history_expired", retry=False, since=0, floor=9)
    )
    _serve(monkeypatch, server)

    assert cli.main(["rooms", "watch", "--room", _ROOM]) == 3

    err = " ".join(capsys.readouterr().err.split())
    assert f"pepper messages list --room {_SURVIVOR} --topic TOPIC" in err


def test_an_upper_case_room_id_is_canonical_before_the_topic_lookup(monkeypatch, capsys):
    server = _Server(_CONNECTED + _scope(topic=_TOPIC) + _event(1))
    _serve(monkeypatch, server)

    assert cli.main(["rooms", "watch", "--room", _ROOM.upper(), "--topic", "dinner"]) == 0

    assert f"/api/v1/groups/{_ROOM}/topics" in server.paths()
