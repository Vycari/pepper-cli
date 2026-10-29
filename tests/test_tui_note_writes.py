"""Note writes in the Notes pane (P3): new, edit through a suspended ``$EDITOR`` with the 412
resolution, delete and the checklist tick, for a room's notes and your own (Textual Pilot, fake
server; ``suspend`` and the editor are patched, since a headless run has no terminal to hand over).
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

from pepper_cli import note_edit
from pepper_cli.tui import app as tui_app
from pepper_cli.tui import state
from pepper_cli.tui.apps_pane import PaneScroll
from pepper_cli.tui.screens import ConfirmScreen, InfoScreen, PickerScreen
from tests import tui_support as ts
from tests.test_tui_apps import (
    SIZE,
    header,
    open_notes,
    open_todos,
    pane_text,
    server_with_apps,
)
from tests.test_tui_own_apps import open_own, own_server
from tests.tui_support import notices, until

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from textual.pilot import Pilot

PACK_BODY = "Bring layers, see https://example.com/gear\n\n- [ ] Sleeping bags\n- [x] Headlamps\n"


@pytest.fixture(autouse=True)
def _fast_clock(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(state, "ACTIVE_INTERVAL", 0.05)
    monkeypatch.setattr(state, "COOLING_INTERVAL", 0.05)
    monkeypatch.setattr(state, "IDLE_INTERVAL", 0.05)
    monkeypatch.setattr(state, "RAIL_INTERVAL", 0.05)
    monkeypatch.setattr(state, "DM_INTERVAL", 0.05)
    monkeypatch.setattr(state, "BACKOFF_STEPS", (0.05, 0.05, 0.05, 0.05))
    monkeypatch.setattr(tui_app, "utcnow", lambda: ts.NOW)
    monkeypatch.setenv("PEPPER_CONFLICT_DIR", str(tmp_path / "conflicts"))


@contextmanager
def _no_suspend(_self: object) -> Iterator[None]:
    yield


def fake_editor(
    monkeypatch: pytest.MonkeyPatch,
    *answers: str | tuple[str | None, str | None],
    during: Callable[[], None] | None = None,
) -> list[str]:
    """Stand in for ``$EDITOR``: each call returns the next answer and records what it was
    handed. ``during`` runs while the editor is "open" (another writer's save, say)."""
    handed: list[str] = []
    queue = list(answers)

    def open_in_editor(initial: str, *, prefix: str = "pepper-") -> tuple[str | None, str | None]:
        assert prefix == "pepper-note-"
        handed.append(initial)
        if during is not None:
            during()
        answer = queue.pop(0)
        return answer if isinstance(answer, tuple) else (answer, None)

    monkeypatch.setattr(tui_app.PepperApp, "suspend", _no_suspend)
    monkeypatch.setattr(note_edit, "open_in_editor", open_in_editor)
    return handed


def writes(server: ts.FakeServer, route: str) -> list[tuple[str | None, dict, str | None]]:
    return [(p, body, match) for r, p, body, match in server.note_writes if r == route]


def note_reads(server: ts.FakeServer) -> int:
    return sum(1 for _m, path, _q in server.requests if path.endswith("/n-pack/detail"))


def another_writer(server: ts.FakeServer) -> Callable[[], None]:
    """A save by someone else that lands while the editor is open."""

    def save() -> None:
        server.note_details["n-pack"].update(body="Someone else was here\n", etag="n-pack-2")
        server.notes[ts.ROOM_A][1]["etag"] = "n-pack-2"

    return save


async def screen_is(pilot: Pilot, app: tui_app.PepperApp, kind: type) -> None:
    await until(pilot, lambda: isinstance(app.screen, kind))


# ---------------------------------------------------------------------------------- edit


async def test_e_edits_a_rooms_note_in_the_editor_and_sends_if_match(monkeypatch) -> None:
    server = server_with_apps()
    handed = fake_editor(monkeypatch, PACK_BODY + "- [ ] Tent pegs\n")
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        said = notices(app)
        await pilot.press("e")
        await until(pilot, lambda: "Saved Packing list." in said)
        assert handed == [PACK_BODY]  # the fresh read's raw body
        assert writes(server, "edit") == [
            (ts.ROOM_A, {"name": None, "body": PACK_BODY + "- [ ] Tent pegs\n"}, '"n-pack-1"')
        ]
        # Re-read after the write: the list now carries the stored etag.
        await until(
            pilot,
            lambda: (
                app.pane is not None and any(n.etag == "n-pack-2" for n in app.pane.notes or [])
            ),
        )


async def test_e_in_an_open_note_shows_the_saved_text(monkeypatch) -> None:
    server = server_with_apps()
    fake_editor(monkeypatch, "Bring layers\n\n- [ ] Tent pegs\n")
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: "Sleeping bags" in pane_text(app))
        await pilot.press("e")
        await until(pilot, lambda: "Tent pegs" in pane_text(app))
        assert "Sleeping bags" not in pane_text(app)
        assert header(app) == "Trip planning › Notes › Packing list"


