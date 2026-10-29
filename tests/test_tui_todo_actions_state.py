"""The pure half of the todo writes (``tui/state.py``) and the shared due-date parser."""

from __future__ import annotations

from datetime import UTC, datetime

from pepper_cli import api_client
from pepper_cli.due import parse_due
from pepper_cli.tui import state
from tests import tui_support as ts

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def _todo(tid: str, title: str = "t", **over: object) -> api_client.TodoEntry:
    return api_client._todo_from_dict(ts.todo(tid, title, **over))


def _pane(*, owner: str | None = None, rooms: bool = False) -> state.AppPaneState:
    pane = state.AppPaneState(owner=owner, slug="todo")
    pane.todos = [_todo("a"), _todo("b")]
    if rooms:
        pane.rooms = [
            api_client.RoomTodos("g1", "Trip", [_todo("r1"), _todo("r2", status="done")]),
        ]
    return pane


def test_parse_due_takes_a_day_or_a_time_in_the_local_zone_and_rejects_the_rest() -> None:
    day, err = parse_due("2026-11-05")
    assert err is None and day is not None and day.startswith("2026-11-05T00:00:00")
    timed, _ = parse_due("2026-11-05 10:30")
    assert timed is not None and timed.startswith("2026-11-05T10:30:00") and timed[-6] in "+-"
    assert parse_due("soon", flag="--at") == (
        None,
        "couldn't parse --at 'soon' (try 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM')",
    )


def test_a_write_goes_to_the_pane_owner_except_for_a_room_section_row() -> None:
    own = _pane(rooms=True)
    assert state.todo_owner(own, "a") is None
    assert state.todo_owner(own, "r1") == "g1"
    assert state.todo_owner(own, "r2") == "g1"  # a room's closed item in the merged fold
    assert state.todo_owner(_pane(owner="g9"), "a") == "g9"


def test_apply_todo_replaces_the_row_in_your_list_or_a_rooms_section() -> None:
    pane = _pane(rooms=True)
    state.apply_todo(pane, _todo("a", status="done"))
    assert pane.todos is not None and [t.status for t in pane.todos] == ["done", "open"]
    state.apply_todo(pane, _todo("r1", status="done"))
    assert pane.rooms is not None and [t.status for t in pane.rooms[0].items] == ["done", "done"]
    assert [t.id for t in pane.todos] == ["a", "b"]


def test_apply_todo_swaps_an_adds_placeholder_for_the_created_row() -> None:
    pane = _pane()
    holder = state.placeholder_todo(pane, "New", due_date=None, due_day="2026-11-05", now=NOW)
    assert holder.id in pane.pending and holder.due_is_date_only is True
    state.apply_todo(pane, _todo("new-1", "New"), replacing=holder.id)
    assert pane.todos is not None and [t.id for t in pane.todos] == ["a", "b", "new-1"]


def test_an_add_whose_placeholder_a_reread_dropped_is_appended_once() -> None:
    pane = _pane()
    entry = _todo("new-1", "New")
    state.apply_todo(pane, entry, replacing="pending:1")
    state.apply_todo(pane, entry, replacing="pending:1")
    assert pane.todos is not None and [t.id for t in pane.todos] == ["a", "b", "new-1"]


def test_a_placeholder_is_dropped_when_a_reread_already_holds_the_created_row() -> None:
    pane = _pane()
    state.placeholder_todo(pane, "New", due_date="2026-11-05T10:00:00+00:00", due_day=None, now=NOW)
    pane.todos = [*(pane.todos or []), _todo("new-1", "New")]
    state.apply_todo(pane, _todo("new-1", "New"), replacing="pending:1")
    assert pane.todos is not None and [t.id for t in pane.todos] == ["a", "b", "new-1"]


def test_a_reread_keeps_an_adds_placeholder_while_it_waits() -> None:
    pane = _pane()
    holder = state.placeholder_todo(pane, "New", due_date=None, due_day=None, now=NOW)
    read = [_todo("a"), _todo("c")]
    assert [t.id for t in state.keep_placeholders(pane, read)] == ["a", "c", holder.id]
    pane.pending.discard(holder.id)  # answered: the next read is the server's list alone
    assert [t.id for t in state.keep_placeholders(pane, read)] == ["a", "c"]


def test_drop_todo_removes_a_row_and_is_quiet_without_a_list() -> None:
    pane = _pane()
    state.drop_todo(pane, "a")
    assert pane.todos is not None and [t.id for t in pane.todos] == ["b"]
    state.drop_todo(state.AppPaneState(owner=None, slug="todo"), "a")


def test_parse_add_due_follows_the_cli_rule_for_your_list_and_a_room() -> None:
    assert state.parse_add_due("  ", room=False) == (None, None, None)
    assert state.parse_add_due("2026-11-05", room=True) == (None, "2026-11-05", None)
    iso, day, err = state.parse_add_due("2026-11-05 10:00", room=False)
    assert iso is not None and day is None and err is None
    assert state.parse_add_due("2026-11-05 10:00", room=True)[2] is not None
    assert state.parse_add_due("2026-02-31", room=True)[2] is not None
    assert state.parse_add_due("nope", room=False)[2] is not None


def test_assignable_members_leave_out_pepper_agents_and_you() -> None:
    roster = [
        api_client.GroupRosterMember("p0", "Pepper", is_pepper=True),
        api_client.GroupRosterMember("me", "Me"),
        api_client.GroupRosterMember("bot", "Bot", is_agent=True),
        api_client.GroupRosterMember("sam", "Sam"),
    ]
    assert state.assignable_members(roster, "me") == [("sam", "Sam")]
    assert state.assignable_members(roster, None) == []  # you are unknown: no picker


def test_reselect_keeps_the_row_or_takes_the_one_now_at_its_place() -> None:
    def rows(*ids: str) -> list[state.PaneRow]:
        return [state.PaneRow(f"todo:{i}", "todo", todo=_todo(i)) for i in ids]

    before = rows("a", "b", "c")
    assert state.reselect(before, "todo:b", rows("a", "b")) == "todo:b"
    assert state.reselect(before, "todo:b", rows("a", "c")) == "todo:c"
    assert state.reselect(before, "todo:c", rows("a", "b")) == "todo:b"
    assert state.reselect(before, "todo:zzz", rows("a")) == "todo:a"
    assert state.reselect(before, "todo:a", []) is None
