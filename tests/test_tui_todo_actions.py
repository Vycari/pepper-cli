"""Todo writes from the Todos pane (P2): done, dismiss, reopen and add, on a room's list and on
your own Todos with its room sections (Textual Pilot, fake server)."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest
from textual.widgets import Input

from pepper_cli.tui import app as tui_app
from pepper_cli.tui import state
from pepper_cli.tui.screens import PickerScreen, PromptScreen
from tests import tui_support as ts
from tests.test_tui_apps import SIZE, open_notes, open_todos, pane_text, server_with_apps
from tests.test_tui_own_apps import open_own, own_server
from tests.tui_support import notices, until

if TYPE_CHECKING:
    from textual.pilot import Pilot

ROOM_TODOS = f"/api/v1/groups/{ts.ROOM_A}/todos"
ROSTER = [
    {"person_id": "p-pepper", "display_name": "Pepper", "is_pepper": True},
    {"person_id": ts.ME_PERSON, "display_name": "Me"},
    {"person_id": "p-sam", "display_name": "Sam Roe"},
    {"person_id": "p-bot", "display_name": "Helper", "is_agent": True},
]


@pytest.fixture(autouse=True)
def _fast_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state, "ACTIVE_INTERVAL", 0.05)
    monkeypatch.setattr(state, "COOLING_INTERVAL", 0.05)
    monkeypatch.setattr(state, "IDLE_INTERVAL", 0.05)
    monkeypatch.setattr(state, "RAIL_INTERVAL", 0.05)
    monkeypatch.setattr(state, "DM_INTERVAL", 0.05)
    monkeypatch.setattr(state, "BACKOFF_STEPS", (0.05, 0.05, 0.05, 0.05))
    monkeypatch.setattr(tui_app, "utcnow", lambda: ts.NOW)


def room_server() -> ts.FakeServer:
    server = server_with_apps()
    server.rosters[ts.ROOM_A] = ROSTER
    return server


def reads(server: ts.FakeServer, path: str) -> int:
    return sum(1 for m, p, _q in server.requests if m == "GET" and p == path)


async def select(pilot: Pilot, steps: int) -> None:
    for _ in range(steps):
        await pilot.press("down")


async def fill(pilot: Pilot, app: tui_app.PepperApp, text: str) -> None:
    await until(pilot, lambda: isinstance(app.screen, PromptScreen))
    app.screen.query_one(Input).value = text
    await pilot.press("enter")


async def test_space_marks_a_room_todo_done_applies_the_answer_then_rereads() -> None:
    server = room_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        before = reads(server, ROOM_TODOS)
        await pilot.press("space")
        await until(pilot, lambda: "Todos  2 open" in pane_text(app))
        assert server.todo_writes == [(f"{ROOM_TODOS}/t-sitter/done", {})]
        assert "▸ Closed (3)" in pane_text(app)
        await until(pilot, lambda: reads(server, ROOM_TODOS) > before)
        # The cursor stayed at its place: the next todo, not the top of the list.
        assert app.pane is not None and app.pane.selected == "todo:t-cooler"


async def test_x_is_done_too_and_d_dismisses() -> None:
    server = room_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        await pilot.press("x")
        await until(pilot, lambda: len(server.todo_writes) == 1)
        await until(pilot, lambda: app.pane is not None and app.pane.selected == "todo:t-cooler")
        await pilot.press("d")
        await until(pilot, lambda: len(server.todo_writes) == 2)
        assert server.todo_writes[1][0] == f"{ROOM_TODOS}/t-cooler/dismiss"
        await until(pilot, lambda: "▸ Closed (4)" in pane_text(app))


async def test_u_reopens_a_closed_todo_from_the_fold() -> None:
    server = room_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        await select(pilot, 3)  # sitter, cooler, books, then the Closed fold
        await pilot.press("enter")
        await select(pilot, 1)
        assert app.pane is not None and app.pane.selected == "todo:t-done"
        await pilot.press("u")
        await until(pilot, lambda: "Todos  4 open" in pane_text(app))
        assert server.todo_writes == [(f"{ROOM_TODOS}/t-done/reopen", {})]


async def test_the_row_shows_a_pending_mark_until_the_answer_lands() -> None:
    server = room_server()
    gate = threading.Event()
    server.gates[f"POST {ROOM_TODOS}/t-sitter/done"] = gate
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        await pilot.press("space")
        await until(pilot, lambda: "… Book the dog sitter" in pane_text(app))
        assert "Todos  3 open" in pane_text(app)  # nothing is assumed before the answer
        await pilot.press("space")  # a second press while pending changes nothing
        gate.set()
        await until(pilot, lambda: "Todos  2 open" in pane_text(app))
        assert "… Book the dog sitter" not in pane_text(app)
        assert len(server.todo_writes) == 1


async def test_a_second_write_while_one_is_saving_is_refused_with_a_notice() -> None:
    server = room_server()
    gate = threading.Event()
    server.gates[f"POST {ROOM_TODOS}/t-sitter/done"] = gate
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        seen = notices(app)
        await open_todos(app, pilot)
        await pilot.press("space")
        await until(pilot, lambda: "… Book the dog sitter" in pane_text(app))
        await select(pilot, 1)
        await pilot.press("space")
        assert any("Still saving" in n for n in seen)
        gate.set()
        await until(pilot, lambda: "Todos  2 open" in pane_text(app))
        assert len(server.todo_writes) == 1


async def test_a_failed_write_restores_the_row_and_shows_the_servers_message() -> None:
    server = room_server()
    server.fail[f"POST {ROOM_TODOS}/t-sitter/done"] = 500
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        seen = notices(app)
        await open_todos(app, pilot)
        await pilot.press("space")
        await until(pilot, lambda: any("Couldn't mark the todo done" in n for n in seen))
        assert "○ Book the dog sitter" in pane_text(app)
        assert "… Book" not in pane_text(app)
        assert "Todos  3 open" in pane_text(app)
        assert app.pane is not None and not app.pane.pending


async def test_the_wrong_key_for_a_rows_status_says_so() -> None:
    server = room_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        seen = notices(app)
        await open_todos(app, pilot)
        await pilot.press("u")
        assert any("already open" in n for n in seen)
        await select(pilot, 3)
        await pilot.press("enter")
        await select(pilot, 1)
        await pilot.press("d")
        assert any("is closed" in n for n in seen)
        assert server.todo_writes == []


async def test_a_key_with_no_todo_selected_says_so() -> None:
    server = room_server()
    server.todos[ts.ROOM_A] = []
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        seen = notices(app)
        await until(pilot, lambda: len(app.rooms) == 2)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await pilot.press("a")
        await until(pilot, lambda: isinstance(app.screen, PickerScreen))
        await pilot.press("1")
        await until(pilot, lambda: app.pane is not None and app.pane.loaded)
        await pilot.press("space")
        assert any("Select a todo" in n for n in seen)


async def test_n_adds_a_room_todo_and_can_ask_a_member() -> None:
    server = room_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        await pilot.press("n")
        await fill(pilot, app, "Buy ice")
        await fill(pilot, app, "2026-11-05")
        await until(pilot, lambda: isinstance(app.screen, PickerScreen))
        # Nobody first; then the members, without Pepper, an agent or you.
        assert [v for v, _ in app.screen._choices] == ["", "p-sam"]
        await pilot.press("2")
        await until(pilot, lambda: "Buy ice" in pane_text(app))
        assert server.todo_writes == [
            (ROOM_TODOS, {"title": "Buy ice", "due": "2026-11-05", "assignee_person_id": "p-sam"})
        ]
        await until(pilot, lambda: "asked of Sam by you" in pane_text(app))
        assert "… Buy ice" not in pane_text(app)


async def test_a_new_row_is_pending_until_the_server_answers() -> None:
    server = room_server()
    gate = threading.Event()
    server.gates[f"POST {ROOM_TODOS}"] = gate
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        await pilot.press("n")
        await fill(pilot, app, "Buy ice")
        await fill(pilot, app, "")
        await until(pilot, lambda: isinstance(app.screen, PickerScreen))
        await pilot.press("1")
        await until(pilot, lambda: "… Buy ice" in pane_text(app))
        gate.set()
        await until(pilot, lambda: "○ Buy ice" in pane_text(app))
        assert app.pane is not None and not app.pane.pending
        assert server.todo_writes == [(ROOM_TODOS, {"title": "Buy ice"})]


async def test_a_reread_during_a_slow_add_keeps_the_pending_row() -> None:
    server = room_server()
    gate = threading.Event()
    server.gates[f"POST {ROOM_TODOS}"] = gate
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        await pilot.press("n")
        await fill(pilot, app, "Buy ice")
        await fill(pilot, app, "")
        await until(pilot, lambda: isinstance(app.screen, PickerScreen))
        await pilot.press("1")
        await until(pilot, lambda: "… Buy ice" in pane_text(app))
        before = reads(server, ROOM_TODOS)
        await pilot.press("r")
        await until(pilot, lambda: reads(server, ROOM_TODOS) > before)
        await until(pilot, lambda: not app._loops["apps"].busy)
        assert "… Buy ice" in pane_text(app)
        gate.set()
        await until(pilot, lambda: "○ Buy ice" in pane_text(app))


async def test_a_failed_add_removes_the_placeholder() -> None:
    server = room_server()
    server.fail[f"POST {ROOM_TODOS}"] = 500
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        seen = notices(app)
        await open_todos(app, pilot)
        await pilot.press("n")
        await fill(pilot, app, "Buy ice")
        await fill(pilot, app, "")
        await until(pilot, lambda: isinstance(app.screen, PickerScreen))
        await pilot.press("1")
        await until(pilot, lambda: any("Couldn't add the todo" in n for n in seen))
        assert "Buy ice" not in pane_text(app)
        assert "Todos  3 open" in pane_text(app)


async def test_a_room_due_date_is_a_day_and_a_bad_one_asks_again() -> None:
    server = room_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        seen = notices(app)
        await open_todos(app, pilot)
        await pilot.press("n")
        await fill(pilot, app, "Buy ice")
        await fill(pilot, app, "2026-11-05 10:00")  # a time is not a room's rule
        await until(pilot, lambda: any("a due date is a day" in n for n in seen))
        await fill(pilot, app, "2026-13-45")
        await fill(pilot, app, "2026-11-05")
        await until(pilot, lambda: isinstance(app.screen, PickerScreen))
        await pilot.press("escape")  # Esc at the member step cancels the add
        await pilot.pause()
        assert server.todo_writes == []
        assert "Buy ice" not in pane_text(app)


async def test_escape_in_a_prompt_cancels_the_add_and_the_title_is_capped() -> None:
    server = room_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        await pilot.press("n")
        await until(pilot, lambda: isinstance(app.screen, PromptScreen))
        assert app.screen.query_one(Input).max_length == state.TITLE_MAX == 500
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("n")
        await fill(pilot, app, "Buy ice")
        await until(pilot, lambda: isinstance(app.screen, PromptScreen))
        await pilot.press("escape")
        await pilot.pause()
        assert server.todo_writes == []


async def test_a_room_without_other_members_adds_without_the_picker() -> None:
    server = room_server()
    server.rosters[ts.ROOM_A] = ROSTER[:2]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        await pilot.press("n")
        await fill(pilot, app, "Buy ice")
        await fill(pilot, app, "")
        await until(pilot, lambda: bool(server.todo_writes))
        assert server.todo_writes == [(ROOM_TODOS, {"title": "Buy ice"})]


async def test_the_roster_is_read_when_the_add_needs_it_and_a_failure_stops_the_add() -> None:
    server = room_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        seen = notices(app)
        await open_todos(app, pilot)
        app.rosters.pop(ts.ROOM_A)
        server.fail[f"GET /api/v1/groups/{ts.ROOM_A}/roster"] = 500
        await pilot.press("n")
        await fill(pilot, app, "Buy ice")
        await fill(pilot, app, "")
        await until(pilot, lambda: any("load the room's members" in n for n in seen))
        assert server.todo_writes == []
        server.fail.clear()
        await pilot.press("n")
        await fill(pilot, app, "Buy ice")
        await fill(pilot, app, "")
        await until(pilot, lambda: isinstance(app.screen, PickerScreen))
        assert ts.ROOM_A in app.rosters


async def test_a_second_add_waits_for_the_first_ones_roster_read() -> None:
    server = room_server()
    roster = f"GET /api/v1/groups/{ts.ROOM_A}/roster"
    gate = threading.Event()
    server.gates[roster] = gate
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        seen = notices(app)
        await open_todos(app, pilot)
        app.rosters.pop(ts.ROOM_A, None)
        await pilot.press("n")
        await fill(pilot, app, "Buy ice")
        await fill(pilot, app, "")
        await until(pilot, lambda: app._loops["todo"].busy)
        await pilot.press("n")
        await until(pilot, lambda: any("Still saving the last change" in n for n in seen))
        assert not isinstance(app.screen, PromptScreen)
        gate.set()
        await until(pilot, lambda: isinstance(app.screen, PickerScreen))
        await pilot.press("1")
        await until(pilot, lambda: "○ Buy ice" in pane_text(app))
        assert server.todo_writes == [(ROOM_TODOS, {"title": "Buy ice"})]


async def test_with_your_person_id_unknown_the_add_skips_the_picker_and_the_roster() -> None:
    server = room_server()
    roster = f"/api/v1/groups/{ts.ROOM_A}/roster"
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        app.rosters.pop(ts.ROOM_A, None)
        app.you_person_id = None  # an older server: whoami carries no person_id
        before = reads(server, roster)
        await pilot.press("n")
        await fill(pilot, app, "Buy ice")
        await fill(pilot, app, "")
        await until(pilot, lambda: "○ Buy ice" in pane_text(app))
        assert not isinstance(app.screen, PickerScreen)
        assert reads(server, roster) == before
        assert server.todo_writes == [(ROOM_TODOS, {"title": "Buy ice"})]


async def test_notes_has_none_of_the_todo_keys() -> None:
    server = room_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        for key in ("n", "d", "u", "x", "space"):
            await pilot.press(key)
        assert not isinstance(app.screen, PromptScreen)
        assert server.todo_writes == []


async def test_your_own_todos_write_to_your_list_and_your_rooms_rows_to_their_room() -> None:
    server = own_server()
    server.todos[ts.ROOM_A] = []
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        await pilot.press("space")  # Renew passport: your own
        await until(pilot, lambda: len(server.todo_writes) == 1)
        assert server.todo_writes[0][0] == "/api/v1/todos/o-passport/done"
        await until(pilot, lambda: "Todos  2 open" in pane_text(app))
        # Cursor now on Call the dentist; next is the room's section row.
        await select(pilot, 1)
        assert app.pane is not None and app.pane.selected == "todo:r-sitter"
        await pilot.press("d")
        await until(pilot, lambda: len(server.todo_writes) == 2)
        assert server.todo_writes[1][0] == f"{ROOM_TODOS}/r-sitter/dismiss"


async def test_a_rooms_closed_item_on_your_todos_reopens_in_its_room() -> None:
    server = own_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        await select(pilot, 3)  # passport, dentist, sitter, the fold
        assert app.pane is not None and app.pane.selected == "fold"
        await pilot.press("enter")
        await select(pilot, 1)
        # Closed, newest first: the room's chore (Oct 5) before your own (Oct 2).
        assert app.pane.selected == "todo:r-done"
        await pilot.press("u")
        await until(pilot, lambda: len(server.todo_writes) == 1)
        assert server.todo_writes[0][0] == f"{ROOM_TODOS}/r-done/reopen"
        await until(pilot, lambda: "Todos  4 open" in pane_text(app))


async def test_n_on_your_own_todos_always_adds_to_your_list_without_asking_anyone() -> None:
    server = own_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot)
        await select(pilot, 2)  # on the room's section row: still your own list
        assert app.pane is not None and app.pane.selected == "todo:r-sitter"
        await pilot.press("n")
        await fill(pilot, app, "Water plants")
        await fill(pilot, app, "2026-11-05 10:00")
        await until(pilot, lambda: bool(server.todo_writes))
        assert not isinstance(app.screen, PickerScreen)
        path, body = server.todo_writes[0]
        assert path == "/api/v1/todos"
        assert body["title"] == "Water plants"
        assert "assignee_person_id" not in body
        assert body["due_date"].startswith("2026-11-05T10:00:00")
        assert body["due_date"].endswith(("Z", "+00:00")) or body["due_date"][-6] in "+-"
        await until(pilot, lambda: "○ Water plants" in pane_text(app))


async def test_n_before_the_list_has_loaded_says_so() -> None:
    server = own_server()
    gate = threading.Event()
    server.gates["/api/v1/todos"] = gate
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        seen = notices(app)
        await until(pilot, lambda: len(app.rooms) == 2 and len(app.dm.entries) >= 2)
        app.sidebar.focus()
        app.sidebar.select_key("app:me:todo")
        await pilot.press("enter")
        await until(pilot, lambda: app.pane is not None)
        await pilot.press("n")
        assert any("still loading" in n for n in seen)
        gate.set()
