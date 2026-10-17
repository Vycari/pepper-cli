"""P4: ``@`` on a sidebar row whose unread activity mentions you (planning/terminal-ui.md §15 C)."""

from __future__ import annotations

import pytest

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
        _activity(server, ts.TOPIC_B1, 1, mentions=[ts.ME])
        room_key = f"room:{ts.ROOM_B}"
        await until(pilot, lambda: ts.TOPIC_B1 in app.mentions.hits)
        await until(pilot, lambda: app.sidebar._by_key[room_key].data.mention)
        assert _label(app, room_key).endswith(" @")
        assert "●" not in _label(app, room_key)
        assert app.title == "@ Pepper"
        # Opening it reads it: the marker and the title go.
        app.open_conversation(state.Conversation(ts.ROOM_B, ts.TOPIC_B1))
        await until(pilot, lambda: not app.sidebar._by_key[room_key].data.mention)
        await until(pilot, lambda: app.title == "Pepper")


async def test_unread_without_a_mention_keeps_the_dot_and_costs_one_fetch() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        _activity(server, ts.TOPIC_B1, 1)
        room_key = f"room:{ts.ROOM_B}"
        await until(pilot, lambda: app.sidebar._by_key[room_key].data.unread)
        await until(pilot, lambda: ts.TOPIC_B1 in app.mentions.checked)
        assert _label(app, room_key).endswith(" ●")
        assert not app.sidebar._by_key[room_key].data.mention
        assert app.title == "● Pepper"
        # Several rail polls later the topic has still been fetched exactly once.
        await pilot.pause(0.4)
        assert _fetches(server, ts.TOPIC_B1) == 1


async def test_a_topic_that_cannot_be_read_is_dropped_not_retried() -> None:
    server = ts.standard_server()
    server.fail[f"/api/v1/groups/{ts.ROOM_B}/topics/{ts.TOPIC_B1}/messages"] = 403
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        _activity(server, ts.TOPIC_B1, 1, mentions=[ts.ME])
        await until(pilot, lambda: ts.TOPIC_B1 in app.mentions.checked)
        await pilot.pause(0.3)
        assert _fetches(server, ts.TOPIC_B1) == 1
        assert ts.TOPIC_B1 not in app.mentions.hits


async def test_a_transport_failure_retries_with_backoff_and_then_finds_the_mention() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        server.fail[f"/api/v1/groups/{ts.ROOM_B}/topics/{ts.TOPIC_B1}/messages"] = 500
        _activity(server, ts.TOPIC_B1, 1, mentions=[ts.ME])
        await until(pilot, lambda: _fetches(server, ts.TOPIC_B1) >= 2)
        server.fail.clear()
        await until(pilot, lambda: ts.TOPIC_B1 in app.mentions.hits)


async def test_one_failing_topic_does_not_hold_up_mentions_in_the_others() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        # A1 sorts first and keeps failing; B1's mention must still be found.
        server.fail[f"/api/v1/groups/{ts.ROOM_A}/topics/{ts.TOPIC_A1}/messages"] = 500
        server.messages[ts.TOPIC_A1].append(ts.message("m-a", NEW, "hi"))
        server.rooms[0]["topics"][0]["last_activity_at"] = NEW
        _activity(server, ts.TOPIC_B1, 1, mentions=[ts.ME])
        await until(pilot, lambda: ts.TOPIC_B1 in app.mentions.hits)
        assert _fetches(server, ts.TOPIC_A1) >= 1
        assert ts.TOPIC_A1 not in app.mentions.checked


@pytest.mark.parametrize("enabled", [False, True])
async def test_the_bell_rings_once_per_new_mention_and_only_when_asked(enabled) -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client(), bell=enabled)
    rings: list[int] = []
    app.bell = lambda: rings.append(1)  # type: ignore[method-assign]
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        _activity(server, ts.TOPIC_B1, 1, mentions=[ts.ME])
        await until(pilot, lambda: ts.TOPIC_B1 in app.mentions.hits)
        await pilot.pause(0.3)
        assert len(rings) == (1 if enabled else 0)


def test_bell_env_flag(monkeypatch) -> None:
    from pepper_cli import tui

    monkeypatch.delenv("PEPPER_TUI_BELL", raising=False)
    assert tui.bell_enabled() is False
    for off in ("", "0", "false", "No"):
        monkeypatch.setenv("PEPPER_TUI_BELL", off)
        assert tui.bell_enabled() is False
    monkeypatch.setenv("PEPPER_TUI_BELL", "1")
    assert tui.bell_enabled() is True
