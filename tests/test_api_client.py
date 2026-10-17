"""Tests for the httpx-based API client.

Uses ``httpx.MockTransport`` to assert what's on the wire (headers,
body, URL) without spinning up a real server. The CLI's
``api_client.py`` module is the contract surface that future
iOS/Android ports learn from — these tests pin that contract.
"""

from __future__ import annotations

import httpx
import pytest

from pepper_cli import api_client
from pepper_cli.secret import SecretStr


def _mock(handler) -> httpx.Client:
    """Build an ``httpx.Client`` whose requests are answered by
    ``handler`` instead of the network. Closing the client closes the
    transport."""
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# whoami


def test_whoami_sends_bearer_header_and_parses_response() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "user_id": "00000000-0000-0000-0000-000000000001",
                "email": "alice@example.com",
                "display_name": "Alice",
                "timezone": "America/Los_Angeles",
            },
        )

    with api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    ) as api:
        who = api.whoami()

    assert seen["method"] == "GET"
    assert seen["url"] == "http://example.com/api/v1/whoami"
    assert seen["auth"] == "Bearer pep_pat_abc"
    assert who.email == "alice@example.com"
    assert who.display_name == "Alice"
    assert who.timezone == "America/Los_Angeles"
    # An older server omits the format fields; they read as the web's defaults.
    assert who.date_format == "mdy"
    assert who.time_format == "12h"


def test_whoami_parses_the_date_and_time_formats() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "user_id": "00000000-0000-0000-0000-000000000001",
                "email": None,
                "display_name": "Alice",
                "timezone": None,
                "date_format": "dmy",
                "time_format": "24h",
            },
        )

    with api_client.ApiClient(
        server_url="http://example.com", token=SecretStr("pep_pat_abc"), client=_mock(handler)
    ) as api:
        who = api.whoami()

    assert who.date_format == "dmy"
    assert who.time_format == "24h"


def test_whoami_parses_the_groups_person_id_and_tolerates_its_absence() -> None:
    body = {
        "user_id": "00000000-0000-0000-0000-000000000001",
        "email": None,
        "display_name": "Alice",
        "timezone": None,
    }
    person = "00000000-0000-0000-0000-0000000000f1"

    def whoami(extra: dict[str, object]) -> api_client.Whoami:
        with api_client.ApiClient(
            server_url="http://example.com",
            token=SecretStr("pep_pat_abc"),
            client=_mock(lambda _r: httpx.Response(200, json={**body, **extra})),
        ) as api:
            return api.whoami()

    assert whoami({"person_id": person}).person_id == person
    assert whoami({"person_id": None}).person_id is None
    # A server older than the field omits it.
    assert whoami({}).person_id is None


def test_whoami_handles_null_optional_fields() -> None:
    """Email + timezone are nullable on the server; client mirrors that."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "user_id": "00000000-0000-0000-0000-000000000002",
                "email": None,
                "display_name": "Bob",
                "timezone": None,
            },
        )

    with api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    ) as api:
        who = api.whoami()

    assert who.email is None
    assert who.timezone is None


def test_whoami_strips_trailing_slash_from_server_url() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200, json={"user_id": "x", "email": None, "display_name": "x", "timezone": None}
        )

    with api_client.ApiClient(
        server_url="http://example.com/",  # trailing slash
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    ) as api:
        api.whoami()

    # No double-slash.
    assert seen["url"] == "http://example.com/api/v1/whoami"


# ---------------------------------------------------------------------------
# chat_send


def test_chat_send_sends_json_body_and_parses_response() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "reply": "hi from pepper",
                "trajectory_id": "11111111-1111-1111-1111-111111111111",
                "user_message_id": "22222222-2222-2222-2222-222222222222",
                "assistant_message_id": "33333333-3333-3333-3333-333333333333",
                "steps": [],
            },
        )

    with api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    ) as api:
        reply = api.chat_send("hi")

    assert seen["method"] == "POST"
    assert seen["url"] == "http://example.com/api/v1/chat/send"
    assert '"message"' in str(seen["body"])
    assert "hi" in str(seen["body"])
    assert reply.reply == "hi from pepper"
    assert reply.trajectory_id == "11111111-1111-1111-1111-111111111111"
    assert reply.user_message_id == "22222222-2222-2222-2222-222222222222"
    assert reply.assistant_message_id == "33333333-3333-3333-3333-333333333333"
    assert reply.steps == []


def test_chat_send_omits_attachment_ids_when_none() -> None:
    """No ``attachment_ids`` passed → the key doesn't ride in the JSON
    body at all (not an empty list) — keeps the wire body identical to
    every existing caller."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "reply": "hi",
                "trajectory_id": None,
                "user_message_id": "u",
                "assistant_message_id": "a",
                "steps": [],
            },
        )

    with api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    ) as api:
        api.chat_send("hi")

    assert "attachment_ids" not in str(seen["body"])


