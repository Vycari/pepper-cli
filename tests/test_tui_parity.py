"""P3 in the running app (Textual Pilot, fake server): reactions, edit and delete, older pages,
folds, new topic, room info, Ctrl+K and ``o``."""

from __future__ import annotations

import inspect
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from rich.console import Group
from rich.text import Text
from textual.geometry import Size
from textual.widget import Widget

from pepper_cli.tui import app as tui_app
from pepper_cli.tui import rows, state
from pepper_cli.tui.screens import (
    ConfirmScreen,
    InfoScreen,
    JumpScreen,
    PickerScreen,
    room_info_text,
)
from tests import tui_support as ts
from tests.tui_support import body_text, plain, until

if TYPE_CHECKING:
    from textual.pilot import Pilot

SIZE = (120, 40)


@pytest.fixture(autouse=True)
def _fast_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state, "ACTIVE_INTERVAL", 0.05)
    monkeypatch.setattr(state, "COOLING_INTERVAL", 0.05)
    monkeypatch.setattr(state, "IDLE_INTERVAL", 0.05)
    monkeypatch.setattr(state, "RAIL_INTERVAL", 0.05)
    monkeypatch.setattr(state, "DM_INTERVAL", 0.05)
    monkeypatch.setattr(state, "BACKOFF_STEPS", (0.05, 0.05, 0.05, 0.05))
    monkeypatch.setattr(tui_app, "utcnow", lambda: ts.NOW)


def _a1(server: ts.FakeServer, *messages: dict) -> None:
    server.messages[ts.TOPIC_A1] = list(messages)


async def _open_a1(app: tui_app.PepperApp, pilot: Pilot, needle: str) -> None:
    await until(pilot, lambda: len(app.rooms) == 2)
    app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
    await until(pilot, lambda: needle in body_text(app))
    app.thread.focus_scroller()
    await pilot.pause()


async def _select_newest(pilot: Pilot) -> None:
    await pilot.press("up")
    await pilot.pause()


async def test_plus_picks_an_emoji_adds_it_and_picking_it_again_takes_it_back() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await _open_a1(app, pilot, "flight options")
        await _select_newest(pilot)
        await pilot.press("plus")
        await until(pilot, lambda: isinstance(app.screen, PickerScreen))
        await pilot.press("1")
        await until(pilot, lambda: "👍 1" in body_text(app))
        assert server.reactions_sent == [("add", "m1", "👍")]
        await pilot.press("plus")
        await until(pilot, lambda: isinstance(app.screen, PickerScreen))
        await pilot.press("enter")  # the highlighted first entry, now yours: takes it back
        await until(pilot, lambda: "👍" not in body_text(app))
        assert server.reactions_sent[-1] == ("clear", "m1", "👍")


async def test_plus_with_nothing_selected_or_on_a_deleted_message_does_not_open_the_picker() -> (
    None
):
    server = ts.standard_server()
    _a1(server, ts.message("m1", "2026-09-16T17:30:00+00:00", "gone", is_deleted=True))
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await _open_a1(app, pilot, rows.DELETED)
        await pilot.press("plus")
        await _select_newest(pilot)
        await pilot.press("plus")
        await pilot.pause()
        assert not isinstance(app.screen, PickerScreen)
        assert server.reactions_sent == []


async def test_e_edits_your_own_message_and_the_server_row_comes_back() -> None:
    server = ts.standard_server()
    _a1(server, ts.message("m1", "2026-09-16T17:30:00+00:00", "old words", is_you=True))
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await _open_a1(app, pilot, "old words")
        await _select_newest(pilot)
        await pilot.press("e")
        await until(pilot, lambda: app.editing is not None)
        assert app.composer.text == "old words"
        assert "Editing your message" in plain(app, "#quote")
        app.composer.load_text("new words")
        await pilot.press("enter")
        await until(pilot, lambda: "new words" in body_text(app))
        assert server.edits == [{"id": "m1", "body": "new words"}]
        assert "(edited)" in body_text(app)
        assert app.editing is None
        assert app.composer.text == ""


async def test_esc_cancels_an_edit_and_brings_the_draft_back() -> None:
    server = ts.standard_server()
    _a1(server, ts.message("m1", "2026-09-16T17:30:00+00:00", "old words", is_you=True))
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await _open_a1(app, pilot, "old words")
        app.composer.load_text("half a thought")
        app.thread.focus_scroller()
        await _select_newest(pilot)
        await pilot.press("e")
        await until(pilot, lambda: app.editing is not None)
        await pilot.press("escape")
        await until(pilot, lambda: app.editing is None)
        assert app.composer.text == "half a thought"
        assert server.edits == []


async def test_e_and_d_refuse_somebody_elses_message() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await _open_a1(app, pilot, "flight options")
        await _select_newest(pilot)
        await pilot.press("e")
        await pilot.press("d")
        await pilot.pause()
        assert app.editing is None
        assert not isinstance(app.screen, ConfirmScreen)
        assert server.deleted == []


