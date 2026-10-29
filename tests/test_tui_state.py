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


def _row(topic_id: str, count: int, *, mention: bool = False) -> api_client.GroupUnreadRow:
    return api_client.GroupUnreadRow(topic_id, count, has_unread_mention=mention)


def test_marks_only_move_forward() -> None:
    marks = state.ReadMarks()
    later, earlier = datetime(2026, 1, 2, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)
    assert marks.merge("t", later) is True
    assert marks.merge("t", earlier) is False
    assert marks.merge("t", None) is False
    assert marks.marks["t"] == later


def test_unread_is_the_servers_row_and_a_topic_it_has_no_row_for_is_read() -> None:
    (room,) = _rooms(
        ts.room(
            ts.ROOM_A,
            "A",
            [
                ts.topic("t1", "One", "2026-02-01T00:00:00Z"),
                ts.topic("t2", "Two", "2026-02-01T00:00:00Z", archived_at="2026-02-02T00:00:00Z"),
                ts.topic("t3", "Three", "2026-02-01T00:00:00Z"),
                ts.topic("t4", "Four", "2026-02-01T00:00:00Z"),
            ],
        )
    )
    marks = state.ReadMarks()
    marks.merge_unread([_row("t1", 2), _row("t2", 1), _row("t4", 0)], {})
    t1, t2, t3, t4 = room.topics
    assert marks.is_unread(t1)
    assert not marks.is_unread(t2)  # archived: counted by the server, hidden by the rail
    assert not marks.is_unread(t3)  # no row: unknown is not unread
    assert not marks.is_unread(t4)  # the server says zero
    # Activity later than the cursor is not the rule any more: only the server's count is.
    marks.merge_server({"t3": "2026-01-01T00:00:00Z"})
    assert not marks.is_unread(t3)


def test_a_cursor_past_the_activity_clears_a_row_and_a_partial_one_does_not() -> None:
    (room,) = _rooms(ts.room(ts.ROOM_A, "A", [ts.topic("t1", "One", "2026-02-01T00:00:00Z")]))
    (topic,) = room.topics
    marks = state.ReadMarks()
    marks.merge_unread([_row("t1", 3, mention=True)], {"t1": "2026-01-01T00:00:00Z"})
    assert marks.is_mentioned(topic)
    marks.merge_advanced({"topic_id": "t1", "last_read_at": "2026-01-31T00:00:00Z"})
    assert marks.settle([room]) is False  # a partial read: the count is the server's
    assert marks.is_unread(topic)
    marks.merge_advanced({"topic_id": "t1", "last_read_at": "2026-02-01T00:00:00+00:00"})
    assert marks.settle([room]) is True
    assert not marks.is_unread(topic) and not marks.is_mentioned(topic)
    assert marks.settle([room]) is False  # nothing left to clear


def test_an_answer_that_crossed_a_read_is_kept_and_settled_by_that_read() -> None:
    (room,) = _rooms(ts.room(ts.ROOM_A, "A", [ts.topic("t1", "One", "2026-02-01T00:00:00Z")]))
    (topic,) = room.topics
    marks = state.ReadMarks()
    marks.merge("t1", state.parse_ts("2026-02-01T00:00:00Z"))  # read here, PUT not landed yet
    marks.merge_unread([_row("t1", 2)], {"t1": "2026-01-01T00:00:00Z"})  # counted from before it
    assert marks.unread["t1"].unread_count == 2  # kept: it may count a message newer than the read
    assert marks.settle([room]) is True  # the read covers the activity, so it clears
    assert not marks.is_unread(topic)


def test_an_answer_that_crossed_a_read_still_shows_a_message_past_it() -> None:
    (room,) = _rooms(ts.room(ts.ROOM_A, "A", [ts.topic("t1", "One", "2026-02-02T00:00:00Z")]))
    (topic,) = room.topics
    marks = state.ReadMarks()
    marks.merge("t1", state.parse_ts("2026-02-01T00:00:00Z"))
    marks.merge_unread([_row("t1", 2, mention=True)], {"t1": "2026-01-01T00:00:00Z"})
    assert marks.settle([room]) is False
    assert marks.is_unread(topic) and marks.is_mentioned(topic)


