"""Tests for the ``ApiClient`` artifacts/notes methods.

Same ``httpx.MockTransport`` style as ``test_api_client_groups.py``: assert
the wire shape (method, URL, query, headers) and that responses parse into
the dataclasses. Server-side behaviour (membership checks, the uniform 404,
the 503 on an unreachable membership seam) is covered in the server's own
test suite — what's pinned here is the client's shape
of the contract.
"""

from __future__ import annotations

import json

import httpx
import pytest

from pepper_cli import api_client
from pepper_cli.secret import SecretStr

_NID = "00000000-0000-0000-0000-0000000000d1"
_GID = "00000000-0000-0000-0000-0000000000a1"


def _mock(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _client(handler) -> api_client.ApiClient:
    return api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    )


def test_list_artifacts_sends_only_the_filters_it_was_given() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json={"truncated": False, "items": []})

    with _client(handler) as api:
        result = api.list_artifacts(kind="inline_text")

    assert seen["path"] == "/api/v1/artifacts"
    assert seen["params"] == {"kind": "inline_text"}
    assert seen["auth"] == "Bearer pep_pat_abc"
    assert result.truncated is False
    assert result.items == []


def test_list_artifacts_sends_every_filter_when_given() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"truncated": False, "items": []})

    with _client(handler) as api:
        api.list_artifacts(
            principal=_GID,
            kind="inline_text",
            q="flights",
            label="trip",
            name="Trip",
            name_prefix="Tr",
            limit=25,
        )

    assert seen["params"] == {
        "principal": _GID,
        "kind": "inline_text",
        "q": "flights",
        "label": "trip",
        "name": "Trip",
        "name_prefix": "Tr",
        "limit": "25",
    }


def test_list_artifacts_parses_rows_and_the_truncated_flag() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "truncated": True,
                "items": [
                    {
                        "id": _NID,
                        "name": "Packing list",
                        "kind": "inline_text",
                        "updated_at": "2026-09-16T17:30:00+00:00",
                        "etag": "abc123",
                        "labels": ["trip"],
                        "summary": "Bring sunscreen",
                    }
                ],
            },
        )

    with _client(handler) as api:
        result = api.list_artifacts()

    assert result.truncated is True
    (row,) = result.items
    assert row.id == _NID
    assert row.name == "Packing list"
    assert row.kind == "inline_text"
    assert row.updated_at == "2026-09-16T17:30:00+00:00"
    assert row.etag == "abc123"
    assert row.labels == ["trip"]
    assert row.summary == "Bring sunscreen"


def test_list_artifacts_row_without_labels_or_summary_defaults_cleanly() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "truncated": False,
                "items": [
                    {
                        "id": _NID,
                        "name": "Runbook",
                        "kind": "inline_text",
                        "updated_at": "2026-09-16T17:30:00+00:00",
                        "etag": "abc123",
                    }
                ],
            },
        )

    with _client(handler) as api:
        (row,) = api.list_artifacts().items

    assert row.labels == []
    assert row.summary is None


def test_get_artifact_detail_reads_the_right_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/v1/artifacts/{_NID}/detail"
        return httpx.Response(
            200,
            json={
                "id": _NID,
                "name": "Packing list",
                "kind": "inline_text",
                "updated_at": "2026-09-16T17:30:00+00:00",
                "etag": "abc123",
                "shared": False,
                "body": "- sunscreen\n- passport\n",
                "labels": ["trip"],
                "summary": "Bring sunscreen",
            },
        )

    with _client(handler) as api:
        detail = api.get_artifact_detail(_NID)

    assert detail.id == _NID
    assert detail.name == "Packing list"
    assert detail.kind == "inline_text"
    assert detail.etag == "abc123"
    assert detail.shared is False
    assert detail.body == "- sunscreen\n- passport\n"
    assert detail.labels == ["trip"]
    assert detail.summary == "Bring sunscreen"


