"""Posting from the terminal UI (``planning/terminal-ui.md`` phase 2), against a fake server.

Room sends (idempotency keys, pending rows, retry), quote-reply, ``@`` mentions, drafts, and
Pepper DM turns streamed into the status line, including a dropped stream and a quit while
a turn is streaming. Timing-sensitive tests blur the app and drive polls by hand.
"""

from __future__ import annotations

import threading

import httpx
import pytest
from textual import events

from pepper_cli.provider.fs import FilesystemProvider
from pepper_cli.tui import app as tui_app
from pepper_cli.tui import state
from pepper_cli.tui.composer import Composer
from pepper_cli.tui.thread import ThreadScroll
from tests import tui_support as ts
from tests.tui_support import body_text, plain, until

A1 = state.Conversation(ts.ROOM_A, ts.TOPIC_A1)
A2 = state.Conversation(ts.ROOM_A, ts.TOPIC_A2)
POST_A1 = f"POST /api/v1/groups/{ts.ROOM_A}/topics/{ts.TOPIC_A1}/messages"

ROSTER = [
    {"person_id": "pepper", "display_name": "Pepper", "is_pepper": True},
    {"person_id": "p-ada", "display_name": "Ada Lovelace"},
    {"person_id": "p-bob", "display_name": "Bob Smith"},
]


@pytest.fixture(autouse=True)
def _fast_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state, "ACTIVE_INTERVAL", 0.05)
    monkeypatch.setattr(state, "COOLING_INTERVAL", 0.05)
    monkeypatch.setattr(state, "IDLE_INTERVAL", 0.05)
    monkeypatch.setattr(state, "RAIL_INTERVAL", 0.05)
    monkeypatch.setattr(state, "DM_INTERVAL", 0.05)
    monkeypatch.setattr(state, "DM_RECONCILE_INTERVAL", 0.05)
    monkeypatch.setattr(state, "BACKOFF_STEPS", (0.05, 0.05, 0.05, 0.05))
    monkeypatch.setattr(tui_app, "utcnow", lambda: ts.NOW)


def server_with_roster() -> ts.FakeServer:
    server = ts.standard_server()
    server.rosters[ts.ROOM_A] = ROSTER
    return server


async def open_topic(pilot, app: tui_app.PepperApp, conv=A1, text="flight options") -> None:
    await until(pilot, lambda: len(app.rooms) == 2)
    app.open_conversation(conv)
    await until(pilot, lambda: text in body_text(app))


async def send(pilot, app: tui_app.PepperApp, text: str) -> None:
    app.composer.focus()
    app.composer.insert(text)
    await pilot.press("enter")


def status_line(app: tui_app.PepperApp) -> str:
    return plain(app, "#dm-status")


async def blur(pilot, app: tui_app.PepperApp) -> None:
    """Stop the topic cadence, and let a poll already in flight land, so that a poll the
    test then drives by hand (``app._run("topic")``) is not skipped as a duplicate."""
    app.on_app_blur(events.AppBlur())
    await until(pilot, lambda: not app._loops["topic"].busy)


# ---------------------------------------------------------------- room sends: failure paths


async def test_a_failed_send_is_retried_with_the_same_idempotency_key() -> None:
    server = server_with_roster()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await open_topic(pilot, app)
        server.fail[POST_A1] = 500
        await send(pilot, app, "see you there")
        await until(pilot, lambda: "failed, r to retry" in body_text(app))
        assert app.composer.text == ""  # the text lives on the failed row, not the composer
        assert app.store.rows.keys() == {"m1"}  # a pending row never enters the store
        server.fail.clear()
        app.thread.focus_scroller()
        await pilot.press("up")  # the newest row: the failed send
        await pilot.press("r")
        await until(pilot, lambda: not app.pending.items)
        assert [p["key"] for p in server.posts] == [server.posts[0]["key"]] * 2
        assert server.posts[0]["key"]
        text = body_text(app)
        assert text.count("see you there") == 1
        assert "failed" not in text


