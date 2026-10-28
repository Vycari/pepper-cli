"""The terminal UI on the person event stream (read-state sync phase 4).

The stream says when to fetch; the polls stay as the fallback. These tests drive the app
against the fake server's scripted ``GET /api/v1/groups/events/stream`` and check what the
frames do: a read on another device clears a marker without touching the rail, nothing is
marked read while the thread is scrolled up, and the polls speed up when the stream is down.
"""

from __future__ import annotations

import threading
from datetime import datetime

import httpx
import pytest
from textual import events
from textual.widgets import Static

from pepper_cli.tui import app as tui_app
from pepper_cli.tui import state, stream
from pepper_cli.tui.thread import ThreadView
from tests import tui_support as ts
from tests.tui_support import body_text, until

LATER = "2026-09-16T18:00:00.123456+00:00"


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fast polls when there is no stream; very slow ones while it is healthy, so anything
    that arrives in these tests came from a frame."""
    monkeypatch.setattr(state, "ACTIVE_INTERVAL", 0.05)
    monkeypatch.setattr(state, "COOLING_INTERVAL", 0.05)
    monkeypatch.setattr(state, "IDLE_INTERVAL", 0.05)
    monkeypatch.setattr(state, "RAIL_INTERVAL", 0.05)
    monkeypatch.setattr(state, "RAIL_INTERVAL_LIVE", 1000.0)
    monkeypatch.setattr(state, "TOPIC_INTERVAL_LIVE", 1000.0)
    monkeypatch.setattr(state, "DM_INTERVAL", 1000.0)
    monkeypatch.setattr(state, "BACKOFF_STEPS", (0.05, 0.05, 0.05, 0.05))
    monkeypatch.setattr(stream, "RECONNECT_FIRST", 0.01)
    monkeypatch.setattr(tui_app, "utcnow", lambda: ts.NOW)


def _rail_polls(server: ts.FakeServer) -> int:
    return sum(1 for _m, path, _q in server.requests if path == "/api/v1/groups")


def _mark_fetches(server: ts.FakeServer) -> int:
    return sum(
        1 for m, path, _q in server.requests if m == "GET" and path == "/api/v1/groups/unread"
    )


def _room_unread(app: tui_app.PepperApp, room_id: str) -> bool:
    return bool(app.sidebar._by_key[f"room:{room_id}"].data.unread)


def _unread_room_b(server: ts.FakeServer, activity: str = LATER) -> None:
    """Room B has activity past the mark the server holds, so it reads as unread."""
    server.rooms[1]["topics"][0]["last_activity_at"] = activity
    server.messages[ts.TOPIC_B1].append(ts.message("m9", activity, "newer"))
    server.bump()


async def _settled(pilot, server: ts.FakeServer) -> None:
    """Wait until the app has stopped fetching on its own (the catch-up after the stream's
    first frame has landed)."""
    last = -1
    while last != len(server.requests):
        last = len(server.requests)
        await pilot.pause(0.3)


async def test_a_read_event_for_an_unfocused_room_clears_its_marker_without_a_rail_poll() -> None:
    server = ts.standard_server()
    _unread_room_b(server)
    gate, hold = threading.Event(), threading.Event()
    server.streams = [
        ts.StreamScript(
            frames=[ts.CURSOR_HEAD, ts.read_advanced(ts.TOPIC_B1, LATER, 2)],
            gates={1: gate},
            hold=hold,
        )
    ]
    app = tui_app.PepperApp(server.client())
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: len(app.rooms) == 2 and app._stream_healthy)
            await until(pilot, lambda: f"room:{ts.ROOM_B}" in app.sidebar._by_key)
            assert _room_unread(app, ts.ROOM_B)
            await _settled(pilot, server)
            polls, marks = _rail_polls(server), _mark_fetches(server)
            gate.set()
            await until(pilot, lambda: not _room_unread(app, ts.ROOM_B))
            await pilot.pause(0.3)
            # The marker cleared from the frame alone: no rail list, no mark fetch.
            assert (_rail_polls(server), _mark_fetches(server)) == (polls, marks)
            assert server.puts == []
    finally:
        hold.set()


async def test_a_read_event_merges_forward_only_at_microsecond_precision() -> None:
    server = ts.standard_server()
    _unread_room_b(server)
    behind = "2026-09-16T18:00:00.123455+00:00"  # one microsecond before the message
    gate, hold = threading.Event(), threading.Event()
    server.streams = [
        ts.StreamScript(
            frames=[
                ts.CURSOR_HEAD,
                ts.read_advanced(ts.TOPIC_B1, behind, 2),
                ts.read_advanced(ts.TOPIC_B1, LATER, 3),
                ts.read_advanced(ts.TOPIC_B1, "2026-09-16T17:00:00+00:00", 4),  # a replay
            ],
            gates={1: gate},
            hold=hold,
        )
    ]
    app = tui_app.PepperApp(server.client())
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: len(app.rooms) == 2 and app._stream_healthy)
            await _settled(pilot, server)
            gate.set()
            await until(pilot, lambda: app.marks.marks[ts.TOPIC_B1] == _at(LATER))
            await pilot.pause(0.2)
            assert app.marks.marks[ts.TOPIC_B1] == _at(LATER)  # the replay did not walk it back
            assert not _room_unread(app, ts.ROOM_B)
    finally:
        hold.set()


def _at(raw: str) -> datetime:
    parsed = state.parse_ts(raw)
    assert parsed is not None
    return parsed


def test_a_mark_one_microsecond_behind_the_message_leaves_it_unread() -> None:
    marks = state.ReadMarks()
    assert marks.merge_advanced({"topic_id": "t", "last_read_at": "2026-09-16T18:00:00.123455Z"})
    assert marks.marks["t"] < _at("2026-09-16T18:00:00.123456+00:00")
    assert marks.merge_advanced({"topic_id": "t", "last_read_at": "2026-09-16T18:00:00.123456Z"})
    assert marks.marks["t"] == _at("2026-09-16T18:00:00.123456+00:00")
    assert (
        marks.merge_advanced({"topic_id": "t", "last_read_at": "2026-09-16T18:00:00.123455Z"})
        is None
    )


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"topic_id": "t"},
        {"topic_id": 3, "last_read_at": "x"},
        {"topic_id": "t", "last_read_at": "no"},
    ],
)
def test_a_malformed_read_event_is_ignored(payload: object) -> None:
    marks = state.ReadMarks()
    assert marks.merge_advanced(payload) is None
    assert marks.marks == {}


async def test_a_message_event_fetches_the_open_topic_and_the_rail_at_once() -> None:
    server = ts.standard_server()
    gate, hold = threading.Event(), threading.Event()
    server.streams = [
        ts.StreamScript(
            frames=[
                ts.CURSOR_HEAD,
                ts.feed_frame("message.created", {"topic_id": ts.TOPIC_A1}, 2),
            ],
            gates={1: gate},
            hold=hold,
        )
    ]
    app = tui_app.PepperApp(server.client())
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: len(app.rooms) == 2 and app._stream_healthy)
            app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
            await until(pilot, lambda: "flight options" in body_text(app))
            await _settled(pilot, server)
            polls = _rail_polls(server)
            server.messages[ts.TOPIC_A1].append(
                ts.message("m7", "2026-09-16T18:05:00+00:00", "ping")
            )
            gate.set()
            await until(pilot, lambda: "ping" in body_text(app))
            await until(pilot, lambda: _rail_polls(server) > polls)
    finally:
        hold.set()


async def test_the_polls_run_at_their_normal_rate_when_the_stream_is_down() -> None:
    server = ts.standard_server()
    hold = threading.Event()
    # Healthy first, then the connection drops and the reconnect finds no stream (404).
    server.streams = [ts.StreamScript(frames=[ts.CURSOR_HEAD], hold=hold)]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2 and app._stream_healthy)
        await _settled(pilot, server)
        _unread_room_b(server)
        await pilot.pause(0.4)
        assert not _room_unread(app, ts.ROOM_B)  # healthy: the 120 s safety net has not run
        hold.set()
        await until(pilot, lambda: not app._stream_healthy)
        await until(pilot, lambda: _room_unread(app, ts.ROOM_B))  # the poll found it
        assert app._person_stream is not None
        await until(pilot, lambda: app._person_stream is not None and app._person_stream.gave_up)


async def test_cursor_ahead_catches_up_and_reopens_at_the_servers_position() -> None:
    server = ts.standard_server()
    hold = threading.Event()
    server.streams = [
        ts.StreamScript(
            frames=[
                ts.CURSOR_HEAD,
                b'event: error\ndata: {"error":"cursor_ahead","retry":false,"next_after_id":3}\n\n',
            ]
        ),
        ts.StreamScript(frames=[ts.HEARTBEAT], hold=hold),
    ]
    app = tui_app.PepperApp(server.client())
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: len(server.stream_opens) == 2)
            assert server.stream_opens[1] == (None, "3")
            await until(pilot, lambda: len(app.rooms) == 2 and app._stream_healthy)
    finally:
        hold.set()


async def _quiet(pilot, app: tui_app.PepperApp, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the open-topic poll re-arming at its fast rate, so a re-render queued by an earlier
    poll cannot scroll the thread back to the end under a test that has just scrolled it."""
    for name in ("ACTIVE_INTERVAL", "COOLING_INTERVAL", "IDLE_INTERVAL"):
        monkeypatch.setattr(state, name, 1000.0)
    await pilot.pause(0.3)
    await until(pilot, lambda: not app._loops["topic"].busy)
    await pilot.pause(0.2)


