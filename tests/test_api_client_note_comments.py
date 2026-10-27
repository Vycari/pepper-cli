"""Tests for the ``ApiClient`` note-comment methods: wire shape and response parsing."""

from __future__ import annotations

import json

import httpx
import pytest

from pepper_cli import api_client
from pepper_cli.secret import SecretStr

_NID = "00000000-0000-0000-0000-0000000000d1"
_GID = "00000000-0000-0000-0000-0000000000a1"
_PATH = f"/api/v1/artifacts/{_NID}/comments"


def _client(handler) -> api_client.ApiClient:
    return api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _msg(mid: str, **over) -> dict:
    base = {
        "id": mid,
        "author": {"id": "p1", "display_name": "Ada", "is_agent": False},
        "body": "hello",
        "body_html": "<p>hello</p>",
        "created_at": "2026-10-26T10:00:00Z",
        "edited_at": None,
        "deleted": False,
        "parent_message_id": None,
        "mentions": [],
        "anchor": None,
        "resolved_at": None,
        "resolved_by": None,
    }
    base.update(over)
    return base


def test_list_parses_threads_replies_and_resolution() -> None:
    anchor = {"exact": "sunscreen", "prefix": "- ", "suffix": "\n", "etag": "e1"}
    body = {
        "artifact_id": _NID,
        "group_id": _GID,
        "topic_id": "t1",
        "etag": "e1",
        "viewer_person_id": "p9",
        "resolved_count": 1,
        "threads": [
            {
                "id": "m1",
                "message": _msg(
                    "m1",
                    anchor=anchor,
                    resolved_at="2026-10-26T11:00:00Z",
                    resolved_by={"id": "p2", "display_name": "Bo", "is_agent": False},
                ),
                "position": {"start": 2, "end": 11},
                "detached": False,
                "replies": [
                    _msg(
                        "m2", parent_message_id="m1", mentions=[{"id": "p3", "display_name": "Cy"}]
                    ),
                    _msg("m3", parent_message_id="m1", body="", deleted=True),
                ],
            },
            {"id": "m4", "message": _msg("m4"), "position": None, "detached": True, "replies": []},
        ],
        "future_field": 1,
    }
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"], seen["path"] = request.method, request.url.path
        return httpx.Response(200, json=body)

    with _client(handler) as api:
        result = api.list_note_comments(_NID)

    assert (seen["method"], seen["path"]) == ("GET", _PATH)
    assert (result.artifact_id, result.group_id, result.topic_id) == (_NID, _GID, "t1")
    assert result.etag == "e1" and result.resolved_count == 1
    first, second = result.threads
    assert first.message.anchor == anchor
    assert first.message.resolved_at == "2026-10-26T11:00:00Z"
    assert first.message.resolved_by == "Bo"
    assert first.position == {"start": 2, "end": 11} and not first.detached
    assert [r.id for r in first.replies] == ["m2", "m3"]
    assert first.replies[0].mentions == ["p3"]
    assert first.replies[0].author == "Ada"
    assert [r.deleted for r in first.replies] == [False, True]
    assert second.detached and second.position is None
    assert second.message.resolved_at is None and second.message.resolved_by is None


def test_list_tolerates_a_sparse_answer() -> None:
    with _client(lambda r: httpx.Response(200, json={"artifact_id": _NID})) as api:
        result = api.list_note_comments(_NID)
    assert result.threads == [] and result.resolved_count == 0 and result.topic_id is None


def test_list_rejects_a_non_object_answer() -> None:
    with (
        _client(lambda r: httpx.Response(200, json=[1])) as api,
        pytest.raises(api_client.ApiError),
    ):
        api.list_note_comments(_NID)


