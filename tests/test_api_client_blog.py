"""``ApiClient``'s blog methods: the wire shape of check, publish and withdraw."""

from __future__ import annotations

import json

import httpx
import pytest

from pepper_cli import api_client
from pepper_cli.secret import SecretStr

_G = "00000000-0000-0000-0000-0000000000a1"
_N = "00000000-0000-0000-0000-0000000000d1"
_P = "00000000-0000-0000-0000-0000000000e1"


def _client(handler) -> api_client.ApiClient:
    return api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_check_sends_published_at_only_when_given() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"slug": "x", "source": "a.b.c"})

    with _client(handler) as api:
        api.blog_check(_G, _N)
        api.blog_check(_G, _N, published_at="2014-03-02T17:00:00Z")
    assert seen[0].url.path == f"/api/v1/groups/{_G}/blog/notes/{_N}/check"
    assert dict(seen[0].url.params) == {}
    assert dict(seen[1].url.params) == {"published_at": "2014-03-02T17:00:00Z"}


def test_publish_sends_only_the_fields_given_and_parses_the_post() -> None:
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v1/groups/{_G}/blog/notes/{_N}/publish"
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "post_id": _P,
                "slug": "2014/03/02/a",
                "url": "https://example.com/b/2014/03/02/a",
                "republished": False,
                "card": "none",
                "copy": "written",
                "later_field": 1,
            },
        )

    with _client(handler) as api:
        result = api.blog_publish(
            _G, _N, source="a.b.c", acknowledge=True, published_at="2014-03-02T17:00:00Z"
        )
    assert sent == [
        {"source": "a.b.c", "acknowledge": True, "published_at": "2014-03-02T17:00:00Z"}
    ]
    assert result == api_client.BlogPublished(
        post_id=_P,
        slug="2014/03/02/a",
        url="https://example.com/b/2014/03/02/a",
        republished=False,
        card="none",
        copy="written",
    )


def test_withdraw_parses_and_tolerates_a_missing_copy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/v1/groups/{_G}/blog/posts/{_P}/withdraw"
        return httpx.Response(200, json={"post_id": _P, "already": True, "card": "edited"})

    with _client(handler) as api:
        result = api.blog_withdraw(_G, _P)
    assert result.already is True
    assert result.copy is None


def test_a_non_object_posts_answer_is_an_api_error() -> None:
    with (
        _client(lambda request: httpx.Response(200, json=[])) as api,
        pytest.raises(api_client.ApiError),
    ):
        api.blog_posts(_G)
