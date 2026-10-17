"""The P3 requests (older pages, reactions, edit, delete, new topic, room info) against the
fake server: wire shapes and paging, with no Textual."""

from __future__ import annotations

from pepper_cli.tui import poller, state
from tests import tui_support as ts


def _many(server: ts.FakeServer, count: int) -> state.Conversation:
    server.messages[ts.TOPIC_A1] = [
        ts.message(f"m{i:02d}", f"2026-09-16T10:{i:02d}:00+00:00", f"body {i}")
        for i in range(count)
    ]
    return state.Conversation(ts.ROOM_A, ts.TOPIC_A1)


def test_older_pages_walk_back_to_the_start_and_then_report_nothing_older() -> None:
    server = ts.standard_server()
    server.page_size = 4
    conv = _many(server, 10)
    with server.client() as api:
        newest = poller.fetch_topic(api, conv, since=None)
        assert [m.id for m in newest.messages] == ["m06", "m07", "m08", "m09"]
        assert newest.next_before == "6"
        middle = poller.fetch_older(api, conv, before=newest.next_before, limit=4)
        assert [m.id for m in middle.messages] == ["m02", "m03", "m04", "m05"]
        oldest = poller.fetch_older(api, conv, before=middle.next_before or "", limit=4)
        assert [m.id for m in oldest.messages] == ["m00", "m01"]
        assert oldest.next_before is None
        assert poller.fetch_older(api, state.PEPPER, before="1", limit=4).messages == []
    queries = [q for _, p, q in server.requests if p.endswith("/messages")]
    assert queries[1] == {"before": "6", "limit": "4"}


def test_react_adds_then_clears_and_answers_the_servers_chip_set() -> None:
    server = ts.standard_server()
    with server.client() as api:
        added = poller.react(api, ts.ROOM_A, "m1", emoji="👍", clear=False)
        assert added == [{"emoji": "👍", "count": 1, "mine": True}]
        both = poller.react(api, ts.ROOM_A, "m1", emoji="🎉", clear=False)
        assert [c["emoji"] for c in both] == ["👍", "🎉"]
        cleared = poller.react(api, ts.ROOM_A, "m1", emoji="👍", clear=True)
        assert cleared == [{"emoji": "🎉", "count": 1, "mine": True}]
    assert server.reactions_sent == [
        ("add", "m1", "👍"),
        ("add", "m1", "🎉"),
        ("clear", "m1", "👍"),
    ]


def test_edit_and_delete_change_the_row_the_server_holds() -> None:
    server = ts.standard_server()
    with server.client() as api:
        answer = poller.edit_message(api, ts.ROOM_A, "m1", body="new words")
        assert (answer["raw_body"], answer["is_edited"]) == ("new words", True)
        gone = poller.delete_message(api, ts.ROOM_A, "m1")
        assert gone["deleted"] is True
    row = server.messages[ts.TOPIC_A1][0]
    assert (row["is_edited"], row["is_deleted"]) == (True, True)
    assert server.edits == [{"id": "m1", "body": "new words"}]
    assert server.deleted == ["m1"]


def test_create_topic_room_info_and_the_topic_rail() -> None:
    server = ts.standard_server()
    with server.client() as api:
        made = poller.create_topic(api, ts.ROOM_A, name="Packing")
        assert made["name"] == "Packing"
        assert poller.fetch_room_info(api, ts.ROOM_A)["display_name"] == "Trip planning"
        rail = poller.fetch_topic_rail(api, ts.ROOM_A)
    assert rail[0].name == "Packing"
    assert server.created_topics == ["Packing"]
