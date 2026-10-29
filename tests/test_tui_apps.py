"""A room's Todos and Notes panes in the running app (Textual Pilot, fake server)."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import httpx
import pytest
from rich.text import Text

from pepper_cli import api_client
from pepper_cli.tui import app as tui_app
from pepper_cli.tui import state
from pepper_cli.tui.apps_pane import AppPane, PaneScroll
from pepper_cli.tui.screens import InfoScreen, PickerScreen
from pepper_cli.tui.sidebar import Sidebar
from tests import tui_support as ts
from tests.tui_support import body_text, notices, plain, until

if TYPE_CHECKING:
    from textual.pilot import Pilot

SIZE = (120, 40)
SAM = {"user_id": "sam-person", "label": "Sam"}
ME_ASKEE = {"user_id": ts.ME, "label": "Me"}  # TodoPerson.user_id is the core user id


@pytest.fixture(autouse=True)
def _fast_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state, "ACTIVE_INTERVAL", 0.05)
    monkeypatch.setattr(state, "COOLING_INTERVAL", 0.05)
    monkeypatch.setattr(state, "IDLE_INTERVAL", 0.05)
    monkeypatch.setattr(state, "RAIL_INTERVAL", 0.05)
    monkeypatch.setattr(state, "DM_INTERVAL", 0.05)
    monkeypatch.setattr(state, "BACKOFF_STEPS", (0.05, 0.05, 0.05, 0.05))
    monkeypatch.setattr(tui_app, "utcnow", lambda: ts.NOW)


def server_with_apps() -> ts.FakeServer:
    server = ts.standard_server()
    server.launchers[ts.ROOM_A] = [
        ts.tile("todo", "Todos", ts.ROOM_A),
        ts.tile("notes", "Notes", ts.ROOM_A),
        ts.tile("meals", "Meals", ts.ROOM_A),
    ]
    server.todos[ts.ROOM_A] = [
        ts.todo(
            "t-cooler",
            "Pack the cooler",
            due_date="2026-11-01T00:00:00+00:00",
            due_is_date_only=True,
        ),
        ts.todo(
            "t-sitter",
            "Book the dog sitter",
            due_date="2026-10-30T00:00:00+00:00",
            due_is_date_only=True,
            assignee=ME_ASKEE,
            requested_by=SAM,
        ),
        ts.todo(
            "t-books",
            "Return the library books",
            source="notes",
            source_ref={"url": "https://example.com/errands"},
        ),
        ts.todo("t-done", "Old chore", status="done", done_at="2026-10-01T00:00:00+00:00"),
        ts.todo(
            "t-dismissed",
            "Skipped chore",
            status="dismissed",
            created_at="2026-09-20T00:00:00+00:00",
        ),
    ]
    server.notes[ts.ROOM_A] = [
        ts.note_item(
            "n-wifi",
            "Cabin wifi",
            "2026-10-21T10:00:00+00:00",
            summary='Network "lakeside", password on the fridge.',
        ),
        ts.note_item(
            "n-pack",
            "Packing list",
            "2026-10-27T10:00:00+00:00",
            summary="Bring layers, it gets cold at night.",
        ),
    ]
    server.note_details["n-pack"] = {
        "id": "n-pack",
        "name": "Packing list",
        "kind": "inline_text",
        "updated_at": "2026-10-27T10:00:00+00:00",
        "etag": '"n-pack-1"',
        "shared": True,
        "body": (
            "Bring layers, see https://example.com/gear\n\n- [ ] Sleeping bags\n- [x] Headlamps\n"
        ),
        "labels": [],
        "summary": None,
    }
    return server


async def settled_scroll(pilot: Pilot, scroller: PaneScroll, *, past: float) -> float:
    """Wait for a page scroll past ``past`` to come to rest (it may animate on some terminals)."""
    seen: list[float] = []

    def still() -> bool:
        seen.append(scroller.scroll_y)
        return len(seen) > 2 and seen[-1] == seen[-2] == seen[-3] and seen[-1] > past

    await until(pilot, still)
    return scroller.scroll_y


async def open_a1(app: tui_app.PepperApp, pilot: Pilot) -> None:
    await until(pilot, lambda: len(app.rooms) == 2)
    app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
    await until(pilot, lambda: "flight options" in body_text(app))
    app.thread.focus_scroller()
    await pilot.pause()


def pane_text(app: tui_app.PepperApp) -> str:
    return plain(app, "#pane-body")


async def pick(pilot: Pilot, app: tui_app.PepperApp, number: str) -> None:
    await pilot.press("a")
    await until(pilot, lambda: isinstance(app.screen, PickerScreen))
    await pilot.press(number)


async def open_todos(app: tui_app.PepperApp, pilot: Pilot) -> None:
    await open_a1(app, pilot)
    await pick(pilot, app, "1")
    await until(pilot, lambda: "Pack the cooler" in pane_text(app))


async def open_notes(app: tui_app.PepperApp, pilot: Pilot) -> None:
    await open_a1(app, pilot)
    await pick(pilot, app, "2")
    await until(pilot, lambda: "Packing list" in pane_text(app))


async def type_command(app: tui_app.PepperApp, pilot: Pilot, text: str) -> None:
    app.composer.load_text(text)
    app.composer.focus()
    await pilot.press("enter")


def marked_open(app: tui_app.PepperApp) -> list[str]:
    """The keys of the sidebar rows drawn as the open one (reverse video)."""
    marked: list[str] = []
    stack = list(app.sidebar.root.children)
    while stack:
        node = stack.pop(0)
        stack[:0] = node.children
        label = node.label
        drawn = isinstance(label, Text) and any("reverse" in str(s.style) for s in label.spans)
        if node.data is not None and drawn:
            marked.append(node.data.key)
    return marked


def header(app: tui_app.PepperApp) -> str:
    return plain(app, "#header").strip()


async def test_a_picks_todos_and_opens_the_rooms_pane_in_the_threads_place() -> None:
    server = server_with_apps()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_a1(app, pilot)
        await pilot.press("a")
        await until(pilot, lambda: isinstance(app.screen, PickerScreen))
        await pilot.press("1")
        await until(pilot, lambda: "Pack the cooler" in pane_text(app))
        assert header(app) == "Trip planning › Todos"
        assert app.query_one(AppPane).display
        assert not app.thread.display
        assert not app.query_one("#compose-box").display
        assert isinstance(app.focused, PaneScroll)
        text = pane_text(app)
        assert "Todos  3 open" in text
        # Due-first: Oct 30 before Nov 1, undated last.
        assert text.index("Book the dog sitter") < text.index("Pack the cooler")
        assert text.index("Pack the cooler") < text.index("Return the library books")
        assert "due Oct 30" in text
        assert "asked of you by Sam" in text
        assert "from notes" in text
        assert "▸ Closed (2)" in text
        assert "Old chore" not in text
        # The room's app row is nested first under the room, with the marker.
        labels = [str(n.label) for n in app.sidebar.root.children[4].children]
        assert labels[0] == "▌ Todos"
        # Every read named the room, once, with every status in one call.
        reads = [q for _m, p, q in server.requests if p.endswith(f"/groups/{ts.ROOM_A}/todos")]
        assert reads[0]["status"] == "all"


async def test_the_launcher_lists_only_installed_apps_and_others_open_in_the_browser() -> None:
    server = server_with_apps()
    opened: list[str] = []
    app = tui_app.PepperApp(server.client(), open_url=lambda url: opened.append(url) or True)
    async with app.run_test(size=SIZE) as pilot:
        await open_a1(app, pilot)
        await pick(pilot, app, "3")
        await until(pilot, lambda: bool(opened))
        assert opened == [f"http://example.com/groups/{ts.ROOM_A}?open=meals"]
        assert app.pane is None


async def test_a_room_with_no_apps_offers_none_and_slash_todo_says_it_is_not_installed() -> None:
    server = server_with_apps()
    app = tui_app.PepperApp(server.client())
    seen = notices(app)
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.open_conversation(state.Conversation(ts.ROOM_B, ts.TOPIC_B1))
        await until(pilot, lambda: "next book" in body_text(app))
        app.thread.focus_scroller()
        await pilot.press("a")
        await until(pilot, lambda: "No apps installed in this room." in seen)
        assert not isinstance(app.screen, PickerScreen)
        app.composer.load_text("/todo")
        app.composer.focus()
        await pilot.press("enter")
        await until(
            pilot,
            lambda: (
                "Todos isn't installed in this room. Install it from Room info on the web." in seen
            ),
        )
        assert app.pane is None
        assert app.composer.text == ""
        assert server.posts == []  # the command was not posted to the room


async def test_unavailable_registry_is_not_taken_for_no_apps_and_is_asked_again() -> None:
    server = server_with_apps()
    server.apps_unavailable.add(ts.ROOM_A)
    app = tui_app.PepperApp(server.client())
    seen = notices(app)
    async with app.run_test(size=SIZE) as pilot:
        await open_a1(app, pilot)
        server.launchers[ts.ROOM_A], held = [], server.launchers[ts.ROOM_A]
        await pilot.press("a")
        await until(pilot, lambda: "Apps unavailable right now." in seen)
        server.apps_unavailable.clear()
        server.launchers[ts.ROOM_A] = held
        await pilot.press("a")  # not cached: read again
        await until(pilot, lambda: isinstance(app.screen, PickerScreen))
        launches = [p for _m, p, _q in server.requests if p.endswith("/apps/launcher")]
        assert len(launches) == 2


async def test_an_app_installed_on_the_web_is_offered_without_reopening_room_info() -> None:
    server = server_with_apps()
    server.launchers[ts.ROOM_A] = [ts.tile("notes", "Notes", ts.ROOM_A)]
    app = tui_app.PepperApp(server.client())
    seen = notices(app)
    async with app.run_test(size=SIZE) as pilot:
        await open_a1(app, pilot)
        await type_command(app, pilot, "/todo")
        await until(pilot, lambda: any("Todos isn't installed" in n for n in seen))
        server.launchers[ts.ROOM_A].insert(0, ts.tile("todo", "Todos", ts.ROOM_A))
        frame = api_client.FeedFrame(
            kind="app.installed",
            id=7,
            payload={"id": 7, "kind": "app.installed", "group_id": ts.ROOM_A, "payload": {}},
        )
        app._on_stream_frame(frame)
        await type_command(app, pilot, "/todo")
        await until(pilot, lambda: "Pack the cooler" in pane_text(app))


async def test_a_launcher_read_in_flight_when_an_app_is_installed_is_not_held() -> None:
    server = server_with_apps()
    server.launchers[ts.ROOM_A] = [ts.tile("notes", "Notes", ts.ROOM_A)]
    path = f"/api/v1/groups/{ts.ROOM_A}/apps/launcher"
    server.gates[path] = gate = threading.Event()
    app = tui_app.PepperApp(server.client())
    seen = notices(app)
    async with app.run_test(size=SIZE) as pilot:
        await open_a1(app, pilot)
        await type_command(app, pilot, "/todo")
        await until(pilot, lambda: any(p == path for _m, p, _q in server.requests))
        frame = api_client.FeedFrame(
            kind="app.installed",
            id=7,
            payload={"id": 7, "kind": "app.installed", "group_id": ts.ROOM_A, "payload": {}},
        )
        app._on_stream_frame(frame)  # lands while the pre-install read is still out
        gate.set()
        await pilot.pause(0.3)
        assert not any("isn't installed" in n for n in seen)
        assert app.composer.text == "/todo"  # dropped, so the command waits to be sent again
        del server.gates[path]
        server.launchers[ts.ROOM_A].insert(0, ts.tile("todo", "Todos", ts.ROOM_A))
        await type_command(app, pilot, "/todo")
        await until(pilot, lambda: "Pack the cooler" in pane_text(app))


async def test_slash_todo_and_slash_notes_open_the_rooms_panes_and_keep_the_draft() -> None:
    server = server_with_apps()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_a1(app, pilot)
        app.composer.load_text("/notes")
        app.composer.focus()
        await pilot.press("enter")
        await until(pilot, lambda: "Packing list" in pane_text(app))
        assert header(app) == "Trip planning › Notes"
        assert server.posts == []
        # The composer is hidden under a room's pane; Esc brings the thread and composer back.
        assert not app.query_one("#compose-box").display
        await pilot.press("escape")
        await until(pilot, lambda: app.pane is None)
        assert app.query_one("#compose-box").display
        assert app.thread.display
        assert header(app) == "Trip planning › Flights"
        # A draft typed before survives a pane being opened over it.
        app.composer.load_text("half a thought")
        app.thread.focus_scroller()
        await pick(pilot, app, "1")
        await until(pilot, lambda: "Pack the cooler" in pane_text(app))
        await pilot.press("escape")
        await until(pilot, lambda: app.pane is None)
        assert app.composer.text == "half a thought"
        app.composer.load_text("/todo")
        app.composer.focus()
        await pilot.press("enter")
        await until(pilot, lambda: app.pane is not None)
        assert header(app) == "Trip planning › Todos"


async def test_a_launcher_read_that_lands_after_the_user_moved_on_is_dropped() -> None:
    server = server_with_apps()
    path = f"/api/v1/groups/{ts.ROOM_A}/apps/launcher"
    server.gates[path] = gate = threading.Event()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_a1(app, pilot)
        await type_command(app, pilot, "/todo")
        await until(pilot, lambda: any(p == path for _m, p, _q in server.requests))
        app.open_conversation(state.Conversation(ts.ROOM_B, ts.TOPIC_B1))
        gate.set()
        await pilot.pause(0.3)
        assert app.pane is None
        assert app.current == state.Conversation(ts.ROOM_B, ts.TOPIC_B1)


async def test_a_failed_launcher_read_leaves_the_command_in_the_composer() -> None:
    server = server_with_apps()
    server.fail[f"/api/v1/groups/{ts.ROOM_A}/apps"] = 500
    app = tui_app.PepperApp(server.client())
    seen = notices(app)
    async with app.run_test(size=SIZE) as pilot:
        await open_a1(app, pilot)
        await type_command(app, pilot, "/notes")
        await until(pilot, lambda: any("Couldn't load the room's apps" in n for n in seen))
        assert app.composer.text == "/notes"
        assert server.posts == []
        server.fail.clear()
        await pilot.press("enter")
        await until(pilot, lambda: "Packing list" in pane_text(app))


async def test_closed_fold_details_and_the_source_link() -> None:
    server = server_with_apps()
    opened: list[str] = []
    app = tui_app.PepperApp(server.client(), open_url=lambda url: opened.append(url) or True)
    seen = notices(app)
    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        await pilot.press("down", "down", "down")  # the first row starts selected: sitter...fold
        assert app.pane is not None
        assert app.pane.selected == "fold"
        await pilot.press("enter")
        await until(pilot, lambda: "Old chore" in pane_text(app))
        text = pane_text(app)
        assert "▾ Closed (2)" in text
        assert "✓ Old chore" in text
        assert "✗ Skipped chore" in text
        await pilot.press("enter")
        await until(pilot, lambda: "Old chore" not in pane_text(app))
        # Enter on a todo shows its details; o opens the source link of the selected one.
        await pilot.press("up")
        assert app.pane.selected == "todo:t-books"
        await pilot.press("o")
        await until(pilot, lambda: bool(opened))
        assert opened == ["https://example.com/errands"]
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, InfoScreen))
        await pilot.press("escape")
        await pilot.press("up")
        assert app.pane.selected == "todo:t-cooler"
        await pilot.press("o")
        await until(pilot, lambda: "No link here." in seen)


async def test_m_reads_again_with_only_the_asks_made_of_you() -> None:
    server = server_with_apps()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        await pilot.press("m")
        await until(pilot, lambda: "Pack the cooler" not in pane_text(app))
        text = pane_text(app)
        assert "Book the dog sitter" in text
        assert "1 open  ·  only asks of you" in text
        asks = [q for _m, p, q in server.requests if p.endswith("/todos") and "assignee" in q]
        assert asks and asks[-1]["assignee"] == "me"
        await pilot.press("m")
        await until(pilot, lambda: "Pack the cooler" in pane_text(app))


async def test_notes_list_newest_edit_first_with_summaries_and_the_showing_row() -> None:
    server = server_with_apps()
    server.notes_truncated = True
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        text = pane_text(app)
        assert header(app) == "Trip planning › Notes"
        assert "Notes  2" in text
        assert text.index("Packing list") < text.index("Cabin wifi")  # re-sorted by edit time
        assert "Bring layers, it gets cold at night." in text
        assert 'Network "lakeside"' in text
        assert "Showing 200 notes. `pepper notes list` searches the rest." in text
        kinds = [q for _m, p, q in server.requests if p == "/api/v1/artifacts"]
        assert kinds[0] == {"kind": "inline_text", "limit": "200", "principal": ts.ROOM_A}
        await pilot.press("m")  # a Todos key: nothing happens in Notes
        await pilot.pause()
        assert app.pane is not None
        assert not app.pane.mine


async def test_a_note_opens_rendered_and_esc_walks_note_then_list_then_topic() -> None:
    server = server_with_apps()
    opened: list[str] = []
    app = tui_app.PepperApp(server.client(), open_url=lambda url: opened.append(url) or True)
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        await pilot.press("enter")  # the newest edit is selected from the start
        await until(pilot, lambda: "Sleeping bags" in pane_text(app))
        text = pane_text(app)
        assert header(app) == "Trip planning › Notes › Packing list"
        assert "☐ Sleeping bags" in text
        assert "☑ Headlamps" in text
        assert "Packing list" in text
        # The viewer read the note with a detail call (never a principal: that is the room's).
        await pilot.press("o")
        await until(pilot, lambda: bool(opened))
        assert opened == ["https://example.com/gear"]
        await pilot.press("escape")
        await until(pilot, lambda: header(app) == "Trip planning › Notes")
        assert "Cabin wifi" in pane_text(app)
        assert app.pane is not None
        await pilot.press("enter")
        await until(pilot, lambda: "Sleeping bags" in pane_text(app))
        # Enter on the room's app row goes back to the list from an open note.
        app.sidebar.focus()
        app.sidebar.select_key(app.pane.key)
        await pilot.pause()
        await pilot.press("enter")
        await until(pilot, lambda: header(app) == "Trip planning › Notes")
        assert isinstance(app.focused, PaneScroll)
        await pilot.press("escape")
        await until(pilot, lambda: app.pane is None)
        assert header(app) == "Trip planning › Flights"
        assert isinstance(app.focused, type(app.thread.scroller))


async def test_the_room_app_row_drops_the_notes_name_from_the_header_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nothing else may redraw the chrome for it: the polls sit still and the list read is held.
    for name in (
        "ACTIVE_INTERVAL",
        "COOLING_INTERVAL",
        "IDLE_INTERVAL",
        "RAIL_INTERVAL",
        "DM_INTERVAL",
    ):
        monkeypatch.setattr(state, name, 600.0)
    server = server_with_apps()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: header(app) == "Trip planning › Notes › Packing list")
        server.gates["/api/v1/artifacts"] = gate = threading.Event()
        app.sidebar.focus()
        app.sidebar.select_key(f"app:{ts.ROOM_A}:notes")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.2)
        try:
            assert header(app) == "Trip planning › Notes"
        finally:
            gate.set()


async def test_server_text_is_never_parsed_as_markup_or_escape_sequences() -> None:
    server = server_with_apps()
    server.todos[ts.ROOM_A] = [ts.todo("x", "[bold red]boom[/] \x1b[31mred", details="d\x1b[2J")]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_todos_like(app, pilot, "boom")
        text = pane_text(app)
        assert "[bold red]boom[/]" in text
        assert "\x1b" not in text


async def open_todos_like(app: tui_app.PepperApp, pilot: Pilot, needle: str) -> None:
    await open_a1(app, pilot)
    await pick(pilot, app, "1")
    await until(pilot, lambda: needle in pane_text(app))


async def test_a_failed_reread_keeps_the_last_list_and_says_so_then_r_recovers() -> None:
    server = server_with_apps()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        server.fail[f"/api/v1/groups/{ts.ROOM_A}/todos"] = 500
        await pilot.press("r")
        await until(pilot, lambda: "Couldn't load Todos" in pane_text(app))
        assert "Pack the cooler" in pane_text(app)
        assert "showing the last read" in pane_text(app)
        assert app.pane is not None  # an error never closes the pane
        server.fail.clear()
        await pilot.press("r")
        await until(pilot, lambda: "Couldn't load" not in pane_text(app))


async def test_a_room_the_caller_left_closes_its_pane_with_a_notice() -> None:
    server = server_with_apps()
    app = tui_app.PepperApp(server.client())
    seen = notices(app)
    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        server.fail[f"/api/v1/groups/{ts.ROOM_A}/todos"] = 404
        await pilot.press("r")
        await until(pilot, lambda: app.pane is None)
        assert "You can't open this room's Todos any more." in seen
        assert app.thread.display


async def test_the_open_pane_is_polled_and_a_closed_one_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(state, "PANE_INTERVAL", 0.05)
    server = server_with_apps()
    app = tui_app.PepperApp(server.client())

    def reads() -> int:
        return len([1 for _m, p, _q in server.requests if p.endswith(f"/{ts.ROOM_A}/todos")])

    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        server.todos[ts.ROOM_A].append(ts.todo("t-new", "Appears by itself"))
        await until(pilot, lambda: "Appears by itself" in pane_text(app))
        await pilot.press("escape")
        await until(pilot, lambda: app.pane is None)
        count = reads()
        await pilot.pause(0.4)
        assert reads() == count


async def test_a_reread_keeps_the_users_scroll_position_past_the_selection() -> None:
    server = server_with_apps()
    server.todos[ts.ROOM_A] += [ts.todo(f"t-{n}", f"Chore number {n}") for n in range(80)]
    app = tui_app.PepperApp(server.client())

    def reads() -> int:
        return len([1 for _m, p, _q in server.requests if p.endswith(f"/{ts.ROOM_A}/todos")])

    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        scroller = app.query_one(PaneScroll)
        scroller.focus()
        await pilot.press("pagedown")
        paged = await settled_scroll(pilot, scroller, past=10)  # the selection is above the view
        count = reads()
        await pilot.press("r")  # the same re-read the 30s poll makes
        await until(pilot, lambda: reads() > count)
        await pilot.pause(0.2)
        assert scroller.scroll_y == paged


async def test_tab_cycles_sidebar_and_pane_only_and_a_topic_row_closes_the_pane() -> None:
    server = server_with_apps()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        app.sidebar.focus()
        await pilot.pause()
        await pilot.press("tab")
        assert isinstance(app.focused, PaneScroll)
        await pilot.press("tab")
        assert isinstance(app.focused, Sidebar)  # no composer stop under a room's pane
        await pilot.press("shift+tab")
        assert isinstance(app.focused, PaneScroll)
        # Opening a topic (here the other room's) closes the pane.
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A2))
        await pilot.pause()
        assert app.pane is None
        assert app.thread.display
        assert app.query_one("#compose-box").display


async def test_opening_another_rooms_app_moves_to_that_room_and_the_old_pane_goes() -> None:
    server = server_with_apps()
    server.launchers[ts.ROOM_B] = [ts.tile("notes", "Notes", ts.ROOM_B)]
    server.notes[ts.ROOM_B] = [ts.note_item("b1", "Reading list", "2026-10-20T10:00:00+00:00")]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        app.sidebar.focus()
        app.sidebar.select_key(f"room:{ts.ROOM_B}")
        await pilot.pause()
        await pilot.press("a")
        await until(pilot, lambda: isinstance(app.screen, PickerScreen))
        await pilot.press("1")
        await until(pilot, lambda: "Reading list" in pane_text(app))
        assert header(app) == "Book club › Notes"
        assert app.current.room_id == ts.ROOM_B


async def test_a_room_with_only_archived_topics_opens_its_pane_in_that_room_and_esc_stays() -> None:
    server = server_with_apps()
    server.rooms[1]["topics"] = [
        ts.topic(
            ts.TOPIC_B1, "Old", "2026-09-15T10:00:00+00:00", archived_at="2026-10-01T00:00:00Z"
        )
    ]
    server.launchers[ts.ROOM_B] = [ts.tile("notes", "Notes", ts.ROOM_B)]
    server.notes[ts.ROOM_B] = [ts.note_item("b1", "Reading list", "2026-10-20T10:00:00+00:00")]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        app.sidebar.focus()
        app.sidebar.select_key(f"room:{ts.ROOM_B}")
        await pilot.pause()
        await pilot.press("a")
        await until(pilot, lambda: isinstance(app.screen, PickerScreen))
        await pilot.press("1")
        await until(pilot, lambda: "Reading list" in pane_text(app))
        assert header(app) == "Book club › Notes"
        assert app.current == state.Conversation(ts.ROOM_B, None)  # not left in room A
        app.query_one(PaneScroll).focus()
        await pilot.press("escape")
        await until(pilot, lambda: app.pane is None)
        assert app.current.room_id == ts.ROOM_B
        assert app.current.topic_id is None
        assert header(app) == "Book club"
        assert marked_open(app) == [f"room:{ts.ROOM_B}"]  # the room's own row says it is open
        app.composer.focus()
        app.composer.load_text("hello")
        await pilot.press("enter")
        await pilot.pause()
        assert not app.pending.in_conversation(app.current)  # nothing posted to a topic
        assert app.composer.text == "hello"  # and the draft is kept, not dropped


async def test_rows_that_arrive_behind_an_open_pane_are_not_marked_read_until_it_closes() -> None:
    server = server_with_apps()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        puts = len(server.puts)
        server.messages[ts.TOPIC_A1].append(
            ts.message("m-new", "2026-09-16T18:30:00+00:00", "arrived while hidden")
        )
        await until(pilot, lambda: "m-new" in app.store.rows)
        await pilot.pause(0.3)
        assert len(server.puts) == puts  # nobody saw it
        await pilot.press("escape")
        await until(pilot, lambda: len(server.puts) > puts)
        assert server.puts[-1] == (ts.TOPIC_A1, "2026-09-16T18:30:00+00:00")


async def test_moving_from_one_rooms_pane_to_anothers_does_not_mark_the_hidden_thread_read() -> (
    None
):
    server = server_with_apps()
    server.launchers[ts.ROOM_B] = [ts.tile("notes", "Notes", ts.ROOM_B)]
    server.notes[ts.ROOM_B] = [ts.note_item("b1", "Reading list", "2026-10-20T10:00:00+00:00")]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        server.messages[ts.TOPIC_A1].append(
            ts.message("m-new", "2026-09-16T18:30:00+00:00", "arrived while hidden")
        )
        await until(pilot, lambda: "m-new" in app.store.rows)
        app.sidebar.focus()
        app.sidebar.select_key(f"room:{ts.ROOM_B}")
        await pilot.pause()
        await pick(pilot, app, "1")
        await until(pilot, lambda: "Reading list" in pane_text(app))
        await pilot.pause(0.3)
        assert (ts.TOPIC_A1, "2026-09-16T18:30:00+00:00") not in server.puts


async def test_an_error_opening_a_note_shows_in_the_viewer() -> None:
    server = server_with_apps()
    del server.note_details["n-pack"]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: "Couldn't open the note" in pane_text(app))
        await pilot.press("escape")
        await until(pilot, lambda: "Cabin wifi" in pane_text(app))
        assert app.pane is not None


async def test_a_notes_list_reread_does_not_clear_the_open_notes_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(state, "PANE_INTERVAL", 0.05)
    server = server_with_apps()
    del server.note_details["n-pack"]
    app = tui_app.PepperApp(server.client())

    def list_reads() -> int:
        return len([1 for _m, p, _q in server.requests if p == "/api/v1/artifacts"])

    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: "Couldn't open the note" in pane_text(app))
        count = list_reads()
        await until(pilot, lambda: list_reads() >= count + 2)  # the list behind it, polled
        await pilot.pause()
        assert "Couldn't open the note" in pane_text(app)
        assert "Loading" not in pane_text(app)


async def test_an_open_note_is_read_again_when_the_polled_list_says_it_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(state, "PANE_INTERVAL", 0.05)
    server = server_with_apps()
    app = tui_app.PepperApp(server.client())

    def detail_reads() -> int:
        return len([1 for _m, p, _q in server.requests if p.endswith("/n-pack/detail")])

    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: "Sleeping bags" in pane_text(app))
        await pilot.pause(0.3)
        assert detail_reads() == 1  # an unchanged note is not read again on the poll
        edited = "2026-10-27T12:00:00+00:00"
        server.note_details["n-pack"].update(
            body="- [ ] Camp stove\n", updated_at=edited, etag='"n-pack-2"'
        )
        server.notes[ts.ROOM_A][1].update(updated_at=edited, etag='"n-pack-2"')
        await until(pilot, lambda: "Camp stove" in pane_text(app))


async def test_a_failing_notes_list_shows_over_the_open_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(state, "PANE_INTERVAL", 0.05)
    server = server_with_apps()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: "Sleeping bags" in pane_text(app))
        server.fail["/api/v1/artifacts"] = 500  # the poll reads only the list: it fails
        await until(pilot, lambda: "Couldn't load Notes" in pane_text(app))
        assert "Sleeping bags" in pane_text(app)


async def test_leaving_a_scrolled_note_shows_the_list_at_its_selection() -> None:
    server = server_with_apps()
    long_body = "".join(f"line {n}\n\n" for n in range(120))
    server.note_details["n-pack"]["body"] = long_body
    server.notes[ts.ROOM_A] += [
        ts.note_item(f"n-{n}", f"Old note {n}", "2026-01-01T10:00:00+00:00") for n in range(60)
    ]  # a list long enough to keep a stale scroll offset
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        scroller = app.query_one(PaneScroll)
        await pilot.press("enter")
        await until(pilot, lambda: "line 0" in pane_text(app))
        await pilot.press("pagedown", "pagedown")
        await settled_scroll(pilot, scroller, past=20)
        await pilot.press("escape")
        await until(pilot, lambda: "Cabin wifi" in pane_text(app))
        await pilot.pause()
        assert scroller.scroll_y <= 2  # the selected row, under the heading, is in view again


async def test_a_detail_that_is_not_a_note_is_refused() -> None:
    server = server_with_apps()
    server.note_details["n-pack"]["kind"] = "file"
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: "Couldn't open the note" in pane_text(app))


async def test_a_transport_failure_on_the_shelf_is_reported() -> None:
    server = server_with_apps()
    server.raise_on["/api/v1/groups/" + ts.ROOM_A + "/apps"] = httpx.ConnectError("down")
    app = tui_app.PepperApp(server.client())
    seen = notices(app)
    async with app.run_test(size=SIZE) as pilot:
        await open_a1(app, pilot)
        await pilot.press("a")
        await until(pilot, lambda: any("Couldn't load the room's apps" in n for n in seen))


async def test_an_ask_means_you_by_your_core_user_id_never_your_person_id() -> None:
    # The server's TodoPerson.user_id is users.id (features/todos/asks.py Party), so a todo asked
    # of an id equal to your groups person id is someone else's, not yours.
    server = server_with_apps()
    server.todos[ts.ROOM_A].append(
        ts.todo("t-alex", "Water the plants", assignee={"user_id": ts.ME_PERSON, "label": "Alex"})
    )
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        assert app.you_ids == {ts.ME}
        text = pane_text(app)
        assert "asked of you by Sam" in text
        assert "asked of Alex" in text
