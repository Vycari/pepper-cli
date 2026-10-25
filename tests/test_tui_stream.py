"""The person event stream client against a fake SSE server on a real socket.

The server answers one scripted connection per request (event frames, heartbeats, an error
frame, a bare close), and records what each connection asked for. That is what pins the
reconnect rules: ``Last-Event-ID`` after a drop, ``?since=`` after ``cursor_ahead``, a catch-up
once when the stream starts at the head, and giving up on a 403.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import pytest

from pepper_cli import api_client
from pepper_cli.secret import SecretStr
from pepper_cli.tui import stream
from tests import tui_support as ts

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass
class Connection:
    frames: list[bytes] = field(default_factory=list)
    status: int = 200


@dataclass
class FakeSse:
    scripts: list[Connection] = field(default_factory=list)
    #: ``(path, Last-Event-ID, since, Authorization)`` of each connection.
    opens: list[tuple[str, str | None, str | None, str | None]] = field(default_factory=list)
    done: threading.Event = field(default_factory=threading.Event)


@pytest.fixture
def sse() -> Iterator[tuple[FakeSse, api_client.ApiClient]]:
    fake = FakeSse()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlparse(self.path)
            since = parse_qs(url.query).get("since", [None])[0]
            fake.opens.append(
                (
                    url.path,
                    self.headers.get("Last-Event-ID"),
                    since,
                    self.headers.get("Authorization"),
                )
            )
            script = fake.scripts.pop(0) if fake.scripts else Connection()
            if script.status != 200:
                body = json.dumps({"detail": "refused"}).encode()
                self.send_response(script.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for frame in script.frames:
                self.wfile.write(frame)
                self.wfile.flush()
            self.close_connection = True
            if not fake.scripts:
                fake.done.set()

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    api = api_client.ApiClient(
        server_url=f"http://127.0.0.1:{server.server_address[1]}", token=SecretStr("pep_pat_test")
    )
    try:
        yield fake, api
    finally:
        server.shutdown()
        server.server_close()


@dataclass
class Recorder:
    frames: list[api_client.FeedFrame] = field(default_factory=list)
    live: list[bool] = field(default_factory=list)
    resyncs: int = 0

    def handler(self) -> stream.StreamHandler:
        return stream.StreamHandler(
            on_frame=self.frames.append, on_live=self.live.append, on_resync=self._resync
        )

    def _resync(self) -> None:
        self.resyncs += 1


def run_until_done(
    fake: FakeSse, api: api_client.ApiClient, rec: Recorder, *, frames: int = 0
) -> stream.PersonStream:
    """Run the stream until every scripted connection was served and ``frames`` event frames
    reached the handler, then stop it."""
    person = stream.PersonStream(api, rec.handler(), delay=lambda _failures: 0.0)
    person.start()
    assert fake.done.wait(timeout=10), "the fake server's scripts were not all used"
    deadline = time.monotonic() + 10
    while len(rec.frames) < frames and time.monotonic() < deadline:
        time.sleep(0.01)
    person.stop()
    person.join(timeout=10)
    return person


def advanced(event_id: int, at: str = "2026-09-16T18:00:00.000001+00:00") -> bytes:
    return ts.read_advanced(ts.TOPIC_A1, at, event_id)


def test_hands_over_events_and_swallows_heartbeats_and_unknown_frames(sse) -> None:
    fake, api = sse
    fake.scripts = [
        Connection(
            [
                b": connected\n\n",
                ts.CURSOR_HEAD,
                ts.HEARTBEAT,
                advanced(2),
                b"event: some.future_kind\ndata: not json\n\n",
                ts.feed_frame("message.created", {"topic_id": ts.TOPIC_A1}, 3),
                b'event: chat.message\ndata: {"message_id": "x"}\n\n',
            ]
        ),
    ]
    rec = Recorder()
    run_until_done(fake, api, rec, frames=3)
    assert [f.kind for f in rec.frames] == [
        "read_state.advanced",
        "message.created",
        "chat.message",
    ]
    assert rec.frames[0].payload["payload"]["last_read_at"] == "2026-09-16T18:00:00.000001+00:00"
    assert fake.opens[0][0] == "/api/v1/groups/events/stream"
    assert fake.opens[0][3] == "Bearer pep_pat_test"  # the PAT, as on every other route
    assert rec.live[0] is True


def test_the_first_connection_starts_at_the_head_and_catches_up_once(sse) -> None:
    fake, api = sse
    fake.scripts = [Connection([ts.CURSOR_HEAD, advanced(2)]), Connection([ts.HEARTBEAT])]
    rec = Recorder()
    run_until_done(fake, api, rec)
    # No position named: no header and no ``since``, so the server starts at its head.
    assert fake.opens[0][1:3] == (None, None)
    assert rec.resyncs == 1


def test_a_drop_reconnects_with_last_event_id_and_no_catch_up(sse) -> None:
    fake, api = sse
    fake.scripts = [
        Connection([ts.CURSOR_HEAD, advanced(7)]),  # then the connection just closes
        Connection([advanced(8)]),
    ]
    rec = Recorder()
    person = run_until_done(fake, api, rec, frames=2)
    assert fake.opens[1][1:3] == ("7", None)
    assert person.last_id == 8
    assert [f.id for f in rec.frames] == [7, 8]
    assert rec.resyncs == 1  # the head once, not again on a resume
    assert False in rec.live  # the drop was reported


def test_a_retryable_error_frame_reconnects_from_the_last_id(sse) -> None:
    fake, api = sse
    fake.scripts = [
        Connection(
            [advanced(4), b'event: error\ndata: {"error":"server_restarting","retry":true}\n\n']
        ),
        Connection([advanced(5)]),
    ]
    rec = Recorder()
    run_until_done(fake, api, rec, frames=2)
    assert fake.opens[1][1] == "4"
    assert [f.id for f in rec.frames] == [4, 5]


def test_cursor_ahead_resyncs_and_reopens_at_the_servers_position(sse) -> None:
    fake, api = sse
    fake.scripts = [
        Connection(
            [
                advanced(40),
                b'event: error\ndata: {"error":"cursor_ahead","retry":false,"next_after_id":3}\n\n',
            ]
        ),
        Connection([advanced(4)]),
    ]
    rec = Recorder()
    person = run_until_done(fake, api, rec, frames=2)
    assert rec.resyncs == 1
    # The new stream names the server's id with ``?since=``, not the stale Last-Event-ID.
    assert fake.opens[1][1:3] == (None, "3")
    assert person.last_id == 4  # an event after that is the position from then on


def test_a_final_error_frame_ends_the_stream_for_good(sse) -> None:
    fake, api = sse
    fake.scripts = [
        Connection([b'event: error\ndata: {"error":"stream_limit","retry":false}\n\n']),
        Connection([advanced(9)]),
    ]
    rec = Recorder()
    person = stream.PersonStream(api, rec.handler(), delay=lambda _failures: 0.0)
    person.run()
    assert person.gave_up
    assert len(fake.opens) == 1
    assert rec.frames == []


@pytest.mark.parametrize("status", [403, 404])
def test_a_refused_stream_is_not_retried(sse, status: int) -> None:
    fake, api = sse
    fake.scripts = [Connection(status=status), Connection([advanced(1)])]
    person = stream.PersonStream(api, Recorder().handler(), delay=lambda _failures: 0.0)
    person.run()
    assert person.gave_up
    assert len(fake.opens) == 1


def test_a_server_error_backs_off_and_retries(sse) -> None:
    fake, api = sse
    fake.scripts = [Connection(status=503), Connection([advanced(1)])]
    delays: list[int] = []

    def delay(failures: int) -> float:
        delays.append(failures)
        return 0.0

    rec = Recorder()
    person = stream.PersonStream(api, rec.handler(), delay=delay)
    person.start()
    assert fake.done.wait(timeout=10)
    person.stop()
    person.join(timeout=10)
    # ``ApiError`` 503 is not a give-up status: a second connection was made.
    assert len(fake.opens) >= 2
    assert delays[0] == 1


def test_reconnect_delay_doubles_and_caps() -> None:
    assert [stream.reconnect_delay(n) for n in (0, 1, 2, 3)] == [0.0, 1.0, 2.0, 4.0]
    assert stream.reconnect_delay(50) == stream.RECONNECT_MAX


def test_reconnect_delay_stays_capped_after_a_very_long_outage() -> None:
    # 2 ** 1024 does not fit a float: an unclamped exponent raised OverflowError here and
    # ended the stream thread for the rest of the session.
    assert stream.reconnect_delay(1025) == stream.RECONNECT_MAX
    assert stream.reconnect_delay(10**6) == stream.RECONNECT_MAX