async def test_d_asks_first_and_only_y_deletes() -> None:
    server = ts.standard_server()
    _a1(server, ts.message("m1", "2026-09-16T17:30:00+00:00", "oops", is_you=True))
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await _open_a1(app, pilot, "oops")
        await _select_newest(pilot)
        await pilot.press("d")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("n")
        await pilot.pause()
        assert server.deleted == []
        await pilot.press("d")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await until(pilot, lambda: rows.DELETED in body_text(app))
        assert server.deleted == ["m1"]


async def test_a_failed_edit_keeps_the_text_and_says_so() -> None:
    server = ts.standard_server()
    _a1(server, ts.message("m1", "2026-09-16T17:30:00+00:00", "old words", is_you=True))
    app = tui_app.PepperApp(server.client())
    said = ts.notices(app)
    async with app.run_test(size=SIZE) as pilot:
        await _open_a1(app, pilot, "old words")
        await _select_newest(pilot)
        await pilot.press("e")
        await until(pilot, lambda: app.editing is not None)
        app.composer.load_text("new words")
        server.fail[f"POST /api/v1/groups/{ts.ROOM_A}/messages/m1/edit"] = 500
        await pilot.press("enter")
        await until(pilot, lambda: any("HTTP 500" in m for m in said))
        assert app.editing is not None
        assert app.composer.text == "new words"
        assert not app.composer.read_only  # editable again, to fix and retry


async def test_nothing_typed_while_an_edit_saves_is_lost() -> None:
    server = ts.standard_server()
    _a1(server, ts.message("m1", "2026-09-16T17:30:00+00:00", "old words", is_you=True))
    gate = threading.Event()
    server.gates[f"POST /api/v1/groups/{ts.ROOM_A}/messages/m1/edit"] = gate
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await _open_a1(app, pilot, "old words")
        app.composer.load_text("my draft")
        app.thread.focus_scroller()
        await _select_newest(pilot)
        await pilot.press("e")
        await until(pilot, lambda: app.editing is not None)
        app.composer.load_text("new words")
        await pilot.press("enter")
        await until(pilot, lambda: app.composer.read_only)
        await pilot.press("x", "y", "alt+enter")  # held still: the text cannot change
        assert app.composer.text == "new words"
        app.thread.focus_scroller()
        await pilot.press("e")  # a second edit waits for the first to save
        assert app.composer.text == "new words"
        gate.set()
        await until(pilot, lambda: app.editing is None)
        assert not app.composer.read_only
        assert app.composer.text == "my draft"  # the draft set aside comes back whole
        await until(pilot, lambda: "new words" in body_text(app))


async def test_esc_during_a_save_then_e_waits_for_the_save_instead_of_losing_new_text() -> None:
    server = ts.standard_server()
    _a1(server, ts.message("m1", "2026-09-16T17:30:00+00:00", "old words", is_you=True))
    gate = threading.Event()
    server.gates[f"POST /api/v1/groups/{ts.ROOM_A}/messages/m1/edit"] = gate
    app = tui_app.PepperApp(server.client())
    said = ts.notices(app)
    async with app.run_test(size=SIZE) as pilot:
        await _open_a1(app, pilot, "old words")
        app.composer.load_text("my draft")
        app.thread.focus_scroller()
        await _select_newest(pilot)
        await pilot.press("e")
        await until(pilot, lambda: app.editing is not None)
        app.composer.load_text("new words")
        await pilot.press("enter")
        await until(pilot, lambda: app.composer.read_only)
        await pilot.press("escape")  # leaves edit mode; the save carries on
        await until(pilot, lambda: app.editing is None)
        assert not app.composer.read_only
        app.thread.focus_scroller()
        await pilot.press("e")  # a second edit of the same message, before the reply lands
        await until(pilot, lambda: any("Still saving" in m for m in said))
        assert app.editing is None  # refused, so the reply has no newer edit to end
        gate.set()
        await until(pilot, lambda: "new words" in body_text(app))
        assert app.editing is None
        assert app.composer.text == "my draft"


async def test_scrolling_up_past_the_first_row_loads_the_page_before_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(state, "OLDER_PAGE", 4)
    server = ts.standard_server()
    server.page_size = 4
    _a1(
        server,
        *(
            ts.message(f"m{i:02d}", f"2026-09-16T10:{i:02d}:00+00:00", f"body {i}")
            for i in range(10)
        ),
    )
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await _open_a1(app, pilot, "body 9")
        assert "body 5" not in body_text(app)
        assert app.store.older == "6"
        for _ in range(4):
            await pilot.press("up")
        await until(pilot, lambda: "body 5" in body_text(app))
        assert next(m.id for m in app.store.ordered()) == "m02"
        assert app.store.older == "2"
        queries = [q for _, p, q in server.requests if p.endswith("/messages")]
        assert {"before": "6", "limit": "4"} in queries
        for _ in range(8):
            await pilot.press("up")
        await until(pilot, lambda: "body 0" in body_text(app))
        assert not app.store.has_older


