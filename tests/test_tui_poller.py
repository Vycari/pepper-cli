"""The blocking half of the poll loops, against the fake server."""

from __future__ import annotations

import httpx
import pytest

from pepper_cli import api_client
from pepper_cli.tui import poller, state
from tests import tui_support as ts


def test_rail_sends_if_none_match_and_a_304_changes_nothing() -> None:
    server = ts.standard_server()
    with server.client() as api:
        rail = poller.RailPoller(api)
        first = rail.step()
        assert first.groups is not None
        assert first.server_marks[ts.TOPIC_A1] == "2026-09-16T17:30:00+00:00"
        rail.commit(first)
        second = rail.step()
        assert second.groups is None
        assert second.server_marks == {}
    gets = [r for r in server.requests if r[1] == "/api/v1/groups"]
    assert len(gets) == 2


def test_rail_reads_every_rooms_unread_in_one_request_and_only_when_a_room_moved() -> None:
    server = ts.standard_server()
    with server.client() as api:
        rail = poller.RailPoller(api)
        first = rail.step()
        rail.commit(first)
        assert _unread_reads(server) == 1  # one request for both rooms on the first load
        assert {r.topic_id for r in first.unread} >= {ts.TOPIC_A1, ts.TOPIC_B1}
        server.rooms[1]["topics"][0]["last_activity_at"] = "2026-09-17T00:00:00+00:00"
        server.bump()
        update = rail.step()
        assert update.groups is not None
        rail.commit(update)
        assert _unread_reads(server) == 2
        # Same list again with a new ETag (e.g. only a member count changed): no unread request.
        server.bump()
        rail.commit(rail.step())
        assert _unread_reads(server) == 2
        assert not [r for r in server.requests if r[1].endswith("/read")]  # no per-room reads


def test_the_unread_total_is_the_servers_not_a_sum_of_rows() -> None:
    server = ts.standard_server()
    server.unread_override = {
        ts.TOPIC_A1: {"unread_count": 2},
        ts.TOPIC_B1: {"unread_count": 3},
    }
    server.unread_total = 3  # the two rows list one message in common
    with server.client() as api:
        update = poller.RailPoller(api).step()
    assert sum(r.unread_count for r in update.unread) > 3
    assert update.unread_total == 3


def _unread_reads(server: ts.FakeServer) -> int:
    return len([r for r in server.requests if r[1] == "/api/v1/groups/unread"])


def test_refetch_all_reads_every_rooms_marks_once_with_nothing_moved() -> None:
    server = ts.standard_server()
    with server.client() as api:
        rail = poller.RailPoller(api)
        rail.commit(rail.step())
        server.marks[ts.TOPIC_B1] = "2026-09-17T00:00:00+00:00"  # a read elsewhere: list unchanged
        rail.refetch_all()
        update = rail.step()
        assert update.groups is not None  # a 200 despite the saved ETag
        assert update.server_marks[ts.TOPIC_B1] == "2026-09-17T00:00:00+00:00"
        rail.commit(update)
        assert _unread_reads(server) == 2
        assert rail.step().groups is None  # once: back to a 304
        assert _unread_reads(server) == 2


def test_refetch_all_survives_the_commit_of_a_step_already_in_flight() -> None:
    server = ts.standard_server()
    with server.client() as api:
        rail = poller.RailPoller(api)
        rail.commit(rail.step())
        in_flight = rail.step()  # started before the request
        rail.refetch_all()
        rail.commit(in_flight)
        update = rail.step()
        assert update.groups is not None and update.full
        rail.commit(update)
        assert _unread_reads(server) == 2


def test_a_catch_up_asked_for_during_a_full_step_gets_its_own() -> None:
    server = ts.standard_server()
    with server.client() as api:
        rail = poller.RailPoller(api)
        rail.commit(rail.step())
        rail.refetch_all()
        in_flight = rail.step()  # a full step, started before the second request
        rail.refetch_all()  # cursor_ahead: another catch-up
        server.marks[ts.TOPIC_B1] = "2026-09-17T00:00:00+00:00"  # read after that step's read
        rail.commit(in_flight)
        update = rail.step()
        assert update.groups is not None  # not a 304: the second request still stands
        assert update.server_marks[ts.TOPIC_B1] == "2026-09-17T00:00:00+00:00"
        rail.commit(update)
        assert rail.step().groups is None


