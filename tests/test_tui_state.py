"""Unit tests of the terminal UI's pure logic (``pepper_cli.tui.state``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pepper_cli import api_client
from pepper_cli.tui import state
from tests import tui_support as ts


def _rooms(*rooms: dict) -> list[api_client.GroupSummary]:
    body = {"enabled": True, "degraded": False, "total": len(rooms), "archived_total": 0}
    return api_client._group_list_from_dict({**body, "groups": list(rooms)}).groups


def _msg(mid: str, at: str) -> api_client.GroupMessage:
    return api_client.group_message_from_dict(ts.message(mid, at))


def test_parse_ts_reads_both_spellings_as_the_same_instant() -> None:
    assert state.parse_ts("2026-09-16T17:30:00Z") == state.parse_ts("2026-09-16T17:30:00+00:00")
    assert state.parse_ts("2026-09-16T17:30:00") == datetime(2026, 9, 16, 17, 30, tzinfo=UTC)
    assert state.parse_ts("nonsense") is None
    assert state.parse_ts(None) is None


def test_marks_only_move_forward_and_first_sight_seeds_read() -> None:
    marks = state.ReadMarks()
    later, earlier = datetime(2026, 1, 2, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)
    assert marks.merge("t", later) is True
    assert marks.merge("t", earlier) is False
    assert marks.merge("t", None) is False
    assert marks.marks["t"] == later
    rooms = _rooms(ts.room(ts.ROOM_A, "A", [ts.topic("t2", "New", "2026-02-01T00:00:00+00:00")]))
    marks.seed_first_sight(rooms)
    assert not marks.is_unread(rooms[0].topics[0], open_topic_id=None)


def test_unread_rule_activity_after_mark_and_not_open_or_archived() -> None:
    (room,) = _rooms(
        ts.room(
            ts.ROOM_A,
            "A",
            [
                ts.topic("t1", "One", "2026-02-01T00:00:00Z"),
                ts.topic("t2", "Two", "2026-02-01T00:00:00Z", archived_at="2026-02-02T00:00:00Z"),
                ts.topic("t3", "Three", ""),
            ],
        )
    )
    marks = state.ReadMarks()
    marks.merge_server({"t1": "2026-01-31T00:00:00+00:00"})
    t1, t2, t3 = room.topics
    assert marks.is_unread(t1, open_topic_id=None)
    assert not marks.is_unread(t1, open_topic_id="t1")  # on screen
    assert not marks.is_unread(t2, open_topic_id=None)  # archived
    assert not marks.is_unread(t3, open_topic_id=None)  # no activity stamp
    marks.merge_server({"t1": "2026-02-01T00:00:00+00:00"})  # same instant, other spelling
    assert not marks.is_unread(t1, open_topic_id=None)


def _big_room() -> list[api_client.GroupSummary]:
    topics = [ts.topic(f"t{i}", f"Topic {i}", f"2026-03-0{i}T00:00:00Z") for i in range(1, 8)]
    return _rooms(ts.room(ts.ROOM_A, "Big", topics), ts.room(ts.ROOM_B, "Other", []))


def test_sidebar_orders_pepper_first_and_caps_topics_at_five_with_show_more() -> None:
    rooms = _big_room()
    marks = state.ReadMarks()
    current = state.Conversation(ts.ROOM_A, "t7")
    rows = state.build_sidebar(
        rooms, marks, current=current, manual={}, show_all=set(), pepper_dot=True
    )
    assert [r.kind for r in rows][:2] == ["pepper", "room"]
    assert rows[0].unread is True
    topics = [r.label for r in rows if r.kind == "topic"]
    assert topics == ["Topic 7", "Topic 6", "Topic 5", "Topic 4", "Topic 3"]
    more = next(r for r in rows if r.kind == "more")
    assert more.hidden_count == 2
    assert more.label == "Show more (2)"
    expanded = state.build_sidebar(
        rooms, marks, current=current, manual={}, show_all={ts.ROOM_A}, pepper_dot=False
    )
    assert len([r for r in expanded if r.kind == "topic"]) == 7
    assert not any(r.kind == "more" for r in expanded)


def test_the_open_topic_stays_visible_past_the_fifth_and_manual_state_wins() -> None:
    rooms = _big_room()
    marks = state.ReadMarks()
    current = state.Conversation(ts.ROOM_A, "t1")
    rows = state.build_sidebar(
        rooms, marks, current=current, manual={}, show_all=set(), pepper_dot=False
    )
    assert "Topic 1" in [r.label for r in rows if r.kind == "topic"]
    collapsed = state.build_sidebar(
        rooms, marks, current=current, manual={ts.ROOM_A: False}, show_all=set(), pepper_dot=False
    )
    assert not any(r.kind in {"topic", "more"} for r in collapsed)
    assert collapsed[1].expandable and not collapsed[1].expanded


def test_row_conversation_and_header_text() -> None:
    rooms = _big_room()
    rows = state.build_sidebar(
        rooms,
        state.ReadMarks(),
        current=state.Conversation(ts.ROOM_A, "t7"),
        manual={},
        show_all=set(),
        pepper_dot=False,
    )
    assert rows[0].conversation == state.PEPPER
    assert next(r for r in rows if r.kind == "room").conversation is None
    topic_row = next(r for r in rows if r.kind == "topic")
    assert topic_row.conversation == state.Conversation(ts.ROOM_A, "t7")
    assert state.header_text(state.PEPPER, rooms) == "Pepper · Today"
    assert state.header_text(state.Conversation(ts.ROOM_A, "t7"), rooms) == "Big › Topic 7"
    assert state.header_text(state.Conversation(ts.ROOM_A, "gone"), rooms) == "Big"
    assert state.header_text(state.Conversation("gone", "x"), rooms) == ""


def test_archived_topic_header_says_so() -> None:
    rooms = _rooms(
        ts.room(
            ts.ROOM_A,
            "A",
            [ts.topic("t1", "Old", "2026-01-01T00:00:00Z", archived_at="2026-01-02T00:00:00Z")],
        )
    )
    assert state.header_text(state.Conversation(ts.ROOM_A, "t1"), rooms) == "A › Old [archived]"


def test_next_unread_wraps_both_ways_and_reports_none() -> None:
    rooms = _rooms(
        ts.room(ts.ROOM_A, "A", [ts.topic("a1", "A1", "2026-02-01T00:00:00Z")]),
        ts.room(ts.ROOM_B, "B", [ts.topic("b1", "B1", "2026-02-02T00:00:00Z")]),
    )
    marks = state.ReadMarks()
    marks.merge_server({"a1": "2026-01-01T00:00:00Z", "b1": "2026-01-01T00:00:00Z"})
    a1, b1 = state.Conversation(ts.ROOM_A, "a1"), state.Conversation(ts.ROOM_B, "b1")
    kwargs = {"pepper_dot": False}
    assert state.next_unread(rooms, marks, current=state.PEPPER, **kwargs) == a1
    assert state.next_unread(rooms, marks, current=a1, **kwargs) == b1
    assert state.next_unread(rooms, marks, current=b1, **kwargs) == a1  # wraps
    assert state.next_unread(rooms, marks, current=a1, step=-1, **kwargs) == b1  # wraps back
    assert state.any_unread(rooms, marks, current=state.PEPPER, pepper_dot=False)
    marks.merge_server({"a1": "2026-02-01T00:00:00Z", "b1": "2026-02-02T00:00:00Z"})
    assert state.next_unread(rooms, marks, current=a1, pepper_dot=True) == state.PEPPER
    assert state.next_unread(rooms, marks, current=a1, **kwargs) is None
    assert not state.any_unread(rooms, marks, current=a1, pepper_dot=False)


def test_message_store_orders_by_time_then_id_and_dedupes() -> None:
    store = state.MessageStore()
    assert store.since() is None
    assert store.newest_created_at() is None
    new = store.add([_msg("b", "2026-01-01T10:00:00Z"), _msg("a", "2026-01-01T10:00:00+00:00")])
    assert new == ["b", "a"]
    assert [m.id for m in store.ordered()] == ["a", "b"]  # tie broken by id
    assert store.add([_msg("a", "2026-01-01T10:00:00Z")]) == []  # an edit replaces, not new
    assert len(store.rows) == 2


def test_cursor_is_the_newest_server_timestamp_minus_the_lookback() -> None:
    store = state.MessageStore()
    store.add([_msg("a", "2026-01-01T10:00:00Z")])
    assert store.cursor == datetime(2026, 1, 1, 10, tzinfo=UTC)
    assert state.parse_ts(store.since()) == datetime(2026, 1, 1, 10, tzinfo=UTC) - state.LOOKBACK
    # An older row arriving later never moves the cursor back.
    store.add([_msg("z", "2026-01-01T09:00:00Z")])
    assert store.cursor == datetime(2026, 1, 1, 10, tzinfo=UTC)


def test_a_late_committed_row_inside_the_lookback_is_still_fetched_and_ordered() -> None:
    """pepper#4829: an own echo lands first; an earlier-committed row shows up one poll later."""
    store = state.MessageStore()
    store.add([_msg("echo", "2026-01-01T10:00:05Z")])
    cutoff = state.parse_ts(store.since())
    late = _msg("late", "2026-01-01T10:00:02Z")  # committed after, stamped before
    assert state.parse_ts(late.created_at) > cutoff  # the window still reaches it
    store.add([late])
    assert [m.id for m in store.ordered()] == ["late", "echo"]
    assert store.newest_created_at() == "2026-01-01T10:00:05Z"


