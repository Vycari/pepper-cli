"""P4: ``@`` on a sidebar row whose unread activity mentions you (planning/terminal-ui.md §15 C)."""

from __future__ import annotations

import io
import threading

import pytest
from rich.console import Console

from pepper_cli import api_client
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
        _activity(server, ts.TOPIC_B1, 1, mentions=[ts.ME_PERSON])
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
        _activity(server, ts.TOPIC_B1, 1, mentions=[ts.ME_PERSON])
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
        _activity(server, ts.TOPIC_B1, 1, mentions=[ts.ME_PERSON])
        await until(pilot, lambda: ts.TOPIC_B1 in app.mentions.hits)
        assert _fetches(server, ts.TOPIC_A1) >= 1
        assert ts.TOPIC_A1 not in app.mentions.checked


async def test_room_updates_do_not_cut_a_mention_retry_short(monkeypatch) -> None:
    monkeypatch.setattr(state, "BACKOFF_STEPS", (30.0,))
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        server.fail[f"/api/v1/groups/{ts.ROOM_B}/topics/{ts.TOPIC_B1}/messages"] = 500
        _activity(server, ts.TOPIC_B1, 1, mentions=[ts.ME_PERSON])
        await until(pilot, lambda: app._loops["mentions"].failures == 1)
        # More room activity arrives on every rail poll while the retry waits.
        for i in range(4):
            at = f"2026-09-16T18:0{i + 1}:00+00:00"
            server.rooms[0]["topics"][0]["last_activity_at"] = at
            server.bump()
            await until(pilot, lambda at=at: app.rooms[0].topics[0].last_activity_at == at)
        assert _fetches(server, ts.TOPIC_B1) == 1
        assert app._loops["mentions"].timer is not None


@pytest.mark.parametrize("enabled", [False, True])
async def test_the_bell_rings_once_per_new_mention_and_only_when_asked(enabled) -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client(), bell=enabled)
    rings: list[int] = []
    app.bell = lambda: rings.append(1)  # type: ignore[method-assign]
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        _activity(server, ts.TOPIC_B1, 1, mentions=[ts.ME_PERSON])
        await until(pilot, lambda: ts.TOPIC_B1 in app.mentions.hits)
        await pilot.pause(0.3)
        assert len(rings) == (1 if enabled else 0)


class _FirstThenRest(threading.Event):
    """A gate that holds the first request on ``first`` and every later one on ``rest``."""

    def __init__(self) -> None:
        super().__init__()
        self.first, self.rest = threading.Event(), threading.Event()
        self._lock = threading.Lock()
        self._seen = 0

    def wait(self, timeout: float | None = None) -> bool:
        with self._lock:
            gate = self.first if self._seen == 0 else self.rest
            self._seen += 1
        return gate.wait(timeout)


async def test_the_bell_stays_quiet_for_a_topic_opened_while_its_check_was_in_flight() -> None:
    server = ts.standard_server()
    gate = _FirstThenRest()
    server.gates[f"GET /api/v1/groups/{ts.ROOM_B}/topics/{ts.TOPIC_B1}/messages"] = gate
    app = tui_app.PepperApp(server.client(), bell=True)
    rings: list[int] = []
    app.bell = lambda: rings.append(1)  # type: ignore[method-assign]
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        _activity(server, ts.TOPIC_B1, 1, mentions=[ts.ME_PERSON])
        await until(pilot, lambda: _fetches(server, ts.TOPIC_B1) == 1)  # the mention check
        # Opened before the check answers; its own page (and read mark) is still on the way.
        app.open_conversation(state.Conversation(ts.ROOM_B, ts.TOPIC_B1))
        await until(pilot, lambda: _fetches(server, ts.TOPIC_B1) == 2)
        gate.first.set()
        await until(pilot, lambda: ts.TOPIC_B1 in app.mentions.checked)
        gate.rest.set()
        await until(pilot, lambda: "ping" in ts.body_text(app))
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


async def test_whoami_person_id_is_what_mentions_are_matched_against() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        assert app.you_person_id == ts.ME_PERSON
        # The core user id is not in any group payload; a mention carrying it is not yours.
        _activity(server, ts.TOPIC_B1, 1, mentions=[ts.ME])
        await until(pilot, lambda: ts.TOPIC_B1 in app.mentions.checked)
        assert ts.TOPIC_B1 not in app.mentions.hits


async def test_no_marker_for_a_mention_of_someone_else() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        _activity(server, ts.TOPIC_B1, 1, mentions=["00000000-0000-0000-0000-00000000cccc"])
        room_key = f"room:{ts.ROOM_B}"
        await until(pilot, lambda: ts.TOPIC_B1 in app.mentions.checked)
        assert not app.sidebar._by_key[room_key].data.mention
        assert _label(app, room_key).endswith(" ●")


async def test_without_a_whoami_person_id_it_is_learned_from_your_own_message() -> None:
    server = ts.standard_server()
    server.person_id = None  # a server that does not send person_id yet
    server.messages[ts.TOPIC_A1].append(_own("m-own", "2026-09-16T17:31:00+00:00"))
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        assert app.you_person_id is None
        # Nothing is asked about while the id is unknown.
        _activity(server, ts.TOPIC_B1, 1, mentions=[ts.ME_PERSON])
        await pilot.pause(0.3)
        assert _fetches(server, ts.TOPIC_B1) == 0
        # Opening a topic that holds a message of yours teaches the id; the unread topic is
        # then asked about and gets its marker.
        app.open_conversation(state.Conversation(ts.ROOM_A, ts.TOPIC_A1))
        await until(pilot, lambda: app.you_person_id == ts.ME_PERSON)
        room_key = f"room:{ts.ROOM_B}"
        await until(pilot, lambda: ts.TOPIC_B1 in app.mentions.hits)
        await until(pilot, lambda: app.sidebar._by_key[room_key].data.mention)
        assert _label(app, room_key).endswith(" @")


async def test_a_learned_person_id_is_kept_and_reasks_topics_already_checked() -> None:
    server = ts.standard_server()
    app = tui_app.PepperApp(server.client())
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: len(app.rooms) == 2)
        app.you_person_id = None
        _activity(server, ts.TOPIC_B1, 1, mentions=[ts.ME_PERSON])
        # Checked while the id was unknown, as an older client would have recorded it.
        app.mentions.checked[ts.TOPIC_B1] = state.parse_ts(NEW)  # type: ignore[assignment]
        app._learn_from_messages(
            [api_client.group_message_from_dict(_own("m-own", "2026-09-16T17:31:00+00:00"))]
        )
        assert app.you_person_id == ts.ME_PERSON
        await until(pilot, lambda: ts.TOPIC_B1 in app.mentions.hits)
        # Kept for the session: a later message of another author does not change it.
        other = ts.message("m-x", NEW, "x", is_you=True, author_person_id="someone-else")
        app._learn_from_messages([api_client.group_message_from_dict(other)])
        assert app.you_person_id == ts.ME_PERSON


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