def _list_gets(server: ts.FakeServer) -> list[dict[str, str]]:
    return [h for h in server.headers_seen if "if-none-match" not in h]


def test_nothing_is_committed_until_the_app_commits() -> None:
    """A result that is never applied must not leave its ETag behind a later 304."""
    server = ts.standard_server()
    with server.client() as api:
        rail = poller.RailPoller(api)
        rail.step()  # dropped: never committed
        again = rail.step()
        assert again.groups is not None  # a 200, not a 304 against the dropped ETag
        assert again.server_marks  # and the marks were asked for again
        rail.commit(again)
        assert rail.step().groups is None


def test_a_transport_error_fetching_marks_commits_nothing() -> None:
    server = ts.standard_server()
    server.raise_on["/api/v1/groups/unread"] = httpx.ConnectError("down")
    with server.client() as api:
        rail = poller.RailPoller(api)
        with pytest.raises(httpx.ConnectError):
            rail.step()
        server.raise_on.clear()
        update = rail.step()
    assert update.groups is not None  # no stale If-None-Match: the list comes back
    assert ts.TOPIC_B1 in update.server_marks
    assert ts.TOPIC_A1 in update.server_marks
    assert len(_list_gets(server)) == 2


def test_a_failed_unread_request_is_asked_again_on_the_next_poll() -> None:
    server = ts.standard_server()
    server.fail["/api/v1/groups/unread"] = 502
    with server.client() as api:
        rail = poller.RailPoller(api)
        first = rail.step()
        assert first.groups is not None  # the rail still shows
        assert first.etag is None  # incomplete: the next poll must get a 200
        assert first.unread == [] and first.server_marks == {}
        rail.commit(first)
        server.fail.clear()
        second = rail.step()
        assert second.groups is not None
        assert ts.TOPIC_A1 in second.server_marks and ts.TOPIC_B1 in second.server_marks
        assert second.etag is not None
        rail.commit(second)
        assert rail.step().groups is None
    assert _unread_reads(server) == 2


def test_forget_etag_asks_for_the_full_list() -> None:
    server = ts.standard_server()
    with server.client() as api:
        rail = poller.RailPoller(api)
        rail.commit(rail.step())
        rail.forget_etag()
        update = rail.step()
    assert update.groups is not None
    assert update.server_marks == {}  # nothing moved, so no unread request


def test_rail_disabled_and_degraded_skip_marks() -> None:
    server = ts.standard_server()
    server.degraded = True
    with server.client() as api:
        update = poller.RailPoller(api).step()
    assert update.groups is not None
    assert update.groups.degraded is True
    assert update.server_marks == {}
    assert not _unread_reads(server)


def test_a_failed_unread_request_is_skipped_but_a_401_is_not() -> None:
    server = ts.standard_server()
    server.fail["/api/v1/groups/unread"] = 500
    with server.client() as api:
        update = poller.RailPoller(api).step()
    assert update.groups is not None  # a failed unread read never hides the rail
    assert update.unread == []
    server.fail = {"/api/v1/groups/unread": 401}
    with server.client() as api, pytest.raises(api_client.ApiError) as caught:
        poller.RailPoller(api).step()
    assert caught.value.status == 401


def test_fetch_topic_sends_since_only_when_given() -> None:
    server = ts.standard_server()
    conv = state.Conversation(ts.ROOM_A, ts.TOPIC_A1)
    with server.client() as api:
        assert len(poller.fetch_topic(api, conv, since=None).messages) == 1
        assert poller.fetch_topic(api, conv, since="2026-09-16T17:30:00+00:00").messages == []
        assert poller.fetch_topic(api, state.PEPPER, since=None).messages == []
    queries = [q for _, p, q in server.requests if p.endswith("/messages")]
    assert queries == [{}, {"since": "2026-09-16T17:30:00+00:00"}]