def test_a_cursor_the_server_counted_from_does_not_settle_its_answer() -> None:
    # /groups is read before /read: a message landing between the two leaves the room's
    # activity at the cursor while the answer counts the new message.
    (room,) = _rooms(ts.room(ts.ROOM_A, "A", [ts.topic("t1", "One", "2026-02-01T00:00:00Z")]))
    (topic,) = room.topics
    marks = state.ReadMarks()
    marks.merge("t1", state.parse_ts("2026-02-01T00:00:00Z"))
    marks.merge_unread([_row("t1", 1, mention=True)], {"t1": "2026-02-01T00:00:00Z"})
    assert marks.settle([room]) is False
    assert marks.settle([room]) is False  # nor does a later repaint against the same rooms
    assert marks.is_unread(topic) and marks.is_mentioned(topic)
    marks.merge_advanced({"topic_id": "t1", "last_read_at": "2026-02-01T00:00:01Z"})
    later = _rooms(ts.room(ts.ROOM_A, "A", [ts.topic("t1", "One", "2026-02-01T00:00:01Z")]))
    assert marks.settle(later) is True  # a cursor that moved after the answer does clear it
    assert not marks.is_unread(topic)


def test_the_unread_total_is_the_servers_and_a_settled_row_does_not_change_it() -> None:
    (room,) = _rooms(ts.room(ts.ROOM_A, "A", [ts.topic("t1", "One", "2026-02-01T00:00:00Z")]))
    marks = state.ReadMarks()
    # Two rows listing one message in common: the rows add up to 5, the server says 4.
    marks.merge_unread([_row("t1", 2), _row("t2", 3)], {}, total=4)
    assert marks.unread_total == 4
    marks.merge_unread([_row("t1", 2)], {})  # an answer without a total keeps the last one
    assert marks.unread_total == 4
    assert marks.settle([room]) is False
    assert marks.unread_total == 4


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
    assert [r.kind for r in rows][:5] == ["own_app", "own_app", "rule", "pepper", "room"]
    assert next(r for r in rows if r.kind == "pepper").unread is True
    assert not any(r.unread for r in rows if r.kind in ("own_app", "rule"))
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
    room_row = next(r for r in collapsed if r.kind == "room")
    assert room_row.expandable and not room_row.expanded


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
    assert next(r for r in rows if r.kind == "pepper").conversation == state.PEPPER
    assert all(r.conversation is None for r in rows if r.kind in ("own_app", "rule"))
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
    marks.merge_unread([_row("a1", 1), _row("b1", 1)], {})
    a1, b1 = state.Conversation(ts.ROOM_A, "a1"), state.Conversation(ts.ROOM_B, "b1")
    kwargs = {"pepper_dot": False}
    assert state.next_unread(rooms, marks, current=state.PEPPER, **kwargs) == a1
    assert state.next_unread(rooms, marks, current=a1, **kwargs) == b1
    assert state.next_unread(rooms, marks, current=b1, **kwargs) == a1  # wraps
    assert state.next_unread(rooms, marks, current=a1, step=-1, **kwargs) == b1  # wraps back
    assert state.any_unread(rooms, marks, pepper_dot=False)
    marks.merge_unread([_row("a1", 0), _row("b1", 0)], {})
    assert state.next_unread(rooms, marks, current=a1, pepper_dot=True) == state.PEPPER
    assert state.next_unread(rooms, marks, current=a1, **kwargs) is None
    assert not state.any_unread(rooms, marks, pepper_dot=False)


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


def test_dm_tracker_a_reply_already_seen_elsewhere_does_not_light_the_dot() -> None:
    dm = state.DmTracker()
    dm.update([_entry("1", "assistant")], is_open=False)
    dm.update([_entry("1", "assistant"), _entry("2", "assistant")], is_open=False, seen={"2"})
    assert dm.dot is False  # the reply strip showed it
    dm.update(
        [_entry("1", "assistant"), _entry("2", "assistant"), _entry("3", "assistant")],
        is_open=False,
        seen={"2"},
    )
    assert dm.dot is True


def test_dm_tracker_marking_one_reply_seen_keeps_the_dot_for_the_others() -> None:
    dm = state.DmTracker()
    dm.update([_entry("1", "assistant")], is_open=False)
    rows = [_entry("1", "assistant"), _entry("2", "assistant"), _entry("3", "assistant")]
    dm.update(rows, is_open=False)
    assert dm.mark_seen("2") is False
    assert dm.dot is True  # "3" is still unread
    assert dm.mark_seen("9") is False  # never lit it
    assert dm.mark_seen("3") is True
    assert dm.dot is False
    dm.update([*rows, _entry("4", "assistant")], is_open=False)
    dm.read()
    assert (dm.dot, dm.unseen) == (False, set())


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