def test_get_artifact_detail_never_sends_a_principal() -> None:
    """Viewer-scoped server-side — unlike ``list_artifacts``, there is no
    ``?principal=`` to attach here at all."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {}
        return httpx.Response(
            200,
            json={
                "id": _NID,
                "name": "Shared note",
                "kind": "inline_text",
                "updated_at": "2026-09-16T17:30:00+00:00",
                "etag": "abc123",
                "shared": True,
                "body": "hi",
            },
        )

    with _client(handler) as api:
        assert api.get_artifact_detail(_NID).shared is True


def test_get_artifact_detail_defaults_labels_and_summary() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": _NID,
                "name": "Bare note",
                "kind": "inline_text",
                "updated_at": "2026-09-16T17:30:00+00:00",
                "etag": "abc123",
                "shared": False,
                "body": None,
            },
        )

    with _client(handler) as api:
        detail = api.get_artifact_detail(_NID)

    assert detail.body is None
    assert detail.labels == []
    assert detail.summary is None


def test_a_missing_or_non_member_note_raises_api_error() -> None:
    """The uniform 404 (missing note, missing room, non-member alike) reaches
    the caller as an :class:`ApiError`, not a silent ``None``."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    with _client(handler) as api:
        try:
            api.get_artifact_detail(_NID)
        except api_client.ApiError as exc:
            assert exc.status == 404
        else:  # pragma: no cover - the call above must raise
            raise AssertionError("expected ApiError")


def test_a_down_membership_seam_raises_a_503_api_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "membership check unavailable"})

    with _client(handler) as api:
        try:
            api.list_artifacts(principal=_GID)
        except api_client.ApiError as exc:
            assert exc.status == 503
        else:  # pragma: no cover - the call above must raise
            raise AssertionError("expected ApiError")


# ---------------------------------------------------------------------------
# create_artifact


def test_create_artifact_sends_the_right_shape() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["body"] = request.read()
        return httpx.Response(200, json={"id": _NID, "name": "Packing list", "etag": "abc123"})

    with _client(handler) as api:
        result = api.create_artifact(name="Packing list", body="- sunscreen\n")

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/artifacts/new"
    assert seen["params"] == {}
    sent = json.loads(seen["body"])
    assert sent == {"name": "Packing list", "kind": "inline_text", "body": "- sunscreen\n"}
    assert result.id == _NID
    assert result.name == "Packing list"
    assert result.etag == "abc123"


def test_create_artifact_sends_the_principal_when_given() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"id": _NID, "name": "Room note", "etag": "abc123"})

    with _client(handler) as api:
        api.create_artifact(name="Room note", body="hi", principal=_GID)

    assert seen["params"] == {"principal": _GID}


# ---------------------------------------------------------------------------
# edit_artifact


def test_edit_artifact_sends_the_right_shape_and_if_match_header() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["if_match"] = request.headers.get("If-Match")
        seen["body"] = request.read()
        return httpx.Response(
            200,
            json={
                "artifact": {
                    "id": _NID,
                    "name": "Packing list",
                    "kind": "inline_text",
                    "updated_at": "2026-09-16T17:30:00+00:00",
                    "etag": "def456",
                    "shared": False,
                    "body": "new body\n",
                }
            },
        )

    with _client(handler) as api:
        detail = api.edit_artifact(_NID, body="new body\n", if_match="abc123")

    assert seen["method"] == "POST"
    assert seen["path"] == f"/api/v1/artifacts/{_NID}/edit"
    assert seen["if_match"] == "abc123"
    sent = json.loads(seen["body"])
    assert sent == {"name": None, "body": "new body\n"}
    assert detail.etag == "def456"
    assert detail.body == "new body\n"


def test_edit_artifact_omits_the_if_match_header_when_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "If-Match" not in request.headers
        return httpx.Response(
            200,
            json={
                "artifact": {
                    "id": _NID,
                    "name": "Packing list",
                    "kind": "inline_text",
                    "updated_at": "2026-09-16T17:30:00+00:00",
                    "etag": "def456",
                    "shared": False,
                    "body": "new body\n",
                }
            },
        )

    with _client(handler) as api:
        api.edit_artifact(_NID, body="new body\n", if_match=None)