async def test_editing_and_deleting_a_row_from_an_older_page_shows_the_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The newest page does not hold these rows, so the reconcile re-read cannot fix them:
    # the edit/delete reply has to.
    monkeypatch.setattr(state, "OLDER_PAGE", 4)
    server = ts.standard_server()
    server.page_size = 4
    _a1(
        server,
        *(
            ts.message(f"m{i:02d}", f"2026-09-16T10:{i:02d}:00+00:00", f"body {i}", is_you=True)
            for i in range(8)
        ),
    )
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await _open_a1(app, pilot, "body 7")
        for _ in range(5):
            await pilot.press("up")
        await until(pilot, lambda: "body 2" in body_text(app))
        app.selected = "msg:m02"
        await pilot.press("e")
        await until(pilot, lambda: app.editing is not None)
        app.composer.load_text("rewritten")
        await pilot.press("enter")
        await until(pilot, lambda: "rewritten" in body_text(app))
        assert "body 2" not in body_text(app)
        assert app.store.rows["m02"].is_edited
        app.thread.focus_scroller()
        app.selected = "msg:m01"
        await pilot.press("d")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await until(pilot, lambda: app.store.rows["m01"].is_deleted)
        await pilot.pause(0.2)  # later polls and the reconcile do not bring either back
        assert "body 1" not in body_text(app)
        assert rows.DELETED in body_text(app)
        assert "rewritten" in body_text(app)


async def test_older_rows_keep_the_view_where_it_was() -> None:
    server = ts.standard_server()
    server.page_size = 6
    _a1(
        server,
        *(
            ts.message(f"m{i:02d}", f"2026-09-16T10:{i:02d}:00+00:00", f"body {i}")
            for i in range(14)
        ),
    )
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(100, 14)) as pilot:
        await _open_a1(app, pilot, "body 13")
        scroller = app.thread.scroller
        scroller.scroll_to(y=0, animate=False)
        await until(pilot, lambda: "body 7" in body_text(app))
        # The rows land before the view moves: the scroll past them waits for the next
        # refresh, so poll for it rather than reading it after one pause.
        await until(pilot, lambda: scroller.scroll_y > 0)  # not left at the top


def _older_thread_server() -> ts.FakeServer:
    server = ts.standard_server()
    _a1(
        server,
        *(
            ts.message(f"m{i:02d}", f"2026-09-16T10:{i:02d}:00+00:00", f"body {i}")
            for i in range(14)
        ),
    )
    return server


async def test_keep_place_retries_until_the_older_rows_are_measured(monkeypatch) -> None:
    """The retry branch, driven by hand: the first callback runs before the layout that
    measures the older rows, waits, and the next one scrolls past them. Out of tries, it
    stops waiting."""
    app = tui_app.PepperApp(_older_thread_server().client())
    async with app.run_test(size=(100, 14)) as pilot:
        await _open_a1(app, pilot, "body 13")
        thread = app.thread
        scroller = thread.scroller
        scroller.scroll_y = 0
        await pilot.pause()
        held: list = []
        scrolled: list[float] = []

        def hold(cb, *_a, **_k) -> None:
            # Only keep-place's own callbacks: the app may schedule others meanwhile.
            if getattr(cb, "__name__", "") == "keep_place":
                held.append(cb)

        monkeypatch.setattr(scroller, "call_after_refresh", hold)
        monkeypatch.setattr(scroller, "scroll_to", lambda *, y, **_: scrolled.append(y))
        # Hold the measured height where it was, as a loaded loop does until its next layout.
        real = inspect.getattr_static(Widget, "virtual_size")
        old = scroller.virtual_size
        measured = [old]
        monkeypatch.setattr(
            type(scroller),
            "virtual_size",
            property(lambda _w: measured[0], lambda w, v: real.__set__(w, v)),
            raising=False,
        )
        # Read here, not assumed 0: the app may itself have loaded an older page by now.
        at = scroller.scroll_y
        thread.show("\n".join(f"older {i}" for i in range(40)), prepended=True)
        keep_place = held.pop()
        keep_place()  # not measured yet: grown is 0
        assert scrolled == []  # it did not scroll by 0
        retry = held.pop()  # it waits for the next refresh instead
        measured[0] = Size(old.width, old.height + 12)  # that layout measures the rows
        retry()
        assert scrolled == [at + 12]  # scrolled past exactly the rows added above
        measured[0] = old
        keep_place(1)  # out of tries with nothing measured: scrolls by 0, waits no longer
        assert scrolled == [at + 12, at]
        assert held == []


async def test_a_reset_stands_down_a_pending_keep_place(monkeypatch) -> None:
    """A keep-place retry from an older page must not move a conversation shown after it."""
    app = tui_app.PepperApp(_older_thread_server().client())
    async with app.run_test(size=(100, 14)) as pilot:
        await _open_a1(app, pilot, "body 13")
        thread = app.thread
        scroller = thread.scroller
        scroller.scroll_y = 0
        await pilot.pause()
        held: list = []
        monkeypatch.setattr(scroller, "call_after_refresh", lambda cb, *a, **k: held.append(cb))
        # Read here, not assumed 0: the app may itself have loaded an older page by now.
        at = scroller.scroll_y
        longer = "\n".join(f"older {i}" for i in range(40)) + "\n" + body_text(app)
        thread.show(longer, prepended=True)
        stale = next(cb for cb in reversed(held) if getattr(cb, "__name__", "") == "keep_place")
        monkeypatch.undo()
        thread.show("\n".join(f"other {i}" for i in range(120)), reset=True)
        await until(pilot, lambda: scroller.scroll_y == scroller.max_scroll_y > 0)
        # Back where the stale one started, so only its conversation tells it apart; with no
        # await in between, nothing else gets to move the view.
        scroller.scroll_y = at
        scrolled: list[float] = []
        monkeypatch.setattr(scroller, "scroll_to", lambda *, y, **_: scrolled.append(y))
        stale(1)  # its last try, where it would otherwise scroll whatever it measures
        assert scrolled == []