def _long_topic(server: ts.FakeServer) -> None:
    server.messages[ts.TOPIC_A1] = [
        ts.message(f"r{i:02d}", f"2026-09-16T17:{i:02d}:00+00:00", f"row {i}") for i in range(40)
    ]


async def test_nothing_is_marked_read_while_the_thread_is_scrolled_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = ts.standard_server()
    _long_topic(server)
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 20)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: "row 39" in body_text(app))
        scroller = app.thread.scroller
        await until(pilot, lambda: scroller.max_scroll_y > 0 and app.thread.pinned)
        newest = "2026-09-16T17:39:00+00:00"
        await until(pilot, lambda: (ts.TOPIC_A1, newest) in server.puts)
        await _quiet(pilot, app, monkeypatch)
        scroller.scroll_home(animate=False)
        await until(pilot, lambda: not app.thread.pinned)
        puts = len(server.puts)
        # A message arrives while the person reads further up.
        arrived = "2026-09-16T18:30:00.000007+00:00"
        server.messages[ts.TOPIC_A1].append(ts.message("r99", arrived, "fresh"))
        server.rooms[0]["topics"][0]["last_activity_at"] = arrived
        server.bump()
        app._run("topic")
        app._run("rail")
        await until(pilot, lambda: "r99" in app.store.rows)
        await until(pilot, lambda: bool(app.query_one("#thread-new", Static).display))
        await pilot.pause(0.4)
        assert len(server.puts) == puts  # no PUT while scrolled up
        assert app.marks.marks[ts.TOPIC_A1] < _at(arrived)
        # Open, but with rows below the screen it is unread: the marker stays.
        assert _room_unread(app, ts.ROOM_A)
        # Leaving it scrolled up publishes nothing either.
        app.open_conversation(state.PEPPER)
        await pilot.pause(0.4)
        assert len(server.puts) == puts
        # Back at the end, the newest row has been on screen: now it is read.
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: (ts.TOPIC_A1, arrived) in server.puts)