async def test_a_duplicate_after_a_timeout_is_stored_once() -> None:
    server = server_with_roster()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await open_topic(pilot, app)
        await blur(pilot, app)
        # The server stores the message, then the answer is lost.
        server.raise_after[POST_A1] = httpx.ReadTimeout("lost")
        await send(pilot, app, "booked it")
        await until(pilot, lambda: "failed, r to retry" in body_text(app))
        assert "timed out" in body_text(app)
        server.raise_after.clear()
        app.thread.focus_scroller()
        await pilot.press("up")
        await pilot.press("r")
        await until(pilot, lambda: not app.pending.items)
        keys = {p["key"] for p in server.posts}
        assert len(server.posts) == 2 and len(keys) == 1
        posted = [m for m in server.messages[ts.TOPIC_A1] if m["raw_body"] == "booked it"]
        assert len(posted) == 1  # the same key answered the row already stored
        await until(pilot, lambda: body_text(app).count("booked it") == 1)


async def test_switching_conversations_while_a_send_is_pending() -> None:
    server = server_with_roster()
    gate = threading.Event()
    server.gates[POST_A1] = gate
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await open_topic(pilot, app)
        await send(pilot, app, "on my way")
        await until(pilot, lambda: "sending…" in body_text(app))
        app.composer.insert("half a thought")
        app.open_conversation(A2)
        await until(pilot, lambda: "hotel options" in body_text(app))
        assert "on my way" not in body_text(app)  # the pending row belongs to its own topic
        assert app.composer.text == ""  # drafts are per destination
        gate.set()
        await until(pilot, lambda: app.pending.items and app.pending.items[0].status == "sent")
        # The topic it went to is no longer on screen, so the send marks nothing: the mark is a
        # high-water mark, and rows stored there before it have not been seen (§5.1).
        created = app.pending.items[0].created_at
        await pilot.pause(0.3)
        assert (ts.TOPIC_A1, created) not in server.puts
        app.open_conversation(A1)
        assert app.composer.text == "half a thought"
        await until(pilot, lambda: not app.pending.items)
        assert body_text(app).count("on my way") == 1
        assert len(server.posts) == 1
        # Back at its end, the row has been on screen: now it is read.
        await until(pilot, lambda: (ts.TOPIC_A1, created) in server.puts)


# ---------------------------------------------------------------- room sends: §11 Pilot


async def test_sending_while_rows_arrive_keeps_the_pending_row_last_and_unbadged() -> None:
    server = server_with_roster()
    gate = threading.Event()
    server.gates[POST_A1] = gate
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await open_topic(pilot, app)
        await blur(pilot, app)
        await send(pilot, app, "mine first")
        await until(pilot, lambda: "sending…" in body_text(app))
        server.messages[ts.TOPIC_A1].append(
            ts.message("m7", "2026-09-16T17:40:00+00:00", "theirs meanwhile")
        )
        app._run("topic")
        await until(pilot, lambda: "theirs meanwhile" in body_text(app))
        text = body_text(app)
        assert text.index("theirs meanwhile") < text.index("mine first")
        assert app.store.newest_created_at() == "2026-09-16T17:40:00+00:00"
        gate.set()  # stored: the send polls the topic at once, and that poll confirms it
        await until(pilot, lambda: not app.pending.items)
        text = body_text(app)
        assert text.index("theirs meanwhile") < text.index("mine first")
        assert "sending" not in text


async def test_an_esc_cancelled_quote_is_not_sent_and_a_second_esc_leaves_the_composer() -> None:
    server = server_with_roster()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await open_topic(pilot, app)
        app.thread.focus_scroller()
        await pilot.press("up", "r")
        await until(pilot, lambda: "Replying to" in plain(app, "#quote"))
        assert "Ada" in plain(app, "#quote") and "flight options" in plain(app, "#quote")
        assert isinstance(app.focused, Composer)
        await pilot.press("escape")
        assert app.query_one("#quote").has_class("empty")
        assert isinstance(app.focused, Composer)
        await pilot.press("escape")
        assert isinstance(app.focused, ThreadScroll)
        await send(pilot, app, "no quote")
        await until(pilot, lambda: server.posts)
        assert "parent_message_id" not in server.posts[0]