async def test_a_scroll_made_while_keep_place_waits_is_not_undone(monkeypatch) -> None:
    """VR-6b3a8f9f: the scroll past older rows waits a refresh or more; a reader who moves
    in that window keeps the place they moved to.

    Driven by hand: run on the event loop, the app's own poll can re-render the pinned thread
    and queue a scroll to the end that lands inside the window, which moves the view for a
    reason this test is not about (a Windows run on pepper-cli#276 ended at 19).
    """
    app = tui_app.PepperApp(_older_thread_server().client())
    async with app.run_test(size=(100, 14)) as pilot:
        await _open_a1(app, pilot, "body 13")
        thread = app.thread
        scroller = thread.scroller
        scroller.scroll_y = 0
        await pilot.pause()
        held: list = []

        def hold(cb, *_a, **_k) -> None:
            if getattr(cb, "__name__", "") == "keep_place":
                held.append(cb)

        scrolled: list[float] = []
        monkeypatch.setattr(scroller, "call_after_refresh", hold)
        monkeypatch.setattr(scroller, "scroll_to", lambda *, y, **_: scrolled.append(y))
        # Read here, not assumed 0: the app may itself have moved the view by now.
        at = scroller.scroll_y
        longer = "\n".join(f"older {i}" for i in range(40)) + "\n" + body_text(app)
        thread.show(longer, prepended=True)
        keep_place = held.pop()
        moved = 3 if at != 3 else 4
        scroller.scroll_y = moved  # the reader moves before keep_place runs
        assert scroller.scroll_y == moved
        keep_place()
        assert scrolled == []  # it stood down rather than scroll past the older rows
        assert held == []  # and did not wait to try again
        assert scroller.scroll_y == moved


async def test_a_stick_to_the_end_stands_down_if_the_reader_moved(monkeypatch) -> None:
    """#274: the scroll to the end a pinned render arms waits a refresh or more; a reader
    who moves in that window keeps the place they moved to. Driven by hand for the same
    reason as the keep-place test above."""
    app = tui_app.PepperApp(_older_thread_server().client())
    async with app.run_test(size=(100, 14)) as pilot:
        await _open_a1(app, pilot, "body 13")
        thread = app.thread
        scroller = thread.scroller
        await until(pilot, lambda: scroller.max_scroll_y > 0 and thread.pinned)
        held: list = []

        def hold(cb, *_a, **_k) -> None:
            if getattr(cb, "__name__", "") == "stick_to_end":
                held.append(cb)

        scrolled: list[bool] = []
        monkeypatch.setattr(scroller, "call_after_refresh", hold)
        monkeypatch.setattr(
            scroller, "scroll_end", lambda **kw: scrolled.append(kw.get("immediate", False))
        )
        at = scroller.scroll_y
        thread.show(body_text(app))  # pinned: arms the scroll to the end
        moved = at - 3
        scroller.scroll_y = moved  # the reader moves before it runs
        held.pop()()
        assert scrolled == []  # it stood down
        assert held == []
        assert scroller.scroll_y == moved


async def test_a_stick_to_the_end_stands_down_if_the_reader_moves_between_refreshes(
    monkeypatch,
) -> None:
    """#274: the first refresh can come while the reader is still at the end and arm the last
    try; a scroll before that one runs must still win, or it drags the reader back down."""
    app = tui_app.PepperApp(_older_thread_server().client())
    async with app.run_test(size=(100, 14)) as pilot:
        await _open_a1(app, pilot, "body 13")
        thread = app.thread
        scroller = thread.scroller
        await until(pilot, lambda: scroller.max_scroll_y > 0 and thread.pinned)
        held: list = []

        def hold(cb, *_a, **_k) -> None:
            if getattr(cb, "__name__", "") == "stick_to_end":
                held.append(cb)

        scrolled: list[bool] = []
        monkeypatch.setattr(scroller, "call_after_refresh", hold)
        monkeypatch.setattr(
            scroller, "scroll_end", lambda **kw: scrolled.append(kw.get("immediate", False))
        )
        at = scroller.scroll_y
        thread.show(body_text(app))
        held.pop()()  # still at the end: this one waits for the layout, and does not scroll
        assert scrolled == []
        assert len(held) == 1
        moved = at - 3
        scroller.scroll_y = moved
        held.pop()()
        assert scrolled == []  # the last try stood down too
        assert scroller.scroll_y == moved


