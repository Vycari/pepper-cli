"""Regression tests for issue #353: malformed-response handling.

Six requirements are tested here:

  1. Non-JSON HTTP 200 body from _request raises ApiError(200) centrally.
  2. skip_bad_rows=True in list_groups_conditional / list_group_messages /
     list_todos / list_artifacts skips bad rows and counts them; the rest of
     the list is returned.
  3. CLI-path callers (no skip_bad_rows flag) are strict: a bad row raises.
  4. PepperApp._note_pane_skipped fires once per count rise and uses
     severity=warning; partial results are never presented as complete, and repeated
     unreadable-row warnings render as one counted sentence.
  5. RailPoller does not retain the ETag when any group row was skipped.
  6. The API's strict default stays unchanged, but the TUI stale-message refresh opts into
     skip_bad_rows so one malformed row cannot starve the queue; transient failures still
     re-queue via _refresh_failed, and an unreadable target warns until a parsed poll, card,
     or older page row replaces the old text the warning was raised for.
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
from pepper_cli.tui.app import PepperApp
from pepper_cli.tui.state import AppPaneState, Conversation

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

    def test_repeated_unreadable_warnings_show_one_counted_notice(self) -> None:
        app = PepperApp(_api({}))
        app.current = Conversation(room_id="r1", topic_id="tp1")
        text = "The edited message could not be read and still shows its previous text."
        for message_id in ("m1", "m2"):
            app._note_pane_skipped(
                1,
                "_refresh_skipped",
                "this message",
                scope=app._refresh_notice_scope(message_id),
                text=text,
            )
        assert app._partial_notice == (
            "2 edited messages could not be read and still show their previous text."
        )
        assert len(app._partial_notices) == 2  # per-message records stay available for clearing
        app._clear_refresh_notice("m1")
        assert app._partial_notice == text
        assert len(app._partial_notices) == 1

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
# Req 6: The API default stays strict; the TUI refresh opts into row skipping
# ---------------------------------------------------------------------------


class TestReq6RefreshWindow:
    def test_api_default_strict_path_raises_api_error_on_bad_message(self) -> None:
        """Callers that do not opt in still get strict parsing. #371 did not change the API
        default; the TUI refresh asks for row skipping separately."""
        bad = _message(id=99)
        api = _api(_messages_body(bad))
        with pytest.raises(ApiError) as exc:
            api.list_group_messages("r1", "tp1", at="m-stale")
        assert exc.value.status == 200

    def test_api_default_does_not_skip_bad_rows(self) -> None:
        """``skip_bad_rows`` remains opt-in for every API caller, including an ``at=`` read."""
        bad = _message(id=99)
        api = _api(_messages_body(bad))
        with pytest.raises(ApiError):
            api.list_group_messages("r1", "tp1", at="m-stale")

    def test_refresh_work_skips_bad_rows_in_its_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _api(_messages_body(_message(id="m-stale"), _message(id="bad", display_name=123)))
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

        monkeypatch.setattr(app, "_start", fake_start)
        app._refresh_next()
        page = captured["work"]()
        assert [m.id for m in page.messages] == ["m-stale"]
        assert page.skipped == 1

    def test_refresh_failed_still_re_queues_a_transient_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _api(_messages_body(_message(id="m-stale")))
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
            captured["on_error"] = on_error

        monkeypatch.setattr(app, "_start", fake_start)
        app._refresh_next()
        on_error = captured["on_error"]
        assert on_error is not None
        assert on_error(httpx.ConnectError("down")) is False
        assert app._stale_rows == ["m-stale"]

    def test_refresh_warns_when_the_target_row_is_unreadable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _api(_messages_body(_message(id="bad", display_name=123)))
        app = PepperApp(api)
        app.current = Conversation(room_id="r1", topic_id="tp1")
        app.store.rows["m-stale"] = GroupMessage(**_message(id="m-stale", raw_body="old"))
        app._stale_rows = ["m-stale"]
        said: list[dict[str, Any]] = []
        app.notify = lambda message, **kwargs: said.append({"message": message, **kwargs})
        monkeypatch.setattr(app, "_render_topic", lambda **_kw: None)
        monkeypatch.setattr(app, "_run", lambda _kind: None)

        captured: dict[str, Any] = {}

        def fake_start(
            kind: str,
            work: Any,
            deliver: Any,
            *,
            on_error: Any = None,
        ) -> None:
            captured["work"] = work
            captured["deliver"] = deliver

        monkeypatch.setattr(app, "_start", fake_start)
        app._refresh_next()
        captured["deliver"](captured["work"]())

        assert app.store.rows["m-stale"].raw_body == "old"
        assert app._stale_rows == []
        assert len(said) == 1
        assert said[0]["severity"] == "warning"
        assert "still shows its previous text" in said[0]["message"]
        assert "still shows its previous text" in app._partial_notice

        captured["deliver"](GroupMessagePage(messages=[app.store.rows["m-stale"]]))
        assert app._partial_notice == ""
        assert len(said) == 1

    def test_another_rows_success_does_not_clear_the_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = PepperApp(_api(_messages_body()))
        app.current = Conversation(room_id="r1", topic_id="tp1")
        app.store.rows["m1"] = GroupMessage(**_message(id="m1", raw_body="old m1"))
        app.store.rows["m2"] = GroupMessage(**_message(id="m2", raw_body="old m2"))
        app._stale_rows = ["m1", "m2"]
        said: list[dict[str, Any]] = []
        app.notify = lambda message, **kwargs: said.append({"message": message, **kwargs})
        monkeypatch.setattr(app, "_render_topic", lambda **_kw: None)
        monkeypatch.setattr(app, "_run", lambda _kind: None)
        deliveries: list[Any] = []

        def fake_start(
            kind: str,
            work: Any,
            deliver: Any,
            *,
            on_error: Any = None,
        ) -> None:
            deliveries.append(deliver)

        monkeypatch.setattr(app, "_start", fake_start)
        app._refresh_next()
        app._refresh_next()
        assert len(deliveries) == 2

        deliveries[0](GroupMessagePage(skipped=1))
        assert app.store.rows["m1"].raw_body == "old m1"
        assert "still shows its previous text" in app._partial_notice
        assert len(said) == 1

        deliveries[1](
            GroupMessagePage(messages=[GroupMessage(**_message(id="m2", raw_body="new m2"))])
        )
        assert app.store.rows["m2"].raw_body == "new m2"
        assert "still shows its previous text" in app._partial_notice
        assert len(said) == 1

        deliveries[0](
            GroupMessagePage(messages=[GroupMessage(**_message(id="m1", raw_body="new m1"))])
        )
        assert app.store.rows["m1"].raw_body == "new m1"
        assert app._partial_notice == ""
        assert len(said) == 1

    def test_a_newest_page_reconcile_does_not_clear_an_older_rows_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = PepperApp(_api(_messages_body()))
        app.current = Conversation(room_id="r1", topic_id="tp1")
        app.store.rows["m1"] = GroupMessage(**_message(id="m1", raw_body="old"))
        app._stale_rows = ["m1"]
        app.loaded_topic = True
        said: list[dict[str, Any]] = []
        app.notify = lambda message, **kwargs: said.append({"message": message, **kwargs})
        monkeypatch.setattr(app, "_learn_from_messages", lambda _messages: None)
        monkeypatch.setattr(app, "_render_topic", lambda **_kw: None)
        monkeypatch.setattr(app, "_fetch_roots", lambda: None)
        monkeypatch.setattr(app, "_run", lambda _kind: None)
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
        app._refresh_next()
        captured["deliver"](GroupMessagePage(skipped=1))
        assert "still shows its previous text" in app._partial_notice
        assert len(said) == 1

        newest = GroupMessage(**_message(id="m2", raw_body="newest"))
        app._on_topic_page(GroupMessagePage(messages=[newest], skipped=0), reconcile=True)
        assert app.store.rows["m1"].raw_body == "old"
        assert "still shows its previous text" in app._partial_notice
        assert len(said) == 1

        fresh = GroupMessage(**_message(id="m1", raw_body="fresh"))
        app._on_topic_page(GroupMessagePage(messages=[fresh], skipped=0), reconcile=True)
        assert app.store.rows["m1"].raw_body == "fresh"
        assert app._partial_notice == ""
        assert len(said) == 1

    def test_a_late_pre_edit_copy_does_not_clear_the_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = PepperApp(_api(_messages_body()))
        app.current = Conversation(room_id="r1", topic_id="tp1")
        old = GroupMessage(**_message(id="m1", raw_body="old"))
        app.store.rows["m1"] = old
        app._stale_rows = ["m1"]
        app.loaded_topic = True
        said: list[dict[str, Any]] = []
        app.notify = lambda message, **kwargs: said.append({"message": message, **kwargs})
        monkeypatch.setattr(app, "_learn_from_messages", lambda _messages: None)
        monkeypatch.setattr(app, "_render_topic", lambda **_kw: None)
        monkeypatch.setattr(app, "_fetch_roots", lambda: None)
        monkeypatch.setattr(app, "_run", lambda _kind: None)
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
        app._refresh_next()
        captured["deliver"](GroupMessagePage(skipped=1))
        assert "still shows its previous text" in app._partial_notice

        app._on_topic_page(GroupMessagePage(messages=[old], skipped=0), reconcile=True)
        assert app.store.rows["m1"].raw_body == "old"
        assert "still shows its previous text" in app._partial_notice
        assert len(said) == 1

    def test_an_older_row_with_a_different_body_keeps_the_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = PepperApp(_api(_messages_body()))
        app.current = Conversation(room_id="r1", topic_id="tp1")
        held = GroupMessage(
            **_message(
                id="m1",
                raw_body="newer",
                is_edited=True,
                edited_at="2026-11-26T12:00:00+00:00",
            )
        )
        app.store.rows["m1"] = held
        app._stale_rows = ["m1"]
        app.loaded_topic = True
        said: list[dict[str, Any]] = []
        app.notify = lambda message, **kwargs: said.append({"message": message, **kwargs})
        monkeypatch.setattr(app, "_learn_from_messages", lambda _messages: None)
        monkeypatch.setattr(app, "_render_topic", lambda **_kw: None)
        monkeypatch.setattr(app, "_fetch_roots", lambda: None)
        monkeypatch.setattr(app, "_run", lambda _kind: None)
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
        app._refresh_next()
        captured["deliver"](GroupMessagePage(skipped=1))
        assert "still shows its previous text" in app._partial_notice

        older = GroupMessage(
            **_message(
                id="m1",
                raw_body="older",
                is_edited=True,
                edited_at="2026-11-26T11:00:00+00:00",
            )
        )
        app._on_topic_page(GroupMessagePage(messages=[older], skipped=0), reconcile=True)
        assert app.store.rows["m1"].raw_body == "newer"
        assert "still shows its previous text" in app._partial_notice
        assert len(said) == 1

    def test_an_incremental_poll_clears_the_warning_with_a_fresh_copy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = PepperApp(_api(_messages_body()))
        app.current = Conversation(room_id="r1", topic_id="tp1")
        app.store.rows["m1"] = GroupMessage(**_message(id="m1", raw_body="old"))
        app._stale_rows = ["m1"]
        app.loaded_topic = True
        said: list[dict[str, Any]] = []
        app.notify = lambda message, **kwargs: said.append({"message": message, **kwargs})
        monkeypatch.setattr(app, "_learn_from_messages", lambda _messages: None)
        monkeypatch.setattr(app, "_render_topic", lambda **_kw: None)
        monkeypatch.setattr(app, "_fetch_roots", lambda: None)
        monkeypatch.setattr(app, "_run", lambda _kind: None)
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
        app._refresh_next()
        captured["deliver"](GroupMessagePage(skipped=1))
        assert "still shows its previous text" in app._partial_notice

        fresh = GroupMessage(**_message(id="m1", raw_body="fresh"))
        app._on_topic_page(GroupMessagePage(messages=[fresh], skipped=0), reconcile=False)
        assert app.store.rows["m1"].raw_body == "fresh"
        assert app._partial_notice == ""
        assert len(said) == 1

    def test_a_poll_without_a_new_body_keeps_the_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = PepperApp(_api(_messages_body()))
        app.current = Conversation(room_id="r1", topic_id="tp1")
        app.store.rows["m1"] = GroupMessage(**_message(id="m1", raw_body="old"))
        app._stale_rows = ["m1"]
        app.loaded_topic = True
        said: list[dict[str, Any]] = []
        app.notify = lambda message, **kwargs: said.append({"message": message, **kwargs})
        monkeypatch.setattr(app, "_learn_from_messages", lambda _messages: None)
        monkeypatch.setattr(app, "_render_topic", lambda **_kw: None)
        monkeypatch.setattr(app, "_fetch_roots", lambda: None)
        monkeypatch.setattr(app, "_run", lambda _kind: None)
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
        app._refresh_next()
        captured["deliver"](GroupMessagePage(skipped=1))
        assert "still shows its previous text" in app._partial_notice

        reacted = GroupMessage(**_message(id="m1", raw_body="old", reactions=[{"key": "ok"}]))
        app._on_topic_page(GroupMessagePage(messages=[reacted], skipped=0), reconcile=False)
        assert app.store.rows["m1"].raw_body == "old"
        assert "still shows its previous text" in app._partial_notice
        assert len(said) == 1

    def test_a_card_read_clears_the_warning_with_a_fresh_copy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = PepperApp(_api(_messages_body()))
        app.current = Conversation(room_id="r1", topic_id="tp1")
        held = GroupMessage(
            **_message(
                id="m1",
                raw_body="old",
                is_edited=True,
                edited_at="2026-11-26T11:00:00+00:00",
            )
        )
        app.card_rows["m1"] = held
        assert "m1" not in app.store.rows  # the card holds the only copy
        app._stale_rows = ["m1"]
        app.loaded_topic = True
        said: list[dict[str, Any]] = []
        app.notify = lambda message, **kwargs: said.append({"message": message, **kwargs})
        monkeypatch.setattr(app, "_learn_from_messages", lambda _messages: None)
        monkeypatch.setattr(app, "_render_topic", lambda **_kw: None)
        monkeypatch.setattr(app, "_fetch_roots", lambda: None)
        monkeypatch.setattr(app, "_run", lambda _kind: None)
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
        app._refresh_next()
        captured["deliver"](GroupMessagePage(skipped=1))
        assert "still shows its previous text" in app._partial_notice

        fresh = GroupMessage(
            **_message(
                id="m1",
                raw_body="fresh",
                is_edited=True,
                edited_at="2026-11-26T12:00:00+00:00",
            )
        )
        app._got_card(GroupMessagePage(messages=[fresh], next_before=None), older=False)
        assert app.card_rows["m1"].raw_body == "fresh"
        assert app._partial_notice == ""
        assert len(said) == 1

    def test_an_older_card_page_keeps_the_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = PepperApp(_api(_messages_body()))
        app.current = Conversation(room_id="r1", topic_id="tp1")
        held = GroupMessage(
            **_message(
                id="m1",
                raw_body="newer",
                is_edited=True,
                edited_at="2026-11-26T12:00:00+00:00",
            )
        )
        app.card_rows["m1"] = held
        assert "m1" not in app.store.rows
        app._stale_rows = ["m1"]
        app.loaded_topic = True
        said: list[dict[str, Any]] = []
        app.notify = lambda message, **kwargs: said.append({"message": message, **kwargs})
        monkeypatch.setattr(app, "_learn_from_messages", lambda _messages: None)
        monkeypatch.setattr(app, "_render_topic", lambda **_kw: None)
        monkeypatch.setattr(app, "_fetch_roots", lambda: None)
        monkeypatch.setattr(app, "_run", lambda _kind: None)
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
        app._refresh_next()
        captured["deliver"](GroupMessagePage(skipped=1))
        assert "still shows its previous text" in app._partial_notice

        older = GroupMessage(
            **_message(
                id="m1",
                raw_body="older",
                is_edited=True,
                edited_at="2026-11-26T11:00:00+00:00",
            )
        )
        app._got_card(GroupMessagePage(messages=[older], next_before=None), older=False)
        assert app.card_rows["m1"].raw_body == "newer"
        assert "still shows its previous text" in app._partial_notice
        assert len(said) == 1

    def test_an_older_page_clears_the_warning_with_a_fresh_copy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = PepperApp(_api(_messages_body()))
        app.current = Conversation(room_id="r1", topic_id="tp1")
        held = GroupMessage(
            **_message(
                id="m1",
                raw_body="old",
                is_edited=True,
                edited_at="2026-11-26T11:00:00+00:00",
            )
        )
        app.store.rows["m1"] = held
        app._stale_rows = ["m1"]
        app.loaded_topic = True
        said: list[dict[str, Any]] = []
        app.notify = lambda message, **kwargs: said.append({"message": message, **kwargs})
        monkeypatch.setattr(app, "_learn_from_messages", lambda _messages: None)
        monkeypatch.setattr(app, "_render_topic", lambda **_kw: None)
        monkeypatch.setattr(app, "_fetch_roots", lambda: None)
        monkeypatch.setattr(app, "_run", lambda _kind: None)
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
        app._refresh_next()
        captured["deliver"](GroupMessagePage(skipped=1))
        assert "still shows its previous text" in app._partial_notice

        fresh = GroupMessage(
            **_message(
                id="m1",
                raw_body="fresh",
                is_edited=True,
                edited_at="2026-11-26T12:00:00+00:00",
            )
        )
        app._got_older(GroupMessagePage(messages=[fresh], next_before="before"))
        assert app.store.rows["m1"].raw_body == "fresh"
        assert app.store.paged_back
        assert app._partial_notice == ""
        assert len(said) == 1

    def test_an_older_page_with_a_stale_copy_keeps_the_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = PepperApp(_api(_messages_body()))
        app.current = Conversation(room_id="r1", topic_id="tp1")
        held = GroupMessage(
            **_message(
                id="m1",
                raw_body="newer",
                is_edited=True,
                edited_at="2026-11-26T12:00:00+00:00",
            )
        )
        app.store.rows["m1"] = held
        app._stale_rows = ["m1"]
        app.loaded_topic = True
        said: list[dict[str, Any]] = []
        app.notify = lambda message, **kwargs: said.append({"message": message, **kwargs})
        monkeypatch.setattr(app, "_learn_from_messages", lambda _messages: None)
        monkeypatch.setattr(app, "_render_topic", lambda **_kw: None)
        monkeypatch.setattr(app, "_fetch_roots", lambda: None)
        monkeypatch.setattr(app, "_run", lambda _kind: None)
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
        app._refresh_next()
        captured["deliver"](GroupMessagePage(skipped=1))
        assert "still shows its previous text" in app._partial_notice

        older = GroupMessage(
            **_message(
                id="m1",
                raw_body="older",
                is_edited=True,
                edited_at="2026-11-26T11:00:00+00:00",
            )
        )
        app._got_older(GroupMessagePage(messages=[older], next_before="before"))
        assert app.store.rows["m1"].raw_body == "newer"
        assert "still shows its previous text" in app._partial_notice
        assert len(said) == 1
