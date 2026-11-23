"""A topic opens at the person's read cursor and is marked read progressively (read-state-sync
§5.1 (b), decision Q8; pepper-cli#132).

A long topic whose cursor is part-way up opens on the first message after it, under a
``new messages`` divider. What is marked read follows the rows that have been fully inside the
visible region, a PUT at a time, and each PUT carries that message's own ``created_at``.
"""

from __future__ import annotations

import threading

import pytest
from textual import events

from pepper_cli.tui import app as tui_app
from pepper_cli.tui import poller, state, stream
from pepper_cli.tui.thread import ThreadView
from tests import tui_support as ts
from tests.tui_support import body_text, until


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ACTIVE_INTERVAL", "COOLING_INTERVAL", "IDLE_INTERVAL", "DM_INTERVAL"):
        monkeypatch.setattr(state, name, 1000.0)
    monkeypatch.setattr(state, "RAIL_INTERVAL", 1000.0)
    monkeypatch.setattr(state, "RAIL_INTERVAL_LIVE", 1000.0)
    monkeypatch.setattr(state, "TOPIC_INTERVAL_LIVE", 1000.0)
    monkeypatch.setattr(state, "BACKOFF_STEPS", (0.05, 0.05, 0.05, 0.05))
    monkeypatch.setattr(stream, "RECONNECT_FIRST", 0.01)
    monkeypatch.setattr(tui_app, "utcnow", lambda: ts.NOW)


def _stamp(i: int) -> str:
    """Row ``i``'s ``created_at``: microseconds, so a reformatted value would not match."""
    return f"2026-09-16T17:{i:02d}:00.{i + 1:06d}+00:00"


def _backlog(server: ts.FakeServer, *, rows: int = 40, read_through: int | None = 9) -> None:
    """A topic of ``rows`` messages whose server cursor stands at row ``read_through``."""
    server.messages[ts.TOPIC_A1] = [
        ts.message(f"r{i:02d}", _stamp(i), f"row {i}") for i in range(rows)
    ]
    server.rooms[0]["topics"][0]["last_activity_at"] = _stamp(rows - 1)
    if read_through is None:
        server.marks.pop(ts.TOPIC_A1, None)
    else:
        server.marks[ts.TOPIC_A1] = _stamp(read_through)
    server.bump()


def _index(raw: str) -> int:
    return int(raw[14:16])


def _put_rows(server: ts.FakeServer) -> list[int]:
    return [_index(through) for topic, through in server.puts if topic == ts.TOPIC_A1]


async def _open(pilot, app: tui_app.PepperApp) -> None:
    await until(pilot, lambda: len(app.rooms) == 2 and app.marks.unread)
    app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
    await until(pilot, lambda: app.loaded_topic and "new messages" in body_text(app))
    await until(pilot, lambda: app.thread.scroller.scroll_y > 0)
    await pilot.pause(0.3)


def _spans(app: tui_app.PepperApp) -> list[tuple[str, int, int]]:
    spans = app._message_spans()
    assert spans is not None
    return spans


async def test_a_topic_with_unread_messages_opens_at_the_first_one_after_the_cursor() -> None:
    server = ts.standard_server()
    _backlog(server)
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 20)) as pilot:
        await _open(pilot, app)
        text = body_text(app)
        assert "row 10" in text
        scroller = app.thread.scroller
        assert not app.thread.pinned
        # The divider is the top line of the view, and the first unread row is the next thing.
        assert scroller.scroll_y == app._divider_line()
        first_unread = next(s for s in _spans(app) if _index(s[0]) == 10)
        assert first_unread[1] == scroller.scroll_y + 1
        # What opened in view is read; the newest row, far below, is not.
        puts = _put_rows(server)
        assert puts
        assert min(puts) >= 10
        assert max(puts) < 39


async def test_the_value_sent_is_the_messages_own_created_at_string() -> None:
    server = ts.standard_server()
    _backlog(server)
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 20)) as pilot:
        await _open(pilot, app)
        await until(pilot, lambda: bool(_put_rows(server)))
        sent = [through for topic, through in server.puts if topic == ts.TOPIC_A1]
        assert all(through == _stamp(_index(through)) for through in sent)


