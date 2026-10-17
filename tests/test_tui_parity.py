"""P3 in the running app (Textual Pilot, fake server): reactions, edit and delete, older pages,
folds, new topic, room info, Ctrl+K and ``o``."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

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
    async with app.run_test(size=SIZE) as pilot:
        await _open_a1(app, pilot, "old words")
        await _select_newest(pilot)
        await pilot.press("e")
        await until(pilot, lambda: app.editing is not None)
        app.composer.load_text("new words")
        server.fail[f"POST /api/v1/groups/{ts.ROOM_A}/messages/m1/edit"] = 500
        await pilot.press("enter")
        await until(pilot, lambda: any("HTTP 500" in n.message for n in app._notifications))
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
        await until(pilot, lambda: any("Still saving" in n.message for n in app._notifications))
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
        await pilot.pause()
        assert scroller.scroll_y > 0  # not left at the top of the new, longer thread


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