async def test_a_pin_change_delivered_after_the_thread_is_gone_is_ignored() -> None:
    """The cue can be dispatched during teardown, once the thread is gone (#139): no crash."""
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 20)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: app.loaded_topic)
        await app.thread.remove()
        app.on_thread_view_pin_changed(ThreadView.PinChanged(True))


async def test_reaching_the_end_marks_the_newest_row_read(monkeypatch: pytest.MonkeyPatch) -> None:
    server = ts.standard_server()
    _long_topic(server)
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 20)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: "row 39" in body_text(app))
        scroller = app.thread.scroller
        await until(pilot, lambda: scroller.max_scroll_y > 0 and app.thread.pinned)
        await _quiet(pilot, app, monkeypatch)
        scroller.scroll_home(animate=False)
        await until(pilot, lambda: not app.thread.pinned)
        arrived = "2026-09-16T18:30:00+00:00"
        server.messages[ts.TOPIC_A1].append(ts.message("r99", arrived, "fresh"))
        app._run("topic")
        await until(pilot, lambda: "r99" in app.store.rows)
        assert (ts.TOPIC_A1, arrived) not in server.puts
        scroller.scroll_end(animate=False)
        await until(pilot, lambda: (ts.TOPIC_A1, arrived) in server.puts)


def _topic_reads(server: ts.FakeServer, topic_id: str) -> int:
    path = f"/api/v1/groups/{ts.ROOM_A}/topics/{topic_id}/messages"
    return sum(1 for m, p, _q in server.requests if m == "GET" and p == path)