def test_fetch_dm_and_publish() -> None:
    server = ts.standard_server()
    conv = state.Conversation(ts.ROOM_A, ts.TOPIC_A1)
    with server.client() as api:
        assert [e.id for e in poller.fetch_dm(api)] == ["h1", "h2"]
        assert poller.publish_read_mark(api, conv, "2026-09-16T18:00:00+00:00") == (
            "2026-09-16T18:00:00+00:00"
        )
        assert poller.publish_read_mark(api, state.PEPPER, "x") == "x"
    assert server.puts == [(ts.TOPIC_A1, "2026-09-16T18:00:00+00:00")]


def test_rail_step_carries_the_servers_unread_rows_for_the_rooms_it_read() -> None:
    server = ts.standard_server()
    server.marks = {}
    server.floor = "2026-09-16T17:00:00+00:00"
    server.messages[ts.TOPIC_B1] = [ts.message("b1", "2026-09-16T17:30:00+00:00", "hi")]
    with server.client() as api:
        update = poller.RailPoller(api).step()
    by_topic = {row.topic_id: row.unread_count for row in update.unread}
    assert by_topic == {ts.TOPIC_A1: 1, ts.TOPIC_A2: 0, ts.TOPIC_B1: 1}


def _apps_server() -> ts.FakeServer:
    server = ts.standard_server()
    server.launchers[ts.ROOM_A] = [ts.tile("todo", "Todos", ts.ROOM_A)]
    server.todos[ts.ROOM_A] = [
        ts.todo("a", "asked", assignee={"user_id": ts.ME, "label": "Me"}),
        ts.todo("b", "plain"),
        ts.todo("c", "closed", status="done", done_at="2026-10-01T00:00:00Z"),
    ]
    server.notes[ts.ROOM_A] = [ts.note_item("n1", "Packing", "2026-10-27T10:00:00+00:00")]
    server.note_details["n1"] = {
        "id": "n1",
        "name": "Packing",
        "kind": "inline_text",
        "updated_at": "2026-10-27T10:00:00+00:00",
        "etag": '"n1-1"',
        "shared": True,
        "body": "text",
    }
    server.note_details["f1"] = {**server.note_details["n1"], "id": "f1", "kind": "file"}
    return server


def test_the_shelf_lists_installed_apps_none_installed_and_the_unavailable_flag() -> None:
    server = _apps_server()
    server.apps_unavailable.add(ts.ROOM_B)
    with server.client() as api:
        tiles, unavailable = poller.fetch_room_shelf(api, ts.ROOM_A)
        assert [t.slug for t in tiles] == ["todo"]
        assert not unavailable
        assert poller.fetch_room_shelf(api, "no-apps-room") == ([], False)
        assert poller.fetch_room_shelf(api, ts.ROOM_B) == ([], True)


def test_a_rooms_todos_are_one_call_for_every_status_and_mine_sends_assignee_me() -> None:
    server = _apps_server()
    with server.client() as api:
        everything = poller.fetch_todos(api, ts.ROOM_A, mine=False)
        asked = poller.fetch_todos(api, ts.ROOM_A, mine=True)
    assert [t.id for t in everything] == ["a", "b", "c"]
    assert [t.id for t in asked] == ["a"]
    reads = [q for _m, p, q in server.requests if p.endswith(f"/groups/{ts.ROOM_A}/todos")]
    assert reads == [{"status": "all"}, {"status": "all", "assignee": "me"}]


def test_a_room_the_caller_left_is_an_api_error_the_app_can_act_on() -> None:
    server = _apps_server()
    server.fail[f"/api/v1/groups/{ts.ROOM_A}/todos"] = 404
    server.fail["/api/v1/artifacts"] = 403
    with server.client() as api:
        with pytest.raises(api_client.ApiError) as todos:
            poller.fetch_todos(api, ts.ROOM_A, mine=False)
        with pytest.raises(api_client.ApiError) as notes:
            poller.fetch_notes(api, ts.ROOM_A)
    assert (todos.value.status, notes.value.status) == (404, 403)


def test_notes_ask_for_the_rooms_inline_text_artifacts_up_to_200_and_carry_truncated() -> None:
    server = _apps_server()
    server.notes_truncated = True
    with server.client() as api:
        found = poller.fetch_notes(api, ts.ROOM_A)
    assert found.truncated
    assert [n.id for n in found.items] == ["n1"]
    sent = [q for _m, p, q in server.requests if p == "/api/v1/artifacts"]
    assert sent == [{"kind": "inline_text", "limit": "200", "principal": ts.ROOM_A}]