async def test_scrolling_down_marks_the_newest_fully_visible_row_each_time() -> None:
    server = ts.standard_server()
    _backlog(server)
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 20)) as pilot:
        await _open(pilot, app)
        scroller = app.thread.scroller
        height = scroller.scrollable_content_region.height
        before = max(_put_rows(server))
        scroller.scroll_to(y=scroller.scroll_y + 10, animate=False)
        await until(pilot, lambda: max(_put_rows(server)) > before)
        top, bottom = scroller.scroll_y, scroller.scroll_y + height
        newest_visible = max(
            _index(s) for s, start, end in _spans(app) if start >= top and end <= bottom
        )
        await until(pilot, lambda: max(_put_rows(server)) == newest_visible)
        # Rows below it have not been on screen, however far down they are: never claimed.
        assert max(_put_rows(server)) < 39
        assert app.marks.marks[ts.TOPIC_A1] == state.parse_ts(_stamp(newest_visible))


@pytest.mark.parametrize("taller", [False, True])
async def test_a_scroll_handled_after_a_redraw_marks_from_the_live_position(
    monkeypatch: pytest.MonkeyPatch, taller: bool
) -> None:
    """VR-6a37bf4b: a ``Scrolled`` handled after a poll redrew the topic names lines of the old
    layout. It is not dropped: the rows on screen now, at the live position, are marked, and
    no further than the live viewport reaches."""
    server = ts.standard_server()
    _backlog(server)
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 20)) as pilot:
        await _open(pilot, app)
        thread = app.thread
        scroller = thread.scroller
        height = scroller.scrollable_content_region.height
        before = max(_put_rows(server))
        held: list[ThreadView.Scrolled] = []
        real_post = thread.post_message

        def hold(message):
            if isinstance(message, ThreadView.Scrolled):
                held.append(message)
                return True
            return real_post(message)

        monkeypatch.setattr(thread, "post_message", hold)
        scroller.scroll_to(y=scroller.scroll_y + 10, animate=False)
        await until(pilot, lambda: bool(held))
        monkeypatch.setattr(thread, "post_message", real_post)
        shown = thread.show_id
        app._render_topic(new_rows=0, reset=False)  # a poll's redraw: same rows, new layout
        assert thread.show_id != shown
        await until(pilot, lambda: app._message_spans() is not None)
        if taller:
            # The viewport was taller when it scrolled (a ``N new`` line has since taken a
            # row): the old height must not reach below the screen as it is now.
            held[-1].height = height + 5
        app.on_thread_view_scrolled(held[-1])
        top, bottom = scroller.scroll_y, scroller.scroll_y + height
        newest_visible = max(
            _index(s) for s, start, end in _spans(app) if start >= top and end <= bottom
        )
        await until(pilot, lambda: max(_put_rows(server)) == newest_visible)
        assert newest_visible > before


async def test_a_row_cut_by_either_edge_is_not_seen() -> None:
    server = ts.standard_server()
    _backlog(server)
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 20)) as pilot:
        await _open(pilot, app)
        scroller = app.thread.scroller
        height = scroller.scrollable_content_region.height
        spans = _spans(app)
        k = 20
        _, start, end = next(s for s in spans if _index(s[0]) == k)
        # Row k's bottom line is one line below the view: it is cut, so row k-1 is the newest.
        scroller.scroll_to(y=end - height - 1, animate=False)
        await pilot.pause(0.1)
        assert _index(app._seen_through() or "") == k - 1
        scroller.scroll_to(y=end - height, animate=False)
        await pilot.pause(0.1)
        assert _index(app._seen_through() or "") == k
        # A row whose top line is above the view is cut as well: the first fully visible one
        # is the next, but it is never the newest unless it is the only one.
        scroller.scroll_to(y=start + 1, animate=False)
        await pilot.pause(0.1)
        assert _index(app._seen_through() or "") > k


