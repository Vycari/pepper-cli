"""The pure half of the Todos and Notes panes (``tui/state.py``)."""

from __future__ import annotations

from datetime import UTC, datetime

from pepper_cli import api_client
from pepper_cli.tui import rows, state
from tests import tui_support as ts

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
YOU = {ts.ME}


def _todo(tid: str, title: str = "t", **over: object) -> api_client.TodoEntry:
    return api_client._todo_from_dict(ts.todo(tid, title, **over))


def _note(nid: str, at: str, **over: object) -> api_client.ArtifactListItem:
    return api_client._artifact_list_item_from_dict(ts.note_item(nid, nid, at, **over))


def _ids(entries: list[api_client.TodoEntry]) -> list[str]:
    return [e.id for e in entries]


def test_open_todos_sort_by_due_then_created_then_server_order_with_undated_last() -> None:
    entries = [
        _todo("undated-new", created_at="2026-10-05T00:00:00+00:00"),
        _todo("later", due_date="2026-11-01T00:00:00+00:00", due_is_date_only=True),
        _todo("undated-old", created_at="2026-10-01T00:00:00+00:00"),
        _todo("timed", due_date="2026-10-30T15:00:00Z", due_is_date_only=False),
        _todo("tie-a", due_date="2026-10-31T00:00:00+00:00", created_at="2026-10-02T00:00:00Z"),
        _todo("tie-b", due_date="2026-10-31T00:00:00+00:00", created_at="2026-10-02T00:00:00Z"),
    ]
    opened, closed = state.todo_sections(entries)
    assert _ids(opened) == ["timed", "tie-a", "tie-b", "later", "undated-old", "undated-new"]
    assert closed == []


def test_closed_todos_fold_newest_first_by_done_time_with_dismissed_beside_done() -> None:
    entries = [
        _todo("done-old", status="done", done_at="2026-10-01T00:00:00+00:00"),
        _todo("dismissed", status="dismissed", created_at="2026-10-20T00:00:00+00:00"),
        _todo("done-new", status="done", done_at="2026-10-10T00:00:00+00:00"),
        _todo("open"),
    ]
    opened, closed = state.todo_sections(entries)
    assert _ids(opened) == ["open"]
    assert _ids(closed) == ["dismissed", "done-new", "done-old"]


def test_ask_line_names_you_and_them_for_each_combination() -> None:
    sam = {"user_id": "sam-id", "label": "Sam"}
    me = {"user_id": ts.ME, "label": "Me"}
    assert state.ask_line(_todo("1"), YOU) is None
    assert state.ask_line(_todo("1", assignee=sam, requested_by=me), YOU) == "asked of Sam by you"
    assert state.ask_line(_todo("1", assignee=me, requested_by=sam), YOU) == "asked of you by Sam"
    assert state.ask_line(_todo("1", assignee=sam), YOU) == "asked of Sam"
    assert state.ask_line(_todo("1", requested_by=sam), YOU) == "asked by Sam"
    nameless = {"user_id": None, "label": None}
    assert state.ask_line(_todo("1", assignee=nameless), YOU) == "asked of someone"


def test_due_label_shows_a_date_only_day_bare_and_year_only_when_not_this_one() -> None:
    date_only = _todo("1", due_date="2026-10-30T00:00:00+00:00", due_is_date_only=True)
    assert state.due_label(date_only, now=NOW) == "due Oct 30"
    other_year = _todo("1", due_date="2027-01-05T00:00:00+00:00", due_is_date_only=True)
    assert state.due_label(other_year, now=NOW) == "due Jan 5, 2027"
    assert state.due_label(_todo("1"), now=NOW) == ""
    timed = _todo("1", due_date="2026-10-30T15:00:00Z", due_is_date_only=False)
    assert state.due_label(timed, now=NOW, hour24=True).startswith("due Oct ")
    assert state.due_label(timed, now=NOW).endswith(("AM", "PM"))


def test_edited_label_is_today_yesterday_or_a_day() -> None:
    local_noon = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    now = local_noon
    assert state.edited_label(now.isoformat(), now=now) == "Today"
    assert state.edited_label((now.replace(day=now.day).isoformat()), now=now) == "Today"
    yesterday = datetime.fromtimestamp(now.timestamp() - 86400, tz=now.tzinfo)
    assert state.edited_label(yesterday.isoformat(), now=now) == "Yesterday"
    old = datetime.fromtimestamp(now.timestamp() - 40 * 86400, tz=now.tzinfo)
    assert state.edited_label(old.isoformat(), now=now) == state.day_label(old, now=now)
    assert state.edited_label(None, now=now) == ""


def test_notes_come_most_recently_edited_first_whatever_the_servers_order() -> None:
    items = [
        _note("a", "2026-10-01T00:00:00+00:00"),
        _note("c", "2026-10-21T00:00:00Z"),
        _note("b", "2026-10-14T00:00:00+00:00"),
    ]
    assert [n.id for n in state.notes_order(items)] == ["c", "b", "a"]


def test_source_label_and_url() -> None:
    assert state.todo_source_label(_todo("1")) == ""
    assert state.todo_source_label(_todo("1", source="gmail")) == "from gmail"
    base = "http://example.com"
    ok = _todo("1", source_ref={"url": "https://mail.example.com/m/1"})
    assert state.todo_source_url(ok, base=base) == "https://mail.example.com/m/1"
    assert state.todo_source_url(_todo("1", source_ref={"href": "/notes/9"}), base=base) == (
        "http://example.com/notes/9"
    )
    assert (
        state.todo_source_url(_todo("1", source_ref={"url": "javascript:alert(1)"}), base=base)
        is None
    )
    assert state.todo_source_url(_todo("1"), base=base) is None