async def test_a_quote_reply_sends_the_parent_and_shows_it_on_the_pending_row() -> None:
    server = server_with_roster()
    gate = threading.Event()
    server.gates[POST_A1] = gate
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await open_topic(pilot, app)
        app.thread.focus_scroller()
        await pilot.press("up", "r")
        await send(pilot, app, "the 9am one")
        await until(pilot, lambda: "sending…" in body_text(app))
        assert app.pending.items[0].quote_text == "flight options"
        assert "Replying to" not in plain(app, "#quote")  # sent: the quote left the composer
        gate.set()
        await until(pilot, lambda: server.posts)
        assert server.posts[0]["parent_message_id"] == "m1"


async def test_mentions_come_from_the_roster_and_are_sent_by_person_id() -> None:
    server = server_with_roster()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await open_topic(pilot, app)
        await until(pilot, lambda: ts.ROOM_A in app.rosters)
        app.composer.focus()
        app.composer.insert("thanks @Bo")
        await until(pilot, lambda: app.composer.menu.display)
        assert [m.person_id for m in app.composer.menu.members] == ["p-bob"]
        await pilot.press("tab")
        assert app.composer.text == "thanks @Bob Smith "
        assert not app.composer.menu.display
        app.composer.insert("and @ada lovelace")
        await pilot.press("escape")  # closes the menu, keeps the text
        assert app.composer.text.endswith("@ada lovelace")
        await pilot.press("enter")
        await until(pilot, lambda: server.posts)
        assert sorted(server.posts[0]["mentions"]) == ["p-ada", "p-bob"]


async def test_a_room_opened_while_another_roster_loads_gets_its_own_roster() -> None:
    server = server_with_roster()
    server.rosters[ts.ROOM_B] = [{"person_id": "p-cy", "display_name": "Cy Young"}]
    gate = threading.Event()
    server.gates[f"/api/v1/groups/{ts.ROOM_A}/roster"] = gate
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await open_topic(pilot, app)
        await until(pilot, lambda: app._loops["roster"].busy)
        app.open_conversation(state.Conversation(ts.ROOM_B, ts.TOPIC_B1))
        await until(pilot, lambda: "next book" in body_text(app))
        assert ts.ROOM_B not in app.rosters  # skipped: room A's request is still running
        gate.set()
        await until(pilot, lambda: ts.ROOM_B in app.rosters)
        assert [m.person_id for m in app.composer.roster] == ["p-cy"]
        assert ts.ROOM_A in app.rosters


async def test_composer_keys_newline_and_slash_and_quit_command() -> None:
    server = server_with_roster()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await open_topic(pilot, app)
        app.sidebar.focus()
        await pilot.press("slash")
        assert isinstance(app.focused, Composer)
        await pilot.press("a", "ctrl+j", "b", "alt+enter", "c")
        assert app.composer.text == "a\nb\nc"
        await pilot.press("enter")
        await until(pilot, lambda: server.posts)
        assert server.posts[0]["message"] == "a\nb\nc"
        await pilot.press("enter")  # empty: nothing sent
        await send(pilot, app, "/quit")
        await until(pilot, lambda: app.return_code == 0)
    assert len(server.posts) == 1


# ---------------------------------------------------------------- Pepper DM turns


def reply_turn(text: str, n: int, **over) -> ts.ScriptedTurn:
    user, assistant = f"u{n}", f"a{n}"
    return ts.ScriptedTurn(
        frames=[
            ("model_thought", {"text": "Looking into it"}),
            ("reply", {"reply": text, "user_message_id": user, "assistant_message_id": assistant}),
        ],
        history=[
            ts.history_entry(user, "user", f"question {n}", f"2026-09-16T09:00:0{n}Z"),
            ts.history_entry(assistant, "assistant", text, f"2026-09-16T09:00:1{n}Z"),
        ],
        **over,
    )


async def test_a_turn_streams_into_the_status_line_and_its_reply_becomes_a_message() -> None:
    server = ts.standard_server()
    gate = threading.Event()
    server.turns = [reply_turn("Here you go", 1, gates={1: gate})]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.dm.entries) == 2)
        await send(pilot, app, "question 1")
        await until(pilot, lambda: "Thinking: Looking into it" in status_line(app))
        assert "question 1" in body_text(app)  # the provisional row
        gate.set()
        await until(pilot, lambda: not app.turns)
        assert "Here you go" in body_text(app)
        assert body_text(app).count("question 1") == 1
        assert app.query_one("#dm-status").has_class("empty")
        assert server.turn_bodies == [{"message": "question 1"}]  # no --root, no manifest