# ---------------------------------------------------------------- posting (phase 2)

_ROSTER = [
    api_client.GroupRosterMember(person_id="pepper", display_name="Pepper", is_pepper=True),
    api_client.GroupRosterMember(person_id="p-al", display_name="Al"),
    api_client.GroupRosterMember(person_id="p-allen", display_name="Allen Hu"),
    api_client.GroupRosterMember(person_id="p-ada", display_name="Ada Lovelace"),
    api_client.GroupRosterMember(person_id="p-ada2", display_name="Ada"),
]


def test_mention_ids_match_whole_names_longest_first() -> None:
    assert state.mention_ids("hi @Allen Hu and @al", _ROSTER) == ("p-allen", "p-al")
    assert state.mention_ids("@Allenby", _ROSTER) == ()  # a name must end at a non-word char
    assert state.mention_ids("@Ada Lovelace", _ROSTER) == ("p-ada",)  # the span is consumed
    assert state.mention_ids("@Ada Lovelace and @Ada.", _ROSTER) == ("p-ada", "p-ada2")
    assert state.mention_ids("mail al@example.com", _ROSTER) == ()
    # The @ starts the text or follows whitespace, as in mention_query: e-mail never mentions.
    assert state.mention_ids("contact me@example.com or help@ada.com", _ROSTER) == ()
    assert state.mention_ids("x@Ada Lovelace", _ROSTER) == ()
    assert state.mention_ids("@ada first", _ROSTER) == ("p-ada2",)
    assert state.mention_ids("line one\n@Ada", _ROSTER) == ("p-ada2",)
    assert state.mention_ids("no mentions", []) == ()


def test_mention_query_needs_an_at_that_starts_a_word() -> None:
    assert state.mention_query("hello @Al") == "Al"
    assert state.mention_query("@") == ""
    assert state.mention_query("@Ada Lo") == "Ada Lo"
    assert state.mention_query("me@example") is None
    assert state.mention_query("plain text") is None
    assert state.mention_query("line one\n@x") is not None


def test_roster_matches_any_word_prefix_in_roster_order() -> None:
    assert [m.person_id for m in state.roster_matches("lo", _ROSTER)] == ["p-ada"]
    assert [m.person_id for m in state.roster_matches("al", _ROSTER)] == ["p-al", "p-allen"]
    assert len(state.roster_matches("", _ROSTER, limit=2)) == 2


def test_quote_snippet_is_the_first_non_blank_line_cut_short() -> None:
    assert state.quote_snippet("\n  first line \nsecond") == "first line"
    long = "x" * (state.QUOTE_SNIPPET_CHARS + 10)
    snippet = state.quote_snippet(long)
    assert len(snippet) == state.QUOTE_SNIPPET_CHARS and snippet.endswith("…")
    assert state.quote_snippet("") == ""


def test_pending_sends_retry_keeps_the_key_and_confirm_drops_stored_rows() -> None:
    conv = state.Conversation(ts.ROOM_A, ts.TOPIC_A1)
    other = state.Conversation(ts.ROOM_A, ts.TOPIC_A2)
    sends = state.PendingSends()
    sends.add(state.PendingSend(key="k1", conversation=conv, body="one"))
    sends.add(state.PendingSend(key="k2", conversation=other, body="two"))
    first = sends.next_queued()
    assert first is not None and first.key == "k1"
    assert not sends.retry("k1")  # only a failed send is retried
    first.status, first.error = "failed", "HTTP 500"
    assert sends.next_queued() is not None and sends.next_queued().key == "k2"
    assert sends.retry("k1")
    assert (first.key, first.status, first.error) == ("k1", "queued", "")
    assert not sends.retry("missing")
    first.status, first.server_id = "sent", "srv-1"
    assert sends.confirm(other, {"srv-1"}) == []  # another topic's store
    assert sends.confirm(conv, {"m0"}) == []
    assert sends.confirm(conv, {"srv-1"}) == ["k1"]
    assert [p.key for p in sends.items] == ["k2"]
    assert sends.in_conversation(other)[0].body == "two"


def _entry(
    eid: str,
    role: str,
    text: str = "t",
    at: str = "2026-09-16T09:00:00Z",
    tid: str | None = None,
) -> api_client.ChatHistoryEntry:
    return api_client.ChatHistoryEntry(
        id=eid, role=role, text=text, channel="terminal", created_at=at, trajectory_id=tid
    )