async def test_rows_that_arrive_when_the_reader_moves_keep_their_cue(monkeypatch) -> None:
    """Codex and Greptile on #279: rows that arrive while pinned, then a reader who moves
    before the deferred scroll runs, are below the screen. They stay counted, or later polls
    deduplicate them and the "N new" cue is never shown."""
    app = tui_app.PepperApp(_older_thread_server().client())
    async with app.run_test(size=(100, 14)) as pilot:
        await _open_a1(app, pilot, "body 13")
        thread = app.thread
        scroller = thread.scroller
        await until(pilot, lambda: scroller.max_scroll_y > 0 and thread.pinned)
        held: list = []

        def hold(cb, *_a, **_k) -> None:
            if getattr(cb, "__name__", "") == "stick_to_end":
                held.append(cb)

        monkeypatch.setattr(scroller, "call_after_refresh", hold)
        monkeypatch.setattr(scroller, "scroll_end", lambda **kw: None)
        at = scroller.scroll_y
        thread.show(body_text(app) + "\ntheir new row", new_rows=1)
        assert not thread.query_one("#thread-new").display  # still at the end, nothing unseen
        moved = at - 3
        scroller.scroll_y = moved  # the reader moves before it runs
        held.pop()()
        assert thread._unseen == 1  # the row is below the screen: still counted
        assert thread.query_one("#thread-new").display
        assert "1 new" in plain(app, "#thread-new")
        assert scroller.scroll_y == moved


async def test_overlapping_scrolls_do_not_restore_a_cue_for_rows_on_screen(monkeypatch) -> None:
    """Greptile on #279: two deferred scrolls can capture the same position. The first
    reaches the end; the second must not mistake that movement for a reader scroll and count
    rows that are already on screen."""
    for interval in ("ACTIVE_INTERVAL", "COOLING_INTERVAL", "IDLE_INTERVAL"):
        monkeypatch.setattr(state, interval, 60.0)
    app = tui_app.PepperApp(_older_thread_server().client())
    async with app.run_test(size=(100, 14)) as pilot:
        await _open_a1(app, pilot, "body 13")
        thread = app.thread
        scroller = thread.scroller
        await until(pilot, lambda: scroller.max_scroll_y > 0 and thread.pinned)
        held: list = []

        def hold(cb, *a, **k) -> None:
            if getattr(cb, "__name__", "") == "stick_to_end":
                held.append((cb, a, k))

        monkeypatch.setattr(scroller, "call_after_refresh", hold)
        thread.show(body_text(app) + "\nfirst new row", new_rows=1)
        # The second render, before the first callback ran: it captured the same position.
        thread.show(body_text(app) + "\nfirst new row\nsecond new row", new_rows=2)
        waiting = list(held)
        held.clear()
        assert len(waiting) == 2
        for cb, a, k in waiting:  # each waits one more refresh
            cb(*a, **k)
        await pilot.pause(0.1)  # the layout measures the new rows
        last = list(held)
        held.clear()
        assert len(last) == 2
        for cb, a, k in last:  # the first reaches the end, the second must stand down
            cb(*a, **k)
        await pilot.pause()
        assert thread.pinned  # the rows are on screen
        assert thread._unseen == 0
        assert not thread.query_one("#thread-new").display


async def test_a_scroll_left_over_from_another_conversation_does_not_count_its_rows(
    monkeypatch,
) -> None:
    """Greptile on #279: a callback can outlive a conversation switch. It must not add its
    rows to the new conversation's cue, which the new render has already reset."""
    app = tui_app.PepperApp(_older_thread_server().client())
    async with app.run_test(size=(100, 14)) as pilot:
        await _open_a1(app, pilot, "body 13")
        thread = app.thread
        scroller = thread.scroller
        await until(pilot, lambda: scroller.max_scroll_y > 0 and thread.pinned)
        held: list = []

        def hold(cb, *a, **k) -> None:
            if getattr(cb, "__name__", "") == "stick_to_end":
                held.append((cb, a, k))

        monkeypatch.setattr(scroller, "call_after_refresh", hold)
        thread.show(body_text(app) + "\nstale row", new_rows=1)
        stale = held.pop()  # the only callback so far: conversation A's
        app.open_conversation(state.Conversation(ts.ROOM_B, ts.TOPIC_B1))
        await until(pilot, lambda: "next book" in body_text(app))
        scroller.scroll_y = 0  # the switch moved the view before the callback ran
        cb, a, k = stale
        cb(*a, **k)
        # Before any poll can repaint: the new conversation starts with nothing counted.
        assert thread._unseen == 0
        assert not thread.query_one("#thread-new").display


async def test_a_first_page_is_marked_read_only_once_its_scroll_lands(monkeypatch) -> None:
    """Codex P2 on #279: the first page used to be marked read the moment the render
    scheduled its scroll to the end, before the newest row was on screen. A reader who moves
    in that window must leave the topic unread."""
    server = ts.standard_server()
    _a1(
        server,
        *(
            ts.message(f"r{i:02d}", f"2026-09-16T17:{i:02d}:00+00:00", f"row {i}")
            for i in range(40)
        ),
    )
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 20)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        scroller = app.thread.scroller
        real = scroller.call_after_refresh
        held: list = []

        def hold(cb, *a, **k) -> None:
            if getattr(cb, "__name__", "") == "stick_to_end":
                held.append(cb)
            else:
                real(cb, *a, **k)

        monkeypatch.setattr(scroller, "call_after_refresh", hold)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: "row 39" in body_text(app))
        await pilot.pause(0.4)  # a few polls while the scroll to the end has not run
        assert server.puts == []  # nothing marked before the newest row is on screen
        scroller.scroll_y = 5  # the reader moves before it runs
        await pilot.pause()
        while held:
            held.pop(0)()
        await pilot.pause(0.4)
        assert server.puts == []  # it stood down: nothing was marked