async def test_a_turn_streaming_with_a_client_tool_call(tmp_path) -> None:
    (tmp_path / "plan.md").write_text("pack the snorkel\n")
    server = ts.standard_server()
    gate = threading.Event()
    server.turns = [
        ts.ScriptedTurn(
            frames=[
                ("client_tools", {"accepted": ["fs_read"], "manifest_reason": None}),
                (
                    "client_tool_call",
                    {"call_id": "c1", "name": "fs_read", "arguments": {"path": "plan.md"}},
                ),
                ("tool_result", {"name": "fs_read"}),
                ("reply", {"reply": "Pack the snorkel.", "assistant_message_id": "a1"}),
            ],
            gates={2: gate},
            history=[
                ts.history_entry("u1", "user", "what's in my plan", "2026-09-16T09:00:00Z"),
                ts.history_entry("a1", "assistant", "Pack the snorkel.", "2026-09-16T09:00:09Z"),
            ],
        )
    ]
    app = tui_app.PepperApp(server.client(), provider=FilesystemProvider([tmp_path]))
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.dm.entries) == 2)
        await send(pilot, app, "what's in my plan")
        await until(pilot, lambda: "Running local tool fs_read" in status_line(app))
        # The tool was answered before the stream went on: synchronously, on the turn thread.
        await until(pilot, lambda: server.tool_results)
        result = server.tool_results[0]
        assert result["call_id"] == "c1"
        assert "pack the snorkel" in result["content"][0]["text"]
        assert not result["is_error"]
        assert {t["name"] for t in server.turn_bodies[0]["client_tools"]} >= {"fs_read"}
        gate.set()
        await until(pilot, lambda: not app.turns)
        assert "Pack the snorkel." in body_text(app)


async def test_a_dropped_stream_is_never_resent_and_reconciles_from_history() -> None:
    server = ts.standard_server()
    gate = threading.Event()
    server.turns = [
        ts.ScriptedTurn(
            frames=[("model_thought", {"text": "Hmm"})],
            history=[
                ts.history_entry("u1", "user", "are we on", "2026-09-16T09:00:00Z"),
                ts.history_entry("a1", "assistant", "Yes, at noon.", "2026-09-16T09:00:09Z"),
            ],
            drop=True,
        )
    ]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.dm.entries) == 2)
        server.gates["/api/v1/chat/history"] = gate
        await send(pilot, app, "are we on")
        await until(pilot, lambda: "Connection lost, checking" in status_line(app))
        assert app.turns[0].phase == "checking"
        gate.set()
        await until(pilot, lambda: not app.turns)
        assert "Yes, at noon." in body_text(app)
        assert body_text(app).count("are we on") == 1
        assert app.query_one("#dm-status").has_class("empty")
        assert len(server.turn_bodies) == 1  # never resent


async def test_a_dropped_stream_gives_up_after_the_window(monkeypatch) -> None:
    monkeypatch.setattr(state, "DM_RECONCILE_WINDOW", 0.2)
    server = ts.standard_server()
    server.turns = [ts.ScriptedTurn(drop=True)]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.dm.entries) == 2)
        await send(pilot, app, "anyone?")
        await until(pilot, lambda: "No reply yet" in status_line(app))
        assert not app.turns
        assert len(server.turn_bodies) == 1


async def test_a_second_send_waits_until_the_current_turn_finishes() -> None:
    server = ts.standard_server()
    gate = threading.Event()
    server.turns = [reply_turn("first answer", 1, gates={1: gate}), reply_turn("second", 2)]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.dm.entries) == 2)
        await send(pilot, app, "question 1")
        await until(pilot, lambda: "Thinking" in status_line(app))
        await send(pilot, app, "question 2")
        assert "waiting to send" in body_text(app)
        await pilot.pause(0.1)
        assert len(server.turn_bodies) == 1
        gate.set()
        await until(pilot, lambda: len(server.turn_bodies) == 2 and not app.turns)
        assert [b["message"] for b in server.turn_bodies] == ["question 1", "question 2"]
        assert "second" in body_text(app)