async def test_leaving_mid_backlog_reopens_at_the_unread_half() -> None:
    server = ts.standard_server()
    _backlog(server)
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 20)) as pilot:
        await _open(pilot, app)
        scroller = app.thread.scroller
        scroller.scroll_to(y=scroller.scroll_y + 10, animate=False)
        await pilot.pause(0.4)
        marked = max(_put_rows(server))
        await until(pilot, lambda: server.marks[ts.TOPIC_A1] == _stamp(marked))
        app.open_conversation(state.PEPPER)
        await pilot.pause(0.2)
        server.bump()
        app._run("rail")
        await pilot.pause(0.4)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: app.loaded_topic and "new messages" in body_text(app))
        await until(pilot, lambda: app.thread.scroller.scroll_y > 0)
        first = next(s for s in _spans(app) if _index(s[0]) == marked + 1)
        assert first[1] == app.thread.scroller.scroll_y + 1  # the divider is above row marked+1


async def test_a_short_unread_tail_opens_at_the_foot_and_is_read() -> None:
    server = ts.standard_server()
    _backlog(server, read_through=37)
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 20)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2 and app.marks.unread)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: "row 39" in body_text(app) and app.thread.at_end)
        assert "new messages" in body_text(app)
        await until(pilot, lambda: 39 in _put_rows(server))


async def test_a_topic_with_nothing_unread_opens_at_the_foot_without_a_divider() -> None:
    server = ts.standard_server()
    _backlog(server, read_through=39)
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 20)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: "row 39" in body_text(app) and app.thread.pinned)
        assert "new messages" not in body_text(app)


async def test_a_topic_the_person_has_no_cursor_in_opens_at_the_foot() -> None:
    """First sight (§5.3): no cursor to open at, and nothing is marked from the local seed."""
    server = ts.standard_server()
    _backlog(server, read_through=None)
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 20)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: "row 39" in body_text(app) and app.thread.pinned)
        assert "new messages" not in body_text(app)


async def test_a_cursor_older_than_the_newest_page_reads_older_pages_before_landing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(state, "OLDER_PAGE", 5)
    server = ts.standard_server()
    server.page_size = 10
    _backlog(server, read_through=14)
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 20)) as pilot:
        await _open(pilot, app)
        reads = [
            q for m, p, q in server.requests if m == "GET" and p.endswith(f"{ts.TOPIC_A1}/messages")
        ]
        assert len(reads) >= 5  # the newest page, then older ones back to the cursor
        first_unread = next(s for s in _spans(app) if _index(s[0]) == 15)
        assert first_unread[1] == app.thread.scroller.scroll_y + 1
        # Nothing was marked while the pages were read: the rows before the cursor's own page
        # were never on screen, and every PUT is past the cursor.
        assert min(_put_rows(server)) >= 15


async def test_nothing_is_marked_while_the_terminal_is_blurred_and_focus_marks_what_is_shown() -> (
    None
):
    server = ts.standard_server()
    _backlog(server)
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 20)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2 and app.marks.unread)
        app.on_app_blur(events.AppBlur())
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: app.loaded_topic and "new messages" in body_text(app))
        await pilot.pause(0.5)
        assert _put_rows(server) == []
        app.on_app_focus(events.AppFocus())
        await until(pilot, lambda: bool(_put_rows(server)))
        assert max(_put_rows(server)) < 39


async def test_a_topic_with_no_cursor_is_not_marked_from_the_rows_on_screen() -> None:
    """First sight never marks (§5.3): with no cursor there is nothing to advance, so the rows
    in the visible region claim nothing. Only reaching the foot does (the existing rule)."""
    server = ts.standard_server()
    _backlog(server)
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 20)) as pilot:
        await _open(pilot, app)
        await until(pilot, lambda: bool(_put_rows(server)))
        app.thread.scroller.scroll_to(y=0, animate=False)
        await pilot.pause(0.2)
        app.marks.marks.pop(ts.TOPIC_A1)
        app._owed.clear()
        app._note_seen(app.thread.render_id)
        assert ts.TOPIC_A1 not in app._owed
        assert ts.TOPIC_A1 not in app.marks.marks


