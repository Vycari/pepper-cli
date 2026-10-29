"""``pepper agents claude-channel``: the Claude Code channel server.

The server runs for real, through ``claude_channel.run`` and the SDK's ``stdio_server`` with
in-memory text streams in place of the process's stdin and stdout (explicit streams skip the fd
claim). A driver thread plays Claude Code: it writes JSON-RPC lines and reads what the server
wrote. HTTP is an ``httpx.MockTransport`` that plays the Pepper server, so the routes, query and
headers are the real ones. No subprocess and no real Claude Code, except the one test that needs
the real fd claim (``test_a_stray_print_...``).
"""

from __future__ import annotations

import functools
import json
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import anyio
import httpx
import mcp_types as types
import pytest
from mcp.server.stdio import stdio_server

from pepper_cli import api_client, claude_channel, cli, credentials
from pepper_cli.secret import SecretStr

_SERVER = "http://example.com"
_ROOM = "00000000-0000-0000-0000-0000000000a1"
_SURVIVOR = "00000000-0000-0000-0000-0000000000a2"
_OTHER_ROOM = "00000000-0000-0000-0000-0000000000a3"
_SGROUP = "00000000-0000-0000-0000-0000000000e1"
_TOPIC = "00000000-0000-0000-0000-0000000000b1"
_OTHER_TOPIC = "00000000-0000-0000-0000-0000000000b2"
_NEW_TOPIC = "00000000-0000-0000-0000-0000000000b3"
_AGENT = "00000000-0000-0000-0000-0000000000d1"
_ALICE = "00000000-0000-0000-0000-0000000000d3"
_BOB = "00000000-0000-0000-0000-0000000000d4"
_MESSAGE = "00000000-0000-0000-0000-0000000000c1"
_MESSAGE_2 = "00000000-0000-0000-0000-0000000000c2"

_UNSCOPED_PATH = "/api/v1/groups/events/stream"
_ROOM_PATH = f"/api/v1/groups/{_ROOM}/events/stream"
_CONNECTED = ": connected\n\n"
_PING = ": ping\n\n"
_META_KEY = re.compile(r"^[A-Za-z0-9_]+$")


