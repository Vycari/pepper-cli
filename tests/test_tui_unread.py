"""Unread in the terminal UI is the server's answer (read-state-sync phase 5c).

The fake server computes ``unread`` rows the way groups does (floor, your own messages,
tombstones); the client shows them and never decides on its own what is unread.
"""

from __future__ import annotations

import threading

import pytest

from pepper_cli.tui import app as tui_app
from pepper_cli.tui import state
from tests import tui_support as ts
from tests.tui_support import until

NEW = "2026-09-16T18:00:00+00:00"


@pytest.fixture(autouse=True)
def _fast_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state, "ACTIVE_INTERVAL", 0.05)
    monkeypatch.setattr(state, "COOLING_INTERVAL", 0.05)
    monkeypatch.setattr(state, "IDLE_INTERVAL", 0.05)
    monkeypatch.setattr(state, "RAIL_INTERVAL", 0.05)
    monkeypatch.setattr(state, "DM_INTERVAL", 0.05)
    monkeypatch.setattr(tui_app, "utcnow", lambda: ts.NOW)


def _flags(app: tui_app.PepperApp) -> dict[str, tuple[bool, bool]]:
    """``topic_id -> (unread, mention)`` for every topic the sidebar shows."""
    rows = state.build_sidebar(
        app.rooms,
        app.marks,
        current=state.PEPPER,
        manual={r.group_id: True for r in app.rooms},
        show_all={r.group_id for r in app.rooms},
        pepper_dot=False,
    )
    return {r.topic_id: (r.unread, r.mention) for r in rows if r.topic_id and r.kind == "topic"}


async def test_a_fresh_profile_shows_what_the_server_says_is_unread() -> None:
    """No local marks and no seed: the markers are the server's rows, not "read as of now"."""
    server = ts.standard_server()
    server.marks = {}  # a fresh profile: the server holds no cursor for anything
    server.floor = "2026-09-16T17:00:00+00:00"
    server.messages[ts.TOPIC_A1] = [
        ts.message("old", "2026-09-16T16:00:00+00:00", "before the floor"),
        ts.message("n1", "2026-09-16T17:10:00+00:00", "after the floor"),
    ]
    server.messages[ts.TOPIC_B1] = [
        ts.message("b1", "2026-09-16T17:20:00+00:00", "ping", mentions=[ts.ME_PERSON]),
        ts.message("b2", "2026-09-16T17:21:00+00:00", "pong"),
    ]
    server.messages[ts.TOPIC_A2] = [ts.message("a2", "2026-09-16T16:00:00+00:00", "old")]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2 and bool(app.marks.unread))
        assert _flags(app) == {
            ts.TOPIC_A1: (True, False),
            ts.TOPIC_A2: (False, False),
            ts.TOPIC_B1: (True, True),
        }
        assert app.marks.unread[ts.TOPIC_B1].unread_count == 2
        assert app.title == "@ Pepper"
        # Nothing was written back to the server on the way: first sight never PUTs.
        assert server.puts == []


async def test_a_server_without_unread_rows_shows_nothing_unread() -> None:
    server = ts.standard_server()
    server.marks = {}
    server.omit_unread = True  # a server that predates phase 5b
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        await pilot.pause(0.2)
        assert not any(unread for unread, _ in _flags(app).values())
        assert app.title == "Pepper"


async def test_your_own_newest_message_does_not_badge_its_topic() -> None:
    """Activity later than the cursor is no longer the rule: the server leaves your own
    messages out of the count, so a topic whose newest message is yours stays read."""
    server = ts.standard_server()
    mine = ts.message("mine", NEW, "mine", is_you=True, author_person_id=ts.ME_PERSON)
    server.messages[ts.TOPIC_B1].append(mine)
    server.rooms[1]["topics"][0]["last_activity_at"] = NEW  # past the mark the server holds
    server.bump()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2 and bool(app.marks.unread))
        await pilot.pause(0.3)
        assert _flags(app)[ts.TOPIC_B1] == (False, False)
        assert app.marks.unread[ts.TOPIC_B1].unread_count == 0
        assert app.title == "Pepper"
        # Someone else's reply after it is unread.
        server.messages[ts.TOPIC_B1].append(ts.message("theirs", "2026-09-16T18:05:00+00:00", "hi"))
        server.rooms[1]["topics"][0]["last_activity_at"] = "2026-09-16T18:05:00+00:00"
        server.bump()
        await until(pilot, lambda: _flags(app)[ts.TOPIC_B1][0])