async def test_a_message_event_while_blurred_waits_for_focus_to_read_the_topic() -> None:
    server = ts.standard_server()
    gate, hold = threading.Event(), threading.Event()
    server.streams = [
        ts.StreamScript(
            frames=[
                ts.CURSOR_HEAD,
                ts.feed_frame("message.created", {"topic_id": ts.TOPIC_A1}, 2),
            ],
            gates={1: gate},
            hold=hold,
        )
    ]
    app = tui_app.PepperApp(server.client())
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: len(app.rooms) == 2 and app._stream_healthy)
            app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
            await until(pilot, lambda: "flight options" in body_text(app))
            await _settled(pilot, server)
            app.on_app_blur(events.AppBlur())
            reads, polls = _topic_reads(server, ts.TOPIC_A1), _rail_polls(server)
            server.messages[ts.TOPIC_A1].append(
                ts.message("m7", "2026-09-16T18:05:00+00:00", "ping")
            )
            gate.set()
            # The sidebar still follows the stream; the hidden thread does not fetch.
            await until(pilot, lambda: _rail_polls(server) > polls)
            await pilot.pause(0.3)
            assert _topic_reads(server, ts.TOPIC_A1) == reads
            assert "ping" not in body_text(app)
            app.on_app_focus(events.AppFocus())
            await until(pilot, lambda: "ping" in body_text(app))
    finally:
        hold.set()


async def test_a_send_that_lands_while_scrolled_up_marks_nothing_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = ts.standard_server()
    _long_topic(server)
    post = f"POST /api/v1/groups/{ts.ROOM_A}/topics/{ts.TOPIC_A1}/messages"
    gate = threading.Event()
    server.gates[post] = gate
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 20)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: "row 39" in body_text(app))
        scroller = app.thread.scroller
        await until(pilot, lambda: scroller.max_scroll_y > 0 and app.thread.pinned)
        await _quiet(pilot, app, monkeypatch)
        app.composer.focus()
        app.composer.insert("mine")
        await pilot.press("enter")
        await until(pilot, lambda: "sending…" in body_text(app))
        # During the round trip the person scrolls up, and someone else posts first.
        scroller.scroll_home(animate=False)
        await until(pilot, lambda: not app.thread.pinned)
        theirs = "2026-09-16T17:59:30+00:00"
        server.messages[ts.TOPIC_A1].append(ts.message("m-theirs", theirs, "theirs"))
        gate.set()
        await until(pilot, lambda: all(i.status != "sending" for i in app.pending.items))
        await pilot.pause(0.4)
        # Their row was never on screen: no PUT reaches it, and the local mark stays behind it.
        assert all(_at(through) < _at(theirs) for _t, through in server.puts)
        assert app.marks.marks[ts.TOPIC_A1] < _at(theirs)
        scroller.scroll_end(animate=False)
        await until(pilot, lambda: any(_at(t) > _at(theirs) for _x, t in server.puts))


async def test_the_catch_up_at_the_streams_head_loads_a_read_from_just_before_it() -> None:
    server = ts.standard_server()
    _unread_room_b(server)
    gate, hold = threading.Event(), threading.Event()
    server.streams = [ts.StreamScript(frames=[ts.CURSOR_HEAD], gates={0: gate}, hold=hold)]
    app = tui_app.PepperApp(server.client())
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: f"room:{ts.ROOM_B}" in app.sidebar._by_key)
            assert _room_unread(app, ts.ROOM_B)
            # Another device reads it before the stream's first cursor: no event will say so,
            # and the room list (activity only) does not change.
            server.marks[ts.TOPIC_B1] = LATER
            gate.set()
            await until(pilot, lambda: not _room_unread(app, ts.ROOM_B))
    finally:
        hold.set()