def test_dm_turn_lands_by_id_or_by_snapshot_only_while_checking() -> None:
    before = {"h1": _entry("h1", "user"), "h2": _entry("h2", "assistant")}
    after = {
        **before,
        "u1": _entry("u1", "user", "q", "2026-09-16T10:00:00Z"),
        "a1": _entry("a1", "assistant", "t", "2026-09-16T10:00:05Z"),
    }
    turn = state.DmTurn(text="q", snapshot=frozenset(before))
    assert not turn.user_landed(after)  # queued: never sent, so never in history
    turn.phase = "streaming"
    assert turn.user_landed(after) and not turn.user_landed(before)
    assert not turn.reply_landed(after)  # a reply is only guessed after a drop
    turn.phase = "checking"
    assert turn.reply_landed(after) and not turn.reply_landed(before)
    turn.user_id, turn.assistant_id = "u9", "a9"
    assert not turn.user_landed(after) and not turn.reply_landed(after)
    assert not turn.gave_up(100.0)
    turn.lost_at = 100.0
    assert not turn.gave_up(100.0 + state.DM_RECONCILE_WINDOW - 1)
    assert turn.gave_up(100.0 + state.DM_RECONCILE_WINDOW)


def test_an_answered_turn_gives_up_on_its_rows_after_the_window_and_is_not_active() -> None:
    turn = state.DmTurn(text="q", phase="done", user_id="u1", assistant_id="a1")
    assert not turn.active
    assert turn.known_ids() == {"u1", "a1"}
    assert not turn.gave_up(1e9)  # no done_at yet
    turn.done_at = 100.0
    assert not turn.gave_up(100.0 + state.DM_RECONCILE_WINDOW - 1)
    assert turn.gave_up(100.0 + state.DM_RECONCILE_WINDOW)
    turn.lost_at = 0.0  # only the window of the turn's own phase counts
    assert not turn.gave_up(100.0 + state.DM_RECONCILE_WINDOW - 1)
    assert state.DmTurn(text="q", phase="streaming").active
    assert state.DmTurn(text="q", phase="checking").active
    assert not state.DmTurn(text="q").active
    assert state.DmTurn(text="q").known_ids() == set()


# ---------------------------------------------------------------------------
# Mentions: the server's ``has_unread_mention`` flag


def _unread_room():
    (room,) = _rooms(
        ts.room(
            ts.ROOM_A,
            "A",
            [
                ts.topic("t1", "One", "2026-02-01T00:00:00Z"),
                ts.topic("t2", "Two", "2026-02-01T00:00:00Z"),
            ],
        )
    )
    marks = state.ReadMarks()
    marks.merge_unread([_row("t1", 2, mention=True), _row("t2", 1)], {})
    return room, marks


def test_a_mention_shows_only_while_the_topic_is_unread() -> None:
    room, marks = _unread_room()
    t1, t2 = room.topics
    assert marks.is_mentioned(t1) and not marks.is_mentioned(t2)
    assert marks.room_mentioned(room)
    assert state.any_mention([room], marks)
    assert marks.mentioned_topics([room]) == {"t1"}
    (archived,) = _rooms(
        ts.room(ts.ROOM_A, "A", [ts.topic("t1", "One", "2026-02-01T00:00:00Z", archived_at="x")])
    )
    assert marks.mentioned_topics([archived]) == set()  # an archived topic shows no mention
    marks.merge_unread([_row("t1", 0, mention=True)], {})  # a flag on a read topic is not shown
    assert not marks.is_mentioned(t1)
    assert not state.any_mention([room], marks)


def test_sidebar_rows_carry_the_mention_on_topic_and_collapsed_room() -> None:
    room, marks = _unread_room()
    for manual, keys in (
        ({}, {"room:" + ts.ROOM_A}),
        ({ts.ROOM_A: True}, {"room:" + ts.ROOM_A, "topic:t1"}),
    ):
        rows = state.build_sidebar(
            [room], marks, current=state.PEPPER, manual=manual, show_all=set(), pepper_dot=False
        )
        assert {r.key for r in rows if r.mention} == keys
    assert {
        r.key
        for r in state.build_sidebar(
            [room],
            marks,
            current=state.PEPPER,
            manual={ts.ROOM_A: True},
            show_all=set(),
            pepper_dot=False,
        )
        if r.unread and r.kind == "topic"
    } == {"topic:t1", "topic:t2"}


