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


def test_rail_refetches_marks_only_for_rooms_whose_topics_moved() -> None:
    server = ts.standard_server()
    with server.client() as api:
        rail = poller.RailPoller(api)
        rail.commit(rail.step())
        before = len([r for r in server.requests if r[1].endswith("/read")])
        assert before == 2  # one per room on the first load
        server.rooms[1]["topics"][0]["last_activity_at"] = "2026-09-17T00:00:00+00:00"
        server.bump()
        update = rail.step()
        assert update.groups is not None
        rail.commit(update)
        after = [r for r in server.requests if r[1].endswith("/read")]
        assert len(after) == 3
        assert after[-1][1] == f"/api/v1/groups/{ts.ROOM_B}/read"
        # Same list again with a new ETag (e.g. only a member count changed): no mark fetches.
        server.bump()
        rail.commit(rail.step())
        assert len([r for r in server.requests if r[1].endswith("/read")]) == 3


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
    server.raise_on[f"/api/v1/groups/{ts.ROOM_B}/read"] = httpx.ConnectError("down")
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


def test_a_room_whose_marks_fail_is_asked_again_on_the_next_poll() -> None:
    server = ts.standard_server()
    server.fail[f"/api/v1/groups/{ts.ROOM_A}/read"] = 500
    with server.client() as api:
        rail = poller.RailPoller(api)
        first = rail.step()
        assert first.etag is None  # incomplete: the next poll must get a 200
        assert ts.TOPIC_B1 in first.server_marks
        rail.commit(first)
        server.fail.clear()
        second = rail.step()
        assert second.groups is not None
        assert ts.TOPIC_A1 in second.server_marks
        assert second.etag is not None
        rail.commit(second)
        assert rail.step().groups is None
    reads = [p for _, p, _ in server.requests if p.endswith("/read")]
    assert reads.count(f"/api/v1/groups/{ts.ROOM_A}/read") == 2
    assert reads.count(f"/api/v1/groups/{ts.ROOM_B}/read") == 1


def test_forget_etag_asks_for_the_full_list() -> None:
    server = ts.standard_server()
    with server.client() as api:
        rail = poller.RailPoller(api)
        rail.commit(rail.step())
        rail.forget_etag()
        update = rail.step()
    assert update.groups is not None
    assert update.server_marks == {}  # nothing moved, so no mark refetch


def test_rail_disabled_and_degraded_skip_marks() -> None:
    server = ts.standard_server()
    server.degraded = True
    with server.client() as api:
        update = poller.RailPoller(api).step()
    assert update.groups is not None
    assert update.groups.degraded is True
    assert update.server_marks == {}
    assert not [r for r in server.requests if r[1].endswith("/read")]


def test_a_room_whose_marks_fail_is_skipped_but_a_401_is_not() -> None:
    server = ts.standard_server()
    server.fail[f"/api/v1/groups/{ts.ROOM_A}/read"] = 500
    with server.client() as api:
        update = poller.RailPoller(api).step()
    assert update.groups is not None  # one room's failure never hides the rail
    assert ts.TOPIC_B1 in update.server_marks
    server.fail = {f"/api/v1/groups/{ts.ROOM_A}/read": 401}
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