@pytest.mark.parametrize("unchanged", [PACK_BODY, PACK_BODY + "\n"])
async def test_an_unchanged_body_sends_nothing(monkeypatch, unchanged) -> None:
    server = server_with_apps()
    fake_editor(monkeypatch, unchanged)
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        said = notices(app)
        await pilot.press("e")
        await until(pilot, lambda: "No changes." in said)
        assert server.note_writes == []


async def test_an_emptied_body_sends_nothing(monkeypatch) -> None:
    server = server_with_apps()
    fake_editor(monkeypatch, "  \n")
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        said = notices(app)
        await pilot.press("e")
        await until(pilot, lambda: any("would be empty" in n for n in said))
        assert server.note_writes == []


async def test_an_editor_failure_is_a_notice_and_sends_nothing(monkeypatch) -> None:
    server = server_with_apps()
    fake_editor(monkeypatch, (None, "editor exited with status 1"))
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        said = notices(app)
        await pilot.press("e")
        await until(pilot, lambda: "editor exited with status 1" in said)
        assert server.note_writes == []


@pytest.mark.parametrize(
    "failure",
    [OSError(28, "No space left on device"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")],
)
async def test_an_editor_file_failure_is_a_notice_and_the_app_carries_on(
    monkeypatch, failure
) -> None:
    server = server_with_apps()
    fake_editor(monkeypatch)

    def broken(_initial: str, *, prefix: str = "pepper-") -> tuple[str | None, str | None]:
        raise failure

    monkeypatch.setattr(note_edit, "open_in_editor", broken)
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        said = notices(app)
        await pilot.press("e")
        await until(
            pilot, lambda: any(n.startswith("couldn't use the editor's file") for n in said)
        )
        assert writes(server, "edit") == []
        assert app.is_running


async def test_with_no_terminal_to_hand_over_the_notice_points_at_the_cli() -> None:
    """Under Pilot the driver cannot suspend, so the real ``App.suspend`` raises."""
    server = server_with_apps()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        said = notices(app)
        await pilot.press("e")
        await until(pilot, lambda: any("pepper notes edit" in n for n in said))
        await pilot.press("c")
        await until(pilot, lambda: any("pepper notes create" in n for n in said))
        assert server.note_writes == []


async def test_the_refusal_of_an_imported_note_is_shown_verbatim(monkeypatch) -> None:
    server = server_with_apps()
    sentence = "This note was imported and can't be edited here."
    server.note_write_error = (403, sentence)
    fake_editor(monkeypatch, "Changed\n")
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        said = notices(app)
        await pilot.press("e")
        await until(pilot, lambda: sentence in said)


async def test_a_failed_save_says_so(monkeypatch) -> None:
    server = server_with_apps()
    server.note_write_error = (500, "boom")
    fake_editor(monkeypatch, "Changed\n")
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        said = notices(app)
        await pilot.press("e")
        await until(pilot, lambda: "Couldn't save the note (HTTP 500)." in said)


# ---------------------------------------------------------------------------------- 412


async def conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path, choice: str, *answers: str
) -> tuple[ts.FakeServer, list[str], list[str]]:
    """Edit Packing list while someone else saves it, then answer the picker with ``choice``."""
    server = server_with_apps()
    handed = fake_editor(monkeypatch, "My edit\n", *answers, during=another_writer(server))
    app = tui_app.PepperApp(server.client())
    said: list[str] = []
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        said = notices(app)
        await pilot.press("e")
        await screen_is(pilot, app, InfoScreen)
        diff = "\n".join(
            str(w.content) for w in app.screen.query("Static") if hasattr(w, "content")
        )
        assert "That note has changed since you read it." in diff
        assert "-Someone else was here" in diff
        assert "+My edit" in diff
        # Your text was saved before anything else, as ``pepper notes edit`` does.
        saved = list((tmp_path / "conflicts").glob("packing-list.*.mine*.md"))
        assert [p.read_text() for p in saved] == ["My edit\n"]
        assert str(saved[0]) in diff
        await pilot.press("escape")
        await screen_is(pilot, app, PickerScreen)
        await pilot.press(choice)
        await until(pilot, lambda: not isinstance(app.screen, PickerScreen))
        await pilot.pause(0.3)
    return server, handed, said


async def test_412_overwrite_resends_the_text_against_the_new_etag(monkeypatch, tmp_path) -> None:
    server, handed, _said = await conflict(monkeypatch, tmp_path, "2")
    assert [m for _p, _b, m in writes(server, "edit")] == ['"n-pack-1"', "n-pack-2"]
    assert writes(server, "edit")[1][1]["body"] == "My edit\n"
    assert server.note_details["n-pack"]["body"] == "My edit\n"
    assert len(handed) == 1


async def test_412_reopen_runs_the_editor_on_the_users_text(monkeypatch, tmp_path) -> None:
    server, handed, _said = await conflict(monkeypatch, tmp_path, "1", "My edit, revised\n")
    assert handed[1] == "My edit\n"  # their own text, not the server's
    assert [m for _p, _b, m in writes(server, "edit")] == ['"n-pack-1"', "n-pack-2"]
    assert server.note_details["n-pack"]["body"] == "My edit, revised\n"


async def test_412_quit_keeps_the_saved_copy_and_sends_nothing_more(monkeypatch, tmp_path) -> None:
    server, _handed, said = await conflict(monkeypatch, tmp_path, "3")
    assert len(writes(server, "edit")) == 1
    assert any(n.startswith("Not saved. Your text is kept at ") for n in said)
    assert server.note_details["n-pack"]["body"] == "Someone else was here\n"


async def test_412_with_no_copy_saved_quit_does_not_claim_one(monkeypatch, tmp_path) -> None:
    server = server_with_apps()
    fake_editor(monkeypatch, "My edit\n", during=another_writer(server))
    monkeypatch.setattr(note_edit, "save_conflict_copy", lambda *_a: (None, "disk full"))
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        await pilot.press("e")
        await screen_is(pilot, app, InfoScreen)
        await pilot.press("escape")
        await screen_is(pilot, app, PickerScreen)
        labels = [label for _value, label in app.screen._choices]
        assert "Quit (your text is lost)" in labels
        assert not any("stays saved" in label for label in labels)


async def test_412_closing_the_picker_with_esc_is_quit(monkeypatch, tmp_path) -> None:
    server = server_with_apps()
    fake_editor(monkeypatch, "My edit\n", during=another_writer(server))
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        said = notices(app)
        await pilot.press("e")
        await screen_is(pilot, app, InfoScreen)
        await pilot.press("escape")
        await screen_is(pilot, app, PickerScreen)
        await pilot.press("escape")
        await until(pilot, lambda: any(n.startswith("Not saved.") for n in said))
        assert len(writes(server, "edit")) == 1


# ---------------------------------------------------------------------------------- new


async def test_c_creates_a_note_titled_by_its_first_non_blank_line(monkeypatch) -> None:
    server = server_with_apps()
    handed = fake_editor(monkeypatch, "\n\n  Camp menu \n\nPancakes\nBacon\n")
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        await pilot.press("c")
        await until(pilot, lambda: "Camp menu" in pane_text(app))
        assert handed == [""]
        assert writes(server, "new") == [
            (
                ts.ROOM_A,
                {"name": "Camp menu", "kind": "inline_text", "body": "Pancakes\nBacon"},
                None,
            )
        ]
        assert app.pane is not None
        assert app.pane.selected == "note:n-new-1"


@pytest.mark.parametrize(
    ("typed", "said_text"),
    [("\n  \n", "Nothing written"), ("Just a title\n", "needs some text")],
)
async def test_c_with_nothing_to_save_sends_nothing(monkeypatch, typed, said_text) -> None:
    server = server_with_apps()
    fake_editor(monkeypatch, typed)
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        said = notices(app)
        await pilot.press("c")
        await until(pilot, lambda: any(said_text in n for n in said))
        assert server.note_writes == []


# -------------------------------------------------------------------------------- delete


async def test_D_asks_first_then_deletes_the_selected_note(monkeypatch) -> None:
    server = server_with_apps()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        await pilot.press("D")
        await screen_is(pilot, app, ConfirmScreen)
        assert "Delete Packing list?" in str(app.screen.query("Static").first().content)
        await pilot.press("n")
        await pilot.pause(0.2)
        assert server.note_writes == []
        await pilot.press("D")
        await screen_is(pilot, app, ConfirmScreen)
        await pilot.press("y")
        await until(pilot, lambda: "Packing list" not in pane_text(app))
        assert writes(server, "delete") == [(ts.ROOM_A, {"params": {"id": "n-pack"}}, None)]
        assert "Cabin wifi" in pane_text(app)


async def test_D_in_an_open_note_deletes_it_and_returns_to_the_list() -> None:
    server = server_with_apps()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: "Sleeping bags" in pane_text(app))
        await pilot.press("D")
        await screen_is(pilot, app, ConfirmScreen)
        await pilot.press("y")
        await until(pilot, lambda: header(app) == "Trip planning › Notes")
        assert "Packing list" not in pane_text(app)