@pytest.mark.parametrize("placement", ["stay", "away", "back", "end"])
async def test_a_live_poll_marks_only_once_its_scroll_lands(placement, monkeypatch) -> None:
    """Codex P2 on #279: a live poll used to mark its rows read the moment the render
    scheduled the scroll to the end, before they were on screen. When the reader moves first
    the scroll stands down, so nothing may have been claimed, and that holds even if they
    return to the offset captured at render time. A reader who moves to the new end has seen
    the rows, and that landing must still mark them."""
    for interval in ("ACTIVE_INTERVAL", "COOLING_INTERVAL", "IDLE_INTERVAL"):
        monkeypatch.setattr(state, interval, 60.0)
    server = _older_thread_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(100, 14)) as pilot:
        await _open_a1(app, pilot, "body 13")
        thread = app.thread
        scroller = thread.scroller
        await until(pilot, lambda: scroller.max_scroll_y > 0 and thread.pinned)
        await until(pilot, lambda: (ts.TOPIC_A1, "2026-09-16T10:13:00+00:00") in server.puts)
        held: list = []
        real = scroller.call_after_refresh

        def hold(cb, *a, **k) -> None:
            if getattr(cb, "__name__", "") == "stick_to_end":
                held.append((cb, a, k))
            else:
                real(cb, *a, **k)

        monkeypatch.setattr(scroller, "call_after_refresh", hold)
        server.messages[ts.TOPIC_A1].append(
            ts.message("m99", "2026-09-16T10:14:00+00:00", "late row")
        )
        app._run("topic")
        await until(pilot, lambda: "late row" in body_text(app))
        await pilot.pause()
        # The row is rendered but below the viewport until the scroll lands.
        assert (ts.TOPIC_A1, "2026-09-16T10:14:00+00:00") not in server.puts
        at = scroller.scroll_y
        if placement == "away":
            scroller.scroll_y = at - 3  # the reader moves before the scroll runs
            await pilot.pause()
        elif placement == "back":
            scroller.scroll_y = at - 3  # away, then back to the offset it captured
            await pilot.pause()
            scroller.scroll_y = at
            await pilot.pause()
            assert scroller.scroll_y == at
            assert scroller.max_scroll_y - at >= 2  # the old end is not the new one
        elif placement == "end":
            scroller.scroll_end(animate=False)  # the reader reaches the new end themselves
            await pilot.pause()
            assert scroller.scroll_y != at
        while held:
            cb, a, k = held.pop(0)
            cb(*a, **k)
        await pilot.pause()
        if placement in ("away", "back"):
            assert thread._unseen == 1  # the row is below the screen, still counted
            assert (ts.TOPIC_A1, "2026-09-16T10:14:00+00:00") not in server.puts
        else:
            await until(pilot, lambda: (ts.TOPIC_A1, "2026-09-16T10:14:00+00:00") in server.puts)


async def test_scrolling_back_a_line_short_of_a_new_row_is_not_marked_read(monkeypatch) -> None:
    """Codex P2 on #279: ``pinned`` accepts one line short of the end, so a one-line growth
    plus a scroll away and back would have landed while the newest line was still hidden."""
    for interval in ("ACTIVE_INTERVAL", "COOLING_INTERVAL", "IDLE_INTERVAL"):
        monkeypatch.setattr(state, interval, 60.0)
    server = _older_thread_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(100, 14)) as pilot:
        await _open_a1(app, pilot, "body 13")
        thread = app.thread
        scroller = thread.scroller
        await until(pilot, lambda: scroller.max_scroll_y > 0 and thread.pinned)
        await until(pilot, lambda: (ts.TOPIC_A1, "2026-09-16T10:13:00+00:00") in server.puts)
        held: list = []
        real = scroller.call_after_refresh

        def hold(cb, *a, **k) -> None:
            if getattr(cb, "__name__", "") == "stick_to_end":
                held.append((cb, a, k))
            else:
                real(cb, *a, **k)

        monkeypatch.setattr(scroller, "call_after_refresh", hold)
        at = scroller.scroll_y
        shown = thread.query_one("#thread-body").content
        thread.show(
            Group(shown, Text("late row")),
            new_rows=1,
            through="2026-09-16T10:14:00+00:00",
        )
        await pilot.pause(0.1)  # the layout measures the one added line
        assert scroller.max_scroll_y - at == 1  # exactly the boundary this guards
        scroller.scroll_y = at - 3
        await pilot.pause()
        scroller.scroll_y = at
        await pilot.pause()
        assert thread.pinned  # one line short, which ``pinned`` tolerates
        assert not thread.at_end  # but the newest line is below the viewport
        while held:
            cb, a, k = held.pop(0)
            cb(*a, **k)
        await pilot.pause()
        assert thread._unseen == 1
        assert (ts.TOPIC_A1, "2026-09-16T10:14:00+00:00") not in server.puts


