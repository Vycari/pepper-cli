"""Conversation cards in the running TUI: replying into a card, the fold, and a root older
than the loaded page (``planning/tui-conversation-cards.md``)."""

from __future__ import annotations

import pytest

from pepper_cli.tui import app as tui_app
from pepper_cli.tui import state
from tests import tui_support as ts
from tests.tui_support import body_text, plain, until

A1 = state.Conversation(ts.ROOM_A, ts.TOPIC_A1)


@pytest.fixture(autouse=True)
def _fast_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ACTIVE_INTERVAL", "COOLING_INTERVAL", "IDLE_INTERVAL", "RAIL_INTERVAL"):
        monkeypatch.setattr(state, name, 0.05)
    monkeypatch.setattr(state, "DM_INTERVAL", 0.05)
    monkeypatch.setattr(state, "BACKOFF_STEPS", (0.05, 0.05, 0.05, 0.05))
    monkeypatch.setattr(tui_app, "utcnow", lambda: ts.NOW)


def _at(minute: int) -> str:
    return f"2026-09-16T17:{minute:02d}:00+00:00"


def _server(rows: list[dict]) -> ts.FakeServer:
    server = ts.standard_server()
    server.messages[ts.TOPIC_A1] = rows
    server.marks[ts.TOPIC_A1] = rows[-1]["created_at"]
    return server


async def _open(pilot, app: tui_app.PepperApp, text: str) -> None:
    await until(pilot, lambda: len(app.rooms) == 2)
    app.open_conversation(A1)
    await until(pilot, lambda: text in body_text(app))


async def _select(pilot, app: tui_app.PepperApp, key: str) -> None:
    app.thread.focus_scroller()
    for _ in range(20):
        if app.selected == key:
            return
        await pilot.press("up")
    raise AssertionError(f"never selected {key}: {app.selected}")


async def test_r_on_a_reply_replies_into_its_card() -> None:
    server = _server(
        [
            ts.message("m1", _at(0), "# Cabin\nBooked it", title="Cabin", title_source="heading"),
            ts.message("r1", _at(1), "dog?", parent_message_id="m1", display_name="Bo"),
        ]
    )
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot, app, "dog?")
        assert "╭─ Cabin" in body_text(app)
        await _select(pilot, app, "msg:r1")
        await pilot.press("r")
        await until(pilot, lambda: "Replying in" in plain(app, "#quote"))
        assert "Cabin" in plain(app, "#quote") and "Bo" in plain(app, "#quote")
        app.composer.insert("yes, bring her")
        await pilot.press("enter")
        await until(pilot, lambda: server.posts)
        assert server.posts[0]["parent_message_id"] == "r1"


async def test_r_on_a_card_replies_to_its_root() -> None:
    server = _server([ts.message("m1", _at(0), "Lunch?")])
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot, app, "Lunch?")
        await _select(pilot, app, "msg:m1")
        await pilot.press("r")
        await until(pilot, lambda: "Replying in" in plain(app, "#quote"))
        assert " to " not in plain(app, "#quote")
        app.composer.insert("noon")
        await pilot.press("enter")
        await until(pilot, lambda: server.posts)
        assert server.posts[0]["parent_message_id"] == "m1"


async def test_enter_on_the_fold_opens_the_earlier_replies() -> None:
    replies = [
        ts.message(f"r{i}", _at(i + 1), f"reply number {i}", parent_message_id="m1")
        for i in range(5)
    ]
    server = _server([ts.message("m1", _at(0), "Plans"), *replies])
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot, app, "reply number 4")
        assert "2 earlier replies" in body_text(app)
        assert "reply number 0" not in body_text(app)
        await _select(pilot, app, "fold:m1")
        await pilot.press("enter")
        await until(pilot, lambda: "reply number 0" in body_text(app))
        await pilot.press("enter")
        await until(pilot, lambda: "reply number 0" not in body_text(app))


async def test_a_reply_whose_root_is_older_than_the_page_nests_under_it() -> None:
    rows = [
        ts.message("m0", _at(0), "# Old plan\nfrom last week", title="Old plan"),
        ts.message("x1", _at(1), "filler one"),
        ts.message("x2", _at(2), "filler two"),
        ts.message("r1", _at(3), "still on?", parent_message_id="m0", display_name="Bo"),
    ]
    server = _server(rows)
    server.page_size = 3
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot, app, "still on?")
        await until(pilot, lambda: "Old plan" in body_text(app))
        assert "m0" in app.fetched_roots
        assert any(query.get("at") == "m0" for _, _, query in server.requests)
        text = body_text(app)
        # The fetched card sits at its reply, the foot of the stream, not above the page.
        assert text.index("filler two") < text.index("Old plan")
        # Asked once: later polls do not read it again.
        asks = sum(1 for _, _, query in server.requests if query.get("at") == "m0")
        app._run("topic")
        await pilot.pause(0.2)
        assert sum(1 for _, _, query in server.requests if query.get("at") == "m0") == asks


async def test_a_bridged_topic_stays_a_plain_list() -> None:
    server = _server(
        [
            ts.message("m1", _at(0), "from email"),
            ts.message("r1", _at(1), "answer", parent_message_id="m1"),
        ]
    )
    server.rooms[0]["topics"][0]["dedicated_conversation"] = True
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot, app, "answer")
        assert "╭" not in body_text(app)