# --------------------------------------------------------------------- space, by pane


async def test_space_is_done_in_todos_and_a_tick_in_an_open_note_never_both() -> None:
    server = server_with_apps()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        await pilot.press("space")
        await until(pilot, lambda: len(server.todo_writes) == 1)
        assert server.todo_writes[0][0].endswith("/done")
        assert server.note_writes == []
    server = server_with_apps()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        said = notices(app)
        await open_notes(app, pilot)
        await pilot.press("space")  # the list, no note open: neither Done nor Tick is live
        await pilot.pause(0.2)
        assert not isinstance(app.screen, PickerScreen)
        assert "Select a todo first." not in said
        await pilot.press("enter")
        await until(pilot, lambda: "☐ Sleeping bags" in pane_text(app))
        await pilot.press("space")
        await screen_is(pilot, app, PickerScreen)
        await pilot.press("1")
        await until(pilot, lambda: "☑ Sleeping bags" in pane_text(app))
        assert len(writes(server, "toggle")) == 1
        assert server.todo_writes == []


async def test_a_topicless_rooms_notes_pane_writes_to_that_room(monkeypatch) -> None:
    server = server_with_apps()
    server.rooms[1]["topics"] = [
        ts.topic(
            ts.TOPIC_B1, "Old", "2026-09-15T10:00:00+00:00", archived_at="2026-10-01T00:00:00Z"
        )
    ]
    server.launchers[ts.ROOM_B] = [ts.tile("notes", "Notes", ts.ROOM_B)]
    server.notes[ts.ROOM_B] = [ts.note_item("b1", "Reading list", "2026-10-20T10:00:00+00:00")]
    fake_editor(monkeypatch, "Book ideas\n\nDune\n")
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_todos(app, pilot)
        app.sidebar.focus()
        app.sidebar.select_key(f"room:{ts.ROOM_B}")
        await pilot.pause()
        await pilot.press("a")
        await screen_is(pilot, app, PickerScreen)
        await pilot.press("1")
        await until(pilot, lambda: "Reading list" in pane_text(app))
        app.query_one(PaneScroll).focus()
        await pilot.press("c")
        await until(pilot, lambda: "Book ideas" in pane_text(app))
        assert [w[0] for w in writes(server, "new")] == [ts.ROOM_B]
        assert app.current == state.Conversation(ts.ROOM_B, None)
        assert header(app) == "Book club › Notes"