def test_a_note_opens_by_detail_and_anything_else_is_refused() -> None:
    server = _apps_server()
    with server.client() as api:
        assert poller.fetch_note(api, "n1").body == "text"
        with pytest.raises(ValueError, match="not a note"):
            poller.fetch_note(api, "f1")
        with pytest.raises(api_client.ApiError):
            poller.fetch_note(api, "missing")


def _own_server() -> ts.FakeServer:
    server = _apps_server()
    server.own_todos = [
        ts.todo("o1", "Renew passport"),
        ts.todo("o2", "Asked", source="request"),
        ts.todo("o3", "Done", status="done", done_at="2026-10-01T00:00:00+00:00"),
    ]
    server.room_todos = {
        ts.ROOM_A: (
            "Trip planning",
            [
                ts.stored_todo(
                    "r1",
                    "Book flights",
                    source="request",
                    source_ref={"assignee_user_id": ts.ME, "assignee_label": "Me"},
                ),
                ts.stored_todo("r2", "Pack"),
            ],
        )
    }
    return server


def test_your_own_todos_read_your_list_and_your_rooms_in_two_calls() -> None:
    server = _own_server()
    with server.client() as api:
        found = poller.fetch_own_todos(api, mine=False)
    assert [t.id for t in found.todos] == ["o1", "o2", "o3"]
    assert found.rooms is not None
    assert [(r.name, [i.id for i in r.items]) for r in found.rooms] == [
        ("Trip planning", ["r1", "r2"])
    ]
    assert found.rooms[0].items[0].assignee is not None
    reads = [(p, q) for _m, p, q in server.requests if p.startswith("/api/v1/todos")]
    assert reads == [("/api/v1/todos", {"status": "all"}), ("/api/v1/todos/rooms", {})]


def test_mine_asks_both_reads_for_what_was_asked_of_you() -> None:
    server = _own_server()
    with server.client() as api:
        found = poller.fetch_own_todos(api, mine=True)
    assert [t.id for t in found.todos] == ["o2"]
    assert found.rooms is not None
    assert [[i.id for i in r.items] for r in found.rooms] == [["r1"]]
    reads = [(p, q) for _m, p, q in server.requests if p.startswith("/api/v1/todos")]
    assert reads == [
        ("/api/v1/todos", {"status": "all", "asked_of_me": "true"}),
        ("/api/v1/todos/rooms", {"assignee": "me"}),
    ]


@pytest.mark.parametrize("how", ["status", "transport", "degraded", "malformed"])
def test_a_failed_rooms_read_leaves_your_own_items(how: str) -> None:
    server = _own_server()
    if how == "status":
        server.fail["/api/v1/todos/rooms"] = 500
    elif how == "transport":
        server.raise_on["/api/v1/todos/rooms"] = httpx.ConnectError("down")
    elif how == "degraded":
        server.rooms_degraded = True
    else:  # an item with no title: the wire shape was not what we know
        server.room_todos = {ts.ROOM_A: ("Trip planning", [{"id": "x"}])}
    with server.client() as api:
        found = poller.fetch_own_todos(api, mine=False)
    assert [t.id for t in found.todos] == ["o1", "o2", "o3"]
    assert found.rooms is None


def test_your_own_list_failing_or_a_401_on_the_rooms_read_raises() -> None:
    server = _own_server()
    server.fail["/api/v1/todos/rooms"] = 401
    with server.client() as api, pytest.raises(api_client.ApiError) as expired:
        poller.fetch_own_todos(api, mine=False)
    assert expired.value.status == 401
    server = _own_server()
    server.fail["/api/v1/todos"] = 500
    with server.client() as api, pytest.raises(api_client.ApiError):
        poller.fetch_own_todos(api, mine=False)


def test_your_own_notes_are_asked_for_with_no_principal() -> None:
    server = _own_server()
    server.notes[""] = [ts.note_item("mine", "My note", "2026-10-01T00:00:00+00:00")]
    with server.client() as api:
        found = poller.fetch_notes(api, None)
    assert [n.id for n in found.items] == ["mine"]
    sent = [q for _m, p, q in server.requests if p == "/api/v1/artifacts"]
    assert sent == [{"kind": "inline_text", "limit": "200"}]