async def test_an_answered_turn_whose_user_row_never_shows_does_not_hold_the_queue(
    monkeypatch,
) -> None:
    # The question was stored before midnight and the answer after it: today's history has
    # the answer but will never have the question.
    # Long enough that turn 1 cannot age out before turn 2's rows are read back, even on a
    # slow runner; short enough to watch it retire.
    monkeypatch.setattr(state, "DM_RECONCILE_WINDOW", 1.0)
    server = ts.standard_server()
    gate = threading.Event()
    first = reply_turn("after midnight", 1, gates={1: gate})
    first.history = [h for h in first.history if h["role"] == "assistant"]
    server.turns = [first, reply_turn("second", 2)]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.dm.entries) == 2)
        await send(pilot, app, "question 1")
        await until(pilot, lambda: "Thinking" in status_line(app))
        await send(pilot, app, "question 2")
        gate.set()
        await until(pilot, lambda: len(server.turn_bodies) == 2)
        await until(pilot, lambda: "second" in body_text(app))
        # "second" shows from the reply frame first; turn 2 retires once a history read has
        # both its rows, and only then is the queue down to turn 1.
        await until(pilot, lambda: {"u2", "a2"} <= app.dm.entries.keys())
        assert [t.text for t in app.turns] == ["question 1"]  # still shown, still waiting
        assert app.turns[0].phase == "done"
        await until(pilot, lambda: not app.turns)  # retired after the window
        assert body_text(app).count("question 1") == 0
        assert "after midnight" in body_text(app)


async def test_a_dropped_turn_never_takes_a_retired_earlier_turns_reply(monkeypatch) -> None:
    # Turn 1 is answered; turn 2 streams past the window, so turn 1 is retired before any
    # history read has shown its rows; turn 3 then drops. Turn 1's reply, which the next
    # history read returns, is not turn 3's: turn 3 waits out its window instead.
    monkeypatch.setattr(state, "DM_RECONCILE_WINDOW", 0.2)
    server = ts.standard_server()
    first, second = threading.Event(), threading.Event()
    server.turns = [
        reply_turn("first", 1, gates={1: first}),
        reply_turn("second", 2, gates={1: second}),
        ts.ScriptedTurn(drop=True),
    ]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.dm.entries) == 2)
        await send(pilot, app, "question 1")
        await until(pilot, lambda: "Thinking" in status_line(app))
        await send(pilot, app, "question 2")
        await send(pilot, app, "question 3")
        first.set()  # turn 2 starts at once: no history read in between
        await until(pilot, lambda: len(server.turn_bodies) == 2)
        assert "a1" not in app.dm.entries
        await pilot.pause(0.4)
        second.set()
        await until(pilot, lambda: len(server.turn_bodies) == 3)
        # Taken for turn 1's reply, turn 3 would be retired quietly with no note.
        await until(pilot, lambda: "No reply yet" in status_line(app))
        assert not app.turns
        assert len(server.turn_bodies) == 3


async def test_an_error_frame_shows_in_the_status_line() -> None:
    server = ts.standard_server()
    server.turns = [ts.ScriptedTurn(frames=[("error", {"kind": "model", "message": "busy"})])]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.dm.entries) == 2)
        await send(pilot, app, "hello?")
        await until(pilot, lambda: "Pepper couldn't answer: busy" in status_line(app))
        assert not app.turns


async def test_a_refused_turn_puts_the_text_back_in_the_composer() -> None:
    server = ts.standard_server()
    server.fail["/api/v1/chat/send"] = 503
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.dm.entries) == 2)
        await send(pilot, app, "try this")
        await until(pilot, lambda: "Couldn't send (HTTP 503)" in status_line(app))
        assert app.composer.text == "try this"
        assert not app.turns