def test_chat_send_includes_attachment_ids_when_given() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "reply": "hi",
                "trajectory_id": None,
                "user_message_id": "u",
                "assistant_message_id": "a",
                "steps": [],
            },
        )

    with api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    ) as api:
        api.chat_send("hi", attachment_ids=["11111111-1111-1111-1111-111111111111"])

    assert "attachment_ids" in str(seen["body"])
    assert "11111111-1111-1111-1111-111111111111" in str(seen["body"])


def test_chat_send_carries_steps_through() -> None:
    """The CLI surfaces the steps list to the caller for future
    "show reasoning" affordances; the client must pass it through
    untouched."""
    steps = [
        {"kind": "thought", "text": "thinking..."},
        {"kind": "tool_call", "name": "list_events", "args": {}},
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "reply": "done",
                "trajectory_id": None,
                "user_message_id": "u",
                "assistant_message_id": "a",
                "steps": steps,
            },
        )

    with api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    ) as api:
        reply = api.chat_send("hi")

    assert reply.steps == steps
    assert reply.trajectory_id is None


# ---------------------------------------------------------------------------
# Error mapping


def test_raises_api_error_on_401() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid bearer token"})

    with (
        api_client.ApiClient(
            server_url="http://example.com",
            token=SecretStr("pep_pat_abc"),
            client=_mock(handler),
        ) as api,
        pytest.raises(api_client.ApiError) as ei,
    ):
        api.whoami()

    assert ei.value.status == 401
    assert ei.value.detail == "invalid bearer token"


def test_raises_api_error_on_422_with_validation_payload() -> None:
    """FastAPI's 422 payload is structured (``{"detail": [...]}``), not
    a plain string. The client falls back to the raw body for the
    error message rather than blowing up on the shape mismatch."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"detail": [{"loc": ["body", "message"], "msg": "field required"}]},
        )

    with (
        api_client.ApiClient(
            server_url="http://example.com",
            token=SecretStr("pep_pat_abc"),
            client=_mock(handler),
        ) as api,
        pytest.raises(api_client.ApiError) as ei,
    ):
        api.chat_send("")

    assert ei.value.status == 422
    # ``detail`` is a list, not a string — left as None on the
    # exception so the CLI surfaces the raw body instead of crashing.
    assert ei.value.detail is None
    assert "field required" in ei.value.body


def test_raises_api_error_on_502_without_json_body() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway")

    with (
        api_client.ApiClient(
            server_url="http://example.com",
            token=SecretStr("pep_pat_abc"),
            client=_mock(handler),
        ) as api,
        pytest.raises(api_client.ApiError) as ei,
    ):
        api.whoami()

    assert ei.value.status == 502
    assert ei.value.detail is None
    assert ei.value.body == "Bad Gateway"


def test_transport_errors_are_not_wrapped() -> None:
    """Connection refused / DNS / TLS errors are caller-handled at the
    CLI boundary so they get different messaging than HTTP errors —
    keep them as ``httpx.RequestError`` rather than wrapping in
    :class:`ApiError`."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    with (
        api_client.ApiClient(
            server_url="http://example.com",
            token=SecretStr("pep_pat_abc"),
            client=_mock(handler),
        ) as api,
        pytest.raises(httpx.RequestError),
    ):
        api.whoami()


def test_context_manager_closes_client() -> None:
    """The ``__exit__`` path must close the underlying httpx.Client so
    connections release deterministically — matters for tests that
    re-use the same MockTransport, less so for production where the
    process exits right after."""
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "user_id": "x",
                    "email": None,
                    "display_name": "x",
                    "timezone": None,
                },
            )
        )
    )
    with api_client.ApiClient(
        server_url="http://example.com", token=SecretStr("pep_pat_abc"), client=client
    ):
        pass
    assert client.is_closed


# ---------------------------------------------------------------------------
# list_skills


def test_list_skills_returns_summary_objects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "http://example.com/api/v1/skills"
        assert request.headers["authorization"] == "Bearer pep_pat_abc"
        return httpx.Response(
            200,
            json=[
                {"name": "morning-brief", "description": "Daily summary"},
                {"name": "inbox-triage", "description": "Sort the inbox"},
            ],
        )

    with api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    ) as api:
        skills = api.list_skills()

    assert len(skills) == 2
    assert skills[0].name == "morning-brief"
    assert skills[0].description == "Daily summary"
    assert skills[1].name == "inbox-triage"


