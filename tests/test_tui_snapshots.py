"""Rendered-screen snapshots of the terminal UI (pytest-textual-snapshot).

Each test builds the app on the injected fake client with a frozen clock, drives it to a
settled screen and compares the SVG. Regenerate with ``pytest --snapshot-update`` after an
intentional visual change; never edit a snapshot by hand.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pepper_cli.tui import app as tui_app
from pepper_cli.tui import state
from pepper_cli.tui.screens import InfoScreen
from tests import tui_support as ts
from tests.tui_support import body_text, until

if TYPE_CHECKING:
    from textual.pilot import Pilot

SIZE = (110, 34)

CARD = {
    "kind": "card",
    "app_key": "notes",
    "slug": "n",
    "title": "Trip notes",
    "principal": "p",
    "url": "/apps/n",
    "summary": {"lines": ["3 open items", "next: book flights"]},
}


@pytest.fixture(autouse=True)
def _frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pollers parked far out so no tick lands between the settled screen and the capture.
    for name in ("ACTIVE_INTERVAL", "COOLING_INTERVAL", "IDLE_INTERVAL", "RAIL_INTERVAL"):
        monkeypatch.setattr(state, name, 3600.0)
    monkeypatch.setattr(state, "DM_INTERVAL", 3600.0)
    monkeypatch.setattr(tui_app, "utcnow", lambda: ts.NOW)


def _server() -> ts.FakeServer:
    server = ts.standard_server()
    server.messages[ts.TOPIC_A1] = [
        ts.message(
            "m1",
            "2026-09-16T17:30:00+00:00",
            "Here is the **plan**:\n\n- book flights\n- pick a hotel",
            reactions=[{"emoji": "👍", "count": 2, "reacted_by_me": True}],
            link_previews=[{"url": "https://www.example.com/a", "title": "Flight deals"}],
        ),
        ts.message("m2", "2026-09-16T17:31:00+00:00", "notes are here", cards=[CARD]),
    ]
    return server


async def _cursor_settled(pilot: Pilot, app: tui_app.PepperApp, key: str) -> None:
    # The sidebar restores its cursor after a refresh; wait for it so the capture never
    # races the restore (the cursor row is part of the rendered screen).
    def settled() -> bool:
        row = app.sidebar.highlighted_row
        return row is not None and row.key == key

    await until(pilot, settled)


def test_thread_with_markdown_reactions_preview_and_card(snap_compare) -> None:
    app = tui_app.PepperApp(_server().client())

    async def run_before(pilot: Pilot) -> None:
        await until(pilot, lambda: len(app.rooms) == 2)
        await _cursor_settled(pilot, app, "pepper")
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: "notes are here" in body_text(app))
        await _cursor_settled(pilot, app, "pepper")
        await pilot.pause()

    assert snap_compare(app, terminal_size=SIZE, run_before=run_before)


def test_room_info_card(snap_compare) -> None:
    app = tui_app.PepperApp(_server().client())

    async def run_before(pilot: Pilot) -> None:
        await until(pilot, lambda: len(app.rooms) == 2)
        await _cursor_settled(pilot, app, "pepper")
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: "notes are here" in body_text(app))
        await _cursor_settled(pilot, app, "pepper")
        await pilot.press("i")
        await until(pilot, lambda: isinstance(app.screen, InfoScreen))
        await pilot.pause()

    assert snap_compare(app, terminal_size=SIZE, run_before=run_before)


def test_your_own_todos_with_a_room_section_and_the_reply_strip(snap_compare) -> None:
    from tests.test_tui_own_apps import own_server, reply_turn

    server = own_server()
    server.turns = [reply_turn('Added "Buy stamps" to your todos.')]
    app = tui_app.PepperApp(server.client())

    async def run_before(pilot: Pilot) -> None:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.sidebar.select_key("app:me:todo")
        await pilot.press("enter")
        await until(pilot, lambda: app.pane is not None and app.pane.loaded)
        app.composer.focus()
        app.composer.insert("add buy stamps")
        await pilot.press("enter")
        await until(pilot, lambda: "Added" in ts.plain(app, "#reply-body") and not app.turns)
        await pilot.press("escape")
        await pilot.pause()

    assert snap_compare(app, terminal_size=SIZE, run_before=run_before)


def test_your_own_notes_with_one_note_open(snap_compare) -> None:
    from tests.test_tui_own_apps import own_server

    app = tui_app.PepperApp(own_server().client())

    async def run_before(pilot: Pilot) -> None:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.sidebar.select_key("app:me:notes")
        await pilot.press("enter")
        await until(pilot, lambda: app.pane is not None and app.pane.loaded)
        await pilot.press("enter")
        await until(pilot, lambda: "Take two photos" in ts.plain(app, "#pane-body"))
        await pilot.pause()

    assert snap_compare(app, terminal_size=SIZE, run_before=run_before)
