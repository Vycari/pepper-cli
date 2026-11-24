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


async def test_a_root_read_lost_to_the_connection_is_asked_again() -> None:
    import httpx

    rows = [
        ts.message("m0", _at(0), "# Old plan\nfrom last week", title="Old plan"),
        ts.message("x1", _at(1), "filler one"),
        ts.message("x2", _at(2), "filler two"),
        ts.message("r1", _at(3), "still on?", parent_message_id="m0", display_name="Bo"),
    ]
    server = _server(rows)
    server.page_size = 3
    answer = server.handler
    lost = []

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("at") == "m0" and not lost:
            lost.append(request)
            raise httpx.ConnectError("connection lost", request=request)
        return answer(request)

    server.handler = flaky  # type: ignore[method-assign]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot, app, "still on?")
        await until(pilot, lambda: "Old plan" in body_text(app))
        assert lost and "m0" in app.fetched_roots


async def test_the_fold_line_is_not_the_root_for_edit_delete_or_react() -> None:
    replies = [
        ts.message(f"r{i}", _at(i + 1), f"reply number {i}", parent_message_id="m1")
        for i in range(5)
    ]
    server = _server([ts.message("m1", _at(0), "Plans", is_you=True), *replies])
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot, app, "reply number 4")
        await _select(pilot, app, "fold:m1")
        assert app._selected_message() is None
        await pilot.press("d")
        await pilot.pause(0.1)
        assert len(app.screen_stack) == 1
        assert not any(method == "DELETE" for method, _, _ in server.requests)


async def test_a_root_read_by_id_stays_drawn_once_an_older_page_brings_it() -> None:
    from pepper_cli import api_client

    rows = [
        ts.message("m0", _at(0), "the original question"),
        ts.message("x1", _at(1), "filler one"),
        ts.message("x2", _at(2), "filler two"),
        ts.message("r1", _at(3), "the answer", parent_message_id="m0"),
    ]
    server = _server(rows)
    server.page_size = 3
    server.rooms[0]["topics"][0]["dedicated_conversation"] = True
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot, app, "the answer")
        await until(pilot, lambda: "m0" in app._root_attempts and not app._loops["roots"].busy)
        app.store.add_older([api_client.group_message_from_dict(rows[0])], None)
        app._render_topic(new_rows=0, reset=False)
        await pilot.pause()
        assert "the original question" in body_text(app)


async def test_the_read_mark_never_passes_a_reply_behind_the_fold() -> None:
    replies = [
        ts.message(f"r{i}", _at(i + 1), f"reply number {i}", parent_message_id="m1")
        for i in range(5)
    ]
    server = _server([ts.message("m1", _at(0), "Plans"), *replies])
    server.marks[ts.TOPIC_A1] = _at(0)  # read through the card's root only
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot, app, "reply number 4")
        await pilot.pause(0.3)
        marked = [at for topic, at in server.puts if topic == ts.TOPIC_A1]
        assert all(at < _at(1) for at in marked), marked
        await _select(pilot, app, "fold:m1")
        await pilot.press("enter")
        await until(pilot, lambda: "reply number 0" in body_text(app))
        app.thread.focus_scroller()
        await pilot.press("end")
        await until(pilot, lambda: (ts.TOPIC_A1, _at(5)) in server.puts)


async def test_a_root_that_keeps_failing_does_not_hold_up_the_next() -> None:
    import httpx

    rows = [
        ts.message("m0", _at(0), "# First plan\nold", title="First plan"),
        ts.message("m1", _at(1), "# Second plan\nold", title="Second plan"),
        ts.message("x1", _at(2), "filler one"),
        ts.message("r0", _at(3), "about the first", parent_message_id="m0"),
        ts.message("r1", _at(4), "about the second", parent_message_id="m1"),
    ]
    server = _server(rows)
    server.page_size = 3
    answer = server.handler
    asked: list[str] = []

    def failing(request: httpx.Request) -> httpx.Response:
        at = request.url.params.get("at")
        if at:
            asked.append(at)
        if at == "m0":
            return httpx.Response(429, json={"detail": "slow down"})
        return answer(request)

    server.handler = failing  # type: ignore[method-assign]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(pilot, app, "about the second")
        await until(pilot, lambda: "Second plan" in body_text(app))
        # The throttled root is asked again, a bounded number of times.
        await until(pilot, lambda: asked.count("m0") == tui_app.ROOT_RETRIES + 1)
        await pilot.pause(0.3)
        assert asked.count("m0") == tui_app.ROOT_RETRIES + 1
