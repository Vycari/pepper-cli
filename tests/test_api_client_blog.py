"""Wire shape of the ``ApiClient`` blog stage and publish methods (``httpx.MockTransport``)."""

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


def test_stage_get_patch_delete_wire_shapes() -> None:
    seen: list[tuple] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"slug": "s", "check": {"source": "a.b.c"}, "future": 1})

    path = f"/api/v1/groups/{_GID}/blog/notes/{_NID}/stage"
    with _client(handler) as api:
        got = api.get_blog_stage(_GID, _NID)
        api.update_blog_stage(_GID, _NID, {"publish_at": None})
        api.delete_blog_stage(_GID, _NID)

    assert got["check"] == {"source": "a.b.c"} and got["future"] == 1
    assert seen == [
        ("GET", path, None),
        ("PATCH", path, {"publish_at": None}),
        ("DELETE", path, None),
    ]


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
        api.get_blog_stage(_GID, _NID)
    assert ei.value.status == status
    assert ei.value.detail == "nope"


def test_a_non_object_answer_is_a_malformed_response() -> None:
    with _client(lambda r: httpx.Response(200, json=[1])) as api:
        with pytest.raises(api_client.ApiError):
            api.get_blog_stage(_GID, _NID)
        with pytest.raises(api_client.ApiError):
            api.publish_blog_note(_GID, _NID, source="s")


def test_publish_sends_published_at_verbatim() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={})

    with _client(handler) as api:
        api.publish_blog_note(_GID, _NID, source="s", published_at="2019-04-02 09:30")

    assert seen["json"] == {"source": "s", "published_at": "2019-04-02 09:30"}


def test_list_posts_and_withdraw_wire_shapes() -> None:
    seen: list[tuple] = []
    pid = "00000000-0000-0000-0000-0000000000f1"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content))
        if request.method == "GET":
            return httpx.Response(200, json={"posts": [], "unpublished": [], "future": 1})
        return httpx.Response(200, json={"post_id": pid, "already": False, "card": "none"})

    with _client(handler) as api:
        listed = api.list_blog_posts(_GID)
        gone = api.withdraw_blog_post(_GID, pid)

    assert listed["future"] == 1 and gone["post_id"] == pid
    assert seen == [
        ("GET", f"/api/v1/groups/{_GID}/blog/posts", b""),
        ("POST", f"/api/v1/groups/{_GID}/blog/posts/{pid}/withdraw", b""),
    ]


def test_list_and_withdraw_non_object_answers_are_malformed() -> None:
    with _client(lambda r: httpx.Response(200, json=[1])) as api:
        with pytest.raises(api_client.ApiError):
            api.list_blog_posts(_GID)
        with pytest.raises(api_client.ApiError):
            api.withdraw_blog_post(_GID, "p")


def test_get_stage_sends_published_at_as_typed_only_when_given() -> None:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.query.decode())
        return httpx.Response(200, json={"check": {}})

    with _client(handler) as api:
        api.get_blog_stage(_GID, _NID)
        api.get_blog_stage(_GID, _NID, published_at="2014-03-02 17:00+01:00")

    assert queries[0] == ""
    assert httpx.QueryParams(queries[1])["published_at"] == "2014-03-02 17:00+01:00"