async def test_a_landed_event_from_a_replaced_render_is_ignored(monkeypatch) -> None:
    """Codex P2 on #279: ``Landed`` is queued. One posted for a render that a later reset
    replaced must not mark the new screen read."""
    for interval in ("ACTIVE_INTERVAL", "COOLING_INTERVAL", "IDLE_INTERVAL"):
        monkeypatch.setattr(state, interval, 60.0)
    app = tui_app.PepperApp(_older_thread_server().client())
    async with app.run_test(size=(100, 14)) as pilot:
        await _open_a1(app, pilot, "body 13")
        thread = app.thread
        scroller = thread.scroller
        held: list = []
        real = scroller.call_after_refresh

        def hold(cb, *a, **k) -> None:
            if getattr(cb, "__name__", "") == "stick_to_end":
                held.append(cb)
            else:
                real(cb, *a, **k)

        monkeypatch.setattr(scroller, "call_after_refresh", hold)
        calls: list[str | None] = []
        monkeypatch.setattr(app, "_note_viewed", lambda **kw: calls.append(kw.get("through")))
        stale = thread.render_id
        thread.show("a later screen", reset=True)
        thread.post_message(tui_app.ThreadView.Landed(stale, "2026-09-16T10:00:00+00:00"))
        await pilot.pause()
        assert calls == []  # the landing names a render that is no longer shown
        thread.post_message(
            tui_app.ThreadView.Landed(thread.render_id, "2026-09-16T10:13:00+00:00")
        )
        await pilot.pause()
        assert calls == ["2026-09-16T10:13:00+00:00"]  # the current render still lands


async def test_a_reset_clamped_to_a_shorter_end_is_marked_read(monkeypatch) -> None:
    """Codex P2 on #279: a shorter page clamps the old scroll position to the new end
    without a reader move. That reset landed, so the topic must be marked read."""
    for interval in ("ACTIVE_INTERVAL", "COOLING_INTERVAL", "IDLE_INTERVAL"):
        monkeypatch.setattr(state, interval, 60.0)
    app = tui_app.PepperApp(_older_thread_server().client())
    async with app.run_test(size=(100, 14)) as pilot:
        await _open_a1(app, pilot, "body 13")
        thread = app.thread
        scroller = thread.scroller
        await until(pilot, lambda: scroller.max_scroll_y > 0 and thread.pinned)
        calls: list[str | None] = []
        monkeypatch.setattr(app, "_note_viewed", lambda **kw: calls.append(kw.get("through")))
        old = scroller.scroll_y
        thread.show("short page", reset=True, through="2026-09-16T10:13:00+00:00")
        await pilot.pause(0.2)  # the layout clamps the old position to the shorter end
        assert scroller.scroll_y != old  # clamped, not a reader scroll
        assert thread.pinned
        assert calls == ["2026-09-16T10:13:00+00:00"]  # the reset landed at the new end


async def test_o_opens_the_first_link_and_never_a_non_web_url() -> None:
    server = ts.standard_server()
    opened: list[str] = []
    _a1(
        server,
        ts.message("m1", "2026-09-16T17:30:00+00:00", "see this"),
        ts.message(
            "m2",
            "2026-09-16T17:31:00+00:00",
            "and this",
            link_previews=[{"url": "https://www.example.com/a", "title": "A"}],
        ),
        ts.message(
            "m3",
            "2026-09-16T17:32:00+00:00",
            "bad one",
            attachments=[{"url": "javascript:alert(1)", "name": "x"}],
        ),
    )
    app = tui_app.PepperApp(server.client(), open_url=lambda url: bool(opened.append(url) or 1))
    async with app.run_test(size=SIZE) as pilot:
        await _open_a1(app, pilot, "bad one")
        await _select_newest(pilot)  # m3
        await pilot.press("o")
        await pilot.pause()
        assert opened == []
        await pilot.press("up")  # m2
        await pilot.press("o")
        await until(pilot, lambda: opened == ["https://www.example.com/a"])


async def test_an_archived_fold_lists_the_rooms_archived_topics_on_demand() -> None:
    server = ts.standard_server()
    server.rooms[0]["topics"].append(
        ts.topic(
            "t-old", "Old plans", "2026-08-01T00:00:00+00:00", archived_at="2026-08-02T00:00:00Z"
        )
    )
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: f"archived:{ts.ROOM_A}" in app.sidebar._by_key)
        assert "topic:t-old" not in app.sidebar._by_key
        app.sidebar.focus()
        app.sidebar.select_key(f"archived:{ts.ROOM_A}")
        await pilot.pause()
        await pilot.press("enter")
        await until(pilot, lambda: "topic:t-old" in app.sidebar._by_key)
        await pilot.press("enter")  # the row is still under the cursor: folds again
        await until(pilot, lambda: "topic:t-old" not in app.sidebar._by_key)