def test_a_dropped_turn_does_not_take_an_unrelated_assistant_row_as_its_reply() -> None:
    before = {"h1": _entry("h1", "assistant", "old", "2026-09-16T09:00:00Z")}
    turn = state.DmTurn(text="q", phase="checking", snapshot=frozenset(before))
    # A reply to another device's message lands first; the turn's own user row is not in yet.
    stray = {**before, "x1": _entry("x1", "assistant", "proactive", "2026-09-16T10:00:00Z")}
    assert turn.reply_id(stray) is None and not turn.reply_landed(stray)
    assert turn.found_reply(stray) is None
    # The user row arrives later than the stray row: the stray row precedes it, so still no match.
    both = {**stray, "u1": _entry("u1", "user", "q", "2026-09-16T10:01:00Z")}
    assert turn.reply_id(both) is None
    # The real reply follows the user row.
    done = {**both, "a1": _entry("a1", "assistant", "the answer", "2026-09-16T10:01:05Z")}
    assert turn.reply_id(done) == "a1" and turn.found_reply(done) == "the answer"


def test_a_dropped_turn_with_an_unidentifiable_user_row_does_not_guess() -> None:
    turn = state.DmTurn(text="q", phase="checking")
    # An assistant row, and a user row whose text is not what was sent (another device's).
    entries = {
        "u1": _entry("u1", "user", "something else", "2026-09-16T10:00:00Z"),
        "a1": _entry("a1", "assistant", "reply", "2026-09-16T10:00:05Z"),
    }
    assert turn.reply_id(entries) is None and not turn.reply_landed(entries)
    # A user row already in the snapshot is not this turn's either.
    old = {"u0": _entry("u0", "user", "q", "2026-09-16T09:00:00Z"), **entries}
    seen = state.DmTurn(text="q", phase="checking", snapshot=frozenset({"u0"}))
    assert seen.reply_id(old) is None


def test_a_dropped_turn_takes_only_the_reply_in_its_user_rows_trajectory() -> None:
    before = {"h1": _entry("h1", "assistant", "old", "2026-09-16T09:00:00Z")}
    turn = state.DmTurn(text="q", phase="checking", snapshot=frozenset(before))
    entries = {
        **before,
        "u1": _entry("u1", "user", "q", "2026-09-16T10:01:00Z", tid="T1"),
        # A reminder (no trajectory) and a reply to another device (its own) land in between.
        "p1": _entry("p1", "assistant", "Reminder: standup", "2026-09-16T10:01:02Z"),
        "o1": _entry("o1", "assistant", "other device", "2026-09-16T10:01:03Z", tid="T2"),
    }
    assert turn.reply_id(entries) is None and not turn.reply_landed(entries)
    done = {**entries, "a1": _entry("a1", "assistant", "the answer", "2026-09-16T10:01:05Z", "T1")}
    assert turn.reply_id(done) == "a1" and turn.found_reply(done) == "the answer"


def test_a_dropped_turn_with_an_unlinked_user_row_skips_another_turns_reply() -> None:
    turn = state.DmTurn(text="q", phase="checking")
    # The user row is not linked yet; a later row carrying a trajectory belongs to another turn.
    entries = {
        "u1": _entry("u1", "user", "q", "2026-09-16T10:01:00Z"),
        "o1": _entry("o1", "assistant", "other device", "2026-09-16T10:01:03Z", tid="T2"),
    }
    assert turn.reply_id(entries) is None
    # The error path: neither row has a trajectory, and the reply still matches.
    failed = {**entries, "a1": _entry("a1", "assistant", "sorry", "2026-09-16T10:01:04Z")}
    assert turn.reply_id(failed) == "a1"


def test_a_dropped_turn_shows_its_bubble_until_its_own_user_row_lands() -> None:
    before = {"h1": _entry("h1", "assistant", "old", "2026-09-16T09:00:00Z")}
    turn = state.DmTurn(text="q", phase="checking", snapshot=frozenset(before))
    other = {**before, "u9": _entry("u9", "user", "lunch?", "2026-09-16T10:01:00Z")}
    assert not turn.user_landed(other)  # another device's row is not this turn's
    mine = {**other, "u1": _entry("u1", "user", "q", "2026-09-16T10:01:01Z")}
    assert turn.user_landed(mine)