async def test_a_refused_turn_keeps_a_draft_typed_while_it_was_sending() -> None:
    server = ts.standard_server()
    server.fail["/api/v1/chat/send"] = 503
    gate = threading.Event()
    server.gates["/api/v1/chat/send"] = gate
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.dm.entries) == 2)
        await send(pilot, app, "try this")
        await until(pilot, lambda: "Sending" in status_line(app))
        app.composer.insert("and then this")
        gate.set()
        await until(pilot, lambda: "Couldn't send (HTTP 503)" in status_line(app))
        assert app.composer.text == "try this\n\nand then this"
        assert not app.turns


async def test_a_turn_refused_while_a_room_is_open_keeps_the_pepper_draft() -> None:
    server = ts.standard_server()
    server.fail["/api/v1/chat/send"] = 503
    gate = threading.Event()
    server.gates["/api/v1/chat/send"] = gate
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.dm.entries) == 2)
        await send(pilot, app, "try this")
        app.composer.insert("half typed")
        await open_topic(pilot, app)
        gate.set()
        await until(pilot, lambda: not app.turns)
        assert app.drafts[state.PEPPER].text == "try this\n\nhalf typed"
        assert app.composer.text == ""  # the room's own draft is untouched
        app.open_conversation(state.PEPPER)
        assert app.composer.text == "try this\n\nhalf typed"


async def test_a_401_on_a_turn_is_fatal() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.dm.entries) == 2)
        server.fail["/api/v1/chat/send"] = 401
        await send(pilot, app, "hi")
        await until(pilot, lambda: app.fatal == tui_app.LOGIN_HINT)
        await pilot.press("x")
    assert app.return_code == 1


def tool_call_turn() -> ts.ScriptedTurn:
    return ts.ScriptedTurn(
        frames=[
            ("client_tool_call", {"call_id": "c1", "name": "fs_list", "arguments": {}}),
            ("reply", {"reply": "Done.", "assistant_message_id": "a1"}),
        ],
    )


async def test_a_401_returning_a_client_tool_result_is_fatal(tmp_path) -> None:
    server = ts.standard_server()
    server.turns = [tool_call_turn()]
    server.fail["/api/v1/chat/tool-result"] = 401
    app = tui_app.PepperApp(server.client(), provider=FilesystemProvider([tmp_path]))
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.dm.entries) == 2)
        await send(pilot, app, "list my files")
        await until(pilot, lambda: app.fatal == tui_app.LOGIN_HINT)
        await pilot.press("x")
    assert app.return_code == 1
    assert len(server.turn_bodies) == 1


async def test_a_missing_call_returning_a_client_tool_result_reads_on(tmp_path) -> None:
    server = ts.standard_server()
    gate = threading.Event()
    turn = tool_call_turn()
    turn.gates = {1: gate}
    server.turns = [turn]
    server.fail["/api/v1/chat/tool-result"] = 404
    app = tui_app.PepperApp(server.client(), provider=FilesystemProvider([tmp_path]))
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.dm.entries) == 2)
        await send(pilot, app, "list my files")
        await until(pilot, lambda: "Couldn't return 'fs_list'" in status_line(app))
        gate.set()
        await until(pilot, lambda: "Done." in body_text(app))
        assert not app.fatal


async def test_quitting_mid_stream_stops_reading_and_closes_the_stream(monkeypatch) -> None:
    server = ts.standard_server()
    gate = threading.Event()
    server.turns = [reply_turn("too late", 1, gates={1: gate})]
    outcomes: list[str] = []
    real_run_turn = tui_app.turns.run_turn

    def run_turn(*args, **kwargs):
        outcomes.append(real_run_turn(*args, **kwargs))
        return outcomes[-1]

    monkeypatch.setattr(tui_app.turns, "run_turn", run_turn)
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.dm.entries) == 2)
        await send(pilot, app, "question 1")
        await until(pilot, lambda: "Thinking" in status_line(app))
        await pilot.press("ctrl+q")
    assert app.return_code == 0
    gate.set()  # the next frame arrives after quit: it is not dispatched
    for worker in [t for t in threading.enumerate() if t.name == "pepper-turn"]:
        worker.join(timeout=5)
        assert not worker.is_alive()
    assert outcomes == ["stopped"]
    assert app.turns[0].reply is None  # nothing was posted back to the closed app
    assert len(server.turn_bodies) == 1