def test_list_skills_empty_response() -> None:
    """A user with no skills gets an empty list, not an error. The
    REPL renders this case fine — completion just covers the static
    commands."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    with api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    ) as api:
        assert api.list_skills() == []


def test_list_skills_raises_api_error_on_401() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid bearer token"})

    with (
        api_client.ApiClient(
            server_url="http://example.com",
            token=SecretStr("pep_pat_abc"),
            client=_mock(handler),
        ) as api,
        pytest.raises(api_client.ApiError) as ei,
    ):
        api.list_skills()
    assert ei.value.status == 401


# ---------------------------------------------------------------------------
# trajectory_get / trajectory_get_raw / trajectory_list


_TRAJ_ID = "11111111-1111-1111-1111-111111111111"
_USER_ID = "22222222-2222-2222-2222-222222222222"


def _trajectory_body(events: list[dict[str, object]] | None = None) -> dict[str, object]:
    """Build a server-shaped ``TrajectoryDetailResponse`` body."""
    return {
        "trajectory": {
            "id": _TRAJ_ID,
            "user_id": _USER_ID,
            "channel": "client_api",
            "created_at": "2026-05-31T12:00:00Z",
            "closed_at": None,
            "meta": {"code_sha": "abcdef1234567890", "code_dirty": False},
        },
        "events": events
        if events is not None
        else [
            {
                "id": "aaaa1111-1111-1111-1111-111111111111",
                "seq": 0,
                "event_type": "user_message",
                "payload": {"text": "hi"},
                "created_at": "2026-05-31T12:00:01Z",
            },
            {
                "id": "aaaa2222-2222-2222-2222-222222222222",
                "seq": 1,
                "event_type": "turn_end",
                "payload": {"status": "ok"},
                "created_at": "2026-05-31T12:00:02Z",
            },
        ],
    }


def test_trajectory_get_returns_parsed_dataclasses() -> None:
    """``trajectory_get`` parses the response into the dataclass tree
    that future iOS/Android ports can mirror — header + ordered events."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_trajectory_body())

    with api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    ) as api:
        detail = api.trajectory_get(_TRAJ_ID)

    assert seen["method"] == "GET"
    assert seen["url"] == f"http://example.com/api/v1/trajectories/{_TRAJ_ID}"
    assert seen["auth"] == "Bearer pep_pat_abc"
    assert detail.trajectory.id == _TRAJ_ID
    assert detail.trajectory.meta["code_sha"] == "abcdef1234567890"
    assert [e.seq for e in detail.events] == [0, 1]
    assert detail.events[0].event_type == "user_message"
    assert detail.events[0].payload == {"text": "hi"}


def test_trajectory_get_raw_returns_wire_dict() -> None:
    """``trajectory_get_raw`` keeps the response body untouched so the
    CLI's ``--json`` flag can re-serialize without parse loss."""
    body = _trajectory_body()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    ) as api:
        raw = api.trajectory_get_raw(_TRAJ_ID)

    assert raw == body


def test_trajectory_get_raises_api_error_on_404() -> None:
    """The CLI maps 404 to its "not found, or not yours" message — and
    the access-trajectory skill falls back to the service-token script
    on that signal. Status must propagate cleanly."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "trajectory not found"})

    with (
        api_client.ApiClient(
            server_url="http://example.com",
            token=SecretStr("pep_pat_abc"),
            client=_mock(handler),
        ) as api,
        pytest.raises(api_client.ApiError) as ei,
    ):
        api.trajectory_get(_TRAJ_ID)

    assert ei.value.status == 404
    assert ei.value.detail == "trajectory not found"


def test_trajectory_list_parses_rows_and_passes_paging_params() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json=[
                {
                    "id": _TRAJ_ID,
                    "channel": "client_api",
                    "created_at": "2026-05-31T12:00:00Z",
                    "closed_at": None,
                    "meta": {},
                    "event_count": 4,
                    "first_user_message": "hi there",
                    "group_id": None,
                    "group_name": None,
                },
            ],
        )

    with api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    ) as api:
        rows = api.trajectory_list(limit=5, offset=10)

    assert seen["url"] == "http://example.com/api/v1/trajectories?limit=5&offset=10"
    assert len(rows) == 1
    assert rows[0].id == _TRAJ_ID
    assert rows[0].event_count == 4
    assert rows[0].first_user_message == "hi there"


def test_trajectory_list_handles_empty_response() -> None:
    """A fresh user with no trajectories gets ``[]``, not an error."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    with api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    ) as api:
        assert api.trajectory_list() == []