def test_create_sends_the_full_payload_and_idempotency_key() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"], seen["path"] = request.method, request.url.path
        seen["json"] = json.loads(request.content)
        seen["key"] = request.headers.get("Idempotency-Key")
        return httpx.Response(200, json={"thread_root_id": "m1", "topic_id": "t1"})

    with _client(handler) as api:
        out = api.create_note_comment(
            _NID, body="Nice", quote="hello", mentions=["p3"], idempotency_key="k-1"
        )

    assert out == {"thread_root_id": "m1", "topic_id": "t1"}
    assert (seen["method"], seen["path"]) == ("POST", _PATH)
    assert seen["json"] == {
        "body": "Nice",
        "quote": "hello",
        "start": None,
        "reply_to": None,
        "mentions": ["p3"],
        "idempotency_key": "k-1",
    }
    assert seen["key"] == "k-1"


def test_create_without_optionals_sends_no_key() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        seen["key"] = request.headers.get("Idempotency-Key")
        return httpx.Response(200, json={})

    with _client(handler) as api:
        api.create_note_comment(_NID, body="x", reply_to="m1")

    assert seen["json"] == {
        "body": "x",
        "quote": None,
        "start": None,
        "reply_to": "m1",
        "mentions": [],
    }
    assert seen["key"] is None


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (422, {"detail": {"error": "quote_ambiguous", "count": 2}}),
        (409, {"detail": {"error": "personal_note"}}),
    ],
)
def test_create_errors_surface_as_api_error_with_the_body(status, payload) -> None:
    with (
        _client(lambda r: httpx.Response(status, json=payload)) as api,
        pytest.raises(api_client.ApiError) as exc,
    ):
        api.create_note_comment(_NID, body="x", quote="q")
    assert exc.value.status == status
    assert json.loads(exc.value.body) == payload


@pytest.mark.parametrize("action", ["resolve", "reopen"])
def test_resolve_and_reopen_post_to_the_thread_and_parse_it(action) -> None:
    seen: dict[str, object] = {}
    thread = {
        "id": "m1",
        "message": _msg("m1", resolved_at="2026-10-26T11:00:00Z"),
        "position": None,
        "detached": False,
        "replies": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"], seen["path"] = request.method, request.url.path
        return httpx.Response(200, json={"thread": thread})

    with _client(handler) as api:
        call = api.resolve_note_thread if action == "resolve" else api.reopen_note_thread
        result = call(_NID, "m1")

    assert (seen["method"], seen["path"]) == ("POST", f"{_PATH}/m1/{action}")
    assert result.message.id == "m1"
    assert result.message.resolved_at == "2026-10-26T11:00:00Z"


def test_resolve_without_a_thread_is_malformed() -> None:
    with (
        _client(lambda r: httpx.Response(200, json={"ok": True})) as api,
        pytest.raises(api_client.ApiError),
    ):
        api.resolve_note_thread(_NID, "m1")


def test_group_message_and_topic_parse_the_note_fields() -> None:
    row = {
        "id": "m1",
        "is_pepper": False,
        "is_you": False,
        "is_deleted": False,
        "is_broken": False,
        "run_start": True,
        "display_name": "Ada",
        "initial": "A",
        "avatar_tint": "t1",
        "raw_body": "x",
        "rendered_html": "<p>x</p>",
        "created_at": "2026-10-26T10:00:00Z",
        "created_at_display": "10:00",
        "day_key": "k",
        "day_kicker": "k",
        "day_heading": "k",
        "day_label": "k",
        "quote_excerpt": "",
        "anchor": {"exact": "hello"},
        "resolved_at": "2026-10-26T11:00:00Z",
        "resolved_by_name": "Bo",
    }
    parsed = api_client.group_message_from_dict(row)
    assert parsed.anchor == {"exact": "hello"}
    assert (parsed.resolved_at, parsed.resolved_by_name) == ("2026-10-26T11:00:00Z", "Bo")
    plain = api_client.group_message_from_dict(
        {k: v for k, v in row.items() if k not in ("anchor", "resolved_at", "resolved_by_name")}
    )
    assert plain.anchor is None and plain.resolved_at is None


@pytest.mark.parametrize(("token", "agent"), [("pep_agt_abc", True), ("pep_pat_abc", False)])
def test_is_agent_token_reads_the_token_prefix(token, agent) -> None:
    api = api_client.ApiClient(server_url="http://example.com", token=SecretStr(token))
    with api:
        assert api.is_agent_token is agent