# --------------------------------------------------------------------------------- tick


async def test_space_ticks_the_line_picked_and_redraws_from_the_response() -> None:
    server = server_with_apps()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: "☐ Sleeping bags" in pane_text(app))
        await pilot.press("space")
        await screen_is(pilot, app, PickerScreen)
        await pilot.press("1")
        await until(pilot, lambda: "☑ Sleeping bags" in pane_text(app))
        assert writes(server, "toggle") == [
            (
                ts.ROOM_A,
                {
                    "index": 0,
                    "checked": True,
                    "text": "Sleeping bags",
                    "occurrence": 0,
                    "matches": 1,
                },
                None,
            )
        ]
        # ... and unticks a ticked one.
        await pilot.press("space")
        await screen_is(pilot, app, PickerScreen)
        await pilot.press("2")
        await until(pilot, lambda: "☐ Headlamps" in pane_text(app))
        assert writes(server, "toggle")[1][1]["checked"] is False


async def test_a_read_begun_before_a_tick_cannot_redraw_the_text_from_before() -> None:
    server = server_with_apps()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: "☐ Sleeping bags" in pane_text(app))
        before = dict(server.note_details["n-pack"])
        gate = threading.Event()
        server.gates["GET /api/v1/artifacts/n-pack/detail"] = gate
        app._reread_note()  # a re-read (the poll saw a new etag) that stalls on the way back
        await until(pilot, lambda: note_reads(server) >= 2)
        await pilot.press("space")
        await screen_is(pilot, app, PickerScreen)
        await pilot.press("1")
        await until(pilot, lambda: "☑ Sleeping bags" in pane_text(app))
        # The stalled read now answers with the note as it was before the tick.
        server.note_details["n-pack"] = before
        del server.gates["GET /api/v1/artifacts/n-pack/detail"]
        gate.set()
        await until(pilot, lambda: app._loops["note"].in_flight is None)
        await pilot.pause()
        assert "☑ Sleeping bags" in pane_text(app)


