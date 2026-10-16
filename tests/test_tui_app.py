"""Headless tests of the terminal UI (Textual Pilot) against a fake server."""

from __future__ import annotations

import asyncio
import io
from typing import TYPE_CHECKING

import pytest
from rich.console import Console
from textual.widgets import Static

from pepper_cli.tui import app as tui_app
from pepper_cli.tui import state
from tests import tui_support as ts

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture(autouse=True)
def _fast_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Poll quickly so a test does not wait out the real 3/10/15 second cadence."""
    monkeypatch.setattr(state, "ACTIVE_INTERVAL", 0.05)
    monkeypatch.setattr(state, "COOLING_INTERVAL", 0.05)
    monkeypatch.setattr(state, "IDLE_INTERVAL", 0.05)
    monkeypatch.setattr(state, "RAIL_INTERVAL", 0.05)
    monkeypatch.setattr(state, "DM_INTERVAL", 0.05)
    monkeypatch.setattr(state, "BACKOFF_STEPS", (0.05, 0.05, 0.05, 0.05))


async def until(pilot, condition: Callable[[], bool], *, seconds: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await pilot.pause(0.02)


def plain(app: tui_app.PepperApp, selector: str) -> str:
    """The text a Static currently shows, with Rich layout applied."""
    console = Console(width=100, file=io.StringIO(), record=True, force_terminal=False)
    console.print(app.query_one(selector, Static).content)
    return console.export_text()


def body_text(app: tui_app.PepperApp) -> str:
    return plain(app, "#thread-body")


async def test_opens_on_the_pepper_dm_and_lists_rooms() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2 and len(app.dm.entries) == 2)
        assert "Pepper" in plain(app, "#header")
        assert "hello there" in body_text(app)
        keys = list(app.sidebar._by_key)
        assert keys[0] == "pepper"
        assert f"room:{ts.ROOM_A}" in keys
        # The server's list ETag came back: later polls send If-None-Match.
        await until(pilot, lambda: any(r[1] == "/api/v1/groups" for r in server.requests[3:]))


async def test_selecting_a_topic_shows_its_messages_and_expands_the_room() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: "flight options" in body_text(app))
        assert "Trip planning › Flights" in plain(app, "#header")
        assert f"topic:{ts.TOPIC_A2}" in app.sidebar._by_key


async def test_a_new_message_in_another_topic_lights_it_and_ctrl_n_opens_it() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        server.messages[ts.TOPIC_B1].append(message_at("m9", "2026-09-16T18:00:00+00:00"))
        server.rooms[1]["topics"][0]["last_activity_at"] = "2026-09-16T18:00:00+00:00"
        server.bump()
        await until(pilot, lambda: app.sidebar.highlighted_row is not None)
        await until(
            pilot,
            lambda: any(
                r.unread for r in (n.data for n in app.sidebar._by_key.values()) if r.kind == "room"
            ),
        )
        await pilot.press("ctrl+n")
        await until(pilot, lambda: app.current == state.Conversation(ts.ROOM_B, ts.TOPIC_B1))
        await until(pilot, lambda: "hello" in body_text(app))
        # Viewing marks it read on the server, through the newest row only.
        await until(pilot, lambda: (ts.TOPIC_B1, "2026-09-16T18:00:00+00:00") in server.puts)


def message_at(mid: str, at: str):
    return ts.message(mid, at, "hello")


async def test_mark_read_publishes_once_per_new_mark_and_retries_after_a_failure() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        server.fail["/api/v1/groups/" + ts.ROOM_A + "/topics/" + ts.TOPIC_A1 + "/read"] = 500
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: "flight options" in body_text(app))
        await until(pilot, lambda: ts.TOPIC_A1 in app._owed)
        server.fail.clear()
        await until(pilot, lambda: not app._owed)
        assert server.puts.count((ts.TOPIC_A1, "2026-09-16T17:30:00+00:00")) == 1


async def test_a_401_shows_the_login_hint_and_a_keypress_exits_1() -> None:
    server = ts.standard_server()
    server.fail["/api/v1/groups"] = 401
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: app.fatal is not None)
        assert "pepper login" in plain(app, "#notice")
        await pilot.press("x")
    assert app.return_code == 1


async def test_rooms_disabled_shows_only_pepper() -> None:
    server = ts.standard_server()
    server.enabled = False
    server.rooms = []
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: "aren't enabled" in plain(app, "#notice"))
        assert list(app.sidebar._by_key) == ["pepper"]


async def test_degraded_keeps_the_last_list_and_says_so() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        server.degraded = True
        server.bump()
        await until(pilot, lambda: "temporarily unavailable" in plain(app, "#notice"))
        assert len(app.rooms) == 2


async def test_a_failing_poll_shows_offline_then_recovers() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        server.fail["/api/v1/chat/history"] = 503
        await until(pilot, lambda: "offline, retrying" in plain(app, "#header"))
        server.fail.clear()
        await until(pilot, lambda: "offline" not in plain(app, "#header"))


async def test_a_dm_reply_while_a_room_is_open_lights_the_pepper_dot_and_the_title() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.dm.entries) == 2)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        server.history.append(
            {
                "id": "h3",
                "role": "assistant",
                "text": "ping",
                "channel": "terminal",
                "created_at": "2026-09-16T09:00:00Z",
            }
        )
        await until(pilot, lambda: app.dm.dot)
        await until(pilot, lambda: app.title.startswith("●"))
        app.open_conversation(state.PEPPER)
        assert not app.dm.dot


async def test_keys_navigate_and_toggle_the_sidebar() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        await pilot.press("ctrl+b")
        assert app.query_one("#left").has_class("hidden")
        await pilot.press("ctrl+b")
        assert not app.query_one("#left").has_class("hidden")
        await pilot.press("ctrl+p")  # nothing unread: only a note
        await pilot.press("pagedown", "pageup")
        await pilot.press("ctrl+q")
    assert app.return_code == 0