async def test_a_stream_edit_or_delete_of_a_row_on_an_older_page_reads_that_row_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The newest page's reconcile never returns a row paged back to, so an event naming a
    held row reads it again by itself (``at``)."""
    monkeypatch.setattr(state, "OLDER_PAGE", 4)
    server = ts.standard_server()
    server.page_size = 4
    server.messages[ts.TOPIC_A1] = [
        ts.message(f"m{i:02d}", f"2026-09-16T10:{i:02d}:00+00:00", f"body {i}") for i in range(8)
    ]
    gate_edit, gate_delete, gate_gone, hold = (threading.Event() for _ in range(4))
    server.streams = [
        ts.StreamScript(
            frames=[
                ts.CURSOR_HEAD,
                ts.feed_frame("message.edited", {"topic_id": ts.TOPIC_A1, "message_id": "m01"}, 2),
                ts.feed_frame("message.deleted", {"topic_id": ts.TOPIC_A1, "message_id": "m02"}, 3),
                ts.feed_frame("message.deleted", {"topic_id": ts.TOPIC_A1, "message_id": "m03"}, 4),
            ],
            gates={1: gate_edit, 2: gate_delete, 3: gate_gone},
            hold=hold,
        )
    ]
    app = tui_app.PepperApp(server.client())
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: len(app.rooms) == 2 and app._stream_healthy)
            app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
            await until(pilot, lambda: "body 7" in body_text(app))
            app._load_older()
            await until(pilot, lambda: "m01" in app.store.rows)
            await _settled(pilot, server)
            rows = server.messages[ts.TOPIC_A1]
            app.on_app_blur(events.AppBlur())
            rows[1].update(
                raw_body="edited",
                rendered_html="<p>edited</p>",
                is_edited=True,
                edited_at="2026-09-16T11:00:00+00:00",
            )
            gate_edit.set()
            # While blurred the row waits, like the topic poll; focus reads it.
            await until(pilot, lambda: "m01" in app._stale_rows)
            await pilot.pause(0.3)
            assert not any("at" in q for _m, _p, q in server.requests)
            app.on_app_focus(events.AppFocus())
            await until(pilot, lambda: app.store.rows["m01"].raw_body == "edited")
            rows[2].update(
                is_deleted=True,
                raw_body="",
                rendered_html="",
                deleted_at="2026-09-16T11:01:00+00:00",
            )
            gate_delete.set()
            await until(pilot, lambda: app.store.rows["m02"].is_deleted)
            # A row the server no longer has at all becomes a tombstone too.
            del rows[3]
            gate_gone.set()
            await until(pilot, lambda: app.store.rows["m03"].is_deleted)
            ats = [q.get("at") for m, _p, q in server.requests if m == "GET" and "at" in q]
            assert ats == ["m01", "m02", "m03"]
    finally:
        hold.set()


async def test_a_row_refresh_that_fails_in_transit_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(state, "BACKOFF_STEPS", (0.05, 0.05, 0.05, 0.05))
    server = ts.standard_server()
    gate, hold = threading.Event(), threading.Event()
    server.streams = [
        ts.StreamScript(
            frames=[
                ts.CURSOR_HEAD,
                ts.feed_frame("message.edited", {"topic_id": ts.TOPIC_A1, "message_id": "m1"}, 2),
            ],
            gates={1: gate},
            hold=hold,
        )
    ]
    path = f"/api/v1/groups/{ts.ROOM_A}/topics/{ts.TOPIC_A1}/messages"
    app = tui_app.PepperApp(server.client())
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: len(app.rooms) == 2 and app._stream_healthy)
            app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
            await until(pilot, lambda: "flight options" in body_text(app))
            await _settled(pilot, server)
            server.messages[ts.TOPIC_A1][0].update(
                raw_body="edited", is_edited=True, edited_at="2026-09-16T18:00:00+00:00"
            )
            server.raise_on[path] = httpx.ConnectError("down")
            gate.set()
            await until(pilot, lambda: any("at" in q for _m, _p, q in server.requests))
            server.raise_on.clear()
            # Retried with backoff (not dropped for the next reconcile): the row is read again.
            await until(pilot, lambda: app.store.rows["m1"].raw_body == "edited")
            ats = [q["at"] for m, _p, q in server.requests if m == "GET" and "at" in q]
            assert len(ats) >= 2 and set(ats) == {"m1"}
    finally:
        hold.set()