async def test_a_failed_older_page_while_opening_ends_the_hold_and_the_end_still_marks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VR-cc3e4d41: nothing retries an older page that failed during the opening, so the hold
    must end there. The topic lands on the oldest loaded row, and reaching the end marks it."""
    monkeypatch.setattr(state, "OLDER_PAGE", 5)
    server = ts.standard_server()
    server.page_size = 10
    _backlog(server, rows=60, read_through=10)
    path = f"/api/v1/groups/{ts.ROOM_A}/topics/{ts.TOPIC_A1}/messages"
    real, failed = poller.fetch_older, []

    def flaky(api, conv, *, before, limit):
        if not failed:
            failed.append(before)
            server.fail[path] = 500
            try:
                return real(api, conv, before=before, limit=limit)
            finally:
                del server.fail[path]
        return real(api, conv, before=before, limit=limit)

    monkeypatch.setattr(poller, "fetch_older", flaky)
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 20)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2 and app.marks.unread)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: bool(failed) and not app._opening and app.loaded_topic)
        await pilot.pause(0.3)
        scroller = app.thread.scroller
        scroller.scroll_end(animate=False)
        await until(pilot, lambda: 59 in _put_rows(server))


async def test_a_first_page_that_beats_the_rail_marks_nothing_until_the_unread_answer() -> None:
    """VR-a6d198ad: with no unread answer yet, the foot cannot be told from a backlog, so
    nothing is marked; once it lands the topic opens at the cursor and only that is read."""
    server = ts.standard_server()
    _backlog(server)
    gate = threading.Event()
    server.gates["/api/v1/groups/unread"] = gate
    app = tui_app.PepperApp(server.client())
    try:
        async with app.run_test(size=(120, 20)) as pilot:
            await pilot.pause(0.2)
            app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
            await until(pilot, lambda: app.loaded_topic and "row 39" in body_text(app))
            await pilot.pause(0.5)
            assert _put_rows(server) == []
            gate.set()
            await until(pilot, lambda: "new messages" in body_text(app))
            await until(pilot, lambda: bool(_put_rows(server)))
            assert max(_put_rows(server)) < 39
    finally:
        gate.set()


async def test_a_failed_unread_request_keeps_the_hold_until_a_poll_answers() -> None:
    """Greptile P1: the rail's room list answering is not the unread answer. With the unread
    route failing, nothing is marked and the topic is not opened at the foot; when a later poll
    answers, it opens at the cursor."""
    server = ts.standard_server()
    _backlog(server)
    server.fail["/api/v1/groups/unread"] = 500
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 20)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: app.loaded_topic and "row 39" in body_text(app))
        await pilot.pause(0.5)
        assert _put_rows(server) == []
        del server.fail["/api/v1/groups/unread"]
        app._rail.refetch_all()
        app._run("rail")
        await until(pilot, lambda: "new messages" in body_text(app))
        await until(pilot, lambda: bool(_put_rows(server)))
        assert max(_put_rows(server)) < 39


async def test_a_reader_who_scrolls_before_the_landing_runs_keeps_their_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Greptile P2: the landing waits two refreshes for the layout; a scroll in that window
    is the reader's, and the landing must not pull them back to the divider."""
    server = ts.standard_server()
    _backlog(server)
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 20)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2 and app.marks.unread)
        scroller = app.thread.scroller
        real, held = scroller.call_after_refresh, []

        def hold(cb, *a, **k) -> None:
            if getattr(cb, "__name__", "") == "land":
                held.append((cb, a, k))
            else:
                real(cb, *a, **k)

        monkeypatch.setattr(scroller, "call_after_refresh", hold)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: app.loaded_topic and "row 39" in body_text(app))
        await until(pilot, lambda: bool(held))
        cb, a, k = held.pop(0)
        cb(*a, **k)  # the first refresh, which takes the scroll count
        scroller.scroll_to(y=3, animate=False)
        await pilot.pause(0.1)
        while held:
            cb, a, k = held.pop(0)
            cb(*a, **k)
        await pilot.pause(0.2)
        assert scroller.scroll_y == 3
