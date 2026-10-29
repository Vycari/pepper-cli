"""The pure half of the Todos and Notes panes (``tui/state.py``)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

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


# --- P1b: your own panes -------------------------------------------------------------------


def _room(gid: str, name: str, *items: api_client.TodoEntry) -> api_client.RoomTodos:
    return api_client.RoomTodos(group_id=gid, name=name, items=list(items))


def test_own_todo_sections_put_your_open_items_first_then_a_section_per_room_in_route_order() -> (
    None
):
    mine = [_todo("own-late"), _todo("own-soon", due_date="2026-10-30T00:00:00Z")]
    rooms = [
        _room("g2", "Work", _todo("w1"), _todo("w-closed", status="done", done_at="2026-10-03Z")),
        _room("g-empty", "Quiet", _todo("q-done", status="done", done_at="2026-10-02T00:00:00Z")),
        _room("g1", "Family", _todo("f-late"), _todo("f-soon", due_date="2026-10-29T00:00:00Z")),
    ]
    opened, sections, closed = state.own_todo_sections(mine, rooms)
    assert _ids(opened) == ["own-soon", "own-late"]
    # Route order, not alphabetical; a room with nothing open has no section.
    assert [(r.name, _ids(items)) for r, items in sections] == [
        ("Work", ["w1"]),
        ("Family", ["f-soon", "f-late"]),
    ]
    # Every room's closed items join the one fold; the empty room's closed item is in it too.
    assert set(_ids(closed)) == {"w-closed", "q-done"}


def test_the_closed_fold_merges_your_closed_items_with_your_rooms_newest_first() -> None:
    mine = [_todo("mine-done", status="done", done_at="2026-10-05T00:00:00Z")]
    rooms = [
        _room("g", "Family", _todo("room-done", status="done", done_at="2026-10-09T00:00:00Z"))
    ]
    assert _ids(state.own_todo_sections(mine, rooms)[2]) == ["room-done", "mine-done"]


def test_your_own_todos_rows_carry_room_headings_that_cannot_be_selected_and_count_all_open() -> (
    None
):
    pane = state.AppPaneState(owner=None, slug="todo")
    pane.todos = [_todo("a")]
    pane.rooms = [
        _room(
            "g", "Family", _todo("f1"), _todo("f2", status="done", done_at="2026-10-01T00:00:00Z")
        )
    ]
    rws = state.pane_rows(pane)
    assert [(r.kind, r.key) for r in rws] == [
        ("todo", "todo:a"),
        ("section", "section:g"),
        ("todo", "todo:f1"),
        ("fold", "fold"),
    ]
    assert rws[1].label == "Family"
    assert state.move_selection(rws, "todo:a", 1) == "todo:f1"  # steps over the heading
    assert state.open_todo_count(pane) == 2
    # A room's pane never shows other rooms' sections, even if some were somehow held.
    pane.owner = "g"
    assert [r.kind for r in state.pane_rows(pane)] == ["todo"]


def test_the_top_level_rows_come_first_carry_no_dot_and_key_like_their_pane() -> None:
    rows_ = state.build_sidebar(
        [], state.ReadMarks(), current=state.PEPPER, manual={}, show_all=set(), pepper_dot=True
    )
    assert [(r.kind, r.label, r.slug) for r in rows_[:3]] == [
        ("own_app", "Todos", "todo"),
        ("own_app", "Notes", "notes"),
        ("rule", state.RULE, None),
    ]
    assert rows_[3].kind == "pepper"
    assert not any(r.unread or r.mention or r.expandable for r in rows_[:3])
    for slug, row in zip(("todo", "notes"), rows_[:2], strict=True):
        assert row.key == state.AppPaneState(owner=None, slug=slug).key


def test_jump_targets_start_with_your_todos_and_notes() -> None:
    first = state.jump_targets([])
    assert [(t.label, t.conversation) for t in first] == [
        ("Todos", state.OwnApp("todo")),
        ("Notes", state.OwnApp("notes")),
        ("Pepper", state.PEPPER),
    ]


def test_a_turn_under_your_pane_names_the_app_and_over_a_note_the_note() -> None:
    todos = state.AppPaneState(owner=None, slug="todo")
    assert state.pane_subject(todos) == ("app:todo", None)
    notes = state.AppPaneState(owner=None, slug="notes")
    assert state.pane_subject(notes) == ("app:notes", None)
    notes.note_id = "n-1"
    assert state.pane_subject(notes) == ("app:notes", {"note_id": "n-1"})
    assert state.pane_subject(state.AppPaneState(owner="r", slug="notes", note_id="n")) == (
        None,
        None,
    )
    assert state.pane_subject(None) == (None, None)


def test_the_composer_placeholder_asks_about_your_list_only_under_your_pane() -> None:
    assert state.pane_placeholder(state.AppPaneState(owner=None, slug="todo"), "d") == (
        "Ask Pepper about your todos…"
    )
    assert state.pane_placeholder(state.AppPaneState(owner=None, slug="notes"), "d") == (
        "Ask Pepper about your notes…"
    )
    assert state.pane_placeholder(state.AppPaneState(owner="r", slug="todo"), "d") == "d"
    assert state.pane_placeholder(None, "d") == "d"


def test_the_reply_strip_keeps_the_exchange_in_flight_and_the_one_before() -> None:
    pane = state.AppPaneState(owner=None, slug="todo")
    turns = [state.DmTurn(text=t, phase="done") for t in ("one", "two", "three")]
    for turn in turns:
        state.remember_exchange(pane, turn)
    assert pane.exchanges == turns[1:]


def test_sends_queued_behind_a_running_turn_never_push_its_answer_out() -> None:
    pane = state.AppPaneState(owner=None, slug="todo")
    done = state.DmTurn(text="earlier", phase="done")
    running = state.DmTurn(text="slow", phase="streaming")
    queued = [state.DmTurn(text=t) for t in ("next", "after")]
    for turn in [done, running, *queued]:
        state.remember_exchange(pane, turn)
    assert pane.exchanges == [done, running, *queued]
    # The running turn answers and the next starts: the strip narrows from the one before.
    running.phase, queued[0].phase = "done", "streaming"
    assert state.strip_turns(pane.exchanges) == [running, *queued]
    for turn in queued:
        turn.phase = "done"
    assert state.strip_turns(pane.exchanges) == queued


def test_a_turn_finds_its_reply_in_history_by_id_or_as_the_new_assistant_row() -> None:
    entries = {
        "old": api_client.ChatHistoryEntry(
            id="old", role="assistant", text="earlier", channel="terminal", created_at="2026-10-01Z"
        ),
        "mine": api_client.ChatHistoryEntry(
            id="mine", role="user", text="q", channel="terminal", created_at="2026-10-02T00:00:00Z"
        ),
        "new": api_client.ChatHistoryEntry(
            id="new",
            role="assistant",
            text="the answer",
            channel="terminal",
            created_at="2026-10-02T00:00:05Z",
        ),
    }
    by_id = state.DmTurn(text="q", assistant_id="new")
    assert by_id.found_reply(entries) == "the answer"
    assert state.DmTurn(text="q", assistant_id="gone").found_reply(entries) is None
    dropped = state.DmTurn(text="q", phase="checking", snapshot=frozenset({"old"}))
    assert dropped.found_reply(entries) == "the answer"
    assert dropped.reply_id(entries) == "new"
    assert state.DmTurn(text="q", snapshot=frozenset(entries)).found_reply(entries) is None
    # Before a drop there is nothing to guess from: only a named row is this turn's.
    assert state.DmTurn(text="q", phase="streaming").reply_id(entries) is None


def test_an_ask_is_not_also_labelled_from_request() -> None:
    assert state.todo_source_label(_todo("1", source="request")) == ""


def test_checklist_items_name_each_line_by_text_occurrence_and_matches() -> None:
    body = "- [ ] milk\n- [x] eggs\n```\n- [ ] in code\n```\n* [X] milk\n+ [ ] bread\nplain\n"
    items = state.checklist_items(body)
    assert [(i.index, i.checked, i.text, i.occurrence, i.matches) for i in items] == [
        (0, False, "milk", 0, 2),
        (1, True, "eggs", 0, 1),
        (2, True, "milk", 1, 2),
        (3, False, "bread", 0, 1),
    ]
    assert [i.label for i in items] == ["☐ milk", "☑ eggs", "☑ milk", "☐ bread"]
    assert state.checklist_items(None) == []


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("Title\n\nBody line\nmore\n", ("Title", "Body line\nmore")),
        ("\n \n  Padded title  \nbody", ("Padded title", "body")),
        ("Only a title", ("Only a title", "")),
        # The first body line keeps its indent (a markdown code block), past the blank lines.
        ("Snippet\n\n    print(1)\n    print(2)\n", ("Snippet", "    print(1)\n    print(2)")),
        ("Title\n  \n\t\n  - nested\n", ("Title", "  - nested")),
        ("Title\n \n\t", ("Title", "")),
        ("", None),
        ("  \n\t\n", None),
    ],
)
def test_split_new_note_takes_the_first_non_blank_line_as_the_title(typed, expected) -> None:
    assert state.split_new_note(typed) == expected
