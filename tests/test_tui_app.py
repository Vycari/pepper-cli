"""Headless tests of the terminal UI (Textual Pilot) against a fake server."""

from __future__ import annotations

import sys
import threading
from typing import TYPE_CHECKING, cast

import pytest
from textual import events
from textual.widgets import Static

from pepper_cli.tui import app as tui_app
from pepper_cli.tui import state
from tests import tui_support as ts
from tests.tui_support import body_text, plain, until

if TYPE_CHECKING:
    from textual.drivers.linux_driver import LinuxDriver


@pytest.fixture(autouse=True)
def _fast_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Poll quickly so a test does not wait out the real 3/10/15 second cadence."""
    monkeypatch.setattr(state, "ACTIVE_INTERVAL", 0.05)
    monkeypatch.setattr(state, "COOLING_INTERVAL", 0.05)
    monkeypatch.setattr(state, "IDLE_INTERVAL", 0.05)
    monkeypatch.setattr(state, "RAIL_INTERVAL", 0.05)
    monkeypatch.setattr(state, "DM_INTERVAL", 0.05)
    monkeypatch.setattr(state, "BACKOFF_STEPS", (0.05, 0.05, 0.05, 0.05))
    monkeypatch.setattr(tui_app, "utcnow", lambda: ts.NOW)


async def test_opens_on_the_pepper_dm_and_lists_rooms() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2 and len(app.dm.entries) == 2)
        assert "Pepper" in plain(app, "#header")
        assert "hello there" in body_text(app)
        keys = list(app.sidebar._by_key)
        # Your own Todos and Notes, a rule, then Pepper, then the rooms.
        assert keys[:4] == ["app:me:todo", "app:me:notes", "rule", "pepper"]
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
        # Your own rows need no rooms: they stay when rooms are off.
        assert list(app.sidebar._by_key) == ["app:me:todo", "app:me:notes", "rule", "pepper"]


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


# --------------------------------------------------------------------------- failure paths


def _read_path(room_id: str, topic_id: str) -> str:
    return f"/api/v1/groups/{room_id}/topics/{topic_id}/read"


def _messages_path(room_id: str, topic_id: str) -> str:
    return f"/api/v1/groups/{room_id}/topics/{topic_id}/messages"


def _count(server: ts.FakeServer, path: str) -> int:
    return sum(1 for _, p, _ in server.requests if p == path)


async def test_startup_retries_whoami_after_a_transient_failure() -> None:
    server = ts.standard_server()
    server.fail["/api/v1/whoami"] = 503
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: "offline, retrying" in plain(app, "#header"))
        assert app.rooms == []
        server.fail.clear()
        await until(pilot, lambda: len(app.rooms) == 2 and len(app.dm.entries) == 2)
        assert "offline" not in plain(app, "#header")
        assert app.you_person_id == ts.ME_PERSON


async def test_a_426_shows_the_upgrade_notice_and_a_keypress_exits_1() -> None:
    server = ts.standard_server()
    server.fail["/api/v1/chat/history"] = 426
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: app.fatal is not None)
        assert "synthetic failure" in plain(app, "#notice")
        await pilot.press("x")
    assert app.return_code == 1


async def test_a_rail_poll_in_flight_when_focus_returns_is_not_duplicated_or_lost() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        gate = threading.Event()
        server.gates["/api/v1/groups"] = gate
        server.messages[ts.TOPIC_B1].append(ts.message("m-new", "2026-09-16T18:00:00+00:00", "hi"))
        server.rooms[1]["topics"][0]["last_activity_at"] = "2026-09-16T18:00:00+00:00"
        server.bump()
        before = _count(server, "/api/v1/groups")
        await until(pilot, lambda: _count(server, "/api/v1/groups") == before + 1)
        app.on_app_focus(events.AppFocus())  # the held request is still in flight
        app.on_app_focus(events.AppFocus())
        await pilot.pause(0.1)
        assert _count(server, "/api/v1/groups") == before + 1
        del server.gates["/api/v1/groups"]
        gate.set()
        # Its result is applied (not dropped as stale behind a saved ETag): room B lights.
        await until(
            pilot,
            lambda: state.any_unread(app.rooms, app.marks, pepper_dot=False),
        )


async def test_switching_topics_cancels_the_old_topic_poll() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: "flight options" in body_text(app))
        old = _messages_path(ts.ROOM_A, ts.TOPIC_A1)
        new = _messages_path(ts.ROOM_A, ts.TOPIC_A2)
        gate = threading.Event()
        server.gates[new] = gate
        # A poll of the old topic started just before the switch would reach the fake server
        # (and be counted) after the switch, on its worker thread. So switch only while none is
        # in flight, with no await in between: no timer can fire, and every old request the
        # server will ever see is already counted.
        await until(pilot, lambda: not app._loops["topic"].busy)
        old_count = _count(server, old)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A2))
        await until(pilot, lambda: _count(server, new) == 1)
        await pilot.pause(0.3)  # several topic intervals: no timer for either topic fires
        assert _count(server, new) == 1
        assert _count(server, old) == old_count
        del server.gates[new]
        gate.set()
        await until(pilot, lambda: "hotel options" in body_text(app))
        assert "flight options" not in body_text(app)


async def test_a_mark_that_failed_after_leaving_the_topic_is_retried() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        server.fail[_read_path(ts.ROOM_A, ts.TOPIC_A1)] = 500
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: ts.TOPIC_A1 in app._owed)
        app.open_conversation(state.PEPPER)  # nothing polls that topic any more
        await until(pilot, lambda: _count(server, _read_path(ts.ROOM_A, ts.TOPIC_A1)) >= 2)
        server.fail.clear()
        await until(pilot, lambda: not app._owed)
        assert (ts.TOPIC_A1, "2026-09-16T17:30:00+00:00") in server.puts


async def test_a_forbidden_topic_does_not_block_the_other_marks() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        gate = threading.Event()
        forbidden = _read_path(ts.ROOM_A, ts.TOPIC_A1)
        server.gates[forbidden] = gate
        server.fail[forbidden] = 403
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: _count(server, forbidden) == 1)
        app.open_conversation(state.Conversation(ts.ROOM_B, ts.TOPIC_B1))
        await until(pilot, lambda: "next book" in body_text(app))
        app.open_conversation(state.PEPPER)
        assert list(app._owed) == [ts.TOPIC_A1, ts.TOPIC_B1]
        del server.gates[forbidden]
        gate.set()
        await until(pilot, lambda: (ts.TOPIC_B1, "2026-09-15T10:00:00+00:00") in server.puts)
        await until(pilot, lambda: not app._owed)
        assert _count(server, forbidden) == 1  # dropped, never retried
        assert "offline" not in plain(app, "#header")


async def test_a_failing_mark_goes_to_the_back_of_the_queue() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        gate = threading.Event()
        flaky = _read_path(ts.ROOM_A, ts.TOPIC_A1)
        server.gates[flaky] = gate
        server.fail[flaky] = 503
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: _count(server, flaky) == 1)
        app.open_conversation(state.Conversation(ts.ROOM_B, ts.TOPIC_B1))
        await until(pilot, lambda: "next book" in body_text(app))
        app.open_conversation(state.PEPPER)
        del server.gates[flaky]
        gate.set()
        await until(pilot, lambda: (ts.TOPIC_B1, "2026-09-15T10:00:00+00:00") in server.puts)
        assert ts.TOPIC_A1 in app._owed  # still owed, retried with backoff
        server.fail.clear()
        await until(pilot, lambda: not app._owed)


async def test_rows_that_land_while_blurred_are_not_marked_read_until_focus() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: (ts.TOPIC_A1, "2026-09-16T17:30:00+00:00") in server.puts)
        path = _messages_path(ts.ROOM_A, ts.TOPIC_A1)
        gate = threading.Event()
        server.gates[path] = gate
        held = _count(server, path)
        await until(pilot, lambda: _count(server, path) == held + 1)  # a poll is in flight
        app.on_app_blur(events.AppBlur())
        server.messages[ts.TOPIC_A1].append(message_at("m8", "2026-09-16T19:00:00+00:00"))
        del server.gates[path]
        gate.set()
        await until(pilot, lambda: "m8" in app.store.rows)
        await pilot.pause(0.3)
        late = "2026-09-16T19:00:00+00:00"
        assert (ts.TOPIC_A1, late) not in server.puts
        assert ts.TOPIC_A1 not in app._owed
        assert _count(server, path) == held + 1  # and the topic poll is paused while blurred
        app.on_app_focus(events.AppFocus())
        await until(pilot, lambda: (ts.TOPIC_A1, late) in server.puts)


async def test_the_new_marker_clears_however_the_bottom_is_reached() -> None:
    server = ts.standard_server()
    server.messages[ts.TOPIC_A1] = [
        ts.message(f"r{i:02d}", f"2026-09-16T17:{i:02d}:00+00:00", f"row {i}") for i in range(40)
    ]
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 20)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: "row 39" in body_text(app))
        scroller = app.thread.scroller
        await until(pilot, lambda: scroller.max_scroll_y > 0 and app.thread.pinned)
        # Pause polling, so only the poll this test drives can refresh the thread.
        app.on_app_blur(events.AppBlur())
        await until(pilot, lambda: not app._loops["topic"].busy)
        await pilot.pause(0.1)
        scroller.scroll_home(animate=False)
        await until(pilot, lambda: not app.thread.pinned)
        server.messages[ts.TOPIC_A1].append(message_at("r99", "2026-09-16T18:30:00+00:00"))
        app._run("topic")
        marker = app.query_one("#thread-new", Static)
        await until(pilot, lambda: bool(marker.display))
        assert not app.thread.pinned
        scroller.scroll_end(animate=False)  # not a page key: a wheel or scrollbar would do this
        await until(pilot, lambda: not marker.display)


async def test_a_topic_retry_survives_blur_and_resumes_after_focus() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        path = _messages_path(ts.ROOM_A, ts.TOPIC_A1)
        server.fail[path] = 503
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: app._loops["topic"].failures >= 1)
        app.on_app_blur(events.AppBlur())
        server.fail.clear()
        # The backoff retry still fires while blurred, and the loop recovers.
        await until(pilot, lambda: app._loops["topic"].failures == 0)
        await until(pilot, lambda: "flight options" in body_text(app))
        # A failing loop whose timer was lost is re-armed by focus.
        server.fail[path] = 503
        app.on_app_focus(events.AppFocus())
        await until(pilot, lambda: app._loops["topic"].failures >= 1)
        await until(pilot, lambda: not app._loops["topic"].busy)
        app._loops["topic"].stop_timer()
        server.fail.clear()
        app.on_app_focus(events.AppFocus())
        await until(pilot, lambda: app._loops["topic"].failures == 0)


async def test_a_mark_dropped_after_a_retry_clears_the_offline_banner() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        path = _read_path(ts.ROOM_A, ts.TOPIC_A1)
        server.fail[path] = 503
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: app._loops["publish"].failures >= 1)
        app.open_conversation(state.PEPPER)
        server.fail[path] = 404
        await until(pilot, lambda: not app._owed)
        await until(pilot, lambda: "offline" not in plain(app, "#header"))
        assert app._loops["publish"].failures == 0


async def test_an_empty_queue_clears_a_stale_publish_failure() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app._loops["publish"].failures = 2
        app._run("publish")
        assert app._loops["publish"].failures == 0
        await until(pilot, lambda: "offline" not in plain(app, "#header"))


async def test_a_result_that_lands_after_quit_is_ignored() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        await pilot.press("ctrl+q")
    generation = app._loops["dm"].generation
    app._delivered("dm", app._on_dm, [], generation)  # would query a torn-down screen
    app._failed("dm", ValueError("late"), generation, None)
    app._update_subline()
    assert app.return_code == 0


async def test_a_result_that_lands_while_the_screen_is_torn_down_is_ignored() -> None:
    """#76: a result queued behind Textual's shutdown runs with some widgets gone.

    ``App._shutdown`` clears ``is_running`` first and then closes the screen's widgets one
    by one, so a callback can find the sidebar still mounted and the header already gone.
    """
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        # The state _shutdown leaves for the callbacks behind it. Left in place: restoring
        # is_running with the header gone would let a late poll result hit the same query.
        app._running = False
        app.query_one("#header").remove()
        await pilot.pause()
        generation = app._loops["dm"].generation
        app._delivered("dm", app._on_dm, [], generation)  # would query the missing header


@pytest.mark.skipif(sys.platform == "win32", reason="LinuxDriver imports termios")
async def test_terminal_app_keeps_the_mouse_in_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    """Terminal.app gets no in-band resize query, so Textual never turns on pixel mice."""
    server = ts.standard_server()

    def query_written(term_program: str) -> list[str]:
        monkeypatch.setenv("TERM_PROGRAM", term_program)
        app = tui_app.PepperApp(server.client())
        driver = cast("LinuxDriver", app.get_driver_class()(app))
        written: list[str] = []
        monkeypatch.setattr(driver, "write", written.append)
        driver._query_in_band_window_resize()
        return written

    assert query_written("Apple_Terminal") == []
    assert query_written("ghostty") == ["\x1b[?2048$p"]