def test_todo_pane_rows_fold_closed_and_selection_moves_and_clamps() -> None:
    pane = state.AppPaneState(owner="r", slug="todo")
    pane.todos = [_todo("a"), _todo("b"), _todo("c", status="done", done_at="2026-10-01T00:00:00Z")]
    keys = [r.key for r in state.pane_rows(pane)]
    assert keys == ["todo:a", "todo:b", "fold"]
    pane.closed_open = True
    assert [r.key for r in state.pane_rows(pane)] == ["todo:a", "todo:b", "fold", "todo:c"]
    pane.closed_open = False
    rws = state.pane_rows(pane)
    assert state.move_selection(rws, None, 1) == "todo:a"
    assert state.move_selection(rws, "todo:a", 1) == "todo:b"
    assert state.move_selection(rws, "todo:b", 5) == "fold"
    assert state.move_selection(rws, "todo:a", -3) == "todo:a"
    assert state.settle_selection(rws, "todo:gone") == "todo:a"
    assert state.move_selection([], "x", 1) is None
    assert state.open_todo_count(pane) == 2


def test_notes_rows_end_with_a_limit_row_that_cannot_be_selected() -> None:
    pane = state.AppPaneState(owner="r", slug="notes", truncated=True)
    pane.notes = [_note("a", "2026-10-01T00:00:00Z"), _note("b", "2026-10-02T00:00:00Z")]
    rws = state.pane_rows(pane)
    assert [r.key for r in rws] == ["note:b", "note:a", "limit"]
    assert state.move_selection(rws, "note:a", 1) == "note:a"


def test_the_sidebar_nests_the_app_row_first_under_only_the_open_rooms_open_pane() -> None:
    server = ts.standard_server()
    with server.client() as api:
        listed = api.list_groups_conditional(if_none_match=None)[0]
    assert listed is not None
    marks = state.ReadMarks()
    cur = state.Conversation(ts.ROOM_A, ts.TOPIC_A1)
    pane = state.AppPaneState(owner=ts.ROOM_A, slug="todo")

    def build(p: state.AppPaneState | None) -> list[state.SidebarRow]:
        return state.build_sidebar(
            listed.groups,
            marks,
            current=cur,
            manual={ts.ROOM_B: True},
            show_all=set(),
            pepper_dot=False,
            pane=p,
        )

    plain = build(None)
    assert not [r for r in plain if r.kind == "app"]
    with_pane = build(pane)
    keys = [r.key for r in with_pane]
    room = keys.index(f"room:{ts.ROOM_A}")
    assert with_pane[room + 1].kind == "app"
    assert with_pane[room + 1].label == "Todos"
    assert with_pane[room + 1].key == pane.key
    assert [r for r in with_pane if r.kind == "app"] == [with_pane[room + 1]]
    other = state.AppPaneState(owner=ts.ROOM_B, slug="notes")
    elsewhere = build(other)
    assert [r.room_id for r in elsewhere if r.kind == "app"] == [ts.ROOM_B]


def test_pane_header_names_the_room_the_app_and_an_open_note() -> None:
    server = ts.standard_server()
    with server.client() as api:
        listed = api.list_groups_conditional(if_none_match=None)[0]
    assert listed is not None
    pane = state.AppPaneState(owner=ts.ROOM_A, slug="todo")
    assert state.pane_header(pane, listed.groups) == "Trip planning › Todos"
    pane = state.AppPaneState(owner=ts.ROOM_A, slug="notes", note_id="n", note_name="Packing")
    assert state.pane_header(pane, listed.groups) == "Trip planning › Notes › Packing"
    assert state.pane_header(state.AppPaneState(owner="gone", slug="notes"), []) == "Room › Notes"
    assert state.pane_header(state.AppPaneState(owner=None, slug="todo"), []) == "Todos"


CHECKLIST_BODY = """\
Intro
- [ ] Sleeping bags
- [x] Headlamps
* [X] Stove
+ [ ] Plus bullet
```
- [ ] inside a fence
```
~~~~
- [ ] inside a long fence
~~~
- [ ] still inside (a short tilde fence does not close it)
~~~~
```a`b
- [ ] after a backtick info string
  - [ ] nested
"""


def test_checklist_lines_follow_the_servers_rules() -> None:
    found = state.checklist_lines(CHECKLIST_BODY)
    assert [(checked, text) for _n, checked, text in found] == [
        (False, "Sleeping bags"),
        (True, "Headlamps"),
        (True, "Stove"),
        (False, "Plus bullet"),
        (False, "after a backtick info string"),
        (False, "nested"),
    ]
    assert state.checklist_lines(None) == []


def test_draw_checklist_replaces_only_the_lines_that_count() -> None:
    drawn = state.draw_checklist(CHECKLIST_BODY)
    assert "- ☐ Sleeping bags" in drawn
    assert "- ☑ Headlamps" in drawn
    assert "* ☑ Stove" in drawn
    assert "- [ ] inside a fence" in drawn
    assert "  - ☐ nested" in drawn


def test_note_body_renders_boxes_and_never_parses_markup() -> None:
    from rich.console import Console

    body = "[bold red]not styled[/] \x1b[31mred\x1b[0m\n\n- [ ] one\n- [x] two\n"
    rendered = rows.note_body_renderable(body)
    console = Console(width=60, record=True, force_terminal=False)
    console.print(rendered)
    out = console.export_text()
    assert "[bold red]not styled[/]" in out
    assert "\x1b" not in out
    assert "☐ one" in out
    assert "☑ two" in out


def test_first_text_url_skips_unsafe_links() -> None:
    text = "see javascript:x and (https://example.com/a), then https://other.example/b."
    assert state.first_text_url(text, base="http://s") == "https://example.com/a"
    assert state.first_text_url("nothing here", base="http://s") is None
