"""The blocking half of the poll loops, against the fake server."""

from __future__ import annotations

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
        second = rail.step()
        assert second.groups is None
        assert second.server_marks == {}
    gets = [r for r in server.requests if r[1] == "/api/v1/groups"]
    assert len(gets) == 2


def test_rail_refetches_marks_only_for_rooms_whose_topics_moved() -> None:
    server = ts.standard_server()
    with server.client() as api:
        rail = poller.RailPoller(api)
        rail.step()
        before = len([r for r in server.requests if r[1].endswith("/read")])
        assert before == 2  # one per room on the first load
        server.rooms[1]["topics"][0]["last_activity_at"] = "2026-09-17T00:00:00+00:00"
        server.bump()
        update = rail.step()
        assert update.groups is not None
        after = [r for r in server.requests if r[1].endswith("/read")]
        assert len(after) == 3
        assert after[-1][1] == f"/api/v1/groups/{ts.ROOM_B}/read"
        # Same list again with a new ETag (e.g. only a member count changed): no mark fetches.
        server.bump()
        rail.step()
        assert len([r for r in server.requests if r[1].endswith("/read")]) == 3


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
        assert len(poller.fetch_topic(api, conv, since=None)) == 1
        assert poller.fetch_topic(api, conv, since="2026-09-16T17:30:00+00:00") == []
        assert poller.fetch_topic(api, state.PEPPER, since=None) == []
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
