"""Wire shape of ``ApiClient.check_blog_note`` / ``publish_blog_note`` (``httpx.MockTransport``)."""

from __future__ import annotations

import json

import httpx
import pytest

from pepper_cli import api_client
from pepper_cli.secret import SecretStr

_GID = "00000000-0000-0000-0000-0000000000a1"
_NID = "00000000-0000-0000-0000-0000000000d1"


def _client(handler) -> api_client.ApiClient:
    return api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_check_is_a_get_on_the_note_check_route_and_returns_the_server_dict() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"], seen["path"] = request.method, request.url.path
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json={"title": "Hi", "source": "a.b.c", "future": 1})

    with _client(handler) as api:
        body = api.check_blog_note(_GID, _NID)

    assert seen == {
        "method": "GET",
        "path": f"/api/v1/groups/{_GID}/blog/notes/{_NID}/check",
        "auth": "Bearer pep_pat_abc",
    }
    assert body == {"title": "Hi", "source": "a.b.c", "future": 1}


def test_publish_sends_only_the_source_by_default() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"], seen["path"] = request.method, request.url.path
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"url": "/blog/x", "republished": False})

    with _client(handler) as api:
        body = api.publish_blog_note(_GID, _NID, source="a.b.c")

    assert seen["method"] == "POST"
    assert seen["path"] == f"/api/v1/groups/{_GID}/blog/notes/{_NID}/publish"
    assert seen["json"] == {"source": "a.b.c"}
    assert body["url"] == "/blog/x"


def test_publish_sends_acknowledge_slug_and_summary_when_given() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={})

    with _client(handler) as api:
        api.publish_blog_note(_GID, _NID, source="s", acknowledge=True, slug="my-post", summary="")

    # An empty summary is a real value (it clears the summary), so it is sent, not dropped.
    assert seen["json"] == {
        "source": "s",
        "acknowledge": True,
        "slug": "my-post",
        "summary": "",
    }


@pytest.mark.parametrize("status", [401, 403, 404, 409, 422])
def test_error_statuses_raise_api_error_with_the_detail(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "nope"})

    with _client(handler) as api, pytest.raises(api_client.ApiError) as ei:
        api.check_blog_note(_GID, _NID)
    assert ei.value.status == status
    assert ei.value.detail == "nope"


def test_a_non_object_answer_is_a_malformed_response() -> None:
    with _client(lambda r: httpx.Response(200, json=[1])) as api:
        with pytest.raises(api_client.ApiError):
            api.check_blog_note(_GID, _NID)
        with pytest.raises(api_client.ApiError):
            api.publish_blog_note(_GID, _NID, source="s")
