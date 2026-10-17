"""Unit tests of the P3 (parity) logic in ``pepper_cli.tui.state``: folds, older pages,
reactions, link resolution and the Ctrl+K fuzzy jump. ``now`` is always injected."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pepper_cli import api_client
from pepper_cli.tui import state
from tests import tui_support as ts

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
BASE = "https://pepper.example.com"
ROOM_C = "00000000-0000-0000-0000-0000000000a3"


def _rooms(*rooms: dict) -> list[api_client.GroupSummary]:
    body = {"enabled": True, "degraded": False, "total": len(rooms), "archived_total": 0}
    return api_client._group_list_from_dict({**body, "groups": list(rooms)}).groups


def _msg(mid: str, at: str) -> api_client.GroupMessage:
    return api_client.group_message_from_dict(ts.message(mid, at))


def _over(**over: Any) -> api_client.GroupMessage:
    return api_client.group_message_from_dict(ts.message("m1", "2026-09-16T17:05:00Z", **over))


def _sidebar(
    rooms: list[api_client.GroupSummary],
    *,
    current: state.Conversation = state.PEPPER,
    marks: state.ReadMarks | None = None,
    folds: state.SidebarFolds | None = None,
    now: datetime | None = None,
) -> list[state.SidebarRow]:
    if marks is None:
        marks = state.ReadMarks()
        marks.seed_first_sight(rooms)  # a fresh install reads everything as read
    return state.build_sidebar(
        rooms,
        marks,
        current=current,
        manual={},
        show_all=set(),
        pepper_dot=False,
        folds=folds,
        now=now,
    )


def _labels(rows: list[state.SidebarRow], kind: str) -> list[str]:
    return [r.label for r in rows if r.kind == kind]


def test_stale_rooms_fold_unless_open_or_unread_and_no_clock_means_no_fold() -> None:
    fresh = ts.room(ts.ROOM_A, "Fresh", [ts.topic("a1", "A", "2026-09-30T00:00:00Z")])
    stale = ts.room(ts.ROOM_B, "Stale", [ts.topic("b1", "B", "2026-09-01T00:00:00Z")])
    empty = ts.room(ROOM_C, "Empty", [])
    rooms = _rooms(fresh, stale, empty)
    rows = _sidebar(rooms, now=NOW)
    assert _labels(rows, "room") == ["Fresh", "Empty"]
    fold = rows[-1]
    assert (fold.kind, fold.key, fold.label) == ("inactive", "inactive", "Show inactive (1)")
    shown = _sidebar(rooms, now=NOW, folds=state.SidebarFolds(inactive=True))
    assert _labels(shown, "room") == ["Fresh", "Stale", "Empty"]
    assert shown[-1].label == "Hide inactive (1)"
    assert shown[-1].expanded is True
    opened = _sidebar(rooms, now=NOW, current=state.Conversation(ts.ROOM_B, "b1"))
    assert "Stale" in _labels(opened, "room")
    assert not any(r.kind == "inactive" for r in opened)
    assert not any(r.kind == "inactive" for r in _sidebar(rooms))
    marks = state.ReadMarks()
    marks.merge_server({"a1": "2026-09-30T00:00:00Z", "b1": "2026-08-01T00:00:00Z"})
    unread = _sidebar(rooms, now=NOW, marks=marks)
    assert "Stale" in _labels(unread, "room")


def test_room_inactive_boundary_is_seven_days_and_archived_topics_do_not_count() -> None:
    (room,) = _rooms(ts.room(ts.ROOM_A, "A", [ts.topic("a1", "A", "2026-09-24T12:00:00Z")]))
    assert not state.room_inactive(room, now=NOW)
    assert state.room_inactive(room, now=NOW + timedelta(seconds=1))
    (archived,) = _rooms(
        ts.room(
            ts.ROOM_A,
            "A",
            [ts.topic("a1", "A", "2026-01-01T00:00:00Z", archived_at="2026-01-02T00:00:00Z")],
        )
    )
    assert state.room_recency(archived) is None
    assert not state.room_inactive(archived, now=NOW)


def test_archived_fold_and_new_topic_row_close_an_expanded_room() -> None:
    rooms = _rooms(
        ts.room(
            ts.ROOM_A,
            "A",
            [
                ts.topic("a1", "Live", "2026-09-30T00:00:00Z"),
                ts.topic("a2", "Old", "2026-09-01T00:00:00Z", archived_at="2026-09-02T00:00:00Z"),
            ],
        )
    )
    current = state.Conversation(ts.ROOM_A, "a1")
    folded = _sidebar(rooms, current=current)
    assert [r.kind for r in folded] == ["pepper", "room", "topic", "archived", "newtopic"]
    assert folded[3].label == "Show archived (1)"
    assert folded[4].room_id == ts.ROOM_A
    unfolded = _sidebar(rooms, current=current, folds=state.SidebarFolds(archived={ts.ROOM_A}))
    assert _labels(unfolded, "topic") == ["Live", "Old"]
    assert _labels(unfolded, "archived") == ["Hide archived (1)"]
    assert not next(r for r in unfolded if r.topic_id == "a2").unread


def test_archived_topics_merge_the_fetched_rail_without_duplicates() -> None:
    (room,) = _rooms(ts.room(ts.ROOM_A, "A", [ts.topic("a1", "One", "2026-09-30T00:00:00Z")]))
    older = api_client.GroupTopicSummary(
        topic_id="a9", name="Nine", last_activity_at="2026-01-01T00:00:00Z", archived_at="x"
    )
    live = api_client.GroupTopicSummary(
        topic_id="a1", name="One", last_activity_at="2026-09-30T00:00:00Z"
    )
    assert [t.topic_id for t in state.archived_topics(room, [older, live])] == ["a9"]
    rows = _sidebar(
        [room],
        current=state.Conversation(ts.ROOM_A, "a1"),
        folds=state.SidebarFolds(fetched={ts.ROOM_A: [older, older]}),
    )
    assert next(r for r in rows if r.kind == "archived").hidden_count == 1


def test_older_pages_merge_without_moving_the_cursor_and_track_exhaustion() -> None:
    store = state.MessageStore()
    assert not store.has_older
    store.add([_msg("m5", "2026-09-16T17:05:00+00:00"), _msg("m6", "2026-09-16T17:06:00+00:00")])
    store.note_newest_page("cur-1")
    assert store.has_older
    cursor = store.cursor
    new = store.add_older(
        [_msg("m3", "2026-09-16T17:03:00+00:00"), _msg("m5", "2026-09-16T17:05:00+00:00")], "cur-2"
    )
    assert new == ["m3"]
    assert store.cursor == cursor
    assert [m.id for m in store.ordered()] == ["m3", "m5", "m6"]
    assert store.older == "cur-2"
    store.note_newest_page("cur-1")  # a reconcile read must not rewind the paging cursor
    assert store.older == "cur-2"
    store.add_older([_msg("m1", "2026-09-16T17:01:00+00:00")], None)
    assert not store.has_older


def test_set_reactions_replaces_the_chip_set_of_one_row_only() -> None:
    store = state.MessageStore()
    store.add([_msg("m1", "2026-09-16T17:05:00+00:00")])
    chips = [{"emoji": "👍", "count": 1, "mine": True}]
    store.set_reactions("m1", chips)
    store.set_reactions("gone", chips)
    assert store.rows["m1"].reactions == chips
    assert "gone" not in store.rows


def test_reaction_action_toggles_only_your_own_chip() -> None:
    chips = [{"emoji": "👍", "count": 2, "mine": True}, {"emoji": "❤️", "count": 1, "mine": False}]
    assert state.reaction_action(chips, "👍") == "clear"
    assert state.reaction_action(chips, "❤️") == "add"
    assert state.reaction_action(chips, "🎉") == "add"
    assert state.reaction_action([], "👍") == "add"


def test_only_your_live_messages_are_editable() -> None:
    assert state.is_own_editable(_over(is_you=True))
    assert not state.is_own_editable(_over(is_you=False))
    assert not state.is_own_editable(_over(is_you=True, is_deleted=True))
    assert not state.is_own_editable(_over(is_you=True, is_broken=True))


def test_safe_http_url_refuses_every_scheme_but_http_and_https() -> None:
    assert state.safe_http_url("https://a.example/x?y=1", base=BASE) == "https://a.example/x?y=1"
    assert state.safe_http_url("/apps/notes?v=1", base=BASE + "/") == BASE + "/apps/notes?v=1"
    for bad in ("javascript:alert(1)", "file:///etc/passwd", "data:text/html,x", "ftp://h/x", " "):
        assert state.safe_http_url(bad, base=BASE) is None
    assert state.safe_http_url(None, base=BASE) is None


def test_a_malformed_address_is_skipped_not_raised() -> None:
    # urlsplit raises ValueError on these; a chat message must not be able to crash the TUI.
    for bad in ("https://[", "http://[::1", "https://[oops]/x"):
        assert state.safe_http_url(bad, base=BASE) is None
        assert state.url_domain(bad) == ""
    message = _over(
        raw_body="broken https://[ then https://ok.example/a",
        link_previews=[{"url": "https://[", "title": "P"}],
    )
    assert state.first_url(message, base=BASE) == "https://ok.example/a"
    assert state.first_url(_over(raw_body="only https://["), base=BASE) is None


def test_a_bare_link_keeps_balanced_brackets_and_drops_the_punctuation_around_it() -> None:
    def urls(body: str) -> list[str]:
        return state.message_urls(_over(raw_body=body), base=BASE)

    wiki = "https://en.wikipedia.org/wiki/Pixel_(computer)"
    assert urls(f"see {wiki} for more") == [wiki]
    assert urls(f"see {wiki}.") == [wiki]
    assert urls("local http://[::1]:8080/path here") == ["http://[::1]:8080/path"]
    assert urls("(see https://x.com/a)") == ["https://x.com/a"]
    assert urls("(see https://x.com/a.)") == ["https://x.com/a"]
    assert urls("it is at https://x.com/a. Then https://y.com/b, and!") == [
        "https://x.com/a",
        "https://y.com/b",
    ]
    assert urls("[text](https://x.com/a)") == ["https://x.com/a"]
    assert urls("[https://x.com](https://y.com)") == ["https://x.com", "https://y.com"]


def test_first_url_orders_links_then_attachments_then_cards() -> None:
    card = {
        "kind": "card",
        "app_key": "notes",
        "slug": "n",
        "title": "Notes",
        "principal": "p",
        "url": "/apps/n",
        "summary": {"lines": []},
    }
    attachment = {"id": "a1", "filename": "f", "url": "/groups/g/attachments/a1"}
    full = _over(
        raw_body="see https://body.example/a and more",
        link_previews=[{"url": "https://preview.example/p", "title": "P"}],
        attachments=[attachment],
        cards=[card],
    )
    assert state.message_urls(full, base=BASE) == [
        "https://preview.example/p",
        "https://body.example/a",
        BASE + "/groups/g/attachments/a1",
        BASE + "/apps/n",
    ]
    assert state.first_url(full, base=BASE) == "https://preview.example/p"
    assert state.first_url(_over(attachments=[attachment], cards=[card]), base=BASE) == (
        BASE + "/groups/g/attachments/a1"
    )
    assert state.first_url(_over(cards=[card]), base=BASE) == BASE + "/apps/n"
    hostile = _over(
        link_previews=[{"url": "javascript:alert(1)"}, {"url": None}, {}],
        attachments=[{"url": "file:///x"}],
    )
    assert state.first_url(hostile, base=BASE) is None
    assert state.url_domain("https://www.example.com/a/b") == "example.com"
    assert state.url_domain("mailto:x") == ""


def test_fuzzy_score_is_an_ordered_subsequence_that_rewards_tight_matches() -> None:
    assert state.fuzzy_score("zzz", "Trip planning") is None
    assert state.fuzzy_score("gnp", "Trip planning") is None
    assert state.fuzzy_score("", "anything") == 0
    tight = state.fuzzy_score("trip", "Trip planning")
    loose = state.fuzzy_score("trip", "Team rotations in Paris")
    assert tight is not None
    assert loose is not None
    assert tight > loose
    assert state.fuzzy_score("TRIP", "trip") == state.fuzzy_score("trip", "TRIP")
    assert state.fuzzy_score("t p", "trip planning") == state.fuzzy_score("tp", "trip planning")


def test_jump_targets_list_live_topics_and_filter_ranks_best_first() -> None:
    rooms = _rooms(
        ts.room(
            ts.ROOM_A,
            "Trip planning",
            [
                ts.topic("a1", "Flights", "2026-09-30T00:00:00Z"),
                ts.topic("a2", "Hotels", "2026-09-20T00:00:00Z"),
                ts.topic("a3", "Gone", "2026-09-10T00:00:00Z", archived_at="2026-09-11T00:00:00Z"),
            ],
        ),
        ts.room(ts.ROOM_B, "Book club", []),
    )
    targets = state.jump_targets(rooms)
    assert [t.label for t in targets] == [
        "Pepper",
        "Trip planning",
        "Trip planning › Flights",
        "Trip planning › Hotels",
    ]
    assert targets[0].conversation == state.PEPPER
    assert targets[1].conversation == state.Conversation(ts.ROOM_A, "a1")
    assert [t.label for t in state.fuzzy_filter("hot", targets)] == ["Trip planning › Hotels"]
    assert state.fuzzy_filter("", targets) == targets
    assert state.fuzzy_filter("qqq", targets) == []
    assert [t.label for t in state.fuzzy_filter("flights", targets)] == ["Trip planning › Flights"]


def test_expect_reconcile_makes_the_next_poll_a_full_read() -> None:
    store = state.MessageStore()
    assert store.next_is_reconcile() is False
    store.expect_reconcile()
    assert store.next_is_reconcile() is True
    assert store.next_is_reconcile() is False


def _store_with(*messages: api_client.GroupMessage) -> state.MessageStore:
    store = state.MessageStore()
    store.add(messages)
    return store


def test_an_edit_reply_patches_the_row_and_a_stale_copy_does_not_undo_it() -> None:
    before = _over(raw_body="old", is_you=True)
    store = _store_with(before)
    store.apply_edit(
        "m1",
        {"raw_body": "new", "rendered_html": "<p>new</p>", "edited_at": "2026-09-16T18:00:00Z"},
    )
    row = store.rows["m1"]
    assert (row.raw_body, row.rendered_html, row.is_edited) == ("new", "<p>new</p>", True)
    store.add([before])  # a poll that started before the edit landed
    assert store.rows["m1"].raw_body == "new"
    older_edit = _over(raw_body="mid", is_edited=True, edited_at="2026-09-16T17:59:00Z")
    store.add([older_edit])
    assert store.rows["m1"].raw_body == "new"
    newer = _over(raw_body="newer", is_edited=True, edited_at="2026-09-16T18:05:00+00:00")
    store.add([newer])
    assert store.rows["m1"].raw_body == "newer"
    store.apply_edit("missing", {"raw_body": "x"})  # a row no longer held: nothing happens
    assert "missing" not in store.rows


def test_an_edit_reply_missing_fields_keeps_what_the_row_had() -> None:
    store = _store_with(_over(raw_body="old"))
    store.apply_edit("m1", {"raw_body": None})
    assert store.rows["m1"].raw_body == "old"
    assert store.rows["m1"].is_edited


def test_a_delete_reply_leaves_a_tombstone_a_stale_copy_cannot_revive() -> None:
    live = _over(raw_body="oops")
    store = _store_with(live)
    store.apply_delete("m1", {"deleted": True, "deleted_at": "2026-09-16T18:00:00Z"})
    row = store.rows["m1"]
    assert (row.is_deleted, row.raw_body, row.deleted_at) == (True, "", "2026-09-16T18:00:00Z")
    store.add([live])
    assert store.rows["m1"].is_deleted
    store.apply_delete("missing", {})
    assert "missing" not in store.rows