@pytest.fixture(autouse=True)
def _logged_in(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    credentials.save(server_url=_SERVER, token=SecretStr("pep_agt_abc"))


@pytest.fixture(autouse=True)
def _no_hard_exit(monkeypatch):
    """``_exit_now`` ends the whole process; here it only records that it was asked to."""
    asked: list[int] = []
    monkeypatch.setattr(claude_channel, "_exit_now", asked.append)
    return asked


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    """Backoff waits last a millisecond, and still end the moment the session does."""

    def wait(self, seconds: float) -> None:
        if self.stop.wait(0.001):
            raise claude_channel._Ended(0)

    monkeypatch.setattr(claude_channel.ChannelWorker, "_wait", wait)


def _now() -> datetime:
    return datetime.now(UTC)


def _frame(event: str, data: dict, event_id: int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _scope(*, room: str = _ROOM, requested: str = _ROOM, topic: str | None = None) -> str:
    return _frame(
        "scope",
        {
            "v": 1,
            "room_id": room,
            "requested_room_id": requested,
            "service_group_id": _SGROUP,
            "topic_id": topic,
            "agent_person_id": _AGENT,
        },
    )


def _event(
    event_id: int,
    *,
    kind: str = "message.created",
    message: str = _MESSAGE,
    topic: str | None = _TOPIC,
    author: str = _ALICE,
    created: datetime | None = None,
    scoped: bool = True,
) -> str:
    payload: dict[str, Any] = {"message_id": message, "author_person_id": author}
    if topic is not None:
        payload["topic_id"] = topic
    data: dict[str, Any] = {
        "id": event_id,
        "kind": kind,
        "group_id": _SGROUP,
        "person_id": _ALICE,
        "payload": payload,
        "created_at": (created or _now()).isoformat(),
    }
    if scoped:
        data["room_id"] = _ROOM
    return _frame(kind, data, event_id)


def _cursor(n: int) -> str:
    return _frame("cursor", {"cursor": n}, n)


def _error(slug: str, *, retry: bool, **extra: Any) -> str:
    return _frame("error", {"error": slug, "retry": retry, **extra})


def _message_json(message_id: str = _MESSAGE, **over: Any) -> dict:
    base = {
        "id": message_id,
        "is_pepper": False,
        "is_you": False,
        "is_deleted": False,
        "is_broken": False,
        "run_start": True,
        "display_name": "Alice Example",
        "initial": "A",
        "avatar_tint": "t1",
        "picture_url": None,
        "raw_body": "Can you check the oven?",
        "rendered_html": "<p>Can you check the oven?</p>",
        "created_at": "2026-10-24T12:00:00+00:00",
        "created_at_display": "12:00 PM",
        "day_key": "2026-10-24",
        "day_kicker": "SATURDAY",
        "day_heading": "October 24, 2026",
        "day_label": "Saturday, October 24",
        "via_display_name": None,
        "is_edited": False,
        "edited_at": None,
        "deleted_at": None,
        "reactions": [],
        "link_previews": [],
        "attachments": [],
        "mention_names": [],
        "quote": None,
        "quote_excerpt": "",
        "cards": [],
        "is_permalink_target": False,
        "author_person_id": _ALICE,
        "mentions": [],
        "parent_message_id": None,
        "is_agent": False,
        "for_display_name": None,
    }
    base.update(over)
    return base


class _Fake:
    """Plays the Pepper server: the rooms list, topics, a script of stream answers (an SSE body,
    or an HTTP status), message reads, posts and reactions. Records every request."""

    def __init__(self, *streams: str | int, features: tuple[str, ...] = ("watch_scope",)) -> None:
        self.streams = list(streams)
        self.features = features
        self.requests: list[httpx.Request] = []
        self.messages: dict[str, dict] = {_MESSAGE: _message_json()}
        self.topics = {_TOPIC: "Dinner", _OTHER_TOPIC: "Chores"}
        self.message_status = 200
        self.fail_messages = 0
        self.fail_status = 503
        self.fail_headers: dict[str, str] = {}
        self.exhausted = threading.Event()
        self.on_stream = None
        self.unreachable = False
        self.room_archived = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/v1/groups":
            room = {
                "group_id": _ROOM,
                "display_name": "Kitchen",
                "color": "#000",
                "sub": "",
                "member_count": 2,
                "archived": self.room_archived,
                "topics": [],
            }
            # ``archived=1`` lists the archived rooms; the default lists the others.
            listed = (request.url.params.get("archived") == "1") == self.room_archived
            return httpx.Response(
                200,
                json={
                    "enabled": True,
                    "degraded": False,
                    "total": 1 if listed else 0,
                    "archived_total": 1 if self.room_archived else 0,
                    "groups": [room] if listed else [],
                    "features": list(self.features),
                },
            )
        if path == f"/api/v1/groups/{_ROOM}/topics":
            rows = [
                {"topic_id": tid, "name": name, "last_activity_at": None}
                for tid, name in self.topics.items()
            ]
            return httpx.Response(200, json={"topics": rows})
        if path.endswith("/events/stream"):
            if self.on_stream is not None:
                self.on_stream()
            if not self.streams:
                self.exhausted.set()
                return httpx.Response(503, json={"detail": "down"})
            item = self.streams.pop(0)
            if isinstance(item, int):
                return httpx.Response(item, json={"detail": "No such group."})
            return httpx.Response(200, text=item, headers={"content-type": "text/event-stream"})
        if path.endswith("/messages") and request.method == "GET":
            if self.unreachable:
                raise httpx.ConnectError("refused")
            if self.fail_messages:
                self.fail_messages -= 1
                return httpx.Response(
                    self.fail_status, json={"detail": "down"}, headers=self.fail_headers
                )
            if self.message_status != 200:
                return httpx.Response(self.message_status, json={"detail": "nope"})
            at = request.url.params.get("at")
            if at is not None and at not in self.messages:
                return httpx.Response(
                    200,
                    json={"messages": [], "next_before": None, "permalink_target_missing": True},
                )
            rows = [self.messages[at]] if at else list(self.messages.values())
            return httpx.Response(
                200,
                json={"messages": rows, "next_before": "older", "permalink_target_missing": False},
            )
        if path.endswith("/messages") and request.method == "POST":
            body = json.loads(request.content)
            row = _message_json(
                "00000000-0000-0000-0000-0000000000f1",
                is_you=True,
                raw_body=body["message"],
                parent_message_id=body.get("parent_message_id"),
            )
            return httpx.Response(200, json={"message": row})
        if path.endswith("/reactions"):
            return httpx.Response(200, json={"reactions": [{"emoji": "👍", "count": 1}]})
        return httpx.Response(500, json={"detail": f"unexpected {path}"})

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def stream_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/events/stream")]


class _Stdin:
    """What Claude Code writes: lines the server reads, then end-of-file."""

    def __init__(self) -> None:
        self._queue: queue.Queue[str | None] = queue.Queue()

    def send(self, line: str) -> None:
        self._queue.put(line)

    def close(self) -> None:
        self._queue.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        line = await anyio.to_thread.run_sync(self._queue.get)
        if line is None:
            raise StopAsyncIteration
        return line


class _Stdout:
    """What the server writes: every chunk, as the writer task wrote it."""

    def __init__(self) -> None:
        self.chunks: list[str] = []

    async def write(self, text: str) -> None:
        self.chunks.append(text)

    async def flush(self) -> None:
        pass

    def messages(self) -> list[dict]:
        return [json.loads(line) for line in "".join(self.chunks).splitlines()]

    def notifications(self) -> list[dict]:
        found = [m for m in self.messages() if m.get("method") == "notifications/claude/channel"]
        return [m["params"] for m in found]

    def replies(self) -> dict[int, dict]:
        return {m["id"]: m for m in self.messages() if "id" in m}


class _Session:
    """The Claude Code side of one run, driven from a thread."""

    def __init__(self, stdin: _Stdin, stdout: _Stdout, fake: _Fake) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.fake = fake
        self._next_id = 100

    def send(self, message: dict, *, eol: str = "\n") -> None:
        self.stdin.send(json.dumps(message) + eol)

    def request(self, method: str, params: dict | None = None, *, eol: str = "\n") -> int:
        self._next_id += 1
        self.send(
            {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}},
            eol=eol,
        )
        return self._next_id

    def handshake(self, version: str = "2025-11-25", *, eol: str = "\n") -> dict:
        request_id = self.request(
            "initialize",
            {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "claude-code", "version": "0"},
            },
            eol=eol,
        )
        reply = self.wait_reply(request_id)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"}, eol=eol)
        return reply

    def wait_for(self, predicate, what: str, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError(f"timed out waiting for {what}")

    def wait_reply(self, request_id: int) -> dict:
        self.wait_for(lambda: request_id in self.stdout.replies(), f"reply {request_id}")
        return self.stdout.replies()[request_id]

    def wait_notifications(self, count: int, settle: float = 0.0) -> list[dict]:
        self.wait_for(lambda: len(self.stdout.notifications()) >= count, f"{count} notifications")
        # Let the worker return its exit code before the session ends the process.
        time.sleep(settle)
        return self.stdout.notifications()

    def wait_exhausted(self) -> None:
        self.wait_for(self.fake.exhausted.is_set, "the stream script to run out")
        # Let the worker finish handling the last frames before the session ends.
        time.sleep(0.05)

    def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        reply = self.wait_reply(self.request("tools/call", {"name": name, "arguments": arguments}))
        result = reply["result"]
        return result["content"][0]["text"], bool(result.get("isError"))


_REAL_CLIENT = api_client.ApiClient


def _install(monkeypatch, fake: _Fake, stdin: _Stdin, stdout: _Stdout) -> None:
    real = _REAL_CLIENT

    def make(**kw):
        return real(**kw, client=httpx.Client(transport=httpx.MockTransport(fake)))

    monkeypatch.setattr(api_client, "ApiClient", make)
    monkeypatch.setattr(
        claude_channel, "stdio_server", lambda **_kw: stdio_server(stdin=stdin, stdout=stdout)
    )
    # The 2-second pause after ``ended`` is for a real session to read it.
    monkeypatch.setattr(
        claude_channel, "serve", functools.partial(claude_channel.serve, grace=0.05)
    )


class _Run:
    def __init__(self, code: int, stdout: _Stdout, fake: _Fake) -> None:
        self.code = code
        self.stdout = stdout
        self.fake = fake

    @property
    def notifications(self) -> list[dict]:
        return self.stdout.notifications()

    def kinds(self) -> list[str]:
        return [n["meta"]["kind"] for n in self.notifications]


def _ended(s: _Session) -> None:
    s.wait_notifications(1, settle=0.3)


def run_channel(
    monkeypatch,
    fake: _Fake,
    argv: list[str] | None = None,
    script=None,
    *,
    handshake: bool = True,
) -> _Run:
    """Run ``pepper agents claude-channel`` against ``fake`` with ``script(session)`` as Claude.

    With no script, the session ends when the stream script has run out. The session's stdin is
    always closed at the end, which is how a real Claude Code ends the process.
    """
    stdin, stdout = _Stdin(), _Stdout()
    _install(monkeypatch, fake, stdin, stdout)
    session = _Session(stdin, stdout, fake)
    failure: list[BaseException] = []

    def drive() -> None:
        try:
            if handshake:
                session.handshake()
            if script is not None:
                script(session)
            else:
                session.wait_exhausted()
        except BaseException as exc:
            failure.append(exc)
        finally:
            stdin.close()

    driver = threading.Thread(target=drive, daemon=True)
    driver.start()
    parser, _ = cli._build_parser()
    args = parser.parse_args(["agents", "claude-channel", *(argv or [])])
    code = claude_channel.run(args)
    driver.join(timeout=10)
    if failure:
        raise failure[0]
    return _Run(code, stdout, fake)


# --- the handshake (design §6, §13.1) ---------------------------------------


def test_initialize_declares_the_channel_and_tools_on_a_handshake_revision(monkeypatch):
    seen: dict = {}

    def script(s: _Session) -> None:
        seen["init"] = s.handshake()

    run_channel(monkeypatch, _Fake(_CONNECTED), script=script, handshake=False)

    result = seen["init"]["result"]
    assert result["protocolVersion"] == "2025-11-25"
    assert result["capabilities"]["experimental"] == {"claude/channel": {}}
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "pepper"


def test_initialize_result_never_answers_the_modern_revision(monkeypatch):
    replies: dict = {}

    def script(s: _Session) -> None:
        replies["asked_modern"] = s.handshake("2026-07-28")

    result = run_channel(monkeypatch, _Fake(_CONNECTED), script=script, handshake=False)

    reply = replies["asked_modern"]["result"]
    assert reply["protocolVersion"] == "2025-11-25"
    assert reply["capabilities"]["experimental"] == {"claude/channel": {}}
    assert reply["capabilities"]["tools"] is not None
    assert reply["serverInfo"]["name"] == "pepper"
    assert reply["instructions"] == claude_channel.INSTRUCTIONS
    assert result.code == 0


@pytest.mark.parametrize("version", ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"])
def test_a_handshake_revision_is_negotiated_as_asked(monkeypatch, version):
    replies: dict = {}

    def script(s: _Session) -> None:
        replies["r"] = s.handshake(version)

    run_channel(monkeypatch, _Fake(_CONNECTED), script=script, handshake=False)

    assert replies["r"]["result"]["protocolVersion"] == version


def test_discover_before_initialize_is_method_not_found_even_with_the_modern_envelope(
    monkeypatch,
):
    replies: dict = {}

    def script(s: _Session) -> None:
        modern = {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}
        replies["modern"] = s.wait_reply(s.request("server/discover", modern))
        replies["bare"] = s.wait_reply(s.request("server/discover"))
        replies["init"] = s.handshake()

    run_channel(monkeypatch, _Fake(_CONNECTED), script=script, handshake=False)

    assert replies["modern"]["error"]["code"] == -32601
    assert replies["bare"]["error"]["code"] == -32601
    assert replies["init"]["result"]["protocolVersion"] == "2025-11-25"


def test_tools_list_names_the_three_p1_tools(monkeypatch):
    replies: dict = {}

    def script(s: _Session) -> None:
        replies["tools"] = s.wait_reply(s.request("tools/list"))

    run_channel(monkeypatch, _Fake(_CONNECTED), script=script)

    tools = replies["tools"]["result"]["tools"]
    assert [t["name"] for t in tools] == ["messages_post", "messages_list", "messages_react"]
    assert all(t["inputSchema"]["type"] == "object" for t in tools)


def test_crlf_line_endings_from_the_client_are_read(monkeypatch):
    replies: dict = {}

    def script(s: _Session) -> None:
        replies["init"] = s.handshake(eol="\r\n")
        replies["tools"] = s.wait_reply(s.request("tools/list", eol="\r\n"))

    result = run_channel(monkeypatch, _Fake(_CONNECTED), script=script, handshake=False)

    assert "tools" in replies["tools"]["result"]
    assert result.code == 0


# --- message events (design §7) ----------------------------------------------


def test_a_message_becomes_one_channel_notification_with_the_body_and_ids(monkeypatch):
    fake = _Fake(_CONNECTED + _scope() + _event(7) + _event(8, kind="mention.created") + _cursor(9))
    fake.messages[_MESSAGE] = _message_json(
        mentions=[_AGENT, _BOB],
        parent_message_id=_MESSAGE_2,
        attachments=[{"id": "x"}, {"id": "y"}],
    )

    result = run_channel(monkeypatch, fake, ["--room", _ROOM])

    (note,) = result.notifications
    assert note["content"] == "Alice Example in Kitchen › Dinner:\nCan you check the oven?"
    assert note["meta"] == {
        "kind": "message",
        "room_id": _ROOM,
        "topic_id": _TOPIC,
        "message_id": _MESSAGE,
        "self_id": _AGENT,
        "parent_id": _MESSAGE_2,
        "sender_id": _ALICE,
        "sender_kind": "human",
        "mentions": f"{_AGENT},{_BOB}",
        "created_at": "2026-10-24T12:00:00+00:00",
        "attachments": "2",
    }
    assert all(_META_KEY.match(k) and isinstance(v, str) for k, v in note["meta"].items())
    reads = [r for r in fake.requests if r.method == "GET" and r.url.path.endswith("/messages")]
    assert [r.url.params["at"] for r in reads] == [_MESSAGE], "the mention is the same message"
    assert reads[0].url.path == f"/api/v1/groups/{_ROOM}/topics/{_TOPIC}/messages"
    assert result.code == 0


def test_sender_kind_is_agent_or_pepper_and_a_top_level_post_has_no_parent(monkeypatch):
    fake = _Fake(_CONNECTED + _scope() + _event(7) + _event(8, message=_MESSAGE_2))
    fake.messages[_MESSAGE] = _message_json(is_agent=True)
    fake.messages[_MESSAGE_2] = _message_json(_MESSAGE_2, is_pepper=True)

    result = run_channel(monkeypatch, fake, ["--room", _ROOM])

    kinds = [n["meta"]["sender_kind"] for n in result.notifications]
    assert kinds == ["agent", "pepper"]
    assert all("parent_id" not in n["meta"] for n in result.notifications)
    assert all("truncated" not in n["meta"] for n in result.notifications)


def test_an_overlong_body_is_cut_and_marked(monkeypatch):
    fake = _Fake(_CONNECTED + _scope() + _event(7))
    fake.messages[_MESSAGE] = _message_json(raw_body="x" * 20_000)

    result = run_channel(monkeypatch, fake, ["--room", _ROOM])

    (note,) = result.notifications
    assert len(note["content"]) == claude_channel.CONTENT_CAP
    assert note["meta"]["truncated"] == "true"


def test_a_name_with_control_characters_is_cleaned_in_the_header_line(monkeypatch):
    fake = _Fake(_CONNECTED + _scope() + _event(7))
    fake.messages[_MESSAGE] = _message_json(display_name="Al\x1b[31mice\nEvil")

    result = run_channel(monkeypatch, fake, ["--room", _ROOM])

    header = result.notifications[0]["content"].split("\n", 1)[0]
    assert "\x1b" not in header
    assert header.endswith("in Kitchen › Dinner:")


def test_a_body_that_cannot_be_found_is_pushed_as_missing_not_dropped(monkeypatch):
    fake = _Fake(_CONNECTED + _scope() + _event(7, message=_MESSAGE_2))

    result = run_channel(monkeypatch, fake, ["--room", _ROOM])

    (note,) = result.notifications
    assert note["meta"]["body"] == "missing"
    assert note["meta"]["message_id"] == _MESSAGE_2
    assert note["meta"]["sender_id"] == _ALICE
    assert "could not be read" in note["content"]


@pytest.mark.parametrize("status", [404, 410, 422])
def test_a_message_read_the_server_refuses_is_pushed_as_missing_and_the_channel_goes_on(
    monkeypatch, status
):
    fake = _Fake(_CONNECTED + _scope() + _event(7) + _event(8, message=_MESSAGE_2))
    fake.message_status = status

    result = run_channel(monkeypatch, fake, ["--room", _ROOM])

    assert result.kinds() == ["message", "message"], "a refused read ends nothing"
    assert [n["meta"]["body"] for n in result.notifications] == ["missing", "missing"]
    assert result.code == 0


def test_edited_deleted_link_cards_and_member_events_are_consumed_silently(monkeypatch, tmp_path):
    cursor_file = tmp_path / "cursor"
    fake = _Fake(
        _CONNECTED
        + _scope()
        + _event(7, kind="message.edited")
        + _event(8, kind="message.deleted")
        + _event(9, kind="message.link_cards_updated")
        + _event(10, kind="member.joined", topic=None)
        + _cursor(11)
    )

    result = run_channel(monkeypatch, fake, ["--room", _ROOM, "--cursor-file", str(cursor_file)])

    assert result.notifications == []
    assert json.loads(cursor_file.read_text())["cursor"] == 11


def test_the_agents_own_post_is_dropped_before_any_fetch_and_the_cursor_moves(
    monkeypatch, tmp_path
):
    cursor_file = tmp_path / "cursor"
    fake = _Fake(_CONNECTED + _scope() + _event(7, author=_AGENT))

    result = run_channel(monkeypatch, fake, ["--room", _ROOM, "--cursor-file", str(cursor_file)])

    assert result.notifications == []
    assert not [r for r in fake.requests if r.url.path.endswith("/messages")]
    assert json.loads(cursor_file.read_text())["cursor"] == 7


def test_self_flag_drops_own_posts_on_an_unscoped_stream_and_is__you_is_the_fallback(
    monkeypatch,
):
    fake = _Fake(
        _CONNECTED
        + _event(7, author=_BOB, scoped=False)
        + _event(8, author=_ALICE, message=_MESSAGE_2, scoped=False)
    )
    fake.messages[_MESSAGE_2] = _message_json(_MESSAGE_2, is_you=True)

    result = run_channel(monkeypatch, fake, ["--self", _BOB])

    assert result.notifications == [], "7 is --self's; 8 is read and is_you"
    reads = [r for r in fake.requests if r.method == "GET" and r.url.path.endswith("/messages")]
    assert [r.url.params["at"] for r in reads] == [_MESSAGE_2]


def test_an_unscoped_stream_maps_the_topic_to_its_room_and_refreshes_on_a_miss(monkeypatch):
    fake = _Fake(
        _CONNECTED
        + _event(7, scoped=False)
        + _event(8, message=_MESSAGE_2, topic=_NEW_TOPIC, scoped=False)
    )
    fake.messages[_MESSAGE_2] = _message_json(_MESSAGE_2)
    fake.on_stream = lambda: fake.topics.update({_NEW_TOPIC: "Dessert"})

    result = run_channel(monkeypatch, fake)

    assert [n["meta"]["topic_id"] for n in result.notifications] == [_TOPIC, _NEW_TOPIC]
    assert {n["meta"]["room_id"] for n in result.notifications} == {_ROOM}
    assert "Kitchen › Dessert" in result.notifications[1]["content"]
    assert fake.stream_requests()[0].url.path == _UNSCOPED_PATH


@pytest.mark.parametrize("scoped", [True, False])
def test_an_archived_rooms_messages_are_placed_and_pushed(monkeypatch, scoped):
    fake = _Fake(_CONNECTED + (_scope() if scoped else "") + _event(7, scoped=scoped))
    fake.room_archived = True

    result = run_channel(monkeypatch, fake, ["--room", _ROOM] if scoped else [])

    assert result.kinds() == ["message"]
    assert "Kitchen › Dinner" in result.notifications[0]["content"]


def test_a_topic_the_map_cannot_place_is_a_single_resync(monkeypatch):
    fake = _Fake(
        _CONNECTED
        + _event(7, topic="00000000-0000-0000-0000-0000000000bf", scoped=False)
        + _event(8, kind="some.new.kind", topic=None, scoped=False)
        + _event(9, kind="another.new.kind", topic=None, scoped=False)
        + _PING
    )

    result = run_channel(monkeypatch, fake)

    assert result.kinds() == ["resync"], "a burst gives one push"


def test_every_message_tag_carries_the_agents_own_person_id(monkeypatch):
    """Discovered from the scope frame, so it is on the first message though the instructions
    were sent before the stream opened."""
    fake = _Fake(_CONNECTED + _scope() + _event(7))

    result = run_channel(monkeypatch, fake, ["--room", _ROOM])

    assert result.notifications[0]["meta"]["self_id"] == _AGENT


def test_self_given_at_start_is_the_self_id_and_an_unknown_one_is_omitted(monkeypatch):
    unscoped = _Fake(_CONNECTED + _event(7, scoped=False))
    given = run_channel(monkeypatch, unscoped, ["--self", _AGENT])
    assert given.notifications[0]["meta"]["self_id"] == _AGENT

    unknown = run_channel(monkeypatch, _Fake(_CONNECTED + _event(7, scoped=False)))
    assert "self_id" not in unknown.notifications[0]["meta"]


def test_an_unreadable_message_still_carries_the_self_id():
    _, meta = claude_channel.message_event(
        info=claude_channel.TopicInfo(_ROOM, "Kitchen", "Dinner"),
        topic_id=_TOPIC,
        message_id=_MESSAGE,
        row=None,
        event={},
        self_id=_AGENT,
    )
    assert meta["self_id"] == _AGENT


def test_a_resync_names_its_cause_gives_room_ids_and_is_safe_on_a_first_run(monkeypatch):
    fake = _Fake(_CONNECTED + _scope() + _event(7, kind="some.new.kind") + _PING)

    result = run_channel(monkeypatch, fake, ["--room", _ROOM])

    (note,) = result.notifications
    assert note["meta"] == {"kind": "resync"}
    assert "some.new.kind" in note["content"]
    assert "If you keep checkpoints" in note["content"]
    assert f"{_TOPIC} (room_id {_ROOM}; Kitchen › Dinner)" in note["content"]


def test_a_first_run_replay_of_old_room_events_owes_no_resync(monkeypatch):
    """The retained window on a first run holds old room events of every kind (a rename, an
    app install); none was missed, and the backlog already says to catch up."""
    old = _now() - timedelta(days=3)
    fake = _Fake(
        _CONNECTED
        + _scope()
        + _event(3, created=old)
        + _event(4, kind="group.renamed", created=old)
        + _event(5, kind="app.installed", created=old)
        + _PING
    )

    result = run_channel(monkeypatch, fake, ["--room", _ROOM])

    assert result.kinds() == ["backlog"]


def test_an_old_unknown_event_after_a_restart_still_owes_a_resync(monkeypatch, tmp_path):
    cursor_file = _scoped_cursor_file(tmp_path / "cursor", 3)
    old = _now() - timedelta(days=3)
    fake = _Fake(_CONNECTED + _scope() + _event(4, kind="group.renamed", created=old) + _PING)

    result = run_channel(monkeypatch, fake, ["--room", _ROOM, "--cursor-file", str(cursor_file)])

    assert result.kinds() == ["resync"]


def test_a_link_previews_event_is_silent(monkeypatch):
    fake = _Fake(_CONNECTED + _scope() + _event(7, kind="message.link_previews_updated") + _PING)

    assert run_channel(monkeypatch, fake, ["--room", _ROOM]).notifications == []


def test_a_resync_is_pushed_before_the_next_message(monkeypatch):
    fake = _Fake(_CONNECTED + _scope() + _event(7, kind="new.kind") + _event(8, message=_MESSAGE))

    result = run_channel(monkeypatch, fake, ["--room", _ROOM])

    assert result.kinds() == ["resync", "message"]


def test_kinds_and_ignore_kinds_narrow_what_is_pushed_and_the_cursor_still_moves(
    monkeypatch, tmp_path
):
    cursor_file = tmp_path / "cursor"
    body = _CONNECTED + _scope() + _event(7, kind="mention.created") + _event(8, kind="new.kind")
    fake = _Fake(body)

    result = run_channel(
        monkeypatch,
        fake,
        ["--room", _ROOM, "--kinds", "message.created", "--cursor-file", str(cursor_file)],
    )

    assert result.notifications == []
    assert json.loads(cursor_file.read_text())["cursor"] == 8

    fake = _Fake(body)
    result = run_channel(
        monkeypatch, fake, ["--room", _ROOM, "--ignore-kinds", "mention.created,new.kind"]
    )
    assert result.notifications == []


# --- the cursor (design §7.4) ------------------------------------------------


def test_the_cursor_file_moves_after_the_notification_is_handed_over(monkeypatch, tmp_path):
    events: list[str] = []
    real_write = claude_channel._write_cursor_file

    def recording_write(path, cursor):
        events.append(f"cursor:{getattr(cursor, 'cursor', cursor)}")
        real_write(path, cursor)

    class Sink:
        def push(self, content, meta):
            events.append(f"push:{meta['kind']}")

    monkeypatch.setattr(claude_channel, "_write_cursor_file", recording_write)
    fake = _Fake(_CONNECTED + _scope() + _event(7) + _cursor(9))
    api = api_client.ApiClient(
        server_url=_SERVER,
        token=SecretStr("pep_agt_abc"),
        client=httpx.Client(transport=httpx.MockTransport(fake)),
    )
    parser, _ = cli._build_parser()
    args = parser.parse_args(["agents", "claude-channel", "--room", _ROOM])
    config = claude_channel.ChannelConfig(
        server=_SERVER,
        room=_ROOM,
        topic=None,
        cursor_file=str(tmp_path / "cursor"),
        kinds=None,
        ignore_kinds=frozenset(),
        self_id=None,
        started_at=_now(),
    )
    stop = threading.Event()
    worker = claude_channel.ChannelWorker(
        api,
        config,
        Sink(),
        args,
        room_id=_ROOM,
        topic_id=None,
        topics={_TOPIC: claude_channel.TopicInfo(_ROOM, "Kitchen", "Dinner")},
        stored=None,
        cursor=0,
        allowed_rooms={_ROOM},
        stop=stop,
    )
    threading.Timer(0.3, stop.set).start()

    assert worker.run() == 0

    assert events == ["push:message", "cursor:7", "cursor:9"]


def test_a_restart_pushes_one_backlog_summary_and_no_old_messages(monkeypatch, tmp_path):
    cursor_file = tmp_path / "cursor"
    cursor_file.write_text(
        json.dumps(
            {
                "v": 1,
                "server": _SERVER,
                "agent_person_id": _AGENT,
                "room_id": _ROOM,
                "topic_id": None,
                "cursor": 3,
            }
        )
    )
    old = _now() - timedelta(hours=2)
    fake = _Fake(
        _CONNECTED
        + _scope()
        + _event(4, created=old)
        + _event(5, message=_MESSAGE_2, created=old + timedelta(minutes=5))
        + _PING
    )

    result = run_channel(monkeypatch, fake, ["--room", _ROOM, "--cursor-file", str(cursor_file)])

    (note,) = result.notifications
    assert note["meta"]["kind"] == "backlog"
    assert note["meta"]["count"] == "2"
    assert note["meta"]["oldest"] == old.isoformat()
    assert f"{_TOPIC} (room_id {_ROOM}; Kitchen › Dinner)" in note["content"]
    assert json.loads(cursor_file.read_text())["cursor"] == 5
    assert fake.stream_requests()[0].url.params["since"] == "3"
    assert not [r for r in fake.requests if r.url.path.endswith("/messages")]


def test_a_first_run_says_so_and_gives_no_count(monkeypatch):
    old = _now() - timedelta(days=1)
    fake = _Fake(_CONNECTED + _scope() + _event(4, created=old) + _PING)

    result = run_channel(monkeypatch, fake, ["--room", _ROOM])

    (note,) = result.notifications
    assert note["meta"] == {"kind": "backlog", "first_run": "true"}
    assert "first run" in note["content"]
    assert f"room_id {_ROOM}" in note["content"]


def test_nothing_is_pushed_for_a_quiet_restart(monkeypatch):
    fake = _Fake(_CONNECTED + _scope() + _PING)

    assert run_channel(monkeypatch, fake, ["--room", _ROOM]).notifications == []


def test_the_backlog_is_pushed_before_the_first_live_message(monkeypatch):
    old = _now() - timedelta(hours=2)
    fake = _Fake(_CONNECTED + _scope() + _event(4, created=old) + _event(5, message=_MESSAGE_2))
    fake.messages[_MESSAGE_2] = _message_json(_MESSAGE_2)

    result = run_channel(monkeypatch, fake, ["--room", _ROOM])

    assert result.kinds() == ["backlog", "message"]


def _scoped_cursor_file(path: Path, cursor: int) -> Path:
    path.write_text(
        json.dumps(
            {
                "v": 1,
                "server": _SERVER,
                "agent_person_id": _AGENT,
                "room_id": _ROOM,
                "topic_id": None,
                "cursor": cursor,
            }
        )
    )
    return path


@pytest.mark.parametrize(
    ("event", "notice"),
    [
        (_event(4, created=_now() - timedelta(hours=2)), "backlog"),
        (_event(4, kind="some.new.kind"), "resync"),
    ],
)
def test_a_restart_before_a_pending_notice_is_pushed_still_owes_it(
    monkeypatch, tmp_path, event, notice
):
    cursor_file = _scoped_cursor_file(tmp_path / "cursor", 3)
    argv = ["--room", _ROOM, "--cursor-file", str(cursor_file)]

    # The session ends after the event and before the heartbeat that would push the notice.
    stopped = run_channel(monkeypatch, _Fake(_CONNECTED + _scope() + event + _cursor(6)), argv)

    assert stopped.notifications == []
    assert json.loads(cursor_file.read_text())["cursor"] == 3, "held behind the owed notice"

    restarted = run_channel(monkeypatch, _Fake(_CONNECTED + _scope() + event + _PING), argv)

    assert restarted.kinds() == [notice]
    assert json.loads(cursor_file.read_text())["cursor"] == 4


def test_a_pre_session_message_after_the_first_heartbeat_is_still_reported(monkeypatch):
    old = _now() - timedelta(hours=2)
    fake = _Fake(_CONNECTED + _scope() + _PING + _event(7, created=old) + _PING)

    result = run_channel(monkeypatch, fake, ["--room", _ROOM])

    assert result.kinds() == ["backlog"]


# --- reconnects, gaps and the end (design §9.4) ------------------------------


def test_a_dropped_connection_reconnects_from_the_cursor_without_telling_the_session(monkeypatch):
    fake = _Fake(
        _CONNECTED + _scope() + _event(7),
        503,
        _CONNECTED + _scope() + _event(8, message=_MESSAGE_2),
    )
    fake.messages[_MESSAGE_2] = _message_json(_MESSAGE_2)

    result = run_channel(monkeypatch, fake, ["--room", _ROOM])

    assert result.kinds() == ["message", "message"]
    resumes = [r.headers.get("last-event-id") for r in fake.stream_requests()[:3]]
    assert resumes == [None, "7", "7"]


def test_history_expired_pushes_a_gap_and_resumes_from_the_floor(monkeypatch, tmp_path):
    cursor_file = tmp_path / "cursor"
    fake = _Fake(
        _CONNECTED + _scope() + _error("history_expired", retry=True, since=5, floor=40),
        _CONNECTED + _scope() + _event(41),
    )

    result = run_channel(monkeypatch, fake, ["--room", _ROOM, "--cursor-file", str(cursor_file)])

    gap, message = result.notifications
    assert gap["meta"] == {
        "kind": "gap",
        "room_id": _ROOM,
        "since": "5",
        "floor": "40",
    }
    assert "5" in gap["content"]
    assert "40" in gap["content"]
    assert message["meta"]["kind"] == "message"
    assert fake.stream_requests()[1].url.params["since"] == "40"
    assert json.loads(cursor_file.read_text())["cursor"] == 41


@pytest.mark.parametrize(
    ("answer", "needle"),
    [
        (401, "token"),
        (403, "not an agent"),
        (404, "no stream"),
        (_error("agent_unavailable", retry=False), "paused or retired"),
        (_error("access_revoked", retry=False), "no longer a member"),
        (_error("unauthorized", retry=False), "token"),
        (_error("room_deleted", retry=False), "deleted"),
        (_error("never_heard_of_it", retry=False), "never_heard_of_it"),
    ],
)
def test_a_terminal_condition_pushes_one_ended_event_and_exits_1(monkeypatch, answer, needle):
    stream = answer if isinstance(answer, int) else _CONNECTED + _scope() + answer
    fake = _Fake(stream)

    result = run_channel(monkeypatch, fake, ["--room", _ROOM], script=_ended)

    (note,) = result.notifications
    assert note["meta"] == {"kind": "ended"}
    assert needle in note["content"]
    assert "Do not retry" in note["content"]
    assert result.code == 1
    assert len(fake.stream_requests()) == 1, "a terminal condition is not retried"


def test_a_scope_the_server_did_not_confirm_ends_the_channel(monkeypatch):
    fake = _Fake(_CONNECTED + _scope(requested=_OTHER_ROOM) + _event(7))

    result = run_channel(monkeypatch, fake, ["--room", _ROOM], script=_ended)

    assert result.kinds() == ["ended"]
    assert "did not confirm" in result.notifications[0]["content"]
    assert result.code == 1


def test_an_event_from_outside_the_scope_ends_the_channel(monkeypatch):
    stray = _event(7).replace(_ROOM, _OTHER_ROOM)
    fake = _Fake(_CONNECTED + _scope() + stray)

    result = run_channel(monkeypatch, fake, ["--room", _ROOM], script=_ended)

    assert result.kinds() == ["ended"]
    assert result.code == 1


def test_a_room_merged_into_another_is_followed_and_its_tools_work(monkeypatch, capsys):
    fake = _Fake(_CONNECTED + _scope(room=_SURVIVOR, requested=_ROOM))
    results: dict = {}

    def script(s: _Session) -> None:
        s.wait_exhausted()
        results["survivor"] = s.call("messages_list", {"room_id": _SURVIVOR, "topic_id": _TOPIC})
        results["other"] = s.call("messages_list", {"room_id": _OTHER_ROOM, "topic_id": _TOPIC})

    run_channel(monkeypatch, fake, ["--room", _ROOM], script=script)

    assert results["survivor"][1] is False
    assert results["other"][1] is True
    assert f"merged into room {_SURVIVOR}" in capsys.readouterr().err


# --- startup (before initialize) ---------------------------------------------


def test_topic_without_room_is_a_usage_error_before_any_request(monkeypatch, capsys):
    fake = _Fake()
    parser, _ = cli._build_parser()
    args = parser.parse_args(["agents", "claude-channel", "--topic", _TOPIC])

    assert claude_channel.run(args) == 2

    assert fake.requests == []
    assert "--topic needs --room" in capsys.readouterr().err


def test_no_credentials_exit_1_on_stderr(capsys):
    credentials.set_profile("nobody")
    parser, _ = cli._build_parser()
    args = parser.parse_args(["agents", "claude-channel"])

    assert claude_channel.run(args) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err


def test_a_server_without_the_scope_capability_is_refused(monkeypatch, capsys):
    fake = _Fake(_CONNECTED, features=())
    stdin, stdout = _Stdin(), _Stdout()
    _install(monkeypatch, fake, stdin, stdout)
    parser, _ = cli._build_parser()
    args = parser.parse_args(["agents", "claude-channel", "--room", _ROOM])

    assert claude_channel.run(args) == 1

    assert fake.stream_requests() == []
    assert "does not support a scoped watch" in capsys.readouterr().err
    assert stdout.chunks == []


def test_an_unresolvable_room_exits_2(monkeypatch, capsys):
    fake = _Fake(_CONNECTED)
    stdin, stdout = _Stdin(), _Stdout()
    _install(monkeypatch, fake, stdin, stdout)
    parser, _ = cli._build_parser()
    args = parser.parse_args(["agents", "claude-channel", "--room", "no such room"])

    assert claude_channel.run(args) == 2

    assert "no room named" in capsys.readouterr().err
    assert fake.stream_requests() == []


def test_a_cursor_file_another_watch_wrote_is_refused(monkeypatch, capsys, tmp_path):
    parser, _ = cli._build_parser()
    stdin, stdout = _Stdin(), _Stdout()
    fake = _Fake(_CONNECTED)
    _install(monkeypatch, fake, stdin, stdout)
    bare = tmp_path / "bare"
    bare.write_text("12\n")
    scoped = tmp_path / "scoped"
    scoped.write_text(
        json.dumps(
            {
                "v": 1,
                "server": "http://other.example",
                "agent_person_id": _AGENT,
                "room_id": _ROOM,
                "topic_id": None,
                "cursor": 3,
            }
        )
    )
    junk = tmp_path / "junk"
    junk.write_text("not a cursor")
    base = ["agents", "claude-channel"]

    assert (
        claude_channel.run(parser.parse_args([*base, "--room", _ROOM, "--cursor-file", str(bare)]))
        == 2
    )
    assert "unscoped watch's cursor" in capsys.readouterr().err
    assert (
        claude_channel.run(
            parser.parse_args([*base, "--room", _ROOM, "--cursor-file", str(scoped)])
        )
        == 2
    )
    assert "another server" in capsys.readouterr().err
    assert claude_channel.run(parser.parse_args([*base, "--cursor-file", str(scoped)])) == 2
    assert "belongs to a scoped watch" in capsys.readouterr().err
    assert claude_channel.run(parser.parse_args([*base, "--cursor-file", str(junk)])) == 2
    assert fake.stream_requests() == []


def test_an_unscoped_run_resumes_from_a_bare_cursor_file(monkeypatch, tmp_path):
    cursor_file = tmp_path / "cursor"
    cursor_file.write_text("12\n")
    fake = _Fake(_CONNECTED + _event(13, author=_BOB, scoped=False))

    run_channel(monkeypatch, fake, ["--self", _BOB, "--cursor-file", str(cursor_file)])

    assert fake.stream_requests()[0].url.path == _UNSCOPED_PATH
    assert fake.stream_requests()[0].url.params["since"] == "12"
    assert cursor_file.read_text().strip() == "13"


def test_a_scoped_cursor_file_for_another_room_ends_the_channel(monkeypatch, tmp_path):
    cursor_file = tmp_path / "cursor"
    cursor_file.write_text(
        json.dumps(
            {
                "v": 1,
                "server": _SERVER,
                "agent_person_id": _AGENT,
                "room_id": _OTHER_ROOM,
                "topic_id": None,
                "cursor": 3,
            }
        )
    )
    fake = _Fake(_CONNECTED + _scope() + _event(7))

    result = run_channel(
        monkeypatch,
        fake,
        ["--room", _ROOM, "--cursor-file", str(cursor_file)],
        script=lambda s: s.wait_notifications(1),
    )

    assert result.kinds() == ["ended"]
    assert result.code == 2


def test_serve_returns_the_verdict_of_a_worker_that_pushed_ended_before_stdin_closed(monkeypatch):
    """The session may hang up the moment it reads ``ended``; the exit code still counts."""
    monkeypatch.setattr(claude_channel, "VERDICT_WAIT_SECONDS", 30.0)
    stdin, stdout = _Stdin(), _Stdout()
    stop = threading.Event()  # set by ``serve`` once it is shutting down
    served = threading.Event()  # set by this test once ``serve`` has returned
    errors: list[BaseException] = []

    def work(sink) -> int:
        sink.push(*claude_channel.control_event("ended", "stopped"))
        # Return only after ``serve`` has seen EOF and, unless it waits for this verdict, after it
        # has returned too: without the wait ``serve`` returns first, sets nothing here, and the
        # test sees 0. The timeout bounds only the passing path, where ``serve`` is blocked on us.
        stop.wait(10)
        served.wait(1.0)
        return 2

    def drive() -> None:
        try:
            session = _Session(stdin, stdout, _Fake(_CONNECTED))
            session.handshake()
            session.wait_notifications(1)
        except BaseException as exc:
            errors.append(exc)
        finally:
            stdin.close()

    threading.Thread(target=drive, daemon=True).start()
    server = claude_channel.build_server(claude_channel.ChannelTools(None, None))  # ty: ignore[invalid-argument-type]
    try:
        code = anyio.run(
            functools.partial(
                claude_channel.serve, server, work, stdin=stdin, stdout=stdout, stop=stop
            )
        )
    finally:
        served.set()

    assert not errors, errors
    assert code == 2


def test_an_ended_push_marks_the_pusher_ending_even_when_the_channel_has_closed():
    """``ending`` goes up before the ``closed`` check, so ``serve`` cannot slip between them."""
    pusher = claude_channel._Pusher(None, None)  # ty: ignore[invalid-argument-type]
    pusher.closed = True

    with pytest.raises(claude_channel.ChannelClosedError):
        pusher.push(*claude_channel.control_event("ended", "stopped"))

    assert pusher.ending.is_set()


# --- the tools (design §9.1, §9.2) -------------------------------------------


def test_messages_post_sends_what_messages_post_sends(monkeypatch):
    fake = _Fake(_CONNECTED + _scope())
    out: dict = {}

    def script(s: _Session) -> None:
        out["post"] = s.call(
            "messages_post",
            {
                "room_id": _ROOM,
                "topic_id": _TOPIC,
                "text": "  On it.  ",
                "reply_to": _MESSAGE,
                "on_behalf_of": _ALICE,
                "mentions": [_BOB],
            },
        )

    run_channel(monkeypatch, fake, ["--room", _ROOM], script=script)

    text, is_error = out["post"]
    assert not is_error
    posted = json.loads(text)
    assert posted["topic_id"] == _TOPIC
    assert posted["is_you"] is True
    (post,) = [r for r in fake.requests if r.method == "POST"]
    assert post.url.path == f"/api/v1/groups/{_ROOM}/topics/{_TOPIC}/messages"
    assert json.loads(post.content) == {
        "message": "On it.",
        "mentions": [_BOB],
        "parent_message_id": _MESSAGE,
        "on_behalf_of_person_id": _ALICE,
    }
    assert post.headers["Idempotency-Key"].startswith(f"claude-channel:{_MESSAGE}:")


def test_messages_post_without_a_reply_uses_a_random_key_and_honours_a_given_one(monkeypatch):
    fake = _Fake(_CONNECTED)

    def script(s: _Session) -> None:
        s.call("messages_post", {"room_id": _ROOM, "topic_id": _TOPIC, "text": "one"})
        s.call("messages_post", {"room_id": _ROOM, "topic_id": _TOPIC, "text": "one"})
        s.call(
            "messages_post",
            {"room_id": _ROOM, "topic_id": _TOPIC, "text": "two", "idempotency_key": "mine"},
        )

    run_channel(monkeypatch, fake, script=script)

    keys = [r.headers["Idempotency-Key"] for r in fake.requests if r.method == "POST"]
    assert keys[0] != keys[1]
    assert keys[2] == "mine"


def test_two_replies_to_one_parent_get_distinct_default_keys_and_both_post(monkeypatch):
    fake = _Fake(_CONNECTED)

    def script(s: _Session) -> None:
        for text in ("first reply", "follow-up the asker requested"):
            s.call(
                "messages_post",
                {"room_id": _ROOM, "topic_id": _TOPIC, "text": text, "reply_to": _MESSAGE},
            )

    run_channel(monkeypatch, fake, script=script)

    keys = [r.headers["Idempotency-Key"] for r in fake.requests if r.method == "POST"]
    assert len(keys) == 2
    assert keys[0] != keys[1]
    assert all(k.startswith(f"claude-channel:{_MESSAGE}:") for k in keys)


def test_a_retried_reply_with_the_same_arguments_reuses_its_default_key(monkeypatch):
    fake = _Fake(_CONNECTED)
    reply = {"room_id": _ROOM, "topic_id": _TOPIC, "text": "On it.", "reply_to": _MESSAGE}

    def script(s: _Session) -> None:
        s.call("messages_post", {**reply, "mentions": [_BOB, _ALICE]})
        s.call("messages_post", {**reply, "text": "  On it.\n", "mentions": [_ALICE, _BOB]})
        s.call("messages_post", {**reply, "mentions": [_BOB]})
        s.call("messages_post", {**reply, "mentions": [_BOB, _ALICE], "on_behalf_of": _ALICE})

    run_channel(monkeypatch, fake, script=script)

    keys = [r.headers["Idempotency-Key"] for r in fake.requests if r.method == "POST"]
    assert keys[0] == keys[1], "the same post retried carries the same key"
    assert len(set(keys[1:])) == 3, "a change to what is sent is a different post"


def test_messages_post_refuses_a_room_outside_room_and_a_bare_on_behalf_of(monkeypatch):
    fake = _Fake(_CONNECTED + _scope())
    out: dict = {}

    def script(s: _Session) -> None:
        out["room"] = s.call(
            "messages_post", {"room_id": _OTHER_ROOM, "topic_id": _TOPIC, "text": "hi"}
        )
        out["behalf"] = s.call(
            "messages_post",
            {"room_id": _ROOM, "topic_id": _TOPIC, "text": "hi", "on_behalf_of": _ALICE},
        )
        out["empty"] = s.call(
            "messages_post",
            {"room_id": _ROOM, "topic_id": _TOPIC, "text": "hi", "reply_to": " "},
        )
        out["missing"] = s.call("messages_post", {"room_id": _ROOM, "topic_id": _TOPIC})
        out["blank"] = s.call("messages_post", {"room_id": _ROOM, "topic_id": _TOPIC, "text": " "})
        out["type"] = s.call("messages_post", {"room_id": _ROOM, "topic_id": 7, "text": "x"})
        out["mentions"] = s.call(
            "messages_post",
            {"room_id": _ROOM, "topic_id": _TOPIC, "text": "x", "mentions": "bob"},
        )

    run_channel(monkeypatch, fake, ["--room", _ROOM], script=script)

    assert all(is_error for _, is_error in out.values())
    assert "not the room this channel serves" in out["room"][0]
    assert "on_behalf_of needs reply_to" in out["behalf"][0]
    assert "reply_to cannot be empty" in out["empty"][0]
    assert "text is required" in out["missing"][0]
    assert "text cannot be empty" in out["blank"][0]
    assert "topic_id must be a string" in out["type"][0]
    assert "mentions must be a list" in out["mentions"][0]
    assert not [r for r in fake.requests if r.method == "POST"], "nothing reached the server"


_TRAVERSAL = f"../../{_OTHER_ROOM}/topics/{_TOPIC}"


@pytest.mark.parametrize(
    ("tool", "arguments", "field"),
    [
        ("messages_post", {"room_id": _ROOM, "topic_id": _TRAVERSAL, "text": "hi"}, "topic_id"),
        ("messages_list", {"room_id": _ROOM, "topic_id": _TRAVERSAL}, "topic_id"),
        (
            "messages_react",
            {
                "room_id": _ROOM,
                "message_id": f"../../{_OTHER_ROOM}/messages/{_MESSAGE}",
                "emoji": "x",
            },
            "message_id",
        ),
        ("messages_list", {"room_id": f"{_ROOM}/../{_OTHER_ROOM}", "topic_id": _TOPIC}, "room_id"),
        ("messages_list", {"room_id": _ROOM, "topic_id": "dinner"}, "topic_id"),
    ],
)
@pytest.mark.parametrize("scoped", [True, False])
def test_an_id_that_is_not_a_uuid_is_refused_so_a_tool_cannot_leave_the_room(
    monkeypatch, tool, arguments, field, scoped
):
    fake = _Fake(_CONNECTED + (_scope() if scoped else ""))
    out: dict = {}

    def script(s: _Session) -> None:
        out["call"] = s.call(tool, arguments)

    run_channel(monkeypatch, fake, ["--room", _ROOM] if scoped else [], script=script)

    text, is_error = out["call"]
    assert is_error
    assert f"{field} must be a UUID" in text
    tool_paths = [p for p in fake.paths() if "/messages" in p or "/reactions" in p]
    assert tool_paths == [], "nothing reached the server"
    assert not [p for p in fake.paths() if _OTHER_ROOM in p]


@pytest.mark.parametrize(
    "secret",
    [
        "pep_agt_" + "abcdef0123456789",
        "my token is pep_pat_" + "zzz",
        "ghp_" + "A1b2C3",
        "github_pat_" + "11AAA",
        "sk-ant-" + "api03-xyz",
        "sk-" + "a" * 20,
        "xoxb-" + "123-456",
        "AKIA" + "ABCDEFGHIJKLMNOP",
        "-----BEGIN RSA PRIVATE KEY-----",
        "dp.st." + "prod.abc",
    ],
)
def test_messages_post_refuses_a_credential_without_echoing_it(monkeypatch, secret):
    fake = _Fake(_CONNECTED)
    out: dict = {}

    def script(s: _Session) -> None:
        out["post"] = s.call(
            "messages_post", {"room_id": _ROOM, "topic_id": _TOPIC, "text": f"here: {secret}"}
        )

    result = run_channel(monkeypatch, fake, script=script)

    text, is_error = out["post"]
    assert is_error
    assert "refused" in text
    assert secret not in text
    assert secret not in "".join(result.stdout.chunks)
    assert not [r for r in fake.requests if r.method == "POST"]


def test_messages_post_allows_text_that_only_looks_a_little_like_a_key(monkeypatch):
    fake = _Fake(_CONNECTED)
    out: dict = {}

    def script(s: _Session) -> None:
        out["post"] = s.call(
            "messages_post",
            {"room_id": _ROOM, "topic_id": _TOPIC, "text": "the sk-learn docs, a task-list"},
        )

    run_channel(monkeypatch, fake, script=script)

    assert out["post"][1] is False


def test_messages_list_returns_the_json_envelope_and_never_marks(monkeypatch):
    fake = _Fake(_CONNECTED)
    out: dict = {}

    def script(s: _Session) -> None:
        out["list"] = s.call(
            "messages_list", {"room_id": _ROOM, "topic_id": _TOPIC, "before": "c", "limit": 5}
        )
        out["at"] = s.call(
            "messages_list", {"room_id": _ROOM, "topic_id": _TOPIC, "at": _MESSAGE_2}
        )
        out["both"] = s.call(
            "messages_list", {"room_id": _ROOM, "topic_id": _TOPIC, "at": "a", "since": "b"}
        )
        out["limit"] = s.call("messages_list", {"room_id": _ROOM, "topic_id": _TOPIC, "limit": 0})

    run_channel(monkeypatch, fake, script=script)

    listed = json.loads(out["list"][0])
    assert [m["id"] for m in listed["items"]] == [_MESSAGE]
    assert listed["next_before"] == "older"
    assert json.loads(out["at"][0])["permalink_target_missing"] is True
    assert "cannot be used together" in out["both"][0]
    assert "limit must be a positive integer" in out["limit"][0]
    reads = [r for r in fake.requests if r.method == "GET" and r.url.path.endswith("/messages")]
    assert reads[0].url.params["before"] == "c"
    assert reads[0].url.params["limit"] == "5"
    assert all("mark" not in str(r.url) for r in fake.requests)


def test_messages_react_returns_the_reactions(monkeypatch):
    fake = _Fake(_CONNECTED)
    out: dict = {}

    def script(s: _Session) -> None:
        out["react"] = s.call(
            "messages_react", {"room_id": _ROOM, "message_id": _MESSAGE, "emoji": "👍"}
        )

    run_channel(monkeypatch, fake, script=script)

    assert json.loads(out["react"][0]) == {"reactions": [{"emoji": "👍", "count": 1}]}
    (react,) = [r for r in fake.requests if r.url.path.endswith("/reactions")]
    assert react.url.path == f"/api/v1/groups/{_ROOM}/messages/{_MESSAGE}/reactions"
    assert json.loads(react.content) == {"emoji": "👍"}


def test_a_failed_tool_call_is_an_error_result_with_the_cli_line_not_a_protocol_error(
    monkeypatch,
):
    fake = _Fake(_CONNECTED)
    fake.message_status = 404
    out: dict = {}

    def script(s: _Session) -> None:
        out["404"] = s.call("messages_list", {"room_id": _ROOM, "topic_id": _TOPIC})
        fake.message_status = 403
        out["403"] = s.call("messages_list", {"room_id": _ROOM, "topic_id": _TOPIC})
        fake.message_status = 401
        out["401"] = s.call("messages_list", {"room_id": _ROOM, "topic_id": _TOPIC})
        fake.message_status = 500
        out["500"] = s.call("messages_list", {"room_id": _ROOM, "topic_id": _TOPIC})
        out["unknown"] = s.call("rooms_list", {})

    result = run_channel(monkeypatch, fake, script=script)

    assert out["404"] == ("not found, or you are not a member of that room", True)
    assert out["403"] == ("not allowed: nope", True)
    assert "refused this agent's token" in out["401"][0]
    assert "pepper login" not in out["401"][0]
    assert out["500"] == ("Server returned 500: nope", True)
    assert out["unknown"] == ("unknown tool 'rooms_list'", True)
    assert all("error" not in m for m in result.stdout.messages() if "id" in m)


def test_an_unreachable_server_is_an_error_result(monkeypatch):
    fake = _Fake(_CONNECTED)
    fake.unreachable = True
    out: dict = {}

    def script(s: _Session) -> None:
        out["list"] = s.call("messages_list", {"room_id": _ROOM, "topic_id": _TOPIC})

    run_channel(monkeypatch, fake, script=script)

    assert out["list"][1] is True
    assert "Couldn't reach the server" in out["list"][0]


# --- the fetch retries without moving the cursor ------------------------------


def test_a_failing_body_fetch_is_retried_and_the_event_is_not_skipped(monkeypatch, tmp_path):
    fake = _Fake(_CONNECTED + _scope() + _event(7))
    fake.fail_messages = 3
    cursor_file = tmp_path / "cursor"

    result = run_channel(monkeypatch, fake, ["--room", _ROOM, "--cursor-file", str(cursor_file)])

    assert result.kinds() == ["message"]
    assert json.loads(cursor_file.read_text())["cursor"] == 7
    assert len([r for r in fake.requests if r.url.path.endswith("/messages")]) == 4


def test_a_persistent_5xx_reading_one_message_is_pushed_unavailable_and_the_stream_goes_on(
    monkeypatch, tmp_path
):
    fake = _Fake(_CONNECTED + _scope() + _event(7) + _event(8, message=_MESSAGE_2))
    fake.fail_messages = claude_channel._FETCH_SERVER_ERROR_ATTEMPTS
    fake.messages[_MESSAGE_2] = _message_json(_MESSAGE_2)
    cursor_file = tmp_path / "cursor"

    result = run_channel(monkeypatch, fake, ["--room", _ROOM, "--cursor-file", str(cursor_file)])

    assert result.kinds() == ["message", "message"]
    first, second = result.notifications
    assert first["meta"]["body"] == "unavailable"
    assert first["meta"]["message_id"] == _MESSAGE
    assert "body" not in second["meta"], "the next message reads normally"
    assert json.loads(cursor_file.read_text())["cursor"] == 8


def test_a_rate_limited_body_fetch_keeps_retrying_on_retry_after_and_is_never_unavailable(
    monkeypatch, tmp_path
):
    waits: list[float] = []
    fetching: set[int] = set()

    class Recording(_Fake):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/messages"):
                fetching.add(threading.get_ident())
            return super().__call__(request)

    def wait(self, seconds: float) -> None:
        # ``_wait`` is patched on the class, so a worker left over from an earlier test (they are
        # daemon threads and nothing joins them) can call this one in the next test's window.
        # Keep only the waits of the worker that read this test's messages.
        if threading.get_ident() in fetching:
            waits.append(seconds)
        if self.stop.wait(0.001):
            raise claude_channel._Ended(0)

    monkeypatch.setattr(claude_channel.ChannelWorker, "_wait", wait)
    fake = Recording(_CONNECTED + _scope() + _event(7))
    fake.fail_messages = claude_channel._FETCH_SERVER_ERROR_ATTEMPTS * 2
    fake.fail_status = 429
    fake.fail_headers = {"Retry-After": "7"}
    cursor_file = tmp_path / "cursor"

    result = run_channel(monkeypatch, fake, ["--room", _ROOM, "--cursor-file", str(cursor_file)])

    assert result.kinds() == ["message"]
    (pushed,) = result.notifications
    assert "body" not in pushed["meta"], "a throttled read is retried, not given up on"
    assert json.loads(cursor_file.read_text())["cursor"] == 7
    fetch_waits = waits[: claude_channel._FETCH_SERVER_ERROR_ATTEMPTS * 2]
    assert fetch_waits == [7.0] * (claude_channel._FETCH_SERVER_ERROR_ATTEMPTS * 2), (
        "Retry-After replaces the backoff gap"
    )
    assert len([r for r in fake.requests if r.url.path.endswith("/messages")]) == 11


def test_a_body_fetch_that_cannot_succeed_ends_the_channel(monkeypatch):
    fake = _Fake(_CONNECTED + _scope() + _event(7))
    fake.message_status = 403

    result = run_channel(monkeypatch, fake, ["--room", _ROOM], script=_ended)

    assert result.kinds() == ["ended"]
    assert result.code == 1


def test_an_unexpected_failure_in_the_worker_still_pushes_ended(monkeypatch):
    def broken(*_args, **_kw):
        raise ValueError("not json")

    monkeypatch.setattr(claude_channel, "message_event", broken)
    fake = _Fake(_CONNECTED + _scope() + _event(7))

    result = run_channel(monkeypatch, fake, ["--room", _ROOM], script=_ended)

    assert result.kinds() == ["ended"]
    assert "internal error (ValueError" in result.notifications[0]["content"]
    assert result.code == 1


# --- stdout is the protocol ----------------------------------------------------


def test_every_byte_on_stdout_is_one_json_rpc_object_per_line(monkeypatch, capsys):
    fake = _Fake(
        _CONNECTED + _scope() + _event(7),
        503,
        503,
        503,
        _CONNECTED + _scope() + _event(8, message=_MESSAGE_2),
    )
    fake.messages[_MESSAGE_2] = _message_json(_MESSAGE_2)

    def script(s: _Session) -> None:
        s.wait_exhausted()
        s.call("messages_list", {"room_id": "nope", "topic_id": "x"})

    result = run_channel(monkeypatch, fake, ["--room", _ROOM], script=script)

    text = "".join(result.stdout.chunks)
    assert text.endswith("\n")
    for line in text.splitlines():
        message = json.loads(line)
        assert message["jsonrpc"] == "2.0"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Feed unavailable" in captured.err, "notices go to stderr"


_CHILD = """
import sys
from pepper_cli import claude_channel, cli

original = claude_channel.ChannelWorker.run

def run(self):
    print("stray print")
    sys.stdout.write("stray write\\n")
    sys.stdout.flush()
    self.sink.push("hello", {"kind": "message"})
    return original(self)

claude_channel.ChannelWorker.run = run
sys.exit(cli.main(sys.argv[1:]))
"""


def _handler(stream_status: int):
    """A real HTTP server's handler: an empty rooms list, and an event stream that either stays
    quiet (200) or is refused (any other status)."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/v1/groups/events/stream") and stream_status == 200:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    self.wfile.write(b": connected\n\n")
                    self.wfile.flush()
                    while True:
                        time.sleep(0.05)
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except OSError:
                    return
            if self.path.startswith("/api/v1/groups/events/stream"):
                body = b'{"detail": "refused"}'
                self.send_response(stream_status)
            else:
                rooms = {
                    "enabled": True,
                    "degraded": False,
                    "total": 0,
                    "archived_total": 0,
                    "groups": [],
                    "features": [],
                }
                body = json.dumps(rooms).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


def _spawn(tmp_path: Path, stream_status: int, argv: list[str]):
    """Start ``argv`` as a real child process logged in to a real local HTTP server."""
    http = ThreadingHTTPServer(("127.0.0.1", 0), _handler(stream_status))
    http.daemon_threads = True
    threading.Thread(target=http.serve_forever, daemon=True).start()
    home = tmp_path / "child-home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(Path(cli.__file__).resolve().parent.parent),
    }
    save = (
        "from pepper_cli import credentials; from pepper_cli.secret import SecretStr; "
        f"credentials.save(server_url='http://127.0.0.1:{http.server_port}', "
        "token=SecretStr('pep_agt_abc'))"
    )
    subprocess.run([sys.executable, "-c", save], env=env, check=True)
    proc = subprocess.Popen(
        [sys.executable, *argv],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    assert proc.stdin is not None
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    }
    proc.stdin.write(json.dumps(init) + "\r\n")
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\r\n")
    proc.stdin.flush()
    return proc, http


@pytest.mark.skipif(sys.platform == "win32", reason="the fd claim is exercised on POSIX")
def test_a_stray_print_lands_on_stderr_and_stdin_eof_exits_0(tmp_path):
    proc, http = _spawn(tmp_path, 200, ["-c", _CHILD, "agents", "claude-channel"])
    try:
        assert proc.stdout is not None
        assert proc.stdin is not None
        lines = [proc.stdout.readline(), proc.stdout.readline()]
        proc.stdin.close()
        rest = proc.stdout.read()
        err = proc.stderr.read() if proc.stderr else ""
        code = proc.wait(timeout=20)
    finally:
        proc.kill()
        http.shutdown()

    parsed = [json.loads(line) for line in [*lines, *rest.splitlines()] if line.strip()]
    assert parsed[0]["result"]["protocolVersion"] == "2025-11-25"
    assert parsed[1]["method"] == "notifications/claude/channel"
    assert parsed[1]["params"] == {"content": "hello", "meta": {"kind": "message"}}
    assert "stray print" in err
    assert "stray write" in err
    assert "stray" not in "".join(lines) + rest
    assert code == 0


@pytest.mark.skipif(sys.platform == "win32", reason="the fd claim is exercised on POSIX")
def test_a_channel_that_ended_exits_1_without_waiting_for_the_session_to_close_stdin(tmp_path):
    proc, http = _spawn(tmp_path, 401, ["-m", "pepper_cli", "agents", "claude-channel"])
    try:
        assert proc.stdout is not None
        code = proc.wait(timeout=20)  # stdin is still open
        out = proc.stdout.read()
        err = proc.stderr.read() if proc.stderr else ""
    finally:
        proc.kill()
        http.shutdown()

    assert code == 1
    notes = [m for m in map(json.loads, out.splitlines()) if "method" in m]
    assert [n["params"]["meta"] for n in notes] == [{"kind": "ended"}]
    assert "The channel stopped" in err
    assert "pep_agt_abc" not in out + err


# --- pure pieces ----------------------------------------------------------------


def test_the_instructions_and_tools_use_the_names_the_events_carry():
    text = claude_channel.INSTRUCTIONS
    for word in (
        "room_id",
        "topic_id",
        "message_id",
        "self_id",
        "reply_to",
        "on_behalf_of",
        "messages_post",
    ):
        assert word in text
    for kind in ("backlog", "resync", "gap", "ended"):
        assert kind in text


def test_control_events_carry_only_the_ids_that_apply():
    content, meta = claude_channel.control_event("gap", "text", room_id=None, since="1")
    assert (content, meta) == ("text", {"kind": "gap", "since": "1"})


def test_clip_content_cuts_at_the_cap():
    assert claude_channel.clip_content("abc") == ("abc", False)
    cut, truncated = claude_channel.clip_content("x" * (claude_channel.CONTENT_CAP + 1))
    assert truncated
    assert len(cut) == claude_channel.CONTENT_CAP


def test_parse_time_accepts_zulu_and_naive_times_and_refuses_junk():
    assert claude_channel._parse_time("2026-10-24T12:00:00Z") == datetime(
        2026, 10, 24, 12, tzinfo=UTC
    )
    assert claude_channel._parse_time("2026-10-24T12:00:00") == datetime(
        2026, 10, 24, 12, tzinfo=UTC
    )
    assert claude_channel._parse_time("yesterday") is None
    assert claude_channel._parse_time(None) is None


def test_the_secret_pattern_matches_the_room_agent_skills_shapes():
    assert claude_channel.SECRET_PATTERN.search("pep_agt_x")
    assert not claude_channel.SECRET_PATTERN.search("a perfectly ordinary sentence")


def test_a_valid_tool_schema_for_each_tool():
    for tool in claude_channel.TOOLS:
        assert isinstance(tool, types.Tool)
        assert set(tool.input_schema["required"]) <= set(tool.input_schema["properties"])


def test_the_command_is_listed_under_agents_with_the_documented_flags():
    parser, _ = cli._build_parser()
    args = parser.parse_args(
        [
            "agents",
            "claude-channel",
            "--room",
            "r",
            "--topic",
            "t",
            "--cursor-file",
            "c",
            "--kinds",
            "message.created",
            "--ignore-kinds",
            "a,b",
            "--self",
            "p",
        ]
    )
    assert args.handler is cli.cmd_agents_claude_channel
    assert args.ignore_kinds == frozenset({"a", "b"})
    assert args.self_id == "p"
    with pytest.raises(SystemExit):
        parser.parse_args(["agents", "claude-channel", "--json"])
    with pytest.raises(SystemExit):
        parser.parse_args(["agents", "claude-channel", "--since", "3"])


def test_the_cli_entry_dispatches_to_the_server_module(monkeypatch):
    seen: list[object] = []
    monkeypatch.setattr(claude_channel, "run", lambda args: seen.append(args) or 0)

    assert cli.main(["agents", "claude-channel", "--room", "x"]) == 0

    assert seen[0].room == "x"
