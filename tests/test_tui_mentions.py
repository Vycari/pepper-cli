"""``@`` on a sidebar row whose unread activity mentions you: the server's ``has_unread_mention``
(planning/terminal-ui.md §15 C, read-state-sync §6)."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from pepper_cli.tui import app as tui_app
from pepper_cli.tui import state
from pepper_cli.tui.sidebar import row_label
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
    monkeypatch.setattr(state, "BACKOFF_STEPS", (0.05, 0.05, 0.05, 0.05))
    monkeypatch.setattr(tui_app, "utcnow", lambda: ts.NOW)


def _activity(server: ts.FakeServer, topic_id: str, room_index: int, **over) -> None:
    server.messages[topic_id].append(ts.message("m-new", NEW, "ping", **over))
    server.rooms[room_index]["topics"][0]["last_activity_at"] = NEW
    server.bump()


def _fetches(server: ts.FakeServer, topic_id: str) -> int:
    return sum(
        1
        for m, p, _ in server.requests
        if m == "GET" and p.endswith(f"/topics/{topic_id}/messages")
    )


def _label(app: tui_app.PepperApp, key: str) -> str:
    node = app.sidebar._by_key[key]
    return row_label(node.data, current=False).plain


async def test_a_mention_in_an_unread_topic_shows_an_at_instead_of_the_dot() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        _activity(server, ts.TOPIC_B1, 1, mentions=[ts.ME_PERSON])
        room_key = f"room:{ts.ROOM_B}"
        await until(pilot, lambda: app.sidebar._by_key[room_key].data.mention)
        assert _label(app, room_key).endswith(" @")
        assert "●" not in _label(app, room_key)
        assert app.title == "@ Pepper"
        # Opening it reads it: the marker and the title go.
        app.open_conversation(state.Conversation(ts.ROOM_B, ts.TOPIC_B1))
        await until(pilot, lambda: not app.sidebar._by_key[room_key].data.mention)
        await until(pilot, lambda: app.title == "Pepper")


async def test_unread_without_a_mention_keeps_the_dot_and_reads_no_messages() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        _activity(server, ts.TOPIC_B1, 1)
        room_key = f"room:{ts.ROOM_B}"
        await until(pilot, lambda: app.sidebar._by_key[room_key].data.unread)
        assert _label(app, room_key).endswith(" ●")
        assert not app.sidebar._by_key[room_key].data.mention
        assert app.title == "● Pepper"
        # The flag is the server's: the client never fetches a topic's messages to look.
        await pilot.pause(0.3)
        assert _fetches(server, ts.TOPIC_B1) == 0


async def test_no_marker_for_a_mention_of_someone_else() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        _activity(server, ts.TOPIC_B1, 1, mentions=["00000000-0000-0000-0000-00000000cccc"])
        room_key = f"room:{ts.ROOM_B}"
        await until(pilot, lambda: app.sidebar._by_key[room_key].data.unread)
        assert not app.sidebar._by_key[room_key].data.mention
        assert _label(app, room_key).endswith(" ●")


@pytest.mark.parametrize("enabled", [False, True])
async def test_the_bell_rings_once_per_new_mention_and_only_when_asked(enabled) -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client(), bell=enabled)
    rings: list[int] = []
    app.bell = lambda: rings.append(1)  # type: ignore[method-assign]
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        _activity(server, ts.TOPIC_B1, 1, mentions=[ts.ME_PERSON])
        await until(pilot, lambda: app.title == "@ Pepper")
        await pilot.pause(0.3)  # later polls repeat the same flag
        assert len(rings) == (1 if enabled else 0)


async def test_the_bell_stays_quiet_for_a_mention_in_the_open_topic() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client(), bell=True)
    rings: list[int] = []
    app.bell = lambda: rings.append(1)  # type: ignore[method-assign]
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.open_conversation(state.Conversation(ts.ROOM_B, ts.TOPIC_B1))
        await until(pilot, lambda: app.loaded_topic)
        _activity(server, ts.TOPIC_B1, 1, mentions=[ts.ME_PERSON])
        await until(pilot, lambda: "ping" in ts.body_text(app))
        await pilot.pause(0.3)
        assert rings == []


def test_bell_env_flag(monkeypatch) -> None:
    from pepper_cli import tui

    monkeypatch.delenv("PEPPER_TUI_BELL", raising=False)
    assert tui.bell_enabled() is False
    for off in ("", "0", "false", "No"):
        monkeypatch.setenv("PEPPER_TUI_BELL", off)
        assert tui.bell_enabled() is False
    monkeypatch.setenv("PEPPER_TUI_BELL", "1")
    assert tui.bell_enabled() is True


def _own(mid: str, at: str) -> dict:
    """A message of yours, as the server sends it: flagged ``is_you`` and carrying your
    person id."""
    return ts.message(mid, at, "mine", is_you=True, author_person_id=ts.ME_PERSON)


def _thread_row_text(app: tui_app.PepperApp, key: str) -> str:
    console = Console(width=100, file=io.StringIO(), record=True, force_terminal=False)
    for row_key, row in app._topic_items():
        if row_key == key:
            console.print(row)
    return console.export_text()


def test_the_fixture_keeps_user_id_and_person_id_apart() -> None:
    assert ts.ME != ts.ME_PERSON


async def test_the_thread_highlights_a_mention_by_person_id() -> None:
    server = ts.standard_server()
    server.messages[ts.TOPIC_A1].append(
        ts.message("m-ping", "2026-09-16T17:31:00+00:00", "ping", mentions=[ts.ME_PERSON])
    )
    server.messages[ts.TOPIC_A1].append(
        ts.message("m-user", "2026-09-16T17:32:00+00:00", "user", mentions=[ts.ME])
    )
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: app.loaded_topic)
        # Only the message that carries your person id gets the "@" beside the name.
        assert " @" in _thread_row_text(app, "msg:m-ping").splitlines()[0]
        assert " @" not in _thread_row_text(app, "msg:m-user").splitlines()[0]