async def test_sending_a_message_does_not_badge_its_own_topic() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.open_conversation(state.Conversation(ts.ROOM_B, ts.TOPIC_B1))
        await until(pilot, lambda: app.loaded_topic)
        app.composer.focus()
        app.composer.insert("hello")
        await pilot.press("enter")
        await until(pilot, lambda: bool(server.posts))
        await pilot.pause(0.4)
        app.open_conversation(state.PEPPER)  # leave: the open topic no longer hides a marker
        await pilot.pause(0.4)
        assert _flags(app)[ts.TOPIC_B1] == (False, False)


async def test_a_read_that_passes_the_activity_clears_the_row_and_a_partial_one_asks_again() -> (
    None
):
    server = ts.standard_server()
    server.messages[ts.TOPIC_B1] = [
        ts.message("b1", "2026-09-16T17:00:00+00:00", "one"),
        ts.message("b2", "2026-09-16T17:30:00+00:00", "two"),
    ]
    server.marks[ts.TOPIC_B1] = "2026-09-15T10:00:00+00:00"
    server.rooms[1]["topics"][0]["last_activity_at"] = "2026-09-16T17:30:00+00:00"
    server.bump()
    first, second, hold = threading.Event(), threading.Event(), threading.Event()
    partial = ts.read_advanced(ts.TOPIC_B1, "2026-09-16T17:00:00+00:00", 2)
    full = ts.read_advanced(ts.TOPIC_B1, "2026-09-16T17:30:00+00:00", 3)
    server.streams = [
        ts.StreamScript(
            frames=[ts.CURSOR_HEAD, partial, full], gates={1: first, 2: second}, hold=hold
        )
    ]
    app = tui_app.PepperApp(server.client())
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: app._stream_healthy and bool(app.marks.unread))
            assert app.marks.unread[ts.TOPIC_B1].unread_count == 2
            # The partial read moved the cursor to b1; what is left is the server's to say.
            server.marks[ts.TOPIC_B1] = "2026-09-16T17:00:00+00:00"
            first.set()
            await until(pilot, lambda: app.marks.unread[ts.TOPIC_B1].unread_count == 1)
            # The full read clears it, from the frame alone.
            second.set()
            await until(pilot, lambda: not _flags(app)[ts.TOPIC_B1][0])
            assert app.marks.unread[ts.TOPIC_B1].unread_count == 0
    finally:
        hold.set()


async def test_viewing_a_topic_to_its_end_clears_its_marker_before_the_put_answers() -> None:
    server = ts.standard_server()
    server.messages[ts.TOPIC_B1].append(ts.message("m-new", NEW, "fresh"))
    server.rooms[1]["topics"][0]["last_activity_at"] = NEW
    server.bump()
    gate = threading.Event()
    server.gates[f"PUT /api/v1/groups/{ts.ROOM_B}/topics/{ts.TOPIC_B1}/read"] = gate
    app = tui_app.PepperApp(server.client())
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: _flags(app).get(ts.TOPIC_B1, (False,))[0])
            app.open_conversation(state.Conversation(ts.ROOM_B, ts.TOPIC_B1))
            await until(pilot, lambda: "fresh" in ts.body_text(app))
            await until(pilot, lambda: not _flags(app)[ts.TOPIC_B1][0])
            assert server.puts == []  # the server has not heard yet
    finally:
        gate.set()