async def test_a_tick_answering_after_you_open_another_note_leaves_its_read_alone() -> None:
    server = server_with_apps()
    server.note_details["n-wifi"] = {
        **server.note_details["n-pack"],
        "id": "n-wifi",
        "name": "Cabin wifi",
        "etag": "n-wifi-1",
        "body": "Network lakeside\n",
    }
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: "☐ Sleeping bags" in pane_text(app))
        tick = threading.Event()
        server.gates["POST /api/v1/artifacts/n-pack/toggle"] = tick
        await pilot.press("space")
        await screen_is(pilot, app, PickerScreen)
        await pilot.press("1")
        await until(
            pilot, lambda: any(p.endswith("/n-pack/toggle") for _m, p, _q in server.requests)
        )
        # Back to the list and into the other note, whose read is held.
        await pilot.press("escape")
        await until(pilot, lambda: "Cabin wifi" in pane_text(app) and app.pane.note_id is None)
        read = threading.Event()
        server.gates["GET /api/v1/artifacts/n-wifi/detail"] = read
        await pilot.press("down", "enter")
        await until(pilot, lambda: app._loops["note"].in_flight is not None)
        # The tick answers first; the wifi read must still be the one the pane takes.
        del server.gates["POST /api/v1/artifacts/n-pack/toggle"]
        tick.set()
        await until(pilot, lambda: bool(writes(server, "toggle")))
        await pilot.pause(0.2)
        del server.gates["GET /api/v1/artifacts/n-wifi/detail"]
        read.set()
        await until(pilot, lambda: "Network lakeside" in pane_text(app))


async def test_a_409_shows_the_servers_sentence_and_reads_the_note_again() -> None:
    server = server_with_apps()
    sentence = "that note has changed — reload to see it as it is now."
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: "☐ Sleeping bags" in pane_text(app))
        said = notices(app)
        reads = note_reads(server)
        server.note_write_error = (409, sentence)
        server.note_details["n-pack"]["body"] = "Moved on\n\n- [ ] Tent pegs\n"
        await pilot.press("space")  # one line left in the note, but the pane still holds two
        await screen_is(pilot, app, PickerScreen)
        await pilot.press("1")
        await until(pilot, lambda: sentence in said)
        await until(pilot, lambda: "Tent pegs" in pane_text(app))
        assert note_reads(server) > reads


async def test_space_with_no_checklist_says_so() -> None:
    server = server_with_apps()
    server.note_details["n-pack"]["body"] = "Just words\n"
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_notes(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: "Just words" in pane_text(app))
        said = notices(app)
        await pilot.press("space")
        await until(pilot, lambda: "This note has no checklist." in said)
        assert server.note_writes == []


# ------------------------------------------------------------------------------ own notes


async def test_your_own_notes_write_without_a_principal(monkeypatch) -> None:
    server = own_server()
    handed = fake_editor(monkeypatch, "Take three photos\n\n- [ ] Form\n", "Visa\n\nforms\n")
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot, "notes")
        said = notices(app)
        await pilot.press("e")
        await until(pilot, lambda: "Saved Passport notes." in said)
        assert handed == ["Take two photos\n\n- [ ] Form\n"]
        assert writes(server, "edit") == [
            (None, {"name": None, "body": "Take three photos\n\n- [ ] Form\n"}, '"m-pass-1"')
        ]
        await pilot.press("enter")
        await until(pilot, lambda: "☐ Form" in pane_text(app))
        await pilot.press("space")  # a single task line: no picker
        await until(pilot, lambda: "☑ Form" in pane_text(app))
        assert writes(server, "toggle")[0][0] is None
        await pilot.press("D")
        await screen_is(pilot, app, ConfirmScreen)
        await pilot.press("y")
        await until(pilot, lambda: header(app) == "Notes")
        assert writes(server, "delete")[0][0] is None
        assert "Passport notes" not in pane_text(app)
        await pilot.press("c")
        await until(pilot, lambda: "Visa" in pane_text(app))
        assert writes(server, "new")[0][0] is None


async def test_the_note_keys_are_not_bound_on_the_todos_pane(monkeypatch) -> None:
    server = own_server()
    fake_editor(monkeypatch, "never\n")
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=SIZE) as pilot:
        await open_own(app, pilot, "todo")
        for key in ("c", "e", "D"):
            await pilot.press(key)
        await pilot.pause(0.2)
        assert server.note_writes == []
        assert not isinstance(app.screen, ConfirmScreen)