def test_edit_artifact_sends_the_principal_when_given() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "artifact": {
                    "id": _NID,
                    "name": "Room note",
                    "kind": "inline_text",
                    "updated_at": "2026-09-16T17:30:00+00:00",
                    "etag": "def456",
                    "shared": True,
                    "body": "hi",
                }
            },
        )

    with _client(handler) as api:
        api.edit_artifact(_NID, body="hi", principal=_GID)

    assert seen["params"] == {"principal": _GID}


def test_edit_artifact_412_raises_artifact_conflict_error_with_current_state() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            412,
            json={
                "detail": "conflict",
                "body": "server's current text\n",
                "etag": "newetag1",
                "updated_at": "2026-09-27T00:00:00+00:00",
            },
        )

    with _client(handler) as api:
        try:
            api.edit_artifact(_NID, body="my text\n", if_match="stale1")
        except api_client.ArtifactConflictError as exc:
            assert exc.status == 412
            assert exc.current_body == "server's current text\n"
            assert exc.current_etag == "newetag1"
            assert exc.current_updated_at == "2026-09-27T00:00:00+00:00"
        else:  # pragma: no cover - the call above must raise
            raise AssertionError("expected ArtifactConflictError")


def test_edit_artifact_other_error_statuses_are_not_wrapped_as_a_conflict() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    with _client(handler) as api:
        try:
            api.edit_artifact(_NID, body="my text\n")
        except api_client.ArtifactConflictError:  # pragma: no cover
            raise AssertionError("404 must not be wrapped as a conflict") from None
        except api_client.ApiError as exc:
            assert exc.status == 404
        else:  # pragma: no cover - the call above must raise
            raise AssertionError("expected ApiError")


# ---------------------------------------------------------------------------
# delete_artifact


def test_delete_artifact_sends_the_right_shape() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["body"] = request.read()
        return httpx.Response(200, json={"result": {"name": "Packing list"}})

    with _client(handler) as api:
        name = api.delete_artifact(_NID)

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/apps/notes/actions/delete"
    assert seen["params"] == {}
    sent = json.loads(seen["body"])
    assert sent == {"params": {"id": _NID}}
    assert name == "Packing list"


def test_delete_artifact_sends_the_principal_when_given() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"result": {"name": "Room note"}})

    with _client(handler) as api:
        api.delete_artifact(_NID, principal=_GID)

    assert seen["params"] == {"principal": _GID}


def test_toggle_checklist_names_the_line_by_its_text_and_passes_the_room() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"], seen["path"] = request.method, request.url.path
        seen["params"] = dict(request.url.params)
        seen["body"] = json.loads(request.content)
        artifact = {
            "id": _NID,
            "name": "Packing",
            "kind": "inline_text",
            "updated_at": "2026-10-28T10:00:00+00:00",
            "etag": "e2",
            "shared": True,
            "body": "- [x] milk\n",
        }
        return httpx.Response(200, json={"id": _NID, "artifact": artifact})

    with _client(handler) as api:
        detail = api.toggle_checklist(
            _NID, index=2, checked=True, text="milk", occurrence=1, matches=2, principal=_GID
        )

    assert (seen["method"], seen["path"]) == ("POST", f"/api/v1/artifacts/{_NID}/toggle")
    assert seen["params"] == {"principal": _GID}
    assert seen["body"] == {
        "index": 2,
        "checked": True,
        "text": "milk",
        "occurrence": 1,
        "matches": 2,
    }
    assert detail is not None
    assert (detail.etag, detail.body) == ("e2", "- [x] milk\n")


def test_toggle_checklist_for_your_own_note_sends_no_principal_and_tolerates_no_artifact() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"id": _NID, "artifact": None})

    with _client(handler) as api:
        detail = api.toggle_checklist(
            _NID, index=0, checked=False, text="a", occurrence=0, matches=1
        )

    assert seen["params"] == {}
    assert detail is None


def test_toggle_checklist_409_carries_the_servers_sentence() -> None:
    sentence = "that note has changed — reload to see it as it is now."

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": sentence})

    with _client(handler) as api:
        try:
            api.toggle_checklist(_NID, index=0, checked=True, text="a", occurrence=0, matches=1)
        except api_client.ApiError as exc:
            assert (exc.status, exc.detail) == (409, sentence)
        else:
            raise AssertionError("expected a 409")