@pytest.mark.parametrize("first_fails", [False, True])
async def test_a_room_opened_while_another_rail_loads_is_read_next(first_fails: bool) -> None:
    server = ts.standard_server()
    gate = threading.Event()
    rail_a = f"GET /api/v1/groups/{ts.ROOM_A}/topics"
    server.gates[rail_a] = gate
    if first_fails:
        server.fail[rail_a] = 500
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: app._loops["topics"].busy)
        app.open_conversation(state.Conversation(ts.ROOM_B, ts.TOPIC_B1))
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))  # queued once only
        app.open_conversation(state.Conversation(ts.ROOM_B, ts.TOPIC_B1))
        gate.set()
        await until(pilot, lambda: ts.ROOM_B in app.folds.fetched)
        await until(pilot, lambda: not app._loops["topics"].busy)
        assert (ts.ROOM_A in app.folds.fetched) is not first_fails
        rails = [p for m, p, _ in server.requests if m == "GET" and p.endswith("/topics")]
        assert rails == [rail_a.removeprefix("GET "), f"/api/v1/groups/{ts.ROOM_B}/topics"]


async def test_rooms_quiet_for_a_week_fold_behind_show_inactive() -> None:
    server = ts.standard_server()
    late = datetime(2026, 10, 20, tzinfo=UTC)
    app = tui_app.PepperApp(server.client(), now=lambda: late)
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        await until(pilot, lambda: "inactive" in app.sidebar._by_key)
        assert f"room:{ts.ROOM_A}" not in app.sidebar._by_key
        app.sidebar.select_key("inactive")
        await pilot.pause()
        await pilot.press("enter")
        await until(pilot, lambda: f"room:{ts.ROOM_A}" in app.sidebar._by_key)
        assert f"room:{ts.ROOM_B}" in app.sidebar._by_key


async def test_new_topic_row_prompts_creates_and_opens_it() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: f"newtopic:{ts.ROOM_A}" in app.sidebar._by_key)
        app.sidebar.focus()
        app.sidebar.select_key(f"newtopic:{ts.ROOM_A}")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press(*"Packing")
        await pilot.press("enter")
        await until(pilot, lambda: server.created_topics == ["Packing"])
        await until(pilot, lambda: (app.current.topic_id or "").startswith("new-topic"))
        await until(pilot, lambda: "Packing" in plain(app, "#header"))


async def test_ctrl_k_jumps_to_a_topic_even_from_the_composer() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.composer.focus()
        await pilot.press("h", "i")
        await pilot.press("ctrl+k")
        await until(pilot, lambda: isinstance(app.screen, JumpScreen))
        await pilot.press(*"book")
        await pilot.press("enter")
        await until(pilot, lambda: app.current == state.Conversation(ts.ROOM_B, ts.TOPIC_B1))
        assert app._draft().text == "" or app.composer.text == ""
        # Ctrl+K inside the picker does not stack a second one.
        await pilot.press("ctrl+k")
        await until(pilot, lambda: isinstance(app.screen, JumpScreen))
        await pilot.press("ctrl+k")
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, JumpScreen))


async def test_ctrl_k_escape_changes_nothing() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        await pilot.press("ctrl+k")
        await until(pilot, lambda: isinstance(app.screen, JumpScreen))
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, JumpScreen))
        assert app.current == state.PEPPER


async def test_i_shows_the_room_info_card() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.sidebar.select_key(f"room:{ts.ROOM_A}")
        await pilot.pause()
        await pilot.press("i")
        await until(pilot, lambda: isinstance(app.screen, InfoScreen))
        text = " ".join(str(w.render()) for w in app.screen.query("Static"))
        assert "Trip planning" in text
        assert "Members (1)" in text
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, InfoScreen))


@pytest.mark.parametrize("close", ["escape", "q", "enter"])
async def test_a_long_room_info_card_scrolls_and_still_closes(close: str) -> None:
    members = [{"display_name": f"Member {i:02d}"} for i in range(50)]
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.push_screen(InfoScreen(room_info_text({"display_name": "Big", "members": members})))
        await until(pilot, lambda: isinstance(app.screen, InfoScreen))
        await pilot.pause()
        body = app.screen.query_one("#info-body")
        hint = app.screen.query("Static").last()
        assert body.max_scroll_y > 0  # taller than the screen: it scrolls, not clips
        assert hint.region.bottom <= SIZE[1]  # the closing hint is on screen
        await pilot.press("pagedown", "end")
        await until(pilot, lambda: body.scroll_y == body.max_scroll_y)
        await pilot.press(close)
        await until(pilot, lambda: not isinstance(app.screen, InfoScreen))


def test_room_info_text_survives_odd_and_hostile_values() -> None:
    out = room_info_text(
        {
            "display_name": "[red]Room[/red]\x1b[2J",
            "members": ["junk", {"display_name": "Ada", "role": "admin", "is_you": True}],
            "topics": [{"archived": True}, {}, "junk"],
            "apps": {"installed": [{"title": "Notes"}, "junk"]},
        }
    ).plain
    assert "[red]Room[/red]" in out
    assert "\x1b" not in out
    assert "Ada (you) · admin" in out
    assert "1 active, 1 archived" in out
    assert "Notes" in out
    assert "unavailable right now" in room_info_text({"apps": {"unavailable": True}}).plain
    assert "none installed" in room_info_text({}).plain


async def test_typing_the_app_shortcut_letters_in_the_composer_types_them() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await _open_a1(app, pilot, "flight options")
        app.composer.focus()
        await pilot.press("h", "i", "o", "e", "d", "plus")
        await pilot.pause()
        assert app.composer.text == "hioed+"
        assert not isinstance(app.screen, InfoScreen)