# ---------------------------------------------------------------------------
# device_start / device_poll — unauthenticated device-authorization flow


def test_device_start_sends_no_auth_header_and_parses_response() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content.decode()
        return httpx.Response(
            201,
            json={
                "device_code": "pep_dev_abc123",
                "user_code": "ABCD-EFGH",
                "verification_url": "https://example.com/auth/activate?user_code=ABCD-EFGH",
                "expires_in": 600,
                "interval": 5,
            },
        )

    start = api_client.device_start("http://example.com", "alice@laptop", client=_mock(handler))

    assert seen["method"] == "POST"
    assert seen["url"] == "http://example.com/auth/device/start"
    # No bearer header — there's no token yet, that's the whole point.
    assert seen["auth"] is None
    assert '"client":"cli"' in str(seen["body"]).replace(" ", "")
    assert '"device_name":"alice@laptop"' in str(seen["body"]).replace(" ", "")
    assert isinstance(start.device_code, SecretStr)
    assert start.device_code.get_secret_value() == "pep_dev_abc123"
    assert start.user_code == "ABCD-EFGH"
    assert start.verification_url == "https://example.com/auth/activate?user_code=ABCD-EFGH"
    assert start.expires_in == 600
    assert start.interval == 5


def test_device_start_strips_trailing_slash_from_server_url() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            201,
            json={
                "device_code": "pep_dev_x",
                "user_code": "AAAA-BBBB",
                "verification_url": "https://example.com/auth/activate",
                "expires_in": 600,
                "interval": 5,
            },
        )

    api_client.device_start("http://example.com/", "dev", client=_mock(handler))
    assert seen["url"] == "http://example.com/auth/device/start"


def test_device_start_raises_api_error_on_422() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "device_name too long"})

    with pytest.raises(api_client.ApiError) as ei:
        api_client.device_start("http://example.com", "x" * 200, client=_mock(handler))
    assert ei.value.status == 422
    assert ei.value.detail == "device_name too long"


def test_device_poll_pending() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "http://example.com/auth/device/poll"
        assert request.headers.get("authorization") is None
        assert '"device_code"' in request.content.decode()
        return httpx.Response(200, json={"status": "pending"})

    result = api_client.device_poll(
        "http://example.com", SecretStr("pep_dev_abc"), client=_mock(handler)
    )
    assert result.status == "pending"
    assert result.token is None
    assert result.user is None


def test_device_poll_denied() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "denied"})

    result = api_client.device_poll(
        "http://example.com", SecretStr("pep_dev_abc"), client=_mock(handler)
    )
    assert result.status == "denied"


def test_device_poll_expired() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "expired"})

    result = api_client.device_poll(
        "http://example.com", SecretStr("pep_dev_abc"), client=_mock(handler)
    )
    assert result.status == "expired"


def test_device_poll_approved_parses_token_and_user() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "approved",
                "token": "pep_pat_freshlyminted",
                "user": {
                    "user_id": "11111111-1111-1111-1111-111111111111",
                    "email": "alice@example.com",
                    "display_name": "Alice",
                    "timezone": "America/Los_Angeles",
                },
            },
        )

    result = api_client.device_poll(
        "http://example.com", SecretStr("pep_dev_abc"), client=_mock(handler)
    )
    assert result.status == "approved"
    assert isinstance(result.token, SecretStr)
    assert result.token.get_secret_value() == "pep_pat_freshlyminted"
    assert result.user is not None
    assert result.user.display_name == "Alice"
    assert result.user.email == "alice@example.com"
    assert result.user.timezone == "America/Los_Angeles"


def test_device_poll_429_maps_to_slow_down_not_api_error() -> None:
    """A 429 is an expected backoff signal in the poll loop, not an
    error — must not raise :class:`api_client.ApiError`."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"status": "slow_down", "interval": 5})

    result = api_client.device_poll(
        "http://example.com", SecretStr("pep_dev_abc"), client=_mock(handler)
    )
    assert result.status == "slow_down"
    assert result.interval == 5


def test_device_poll_raises_api_error_on_other_4xx() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "malformed device_code"})

    with pytest.raises(api_client.ApiError) as ei:
        api_client.device_poll("http://example.com", SecretStr("bogus"), client=_mock(handler))
    assert ei.value.status == 400
    assert ei.value.detail == "malformed device_code"