# ---------------------------------------------------------------------------
# get_note_links
#
# The fixtures here are synthetic. The detail route's ``wiki_links`` and
# ``backlinks`` are not in the OpenAPI snapshot (it answers an untyped
# ``JSONResponse``), so these pin the client's tolerance, not the server shape.


def _links_detail(**over) -> dict:
    base = {
        "id": _NID,
        "wiki_links": [
            {
                "text": "Trip runbook",
                "label": "runbook",
                "state": "resolved",
                "note_id": "00000000-0000-0000-0000-0000000000d2",
                "principal": "alice",
            }
        ],
        "backlinks": [{"id": "00000000-0000-0000-0000-0000000000d3", "name": "Day plan"}],
    }
    base.update(over)
    return base


def test_get_note_links_reads_the_detail_path_and_parses_the_links() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json=_links_detail())

    with _client(handler) as api:
        links = api.get_note_links(_NID)

    assert seen["path"] == f"/api/v1/artifacts/{_NID}/detail"
    assert links.artifact_id == _NID
    assert len(links.wiki_links) == 1
    link = links.wiki_links[0]
    assert (link.text, link.label, link.state) == ("Trip runbook", "runbook", "resolved")
    assert link.note_id == "00000000-0000-0000-0000-0000000000d2"
    assert link.principal == "alice"
    assert links.skipped_wiki_links == 0


def test_get_note_links_keeps_backlinks_exactly_as_sent() -> None:
    backlinks = [{"anything": [1, 2]}, "a string", 7]
    with _client(
        lambda request: httpx.Response(200, json=_links_detail(backlinks=backlinks))
    ) as api:
        assert api.get_note_links(_NID).backlinks == backlinks


def test_get_note_links_defaults_when_the_fields_are_absent() -> None:
    with _client(lambda request: httpx.Response(200, json={"id": _NID})) as api:
        links = api.get_note_links(_NID)
    assert links.wiki_links == []
    assert links.backlinks == []
    assert links.skipped_wiki_links == 0


def test_get_note_links_counts_unreadable_wiki_entries_instead_of_dropping_them() -> None:
    body = _links_detail(wiki_links=["not an object", {"label": "fine", "state": "ambiguous"}])
    with _client(lambda request: httpx.Response(200, json=body)) as api:
        links = api.get_note_links(_NID)
    assert links.skipped_wiki_links == 1
    assert [link.label for link in links.wiki_links] == ["fine"]
    # An entry with no note_id is unresolved, not malformed.
    assert links.wiki_links[0].note_id is None


@pytest.mark.parametrize("field", ["wiki_links", "backlinks"])
@pytest.mark.parametrize("wrong", ["oops", {"a": 1}, 7, True])
def test_get_note_links_rejects_a_wrongly_typed_field(field, wrong) -> None:
    """A present-but-mistyped field is a contract error, not "no links".

    Reading it as ``[]`` would print a linked note as an orphan and exit 0,
    indistinguishable from a note that genuinely has none.
    """
    with _client(lambda request: httpx.Response(200, json=_links_detail(**{field: wrong}))) as api:
        try:
            api.get_note_links(_NID)
        except api_client.ApiError as exc:
            assert field in exc.detail
        else:
            raise AssertionError(f"{field}={wrong!r} must not read as no links")


def test_get_note_links_accepts_an_explicit_empty_or_absent_field() -> None:
    """Absent and ``[]`` both mean the server sent no links; only a wrong
    type is an error."""
    for body in ({"id": _NID}, _links_detail(wiki_links=[], backlinks=[])):
        with _client(lambda request, b=body: httpx.Response(200, json=b)) as api:
            links = api.get_note_links(_NID)
        assert (links.wiki_links, links.backlinks) == ([], [])


def test_get_note_links_rejects_a_non_object_detail() -> None:
    with _client(lambda request: httpx.Response(200, json=["not", "a", "dict"])) as api:
        try:
            api.get_note_links(_NID)
        except api_client.ApiError as exc:
            assert exc.status == 200
        else:
            raise AssertionError("a non-object detail must not parse as no links")
