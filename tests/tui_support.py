"""A stateful fake of the server's groups + chat routes, for the terminal UI tests.

Everything is synthetic. The handler is thread-safe enough for the app's worker threads:
state is only mutated by the test body or under the GIL by the handler's own appends.
"""

from __future__ import annotations

import asyncio
import io
import json
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
from rich.console import Console
from textual.widgets import Static

from pepper_cli import api_client
from pepper_cli.secret import SecretStr

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import App

ME = "00000000-0000-0000-0000-00000000aaaa"
ROOM_A = "00000000-0000-0000-0000-0000000000a1"
ROOM_B = "00000000-0000-0000-0000-0000000000a2"
TOPIC_A1 = "00000000-0000-0000-0000-0000000000b1"
TOPIC_A2 = "00000000-0000-0000-0000-0000000000b2"
TOPIC_B1 = "00000000-0000-0000-0000-0000000000b3"


async def until(pilot, condition: Callable[[], bool], *, seconds: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await pilot.pause(0.02)


def plain(app: App, selector: str) -> str:
    """The text a Static currently shows, with Rich layout applied."""
    console = Console(width=100, file=io.StringIO(), record=True, force_terminal=False)
    console.print(app.query_one(selector, Static).content)
    return console.export_text()


def body_text(app: App) -> str:
    return plain(app, "#thread-body")


def topic(topic_id: str, name: str, at: str, **over: Any) -> dict[str, Any]:
    return {
        "topic_id": topic_id,
        "name": name,
        "last_activity_at": at,
        "dedicated_conversation": False,
        "archived_at": None,
        "imported_history": False,
        **over,
    }


def room(group_id: str, name: str, topics: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    return {
        "group_id": group_id,
        "display_name": name,
        "color": "#3366cc",
        "sub": "active 2h ago",
        "member_count": 3,
        "archived": False,
        "topics": topics,
        **over,
    }


def message(mid: str, at: str, body: str = "hello", **over: Any) -> dict[str, Any]:
    return {
        "id": mid,
        "is_pepper": False,
        "is_you": False,
        "is_deleted": False,
        "is_broken": False,
        "run_start": True,
        "display_name": "Ada",
        "initial": "A",
        "avatar_tint": "#aa5500",
        "picture_url": None,
        "raw_body": body,
        "rendered_html": f"<p>{body}</p>",
        "created_at": at,
        "created_at_display": "5:30 PM",
        "day_key": at[:10],
        "day_kicker": "WEDNESDAY",
        "day_heading": "September 16, 2026",
        "day_label": "Today",
        "via_display_name": None,
        "is_edited": False,
        "edited_at": None,
        "deleted_at": None,
        "reactions": [],
        "link_previews": [],
        "attachments": [],
        "mention_names": [],
        "quote": None,
        "quote_excerpt": body,  # live servers fill this with the message's own text
        "cards": [],
        "is_permalink_target": False,
        **over,
    }


def history_entry(hid: str, role: str, text: str, at: str) -> dict[str, Any]:
    return {"id": hid, "role": role, "text": text, "channel": "terminal", "created_at": at}


def sse(kind: str, payload: dict[str, Any]) -> bytes:
    return f"event: {kind}\ndata: {json.dumps(payload)}\n\n".encode()


@dataclass
class ScriptedTurn:
    """What the fake answers to one ``POST /api/v1/chat/send``.

    ``frames`` stream in order. ``gates`` holds the stream before frame ``i`` until its event
    is set. ``history`` is appended to the DM history just before the terminal frame, or when
    the stream ends or drops without one (the server ran the turn either way). ``drop`` raises
    a transport error after the frames, the way a connection lost mid-turn surfaces from
    ``iter_lines``.
    """

    frames: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    gates: dict[int, threading.Event] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    drop: bool = False


@dataclass
class FakeServer:
    rooms: list[dict[str, Any]] = field(default_factory=list)
    messages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    marks: dict[str, str] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    enabled: bool = True
    degraded: bool = False
    time_format: str = "12h"
    #: path prefix -> status to answer instead (e.g. ``{"/api/v1/groups": 401}``).
    fail: dict[str, int] = field(default_factory=dict)
    #: path prefix -> exception to raise instead of answering (a transport failure).
    raise_on: dict[str, Exception] = field(default_factory=dict)
    #: exact path, or ``"METHOD path"``, -> event the handler waits on before answering.
    gates: dict[str, threading.Event] = field(default_factory=dict)
    #: path prefix -> exception raised *after* the request was handled (a lost answer).
    raise_after: dict[str, Exception] = field(default_factory=dict)
    #: ``group_id -> roster members`` (dicts as ``/roster`` answers them).
    rosters: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: Room post attempts, in arrival order: the JSON body plus the ``Idempotency-Key``.
    posts: list[dict[str, Any]] = field(default_factory=list)
    #: ``Idempotency-Key -> stored row``: a repeated key answers the row already stored.
    stored: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Answers to ``chat/send``, popped one per turn.
    turns: list[ScriptedTurn] = field(default_factory=list)
    #: The JSON body of each ``chat/send`` received.
    turn_bodies: list[dict[str, Any]] = field(default_factory=list)
    #: The JSON body of each ``chat/tool-result`` received.
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    clock: int = 0
    rev: int = 1
    requests: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    puts: list[tuple[str, str]] = field(default_factory=list)
    headers_seen: list[dict[str, str]] = field(default_factory=list)

    def bump(self) -> None:
        """The room list changed: new ETag."""
        self.rev += 1

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = dict(request.url.params)
        self.requests.append((request.method, path, query))
        keyed = f"{request.method} {path}"
        if request.method == "POST" and path.endswith("/messages"):
            # Every attempt, including one the fake then fails: retries are what is tested.
            body = json.loads(request.content)
            self.posts.append({**body, "key": request.headers.get("Idempotency-Key")})
        gate = self.gates.get(keyed) or self.gates.get(path)
        if gate is not None:
            gate.wait(timeout=10)
        for prefix, exc in list(self.raise_on.items()):
            if path.startswith(prefix) or keyed.startswith(prefix):
                raise exc
        for prefix, status in list(self.fail.items()):
            if path.startswith(prefix) or keyed.startswith(prefix):
                return httpx.Response(status, json={"detail": "synthetic failure"})
        response = self._answer(request, path, query)
        for prefix, exc in list(self.raise_after.items()):
            if path.startswith(prefix) or keyed.startswith(prefix):
                raise exc
        return response

    def _next_at(self) -> str:
        """A created_at later than every fixture row (the server's clock moves on)."""
        self.clock += 1
        return f"2026-09-16T18:{self.clock // 60:02d}:{self.clock % 60:02d}+00:00"

    def _post_message(self, request: httpx.Request, topic_id: str) -> httpx.Response:
        body = json.loads(request.content)
        key = request.headers.get("Idempotency-Key")
        if key is not None and key in self.stored:
            return httpx.Response(200, json={"message": self.stored[key]})
        row = message(
            f"posted-{len(self.posts)}",
            self._next_at(),
            body["message"],
            is_you=True,
            display_name="Me",
        )
        if key is not None:
            self.stored[key] = row
        self.messages.setdefault(topic_id, []).append(row)
        return httpx.Response(201, json={"message": row})

    def _chat_send(self, request: httpx.Request) -> httpx.Response:
        self.turn_bodies.append(json.loads(request.content))
        script = self.turns.pop(0) if self.turns else ScriptedTurn()

        def stream() -> Any:
            stored = False
            for i, (kind, payload) in enumerate(script.frames):
                if (gate := script.gates.get(i)) is not None:
                    gate.wait(timeout=10)
                if kind in ("reply", "error") and not stored:
                    # The server stores the turn before its terminal frame, and the client
                    # stops reading at that frame.
                    self.history.extend(script.history)
                    stored = True
                yield sse(kind, payload)
            if not stored:
                self.history.extend(script.history)
            if script.drop:
                raise httpx.ReadError("connection dropped")

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream())

    def _answer(self, request: httpx.Request, path: str, query: dict[str, str]) -> httpx.Response:
        if path == "/api/v1/whoami":
            return httpx.Response(
                200,
                json={
                    "user_id": ME,
                    "email": None,
                    "display_name": "Me",
                    "timezone": "UTC",
                    "time_format": self.time_format,
                },
            )
        if path == "/api/v1/groups":
            etag = f'"rev-{self.rev}"'
            if request.headers.get("If-None-Match") == etag:
                return httpx.Response(304, headers={"ETag": etag})
            self.headers_seen.append(dict(request.headers))
            body = {
                "enabled": self.enabled,
                "degraded": self.degraded,
                "total": len(self.rooms),
                "archived_total": 0,
                "groups": self.rooms,
            }
            return httpx.Response(200, json=body, headers={"ETag": etag})
        if path == "/api/v1/chat/history":
            return httpx.Response(200, json=list(self.history))
        if path == "/api/v1/chat/send":
            return self._chat_send(request)
        if path == "/api/v1/chat/tool-result":
            self.tool_results.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        parts = path.strip("/").split("/")
        if parts[:3] == ["api", "v1", "groups"] and len(parts) >= 5:
            if parts[4] == "read" and request.method == "GET":
                marks = [{"topic_id": t, "read_through": v} for t, v in self.marks.items()]
                return httpx.Response(200, json={"marks": marks})
            if len(parts) == 7 and parts[4] == "topics" and parts[6] == "read":
                through = json.loads(request.content)["read_through"]
                self.puts.append((parts[5], through))
                self.marks[parts[5]] = through
                return httpx.Response(200, json={"read_through": through})
            if len(parts) == 5 and parts[4] == "roster":
                return httpx.Response(200, json={"members": self.rosters.get(parts[3], [])})
            if len(parts) == 7 and parts[6] == "messages" and request.method == "POST":
                return self._post_message(request, parts[5])
            if len(parts) == 7 and parts[6] == "messages":
                rows = list(self.messages.get(parts[5], []))
                since = query.get("since")
                if since is not None:
                    from pepper_cli.tui.state import parse_ts

                    cutoff = parse_ts(since)
                    rows = [r for r in rows if parse_ts(r["created_at"]) > cutoff]
                return httpx.Response(
                    200,
                    json={"messages": rows, "next_before": None, "permalink_target_missing": False},
                )
        return httpx.Response(404, json={"detail": f"unmocked {request.method} {path}"})

    def client(self) -> api_client.ApiClient:
        return api_client.ApiClient(
            server_url="http://example.com",
            token=SecretStr("pep_pat_test"),
            client=httpx.Client(transport=httpx.MockTransport(self.handler)),
        )


def standard_server() -> FakeServer:
    """Two rooms; room A has two topics (newest first), all read at the start."""
    server = FakeServer()
    server.rooms = [
        room(
            ROOM_A,
            "Trip planning",
            [
                topic(TOPIC_A1, "Flights", "2026-09-16T17:30:00+00:00"),
                topic(TOPIC_A2, "Hotels", "2026-09-16T16:00:00+00:00"),
            ],
        ),
        room(ROOM_B, "Book club", [topic(TOPIC_B1, "General", "2026-09-15T10:00:00+00:00")]),
    ]
    server.messages = {
        TOPIC_A1: [message("m1", "2026-09-16T17:30:00+00:00", "flight options")],
        TOPIC_A2: [message("m2", "2026-09-16T16:00:00+00:00", "hotel options")],
        TOPIC_B1: [message("m3", "2026-09-15T10:00:00+00:00", "next book")],
    }
    server.marks = {
        TOPIC_A1: "2026-09-16T17:30:00+00:00",
        TOPIC_A2: "2026-09-16T16:00:00+00:00",
        TOPIC_B1: "2026-09-15T10:00:00+00:00",
    }
    server.history = [
        {
            "id": "h1",
            "role": "user",
            "text": "hi pepper",
            "channel": "terminal",
            "created_at": "2026-09-16T08:00:00Z",
        },
        {
            "id": "h2",
            "role": "assistant",
            "text": "hello there",
            "channel": "terminal",
            "created_at": "2026-09-16T08:00:05Z",
        },
    ]
    return server