def test_every_fifth_tick_is_a_reconcile() -> None:
    store = state.MessageStore()
    assert [store.next_is_reconcile() for _ in range(10)] == [
        False, False, False, False, True, False, False, False, False, True,
    ]  # fmt: skip


def _entry(eid: str, role: str) -> api_client.ChatHistoryEntry:
    return api_client.ChatHistoryEntry(
        id=eid, role=role, text="x", channel="terminal", created_at="2026-01-01T10:00:00Z"
    )


def test_dm_tracker_baselines_then_lights_on_a_new_reply_unless_open() -> None:
    dm = state.DmTracker()
    assert dm.update([_entry("1", "assistant")], is_open=False) == ["1"]
    assert dm.dot is False  # first read is the baseline
    assert dm.update([_entry("1", "assistant"), _entry("2", "user")], is_open=False) == ["2"]
    assert dm.dot is False  # my own message never lights it
    dm.update(
        [_entry("1", "assistant"), _entry("2", "user"), _entry("3", "assistant")], is_open=False
    )
    assert dm.dot is True
    dm.update([_entry("1", "assistant"), _entry("4", "assistant")], is_open=True)
    assert dm.dot is False
    assert [e.id for e in dm.ordered()] == ["1", "4"]


def test_dm_tracker_drops_rows_that_leave_the_history_after_midnight() -> None:
    dm = state.DmTracker()
    dm.update([_entry("y1", "user"), _entry("y2", "assistant")], is_open=False)
    # Past midnight the history is a new day's thread: yesterday's rows are gone from it.
    assert dm.update([_entry("t1", "assistant")], is_open=False) == ["t1"]
    assert [e.id for e in dm.ordered()] == ["t1"]
    assert dm.dot is True
    # An empty new day holds nothing over from the old one.
    assert dm.update([], is_open=True) == []
    assert dm.ordered() == []


def test_poll_cadence_tiers_and_pause() -> None:
    assert state.poll_interval(0) == 3
    assert state.poll_interval(59.9) == 3
    assert state.poll_interval(60) == 10
    assert state.poll_interval(119) == 10
    assert state.poll_interval(120) == 15
    assert state.poll_interval(0, hidden=True) is None


def test_backoff_steps_cap_at_thirty() -> None:
    assert [state.backoff_delay(n) for n in (0, 1, 2, 3, 4, 9)] == [0, 3, 6, 12, 30, 30]


def test_lookback_is_ten_seconds() -> None:
    assert timedelta(seconds=10) == state.LOOKBACK
