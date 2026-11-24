"""Regression tests for issue #353: malformed-response handling.

Six requirements are tested here:

  1. Non-JSON HTTP 200 body from _request raises ApiError(200) centrally.
  2. skip_bad_rows=True in list_groups_conditional / list_group_messages /
     list_todos / list_artifacts skips bad rows and counts them; the rest of
     the list is returned.
  3. CLI-path callers (no skip_bad_rows flag) are strict: a bad row raises.
  4. PepperApp._note_pane_skipped fires once per count rise and uses
     severity=warning; partial results are never presented as complete.
  5. RailPoller does not retain the ETag when any group row was skipped.
  6. Stale-message refresh re-queues via _refresh_failed on ApiError
     (strict at= path), so the retry requirement is met.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from pepper_cli import api_client
from pepper_cli.api_client import (
    ApiError,
    ArtifactListResponse,
    GroupList,
    GroupMessage,
    GroupMessagePage,
    TodoList,
)
from pepper_cli.secret import SecretStr
from pepper_cli.tui import poller
from pepper_cli.tui import state as tui_state
from pepper_cli.tui.app import PepperApp
from pepper_cli.tui.state import AppPaneState, Conversation
from tests import tui_support as ts
from tests.tui_support import body_text, until

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _api_raw(handler) -> api_client.ApiClient:
    """ApiClient backed by a custom httpx transport handler."""
    return api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _api(body: object) -> api_client.ApiClient:
    return _api_raw(lambda _r: httpx.Response(200, json=body))


def _api_text(text: str, status: int = 200) -> api_client.ApiClient:
    return _api_raw(
        lambda _r: httpx.Response(status, text=text, headers={"content-type": "text/html"})
    )


def _todo(**over: object) -> dict:
    base = {"id": "t1", "title": "x", "status": "open", "source": "user", "created_at": "z"}
    return {**base, **over}


def _group(**over: object) -> dict:
    base = {
        "group_id": "g1",
        "display_name": "G",
        "color": "c",
        "sub": "s",
        "member_count": 2,
        "archived": False,
        "topics": [{"topic_id": "tp", "name": "General", "last_activity_at": None}],
    }
    return {**base, **over}


def _groups_body(*groups: dict) -> dict:
    return {
        "enabled": True,
        "degraded": False,
        "total": len(groups),
        "archived_total": 0,
        "groups": list(groups),
    }


def _artifact(**over: object) -> dict:
    base = {
        "id": "a1",
        "kind": "inline_text",
        "name": "note",
        "updated_at": "z",
        "etag": "e1",
    }
    return {**base, **over}


def _artifacts_body(*items: dict, truncated: bool = False) -> dict:
    return {"items": list(items), "truncated": truncated}


def _message(**over: object) -> dict:
    str_fields = [
        "id",
        "display_name",
        "initial",
        "avatar_tint",
        "raw_body",
        "rendered_html",
        "created_at",
        "created_at_display",
        "day_key",
        "day_kicker",
        "day_heading",
        "day_label",
        "quote_excerpt",
    ]
    bool_fields = ["is_pepper", "is_you", "is_deleted", "is_broken", "run_start"]
    row: dict = dict.fromkeys(str_fields, "s")
    row |= dict.fromkeys(bool_fields, False)
    return {**row, **over}


def _messages_body(*messages: dict) -> dict:
    return {"messages": list(messages), "next_before": None}


# ---------------------------------------------------------------------------
# Req 1: non-JSON HTTP 200 raises ApiError(200) centrally via _request
# ---------------------------------------------------------------------------


class TestReq1NonJsonCentralMapping:
    def test_html_body_on_200_raises_api_error_200_for_list_todos(self) -> None:
        api = _api_text("<html>oops</html>")
        with pytest.raises(ApiError) as exc:
            api.list_todos()
        assert exc.value.status == 200
        assert "not valid JSON" in str(exc.value.detail)

    def test_html_body_on_200_raises_api_error_200_for_list_groups(self) -> None:
        api = _api_text("<html>oops</html>")
        with pytest.raises(ApiError) as exc:
            api.list_groups()
        assert exc.value.status == 200

    def test_html_body_on_200_raises_api_error_200_for_list_artifacts(self) -> None:
        api = _api_text("<html>oops</html>")
        with pytest.raises(ApiError) as exc:
            api.list_artifacts(kind="inline_text")
        assert exc.value.status == 200

    def test_body_missing_entirely_still_maps_to_api_error(self) -> None:
        """An empty non-JSON body also maps to ApiError(200)."""
        api = _api_text("")
        with pytest.raises(ApiError) as exc:
            api.list_todos()
        assert exc.value.status == 200

    def test_request_wrapper_maps_non_json_200_to_api_error_200(self) -> None:
        api = _api_text("<html>oops</html>")
        with pytest.raises(ApiError) as exc:
            api._request("GET", "/api/v1/todos").json()
        assert exc.value.status == 200
        assert "not valid JSON" in str(exc.value.detail)

    def test_request_wrapper_keeps_non_200_http_error_handling(self) -> None:
        api = _api_raw(lambda request: httpx.Response(404, json={"error": "not found"}))
        with pytest.raises(ApiError) as exc:
            api._request("GET", "/api/v1/todos")
        assert exc.value.status == 404

    def test_request_wrapper_response_json_supports_kwargs(self) -> None:
        api = _api({"count": 5})
        response = api._request("GET", "/api/v1/todos", params={"limit": 10})
        assert response.json() == {"count": 5}
        assert response.json(parse_int=int) == {"count": 5}
        assert response.json(parse_int=str) == {"count": "5"}


# ---------------------------------------------------------------------------
# Req 2: skip_bad_rows=True skips malformed rows and counts them
# ---------------------------------------------------------------------------


class TestReq2SkipBadRows:
    def test_list_todos_skip_one_bad_row_returns_rest(self) -> None:
        good = _todo(id="t-good")
        bad = _todo(id=99)  # id must be str
        api = _api([good, bad])
        result = api.list_todos(skip_bad_rows=True)
        assert isinstance(result, TodoList)
        assert len(result) == 1
        assert result[0].id == "t-good"
        assert result.skipped == 1

    def test_list_todos_skip_bad_rows_all_good_skipped_is_zero(self) -> None:
        api = _api([_todo(id="t1"), _todo(id="t2")])
        result = api.list_todos(skip_bad_rows=True)
        assert result.skipped == 0
        assert len(result) == 2

    def test_list_todos_skip_bad_rows_all_bad_returns_empty(self) -> None:
        api = _api([_todo(id=1), _todo(id=2)])
        result = api.list_todos(skip_bad_rows=True)
        assert len(result) == 0
        assert result.skipped == 2

    def test_list_groups_conditional_skip_one_bad_group(self) -> None:
        good = _group(group_id="g-good")
        bad = _group(group_id=99)  # group_id must be str
        body = _groups_body(good, bad)
        api = _api(body)
        groups, _etag = api.list_groups_conditional(if_none_match=None, skip_bad_rows=True)
        assert isinstance(groups, GroupList)
        assert groups.skipped == 1
        assert len(groups.groups) == 1
        assert groups.groups[0].group_id == "g-good"

    def test_list_group_messages_skip_one_bad_message(self) -> None:
        good = _message(id="m-good")
        bad = _message(id=99)  # id must be str
        api = _api(_messages_body(good, bad))
        page = api.list_group_messages("r1", "tp1", skip_bad_rows=True)
        assert isinstance(page, GroupMessagePage)
        assert page.skipped == 1
        assert len(page.messages) == 1
        assert page.messages[0].id == "m-good"

    def test_list_group_messages_bounds_the_row_it_skipped(self) -> None:
        before = _message(id="m-before", created_at="2026-09-16T18:00:00+00:00")
        bad = _message(id=99, created_at="2026-09-16T18:01:00+00:00")
        first = _message(id="m-first", created_at="2026-09-16T18:02:00+00:00")
        last = _message(id="m-last", created_at="2026-09-16T18:03:00+00:00")
        api = _api(_messages_body(before, bad, first, last))
        page = api.list_group_messages("r1", "tp1", skip_bad_rows=True)
        assert [message.id for message in page.messages] == ["m-before", "m-first", "m-last"]
        assert page.skipped_at == [bad["created_at"]]
        assert page.skipped_at_unknown is False
        assert page.skipped_before_last == before["created_at"]
        assert page.skipped_after_first == first["created_at"]
        assert page.skipped_after_last == last["created_at"]

    def test_list_group_messages_flags_mixed_timestamp_quality_in_skipped_rows(self) -> None:
        with_at = _message(id=99, created_at="2026-09-16T18:01:00+00:00")
        without_at = _message(id=100)
        without_at.pop("created_at")
        api = _api(_messages_body(with_at, without_at))
        page = api.list_group_messages("r1", "tp1", skip_bad_rows=True)
        assert page.skipped == 2
        assert page.skipped_at == [with_at["created_at"]]
        assert page.skipped_at_unknown is True

    def test_list_artifacts_skip_one_bad_item(self) -> None:
        good = _artifact(id="a-good")
        bad = _artifact(name=99)  # name must be str, not int
        api = _api(_artifacts_body(good, bad))
        result = api.list_artifacts(kind="inline_text", skip_bad_rows=True)
        assert isinstance(result, ArtifactListResponse)
        assert result.skipped == 1
        assert len(result.items) == 1
        assert result.items[0].id == "a-good"


# ---------------------------------------------------------------------------
# Req 3: CLI-path callers (no skip flag) are strict
# ---------------------------------------------------------------------------


class TestReq3StrictCLIPaths:
    def test_list_todos_strict_raises_on_bad_row(self) -> None:
        bad = _todo(id=99)
        api = _api([bad])
        with pytest.raises(ApiError) as exc:
            api.list_todos()
        assert exc.value.status == 200

    def test_list_groups_strict_raises_on_bad_group(self) -> None:
        bad = _group(group_id=99)
        api = _api(_groups_body(bad))
        with pytest.raises(ApiError) as exc:
            api.list_groups()
        assert exc.value.status == 200

    def test_list_groups_conditional_strict_raises_on_bad_group(self) -> None:
        bad = _group(group_id=99)
        api = _api(_groups_body(bad))
        with pytest.raises(ApiError) as exc:
            api.list_groups_conditional(if_none_match=None)
        assert exc.value.status == 200

    def test_list_group_messages_strict_raises_on_bad_message(self) -> None:
        bad = _message(id=99)
        api = _api(_messages_body(bad))
        with pytest.raises(ApiError) as exc:
            api.list_group_messages("r1", "tp1")
        assert exc.value.status == 200

    def test_list_artifacts_strict_raises_on_bad_item(self) -> None:
        bad = _artifact(name=99)  # name must be str, not int
        api = _api(_artifacts_body(bad))
        with pytest.raises(ApiError) as exc:
            api.list_artifacts(kind="inline_text")
        assert exc.value.status == 200


# ---------------------------------------------------------------------------
# Req 4: _note_pane_skipped fires once per count rise, severity=warning
# ---------------------------------------------------------------------------


class _StubApp:
    """Minimal stub that provides the state _note_pane_skipped reads/writes."""

    def __init__(self) -> None:
        self._rail_skipped = 0
        self._topic_skipped = 0
        self._todos_skipped = 0
        self._notes_skipped = 0
        self._partial_notices: dict[str, str] = {}
        self._partial_notice = ""
        self.notices: list[dict[str, Any]] = []

    def notify(self, message: str, **kwargs: Any) -> None:
        self.notices.append({"message": message, **kwargs})


class TestReq4PaneSkippedNotice:
    def test_no_notice_when_skipped_is_zero(self) -> None:
        app: Any = _StubApp()
        PepperApp._note_pane_skipped(app, 0, "_rail_skipped", "the sidebar")
        assert app.notices == []
        assert app._partial_notice == ""

    def test_notice_fires_when_count_rises_from_zero(self) -> None:
        app: Any = _StubApp()
        PepperApp._note_pane_skipped(app, 2, "_rail_skipped", "the sidebar")
        assert len(app.notices) == 1
        assert "2 item(s)" in app.notices[0]["message"]
        assert "the sidebar" in app.notices[0]["message"]
        assert app.notices[0]["severity"] == "warning"
        assert (
            app._partial_notice == "2 item(s) could not be read and are not shown in the sidebar."
        )

    def test_partial_marker_clears_when_complete_response_arrives(self) -> None:
        app: Any = _StubApp()
        PepperApp._note_pane_skipped(app, 2, "_rail_skipped", "the sidebar")
        assert app._partial_notice != ""
        PepperApp._note_pane_skipped(app, 0, "_rail_skipped", "the sidebar")
        assert app._partial_notice == ""
        assert len(app.notices) == 1

    def test_topic_partial_warning_survives_clean_incremental_poll(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = PepperApp(_api({}))
        app.current = Conversation(room_id="r1", topic_id="tp1")
        app._on_topic = lambda messages: None
        partial = GroupMessagePage(messages=[], skipped=1)
        app._on_topic_page(partial, newest=False)
        assert "this topic" in app._partial_notice
        assert app._topic_skipped == 1
        clean = GroupMessagePage(messages=[], skipped=0)
        app._on_topic_page(clean, newest=False)
        assert "this topic" in app._partial_notice
        assert app._topic_skipped == 1

        captured: dict[str, Any] = {}

        def fake_start(
            kind: str,
            work: Any,
            deliver: Any,
            *,
            on_error: Any = None,
        ) -> None:
            captured["deliver"] = deliver

        monkeypatch.setattr(app, "_start", fake_start)
        app.store.ticks = 4
        app._run("topic")
        complete = GroupMessagePage(messages=[], skipped=0, next_before="cursor")
        captured["deliver"](complete)
        assert app._partial_notice == ""
        assert app._topic_skipped == 0

    def test_topic_newest_page_does_not_clear_partial_warning(self) -> None:
        app = PepperApp(_api({}))
        app._on_topic = lambda messages: None
        app._note_pane_skipped(1, "_topic_skipped", "this topic")
        page = GroupMessagePage(messages=[], skipped=0, next_before="cursor")
        app._on_topic_page(page, newest=True, reconcile=False)
        assert "this topic" in app._partial_notice
        assert app._topic_skipped == 1
        assert app.store.older == "cursor"

    def test_topic_reconcile_clears_partial_warning(self) -> None:
        app = PepperApp(_api({}))
        app._on_topic = lambda messages: None
        app._note_pane_skipped(1, "_topic_skipped", "this topic")
        page = GroupMessagePage(messages=[], skipped=0, next_before="cursor")
        app._on_topic_page(page, newest=False, reconcile=True)
        assert app._partial_notice == ""
        assert app._topic_skipped == 0
        assert app.store.older == "cursor"

    def test_topic_reconcile_with_skipped_rows_keeps_partial_warning(self) -> None:
        app = PepperApp(_api({}))
        app._on_topic = lambda messages: None
        app._note_pane_skipped(1, "_topic_skipped", "this topic")
        page = GroupMessagePage(messages=[], skipped=2, next_before="cursor")
        app._on_topic_page(page, newest=False, reconcile=True)
        assert "this topic" in app._partial_notice
        assert app._topic_skipped == 2
        assert app.store.older == "cursor"

    def test_older_page_is_strict_about_malformed_rows(self) -> None:
        conv = Conversation(room_id="r1", topic_id="tp1")
        api = _api(_messages_body(_message(id="m-good"), _message(id=99)))
        with pytest.raises(ApiError) as exc:
            poller.fetch_older(api, conv, before="before", limit=50)
        assert exc.value.status == 200

    def test_topic_warning_hidden_after_switching_conversation(self) -> None:
        app = PepperApp(_api({}))
        app.current = Conversation(room_id="r1", topic_id="tp1")
        app._on_topic = lambda messages: None
        page = GroupMessagePage(messages=[], skipped=1)
        app._on_topic_page(page, newest=False)
        assert "this topic" in app._partial_notice
        app.current = Conversation(room_id="r2", topic_id="tp2")
        app._refresh_partial_notice()
        assert app._partial_notice == ""

    def test_pane_warning_hidden_when_pane_closes(self) -> None:
        app = PepperApp(_api({}))
        app.pane = AppPaneState(owner="g1", slug="todo")
        app._note_pane_skipped(1, "_todos_skipped", "todos")
        assert "todos" in app._partial_notice
        app.pane = None
        app._refresh_partial_notice()
        assert app._partial_notice == ""

    def test_partial_marker_tracks_each_pane_independently(self) -> None:
        app: Any = _StubApp()
        PepperApp._note_pane_skipped(app, 2, "_rail_skipped", "the sidebar")
        PepperApp._note_pane_skipped(app, 1, "_todos_skipped", "todos")
        assert "the sidebar" in app._partial_notice
        assert "todos" in app._partial_notice
        PepperApp._note_pane_skipped(app, 0, "_rail_skipped", "the sidebar")
        assert "the sidebar" not in app._partial_notice
        assert "todos" in app._partial_notice
        PepperApp._note_pane_skipped(app, 0, "_todos_skipped", "todos")
        assert app._partial_notice == ""

    def test_notice_not_repeated_at_same_count(self) -> None:
        app: Any = _StubApp()
        PepperApp._note_pane_skipped(app, 1, "_rail_skipped", "the sidebar")
        PepperApp._note_pane_skipped(app, 1, "_rail_skipped", "the sidebar")
        assert len(app.notices) == 1, "same count on the next poll must not fire again"

    def test_notice_fires_again_when_count_rises_further(self) -> None:
        app: Any = _StubApp()
        PepperApp._note_pane_skipped(app, 1, "_rail_skipped", "the sidebar")
        PepperApp._note_pane_skipped(app, 3, "_rail_skipped", "the sidebar")
        assert len(app.notices) == 2

    def test_notice_uses_correct_attr_per_pane(self) -> None:
        app: Any = _StubApp()
        PepperApp._note_pane_skipped(app, 1, "_topic_skipped", "this topic")
        PepperApp._note_pane_skipped(app, 1, "_todos_skipped", "todos")
        PepperApp._note_pane_skipped(app, 1, "_notes_skipped", "notes")
        assert len(app.notices) == 3
        labels = [n["message"] for n in app.notices]
        assert any("this topic" in text for text in labels)
        assert any("todos" in text for text in labels)
        assert any("notes" in text for text in labels)

    def test_state_updated_to_new_count(self) -> None:
        app: Any = _StubApp()
        PepperApp._note_pane_skipped(app, 5, "_todos_skipped", "todos")
        assert app._todos_skipped == 5


# ---------------------------------------------------------------------------
# Req 5: RailPoller does not retain the ETag when groups.skipped > 0
# ---------------------------------------------------------------------------


class TestReq5NoETagOnPartialList:
    def test_etag_not_retained_when_group_rows_were_skipped(self) -> None:
        """RailPoller.step() must not carry an ETag for a partial list."""
        good = _group(group_id="g-good")
        bad = _group(group_id=99)
        body = _groups_body(good, bad)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body, headers={"etag": "etag-v1"})

        api = _api_raw(handler)
        rail = poller.RailPoller(api)
        rail._etag = "old-etag"
        update = rail.step()
        assert update.groups is not None
        assert update.groups.skipped == 1
        assert update.etag is None
        assert rail._etag is None

    def test_etag_retained_when_no_rows_were_skipped(self) -> None:
        good = _group(group_id="g-good")
        body = _groups_body(good)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body, headers={"etag": "etag-v2"})

        api = _api_raw(handler)
        rail = poller.RailPoller(api)
        update = rail.step()
        assert update.groups is not None
        assert update.groups.skipped == 0
        assert update.etag == "etag-v2"
        rail.commit(update)
        assert rail._etag == "etag-v2"

    def test_degraded_partial_response_does_not_retain_etag(self) -> None:
        good = _group(group_id="g-good")
        bad = _group(group_id=99)
        body = {
            "enabled": True,
            "degraded": True,
            "total": 2,
            "archived_total": 0,
            "groups": [good, bad],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body, headers={"etag": "etag-degraded"})

        api = _api_raw(handler)
        rail = poller.RailPoller(api)
        rail._etag = "old-etag"
        update = rail.step()
        assert update.groups is not None
        assert update.groups.degraded is True
        assert update.groups.skipped == 1
        assert update.etag is None
        assert rail._etag is None


# ---------------------------------------------------------------------------
# Req 6: Stale-message refresh re-queues on ApiError (strict at= path)
# ---------------------------------------------------------------------------


class TestReq6StaleMessageRetryOnError:
    def test_refresh_strict_path_raises_api_error_on_bad_message(self) -> None:
        """The at= refresh path uses the strict (no skip_bad_rows) call.
        A malformed row in the refresh response raises ApiError, allowing
        the caller (_refresh_failed) to re-queue the message id."""
        bad = _message(id=99)
        api = _api(_messages_body(bad))
        with pytest.raises(ApiError) as exc:
            api.list_group_messages("r1", "tp1", at="m-stale")
        assert exc.value.status == 200

    def test_refresh_strict_path_does_not_skip_bad_rows(self) -> None:
        """The strict at= path must NOT silently skip: raising is correct so the
        retry loop can re-queue the message."""
        bad = _message(id=99)
        api = _api(_messages_body(bad))
        # skip_bad_rows not set (default False) = strict = raises
        with pytest.raises(ApiError):
            api.list_group_messages("r1", "tp1", at="m-stale")

    def test_refresh_failed_re_queues_after_malformed_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _api(_messages_body(_message(id=99)))
        app = PepperApp(api)
        app.current = Conversation(room_id="r1", topic_id="tp1")
        app.store.rows["m-stale"] = GroupMessage(**_message(id="m-stale"))
        app._stale_rows = ["m-stale"]

        captured: dict[str, Any] = {}

        def fake_start(
            kind: str,
            work: Any,
            deliver: Any,
            *,
            on_error: Any = None,
        ) -> None:
            captured["work"] = work
            captured["on_error"] = on_error

        monkeypatch.setattr(app, "_start", fake_start)
        app._refresh_next()
        on_error = captured["on_error"]
        assert on_error is not None
        with pytest.raises(ApiError) as exc:
            captured["work"]()
        assert exc.value.status == 200
        assert on_error(exc.value) is False
        assert app._stale_rows == ["m-stale"]


# ---------------------------------------------------------------------------
# A dropped message stays unread until a complete page recovers its range
# ---------------------------------------------------------------------------


def _quiet_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ACTIVE_INTERVAL", "COOLING_INTERVAL", "IDLE_INTERVAL", "DM_INTERVAL"):
        monkeypatch.setattr(tui_state, name, 1000.0)
    monkeypatch.setattr(tui_state, "RAIL_INTERVAL", 1000.0)
    monkeypatch.setattr(tui_state, "RAIL_INTERVAL_LIVE", 1000.0)


def _gap_server() -> tuple[ts.FakeServer, list[str]]:
    stamps = [f"2026-09-16T18:{i:02d}:00+00:00" for i in range(5)]
    rows = [ts.message(f"m{i}", stamp, f"row {i}") for i, stamp in enumerate(stamps)]
    rows[2] = {**rows[2], "display_name": 42}  # between readable rows, and unparseable
    server = ts.standard_server()
    server.messages[ts.TOPIC_A1] = rows
    server.marks[ts.TOPIC_A1] = stamps[1]
    server.rooms[0]["topics"][0]["last_activity_at"] = stamps[-1]
    server.bump()
    return server, stamps


class TestTopicPartialReadMark:
    async def test_viewing_a_newer_message_does_not_mark_the_missing_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _quiet_polls(monkeypatch)
        server, stamps = _gap_server()
        app = PepperApp(server.client())
        async with app.run_test(size=(120, 20)) as pilot:
            await until(pilot, lambda: len(app.rooms) == 2 and app.marks.unread)
            app.open_conversation(Conversation(ts.ROOM_A, ts.TOPIC_A1))
            await until(pilot, lambda: app.loaded_topic and "row 4" in body_text(app))
            await pilot.pause(0.3)
            assert app._topic_skipped == 1
            assert "this topic" in app._partial_notice
            assert app.store.read_held
            assert [through for topic, through in server.puts if topic == ts.TOPIC_A1] == []
            assert app.marks.marks[ts.TOPIC_A1] == tui_state.parse_ts(stamps[1])

    async def test_a_complete_page_releases_the_hold_before_marking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _quiet_polls(monkeypatch)
        server, stamps = _gap_server()
        app = PepperApp(server.client())
        async with app.run_test(size=(120, 20)) as pilot:
            await until(pilot, lambda: len(app.rooms) == 2 and app.marks.unread)
            app.open_conversation(Conversation(ts.ROOM_A, ts.TOPIC_A1))
            await until(pilot, lambda: app.loaded_topic and "row 4" in body_text(app))
            await pilot.pause(0.3)
            assert app.store.read_held

            server.messages[ts.TOPIC_A1][2] = ts.message("m2", stamps[2], "row 2")
            conv = Conversation(ts.ROOM_A, ts.TOPIC_A1)
            page = poller.fetch_topic(app._api, conv, since=stamps[1])
            assert page.skipped == 0
            app._on_topic_page(page, newest=False, reconcile=False)
            assert not app.store.read_held
            assert "this topic" in app._partial_notice  # only a full reconcile clears it

            app._note_viewed(render_id=app.thread.render_id, through=app.thread.through)
            app._run("publish")
            await until(
                pilot,
                lambda: any(
                    through == stamps[4] for topic, through in server.puts if topic == ts.TOPIC_A1
                ),
            )

    def test_pagination_releases_the_hold_after_a_repaired_row_is_loaded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _quiet_polls(monkeypatch)
        server = ts.standard_server()
        server.page_size = 2
        stamps = [f"2026-09-16T18:{i:02d}:00+00:00" for i in range(6)]
        rows = [ts.message(f"m{i}", stamp, f"row {i}") for i, stamp in enumerate(stamps)]
        rows[4] = {**rows[4], "display_name": 42}
        rows[4].pop("created_at")  # no usable timestamp, so only a full read can release
        server.messages[ts.TOPIC_A1] = rows
        server.marks[ts.TOPIC_A1] = stamps[-1]
        server.rooms[0]["topics"][0]["last_activity_at"] = stamps[-1]
        server.bump()

        app = PepperApp(server.client())
        app.current = Conversation(ts.ROOM_A, ts.TOPIC_A1)
        app._on_topic = lambda messages: None
        app._render_topic = lambda **_: None
        app._fetch_roots = lambda: None
        app._opening = False
        conv = app.current

        partial = poller.fetch_topic(app._api, conv, since=None)
        assert partial.skipped == 1
        assert partial.skipped_at_unknown is True
        app._on_topic_page(partial, newest=True, reconcile=True)
        assert app.store.read_held

        server.messages[ts.TOPIC_A1][4] = ts.message("m4", stamps[4], "row 4")
        clean = poller.fetch_topic(app._api, conv, since=None)
        assert clean.skipped == 0
        assert clean.next_before == "4"
        app._on_topic_page(clean, newest=True, reconcile=True)
        assert app.store.read_held  # older pages have not been read in this pass yet
        assert app.store.read_hold_clean_pass

        older = poller.fetch_older(app._api, conv, before=app.store.older, limit=2)
        assert older.next_before == "2"
        app._got_older(older)
        assert app.store.read_held

        last = poller.fetch_older(app._api, conv, before=app.store.older, limit=2)
        assert last.next_before is None
        app._got_older(last)
        assert not app.store.read_held
