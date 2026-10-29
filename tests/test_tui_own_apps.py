"""Your own Todos and Notes panes in the running app (P1b): the top-level rows, the rooms'
sections, and the Pepper composer that stays under them (Textual Pilot, fake server)."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest

from pepper_cli.tui import app as tui_app
from pepper_cli.tui import state
from pepper_cli.tui.apps_pane import AppPane, PaneScroll
from pepper_cli.tui.composer import PLACEHOLDER
from pepper_cli.tui.screens import JumpScreen
from pepper_cli.tui.sidebar import DOT, Sidebar
from tests import tui_support as ts
from tests.test_tui_apps import SAM, SIZE, header, pane_text, server_with_apps
from tests.tui_support import body_text, plain, until

if TYPE_CHECKING:
    from textual.pilot import Pilot


@pytest.fixture(autouse=True)
def _fast_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state, "ACTIVE_INTERVAL", 0.05)
    monkeypatch.setattr(state, "COOLING_INTERVAL", 0.05)
    monkeypatch.setattr(state, "IDLE_INTERVAL", 0.05)
    monkeypatch.setattr(state, "RAIL_INTERVAL", 0.05)
    monkeypatch.setattr(state, "DM_INTERVAL", 0.05)
    monkeypatch.setattr(state, "BACKOFF_STEPS", (0.05, 0.05, 0.05, 0.05))
    monkeypatch.setattr(tui_app, "utcnow", lambda: ts.NOW)


def own_server() -> ts.FakeServer:
    server = server_with_apps()
    server.own_todos = [
        ts.todo(
            "o-passport",
            "Renew passport",
            due_date="2026-11-03T00:00:00+00:00",
            due_is_date_only=True,
        ),
        ts.todo("o-dentist", "Call the dentist", created_at="2026-09-11T00:00:00+00:00"),
        ts.todo("o-done", "Old own chore", status="done", done_at="2026-10-02T00:00:00+00:00"),
    ]
    server.room_todos = {
        ts.ROOM_A: (
            "Trip planning",
            [
                ts.stored_todo(
                    "r-sitter",
                    "Book the dog sitter",
                    source="request",
                    due_date="2026-10-30T00:00:00+00:00",
                    due_is_date_only=True,
                    source_ref={
                        "assignee_user_id": ts.ME,
                        "assignee_label": "Me",
                        "requester_id": SAM["user_id"],
                        "requester_label": "Sam",
                    },
                ),
                ts.stored_todo(
                    "r-done", "Old room chore", status="done", done_at="2026-10-05T00:00:00+00:00"
                ),
            ],
        )
    }
    server.notes[""] = [
        ts.note_item("m-pass", "Passport notes", "2026-10-26T10:00:00+00:00", summary="Photos"),
        ts.note_item("m-misc", "Misc", "2026-10-01T10:00:00+00:00"),
    ]
    server.note_details["m-pass"] = {
        **server.note_details["n-pack"],
        "id": "m-pass",
        "name": "Passport notes",
        "etag": '"m-pass-1"',
        "body": "Take two photos\n\n- [ ] Form\n",
    }
    return server


def reply_turn(text: str, n: int = 1, **over) -> ts.ScriptedTurn:
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


async def open_own(app: tui_app.PepperApp, pilot: Pilot, slug: str = "todo") -> None:
    await until(pilot, lambda: len(app.rooms) == 2 and len(app.dm.entries) >= 2)
    app.sidebar.focus()
    app.sidebar.select_key(f"app:me:{slug}")
    await pilot.pause()
    await pilot.press("enter")
    await until(pilot, lambda: app.pane is not None and app.pane.loaded)


async def say(pilot: Pilot, app: tui_app.PepperApp, text: str) -> None:
    app.composer.focus()
    app.composer.insert(text)
    await pilot.press("enter")


def strip(app: tui_app.PepperApp) -> str:
    return plain(app, "#reply-body")


def label(app: tui_app.PepperApp, key: str) -> str:
    return str(app.sidebar._by_key[key].label)


async def test_enter_on_the_todos_row_opens_your_pane_with_your_rooms_sections() -> None:
    server = own_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        assert header(app) == "Todos"
        assert app.current == state.PEPPER
        assert app.query_one(AppPane).display
        assert not app.thread.display
        assert isinstance(app.focused, PaneScroll)
        text = pane_text(app)
        # Your own open items (due-first), then the room's section, then one merged fold.
        assert "Todos  3 open" in text
        assert text.index("Renew passport") < text.index("Call the dentist")
        assert text.index("Call the dentist") < text.index("Trip planning")
        assert text.index("Trip planning") < text.index("Book the dog sitter")
        assert "asked of you by Sam" in text
        assert "due Oct 30" in text
        assert "▸ Closed (2)" in text
        assert "Old own chore" not in text
        # The row carries the marker while open; the other row, the rule and Pepper do not.
        assert label(app, "app:me:todo") == "▌ Todos"
        assert label(app, "app:me:notes") == "Notes"
        assert label(app, "pepper") == "Pepper"
        assert set(label(app, "rule")) == {"─"}
        reads = [(p, q) for _m, p, q in server.requests if p.startswith("/api/v1/todos")]
        assert reads[0] == ("/api/v1/todos", {"status": "all"})
        assert reads[1] == ("/api/v1/todos/rooms", {})


async def test_the_fold_unfolds_with_every_closed_item_newest_first() -> None:
    app = tui_app.PepperApp(own_server().client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        for _ in range(4):
            await pilot.press("down")
        await pilot.press("enter")
        text = pane_text(app)
        assert "▾ Closed (2)" in text
        assert text.index("Old room chore") < text.index("Old own chore")


async def test_the_top_level_rows_exist_without_rooms_and_open_your_notes() -> None:
    server = own_server()
    server.enabled = False
    server.rooms = []
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: "aren't enabled" in plain(app, "#notice"))
        app.sidebar.focus()
        app.sidebar.select_key("app:me:notes")
        await pilot.pause()
        await pilot.press("enter")
        await until(pilot, lambda: "Passport notes" in pane_text(app))
        assert header(app) == "Notes"
        text = pane_text(app)
        assert text.index("Passport notes") < text.index("Misc")
        assert "Photos" in text
        assert label(app, "app:me:notes") == "▌ Notes"
        sent = [q for _m, p, q in server.requests if p == "/api/v1/artifacts"]
        assert sent[0] == {"kind": "inline_text", "limit": "200"}  # yours: no principal


async def test_m_reads_both_lists_again_with_only_the_asks_made_of_you() -> None:
    server = own_server()
    server.own_todos.append(
        ts.todo(
            "o-asked", "Asked of me", source="request", assignee={"user_id": ts.ME, "label": "Me"}
        )
    )
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        await pilot.press("m")
        await until(pilot, lambda: "only asks of you" in pane_text(app))
        await until(pilot, lambda: "Renew passport" not in pane_text(app))
        text = pane_text(app)
        assert "Asked of me" in text
        assert "Book the dog sitter" in text
        assert "Todos  2 open" in text
        reads = [q for _m, p, q in server.requests if p.startswith("/api/v1/todos")]
        assert {"status": "all", "asked_of_me": "true"} in reads
        assert {"assignee": "me"} in reads


async def test_a_failed_rooms_read_keeps_your_items_with_one_line_and_r_recovers() -> None:
    server = own_server()
    server.fail["/api/v1/todos/rooms"] = 500
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        text = pane_text(app)
        assert "Renew passport" in text
        assert "Couldn't load your rooms' todos." in text
        assert "Book the dog sitter" not in text
        assert app.pane is not None
        assert app.pane.error is None  # the pane itself did not fail
        server.fail.clear()
        await pilot.press("r")
        await until(pilot, lambda: "Book the dog sitter" in pane_text(app))
        assert "Couldn't load your rooms'" not in pane_text(app)


async def test_a_degraded_rooms_answer_is_not_taken_for_no_todos() -> None:
    server = own_server()
    server.rooms_degraded = True
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        assert "Couldn't load your rooms' todos." in pane_text(app)


async def test_your_own_list_failing_shows_the_error_in_the_pane_and_keeps_it_open() -> None:
    server = own_server()
    server.fail["/api/v1/todos"] = 500
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.sidebar.select_key("app:me:todo")
        await pilot.press("enter")
        await until(pilot, lambda: "Couldn't load Todos" in pane_text(app))
        assert app.pane is not None


async def test_the_pepper_composer_stays_under_your_pane_with_a_placeholder_about_the_list() -> (
    None
):
    app = tui_app.PepperApp(own_server().client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        assert app.query_one("#compose-box").display
        assert app.composer.placeholder == "Ask Pepper about your todos…"
        assert not app.query_one("#reply-strip").display  # nothing sent yet
        await pilot.press("escape")
        await until(pilot, lambda: app.pane is None)
        assert app.composer.placeholder == PLACEHOLDER
        await open_own(app, pilot, "notes")
        assert app.composer.placeholder == "Ask Pepper about your notes…"


async def test_a_send_carries_the_subject_streams_into_the_strip_and_rereads_the_list() -> None:
    server = own_server()
    gate = threading.Event()
    server.turns = [reply_turn('Added "Buy stamps" to your todos.', gates={1: gate})]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        await say(pilot, app, "add buy stamps")
        await until(pilot, lambda: "Thinking: Looking into it" in plain(app, "#dm-status"))
        assert app.query_one("#reply-strip").display
        assert "add buy stamps" in strip(app)
        assert "Pepper" in strip(app)
        assert header(app) == "Todos"  # the pane stays; the reply did not switch to the DM
        # Pepper adds the item server-side; only the re-read at the turn's end can show it
        # (the 30s poll is far off).
        server.own_todos.append(ts.todo("o-stamps", "Buy stamps"))
        gate.set()
        await until(pilot, lambda: "Added" in strip(app))
        await until(pilot, lambda: "Buy stamps" in pane_text(app))
        assert app.pane is not None
        assert app.current == state.PEPPER
        assert server.turn_bodies == [{"message": "add buy stamps", "subject": "app:todo"}]
        # The exchange is an ordinary turn in the Pepper thread.
        await until(pilot, lambda: len(app.dm.entries) == 4)
        await pilot.press("escape")  # composer -> pane
        await pilot.press("escape")  # pane -> the DM
        await until(pilot, lambda: app.pane is None)
        assert "question 1" in body_text(app)
        assert 'Added "Buy stamps"' in body_text(app)
        assert not app.dm.dot


async def test_over_an_open_note_the_send_carries_the_note_id() -> None:
    server = own_server()
    server.turns = [reply_turn("It has two photos to take.")]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot, "notes")
        await pilot.press("enter")
        await until(pilot, lambda: "Take two photos" in pane_text(app))
        assert header(app) == "Notes › Passport notes"
        await say(pilot, app, "what do I need?")
        await until(pilot, lambda: "two photos to take" in strip(app))
        assert server.turn_bodies == [
            {
                "message": "what do I need?",
                "subject": "app:notes",
                "client_context": {"note_id": "m-pass"},
            }
        ]
        # Back on the list, the next send names the app alone.
        server.turns = [reply_turn("Two notes.", 2)]
        app.pane.note_id = None
        await say(pilot, app, "how many?")
        await until(pilot, lambda: "Two notes." in strip(app))
        assert server.turn_bodies[1] == {"message": "how many?", "subject": "app:notes"}


async def test_the_strip_holds_the_exchange_in_flight_and_the_one_before() -> None:
    server = own_server()
    server.turns = [reply_turn("first answer", 1), reply_turn("second answer", 2)]
    server.turns.append(reply_turn("third answer", 3))
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        for n, word in enumerate(("one", "two", "three"), 1):
            await say(pilot, app, f"ask {word}")
            await until(
                pilot, lambda n=n: f"{('first', 'second', 'third')[n - 1]} answer" in strip(app)
            )
            await until(pilot, lambda: not app.turns)
        text = strip(app)
        assert "ask one" not in text
        assert "ask two" in text
        assert "second answer" in text
        assert "ask three" in text
        assert "third answer" in text


async def test_the_strip_stops_waiting_to_send_once_the_turn_is_running() -> None:
    server = own_server()
    gate = threading.Event()
    server.turns = [reply_turn("an answer", gates={1: gate})]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        await say(pilot, app, "slow one")
        await until(pilot, lambda: "Looking into it" in plain(app, "#dm-status"))
        # Nothing but the turn's own start redraws the strip here (the pane polls every 30s).
        assert "waiting to send" not in strip(app)
        assert "slow one" in strip(app)
        gate.set()
        await until(pilot, lambda: "an answer" in strip(app))


async def test_a_dropped_stream_says_checking_in_the_strip_while_history_is_read() -> None:
    server = own_server()
    server.turns = [ts.ScriptedTurn(frames=[("model_thought", {"text": "Hmm"})], drop=True)]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        await say(pilot, app, "still there?")
        await until(pilot, lambda: bool(app.turns) and app.turns[0].phase == "checking")
        await pilot.pause()
        assert "checking for the answer" in strip(app)
        assert "waiting to send" not in strip(app)


async def test_closing_the_pane_clears_the_strip() -> None:
    server = own_server()
    server.turns = [reply_turn("an answer")]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        await say(pilot, app, "hello")
        await until(pilot, lambda: "an answer" in strip(app))
        await pilot.press("escape", "escape")
        await until(pilot, lambda: app.pane is None)
        await open_own(app, pilot)
        assert not app.query_one("#reply-strip").display
        assert "an answer" not in strip(app)


async def test_a_turn_already_running_when_the_pane_opens_does_not_appear_in_the_strip() -> None:
    server = own_server()
    gate = threading.Event()
    server.turns = [reply_turn("late answer", gates={1: gate})]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: len(app.dm.entries) == 2)
        await say(pilot, app, "from the DM")
        await until(pilot, lambda: "Thinking" in plain(app, "#dm-status"))
        await open_own(app, pilot)
        gate.set()
        await until(pilot, lambda: not app.turns)
        assert not app.query_one("#reply-strip").display
        # The pane hides the DM, so the reply it did not carry lights the Pepper dot (§6.6).
        await until(pilot, lambda: app.dm.dot)
        await until(pilot, lambda: label(app, "pepper").endswith(DOT))
        await pilot.press("escape", "escape")
        await until(pilot, lambda: "late answer" in body_text(app))
        assert not app.dm.dot
        await until(pilot, lambda: label(app, "pepper") == "Pepper")


async def test_a_reply_history_shows_before_its_frame_is_read_once_the_strip_has_it() -> None:
    # A DM read already in flight when the turn starts can come back holding the answer
    # while the turn still streams: that lights the dot, and the reply frame that puts the
    # answer in the strip must put it out again.
    await _race_a_read_past_the_frame(extra=[], dot_after=False)


async def test_an_unrelated_reply_keeps_the_dot_when_the_strip_shows_its_own() -> None:
    other = ts.history_entry("x9", "assistant", "from elsewhere", "2026-09-16T09:00:05Z")
    await _race_a_read_past_the_frame(extra=[other], dot_after=True)


async def _race_a_read_past_the_frame(*, extra: list[dict], dot_after: bool) -> None:
    server = own_server()
    frame_gate, read_gate = threading.Event(), threading.Event()
    turn = reply_turn("the answer", gates={1: frame_gate})
    rows, turn.history = turn.history, []
    server.turns = [turn]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        server.gates["/api/v1/chat/history"] = read_gate
        app._run("dm")  # held at the server
        await say(pilot, app, "a question")
        await until(pilot, lambda: "Looking into it" in plain(app, "#dm-status"))
        server.history.extend([*rows, *extra])
        read_gate.set()
        await until(pilot, lambda: "a1" in app.dm.entries)
        assert app.dm.dot
        del server.gates["/api/v1/chat/history"]
        frame_gate.set()
        await until(pilot, lambda: "the answer" in strip(app))
        await until(pilot, lambda: not app.turns)
        assert app.dm.dot is dot_after
        assert label(app, "pepper").endswith(DOT) is dot_after


async def test_a_refused_send_gives_the_text_back_and_leaves_no_strip_entry() -> None:
    server = own_server()
    server.fail["/api/v1/chat/send"] = 503
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        await say(pilot, app, "try this")
        await until(pilot, lambda: "Couldn't send" in plain(app, "#dm-status"))
        assert app.composer.text == "try this"
        assert not app.query_one("#reply-strip").display


async def test_an_error_frame_leaves_the_exchange_in_the_strip_without_an_answer() -> None:
    server = own_server()
    server.turns = [ts.ScriptedTurn(frames=[("error", {"message": "model service unavailable"})])]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        await say(pilot, app, "will fail")
        await until(pilot, lambda: "couldn't answer" in plain(app, "#dm-status"))
        assert "will fail" in strip(app)
        assert "(no answer)" in strip(app)


async def test_a_dropped_stream_fills_the_strip_from_history_when_the_reply_lands() -> None:
    server = own_server()
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
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        await say(pilot, app, "are we on")
        await until(pilot, lambda: "Yes, at noon." in strip(app))
        assert not app.turns


async def test_the_strip_shows_pepper_text_as_text_never_markup_or_escapes() -> None:
    server = own_server()
    server.turns = [reply_turn("[bold red]not styled[/] \x1b[31mred\x1b[0m and **bold**")]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        await say(pilot, app, "[link=http://x]me[/link] \x1b[2J")
        await until(pilot, lambda: "not styled" in strip(app))
        text = strip(app)
        assert "[bold red]not styled[/]" in text
        assert "[link=http://x]me[/link]" in text
        assert "\x1b" not in text
        assert "**bold**" not in text  # markdown-rendered


async def test_server_text_in_a_room_section_heading_is_never_markup() -> None:
    server = own_server()
    server.room_todos = {ts.ROOM_A: ("[bold]Fam[/bold]\x1b[31m", server.room_todos[ts.ROOM_A][1])}
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        assert "[bold]Fam[/bold]" in pane_text(app)
        assert "\x1b" not in pane_text(app)


async def test_tab_cycles_sidebar_pane_composer_under_your_pane() -> None:
    app = tui_app.PepperApp(own_server().client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        assert isinstance(app.focused, PaneScroll)
        await pilot.press("tab")
        assert app.focused is app.composer
        await pilot.press("tab")
        assert isinstance(app.focused, Sidebar)
        await pilot.press("tab")
        assert isinstance(app.focused, PaneScroll)
        await pilot.press("shift+tab")
        assert isinstance(app.focused, Sidebar)
        await pilot.press("shift+tab")
        assert app.focused is app.composer


async def test_the_footer_offers_the_composer_stop_only_under_your_own_pane() -> None:
    server = own_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        scroller = app.query_one(PaneScroll)
        assert scroller.check_action("composer", ())
        await pilot.press("escape", "escape")
        await until(pilot, lambda: app.pane is None)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: "flight options" in body_text(app))
        app.sidebar.focus()
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("1")
        await until(pilot, lambda: app.pane is not None and app.pane.loaded)
        assert not app.query_one(PaneScroll).check_action("composer", ())


async def test_esc_in_the_composer_moves_to_the_pane_and_never_closes_it() -> None:
    app = tui_app.PepperApp(own_server().client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        app.composer.focus()
        app.composer.insert("a half-written thought")
        await pilot.pause()
        await pilot.press("escape")
        assert isinstance(app.focused, PaneScroll)
        assert app.pane is not None
        assert app.composer.text == "a half-written thought"  # the DM draft is kept
        await pilot.press("escape")  # now on the pane's list: out to the DM
        await until(pilot, lambda: app.pane is None)
        assert app.current == state.PEPPER
        assert app.thread.display
        assert header(app) == "Pepper · Today"
        assert "hello there" in body_text(app)  # the DM's rows, not a stale topic
        assert app.composer.text == "a half-written thought"
        assert app.sidebar.highlighted_row is not None
        assert app.sidebar.highlighted_row.kind == "pepper"


async def test_esc_walks_note_then_list_then_the_pepper_dm() -> None:
    app = tui_app.PepperApp(own_server().client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot, "notes")
        await pilot.press("enter")
        await until(pilot, lambda: header(app) == "Notes › Passport notes")
        await pilot.press("escape")
        await until(pilot, lambda: header(app) == "Notes")
        assert app.pane is not None
        await pilot.press("escape")
        await until(pilot, lambda: app.pane is None)
        assert header(app) == "Pepper · Today"
        assert isinstance(app.focused, type(app.thread.scroller))


async def test_slash_todo_and_slash_notes_in_the_pepper_composer_open_your_panes() -> None:
    server = own_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: len(app.dm.entries) == 2)
        await say(pilot, app, "/notes")
        await until(pilot, lambda: app.pane is not None and app.pane.loaded)
        assert header(app) == "Notes"
        assert app.pane.owner is None
        assert app.composer.text == ""
        await say(pilot, app, "/todo")  # from under another pane of yours
        await until(pilot, lambda: app.pane is not None and app.pane.slug == "todo")
        await until(pilot, lambda: "Renew passport" in pane_text(app))
        assert header(app) == "Todos"
        assert server.turn_bodies == []  # neither was sent to Pepper


async def test_enter_on_the_open_rows_returns_to_the_list_and_reads_again() -> None:
    server = own_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot, "notes")
        await pilot.press("enter")
        await until(pilot, lambda: header(app) == "Notes › Passport notes")
        count = len([1 for _m, p, _q in server.requests if p == "/api/v1/artifacts"])
        app.sidebar.focus()
        app.sidebar.select_key("app:me:notes")
        await pilot.pause()
        await pilot.press("enter")
        await until(pilot, lambda: header(app) == "Notes")
        await until(
            pilot,
            lambda: len([1 for _m, p, _q in server.requests if p == "/api/v1/artifacts"]) > count,
        )


async def test_ctrl_k_lists_your_todos_and_notes_first_and_opens_your_pane() -> None:
    app = tui_app.PepperApp(own_server().client())
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await pilot.press("ctrl+k")
        await until(pilot, lambda: isinstance(app.screen, JumpScreen))
        assert [t.label for t in app.screen._shown[:3]] == ["Todos", "Notes", "Pepper"]
        await pilot.press(*"notes")
        await pilot.press("enter")
        await until(pilot, lambda: app.pane is not None and app.pane.loaded)
        assert header(app) == "Notes"
        assert app.current == state.PEPPER  # from a room's topic, via the DM
        assert app.query_one("#compose-box").display


async def test_opening_a_conversation_or_a_room_pane_closes_your_pane() -> None:
    app = tui_app.PepperApp(own_server().client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: "flight options" in body_text(app))
        assert app.pane is None
        assert app.composer.placeholder == PLACEHOLDER
        await open_own(app, pilot, "notes")
        assert app.current == state.PEPPER
        # The Pepper row closes it too, onto the DM's rows.
        app.open_conversation(state.PEPPER)
        await pilot.pause()
        assert app.pane is None
        assert "hello there" in body_text(app)


async def test_a_room_pane_still_hides_the_composer_and_has_no_strip_even_after_your_own() -> None:
    server = own_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: "flight options" in body_text(app))
        app.sidebar.focus()
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("1")
        await until(pilot, lambda: app.pane is not None and app.pane.loaded)
        assert header(app) == "Trip planning › Todos"
        assert not app.query_one("#compose-box").display
        assert not app.query_one("#reply-strip").display


async def test_the_list_is_read_again_whenever_a_pane_sent_turn_ends_and_not_for_a_dm_turn() -> (
    None
):
    server = own_server()
    server.turns = [reply_turn("done", 1), reply_turn("also done", 2)]
    app = tui_app.PepperApp(server.client())

    def todo_reads() -> int:
        return len([1 for _m, p, _q in server.requests if p == "/api/v1/todos"])

    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        before = todo_reads()
        await say(pilot, app, "tick the first")
        await until(pilot, lambda: todo_reads() > before)
        await until(pilot, lambda: not app.turns)
        await pilot.press("escape", "escape")
        await until(pilot, lambda: app.pane is None)
        settled = todo_reads()
        await say(pilot, app, "from the DM")
        await until(pilot, lambda: not app.turns and len(app.dm.entries) >= 6)
        assert todo_reads() == settled  # no pane is open: nothing polled, nothing re-read


async def test_a_dropped_stream_that_never_answers_ends_the_strips_waiting(monkeypatch) -> None:
    monkeypatch.setattr(state, "DM_RECONCILE_WINDOW", 0.2)
    server = own_server()
    server.turns = [ts.ScriptedTurn(drop=True)]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        await say(pilot, app, "anyone?")
        await until(pilot, lambda: "No reply yet" in plain(app, "#dm-status"))
        assert "checking for the answer" not in strip(app)
        # It may have run, so the strip doesn't say it went unanswered.
        assert "(no reply yet; it may still show in the DM)" in strip(app)
        assert "(no answer)" not in strip(app)


async def test_the_cursor_starts_on_pepper_not_on_the_first_row() -> None:
    app = tui_app.PepperApp(own_server().client())
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        await pilot.pause()
        assert app.sidebar.highlighted_row is not None
        assert app.sidebar.highlighted_row.kind == "pepper"


async def test_redraws_before_the_first_restore_still_rest_on_the_open_conversation() -> None:
    # Two redraws inside one refresh: the second must not keep the tree's default cursor
    # (the first row), and the first's queued restore must not act on replaced nodes.
    from textual.app import App, ComposeResult

    rows = [
        state.SidebarRow("app:me:todo", "own_app", "Todos", 0, slug="todo"),
        state.SidebarRow("rule", "rule", "────", 0),
        state.SidebarRow("pepper", "pepper", "Pepper", 0),
    ]

    class Host(App[None]):
        def compose(self) -> ComposeResult:
            yield Sidebar()

    host = Host()
    async with host.run_test(size=SIZE) as pilot:
        sidebar = host.query_one(Sidebar)
        sidebar.set_rows(rows, current_key="pepper")
        sidebar.set_rows(
            [*rows, state.SidebarRow("x", "inactive", "Older", 0)], current_key="pepper"
        )
        await pilot.pause()
        assert sidebar.highlighted_row is not None
        assert sidebar.highlighted_row.key == "pepper"
        sidebar.select_key("app:me:todo")
        sidebar.set_rows(rows, current_key="pepper")  # once placed, a redraw keeps the cursor
        await pilot.pause()
        assert sidebar.highlighted_row.key == "app:me:todo"


async def test_up_and_down_step_over_the_rule_under_your_own_rows() -> None:
    app = tui_app.PepperApp(own_server().client())
    async with app.run_test(size=SIZE) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        await until(pilot, lambda: getattr(app.sidebar.highlighted_row, "kind", None) == "pepper")
        app.sidebar.focus()
        await pilot.press("up")
        assert app.sidebar.highlighted_row is not None
        assert app.sidebar.highlighted_row.key == "app:me:notes"
        await pilot.press("down")
        assert app.sidebar.highlighted_row.kind == "pepper"


async def test_a_failed_reread_keeps_the_room_sections_already_on_screen() -> None:
    server = own_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        assert "Book the dog sitter" in pane_text(app)
        server.fail["/api/v1/todos/rooms"] = 500
        await pilot.press("r")
        await until(pilot, lambda: "Couldn't load your rooms' todos." in pane_text(app))
        assert "Book the dog sitter" in pane_text(app)
