r"""Tests for ``ApiClient.chat_send_stream`` and the SSE parser.

The parser is the only nontrivial piece of the client — it has to be
forgiving of malformed frames (a future server-side heartbeat
shouldn't crash the REPL), respect the SSE wire format (single space
after the colon, multi-line ``data:`` joined with ``\\n``), and
short-circuit on non-2xx before yielding anything.
"""

from __future__ import annotations

import httpx
import pytest

from pepper_cli import api_client, cli
from pepper_cli.api_client import _build_stream_event, _iter_sse_events
from pepper_cli.secret import SecretStr


def _mock(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _sse(*frames: str) -> str:
    r"""Build an SSE response body from already-formatted frames.

    Each frame must be terminated by ``\\n\\n``; this helper just
    concatenates so the tests read declaratively.
    """
    return "".join(frames)


def _frame(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


# ---------------------------------------------------------------------------
# Parser-only tests (no HTTP layer)


def test_parser_yields_event_with_kind_and_payload() -> None:
    body = _frame("model_thought", '{"text": "hello"}')
    events = list(_iter_sse_events(iter(body.split("\n"))))
    assert len(events) == 1
    assert events[0].kind == "model_thought"
    assert events[0].payload == {"text": "hello"}


def test_parser_handles_multiple_frames_in_order() -> None:
    body = (
        _frame("model_thought", '{"text": "a"}')
        + _frame("tool_call", '{"name": "list_events"}')
        + _frame(
            "reply",
            '{"reply": "done", "trajectory_id": null, '
            '"user_message_id": "u", "assistant_message_id": "a", "steps": []}',
        )
    )
    events = list(_iter_sse_events(iter(body.split("\n"))))
    assert [e.kind for e in events] == ["model_thought", "tool_call", "reply"]
    assert events[-1].payload["reply"] == "done"


def test_parser_drops_frames_with_malformed_json() -> None:
    """Defensive: a single bad frame must not kill the whole stream.
    A future server-side heartbeat or a corrupted frame in the middle
    of a long turn should leave the rest of the stream working."""
    body = (
        _frame("model_thought", '{"text": "fine"}')
        + _frame("model_thought", "not-json{")
        + _frame("reply", '{"reply": "still works"}')
    )
    events = list(_iter_sse_events(iter(body.split("\n"))))
    kinds = [e.kind for e in events]
    assert "reply" in kinds  # the good frame after the bad one made it through
    assert events[-1].payload["reply"] == "still works"
    # The bad frame was dropped.
    assert len(events) == 2


def test_parser_skips_comment_lines() -> None:
    """Lines starting with ``:`` are SSE comments — used for
    keep-alives. They must not produce events or break parsing."""
    body = ": keep-alive ping\n\n" + _frame("reply", '{"reply": "hi"}')
    events = list(_iter_sse_events(iter(body.split("\n"))))
    assert len(events) == 1
    assert events[0].kind == "reply"


def test_parser_joins_multiline_data_with_newline() -> None:
    r"""Per SSE spec, multiple ``data:`` lines in one frame are joined
    with ``\\n``. Pepper's server doesn't emit multi-line data today,
    but the parser is the public boundary — keep it spec-compliant."""
    body = 'event: x\ndata: {"a":\ndata: "b"}\n\n'
    events = list(_iter_sse_events(iter(body.split("\n"))))
    assert len(events) == 1
    assert events[0].payload == {"a": "b"}


def test_parser_defaults_event_to_message_when_omitted() -> None:
    """An SSE frame without an ``event:`` line defaults to
    ``"message"`` per spec. Not something the server emits today, but
    making the parser tolerant means a non-pepper SSE source can't
    crash the REPL."""
    events = list(_iter_sse_events(iter(['data: {"x": 1}', "", ""])))
    assert len(events) == 1
    assert events[0].kind == "message"


def test_parser_flushes_trailing_frame_without_blank_line() -> None:
    """Httpx's ``iter_lines`` may not emit a trailing blank if the
    server closed the stream right after the last frame's data line.
    The parser must still emit that final frame."""
    # No terminating \n\n on the last frame.
    body = 'event: reply\ndata: {"reply": "ok"}'
    events = list(_iter_sse_events(iter(body.split("\n"))))
    assert len(events) == 1
    assert events[0].payload["reply"] == "ok"


def test_build_stream_event_returns_none_for_non_dict_json() -> None:
    """JSON arrays / scalars in ``data:`` aren't usable payloads —
    we expect a dict. Return None rather than crash."""
    assert _build_stream_event("x", ["[1, 2, 3]"]) is None
    assert _build_stream_event("x", ['"string"']) is None
    assert _build_stream_event("x", ["42"]) is None


# ---------------------------------------------------------------------------
# Integration: chat_send_stream through ApiClient


def test_chat_send_stream_yields_events_in_order() -> None:
    body = (
        _frame("model_thought", '{"text": "thinking"}')
        + _frame("tool_call", '{"name": "list_events", "args": {"when": "today"}}')
        + _frame("tool_result", '{"name": "list_events", "status": "ok", "text": "3 events"}')
        + _frame(
            "reply",
            '{"reply": "you have 3 events today", "trajectory_id": "t", '
            '"user_message_id": "u", "assistant_message_id": "a", "steps": []}',
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept"] == "text/event-stream"
        assert request.headers["authorization"] == "Bearer pep_pat_abc"
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    with api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    ) as api:
        events = list(api.chat_send_stream("hi"))

    assert [e.kind for e in events] == [
        "model_thought",
        "tool_call",
        "tool_result",
        "reply",
    ]
    assert events[-1].payload["reply"] == "you have 3 events today"


def test_chat_stream_accepts_older_server_json_reply_without_resubmitting(capsys) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["accept"] == "text/event-stream"
        return httpx.Response(
            200,
            json={
                "reply": "saved answer",
                "trajectory_id": "t",
                "user_message_id": "u",
                "assistant_message_id": "a",
                "steps": [],
            },
            headers={"content-type": "application/json; charset=utf-8"},
        )

    with api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    ) as api:
        assert cli._stream_one_turn(api, "hi", trace=False) is True

    assert len(requests) == 1
    assert requests[0].url.path == "/api/v1/chat/send"
    out = capsys.readouterr()
    assert "saved answer" in out.out
    assert out.err == ""


@pytest.mark.parametrize(
    "body",
    [b"not-json", b"{}", b'{"reply": null}', b'{"reply": 123}', b"[]"],
)
def test_chat_stream_rejects_invalid_older_server_json_reply_without_resubmitting(
    body: bytes, capsys
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    with api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    ) as api:
        assert cli._stream_one_turn(api, "hi", trace=False) is False

    assert len(requests) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "invalid chat reply" in out.err.lower()
    assert "check the web chat" in out.err.lower()


def test_chat_stream_has_long_default_read_timeout_but_preserves_explicit_timeout() -> None:
    with api_client.ApiClient(
        server_url="http://example.com", token=SecretStr("pep_pat_abc")
    ) as api:
        assert api._client.timeout.read == api_client.DEFAULT_TIMEOUT
        assert api._chat_stream_read_timeout == api_client.DEFAULT_CHAT_STREAM_READ_TIMEOUT

    with api_client.ApiClient(
        server_url="http://example.com", token=SecretStr("pep_pat_abc"), timeout=7.0
    ) as api:
        assert api._client.timeout.read == 7.0
        assert api._chat_stream_read_timeout == 7.0


def test_chat_stream_passes_read_timeout_to_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"]["read"] == 7.0
        return httpx.Response(200, content=_frame("reply", '{"reply": "done"}'))

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=7.0)
    with api_client.ApiClient(
        server_url="http://example.com", token=SecretStr("pep_pat_abc"), client=client
    ) as api:
        assert [e.kind for e in api.chat_send_stream("hi")] == ["reply"]


def test_chat_send_stream_raises_api_error_on_401_before_yielding() -> None:
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
        # ``list`` forces the generator to start; that's where the
        # status check fires.
        list(api.chat_send_stream("hi"))
    assert ei.value.status == 401
    assert ei.value.detail == "invalid bearer token"


def test_chat_send_stream_propagates_transport_errors() -> None:
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
        list(api.chat_send_stream("hi"))


def test_chat_send_stream_handles_error_frame() -> None:
    """The server emits a terminal ``event: error`` when the agent
    fails mid-stream. The parser surfaces it like any other event —
    the REPL is responsible for rendering and not advancing."""
    body = _frame("model_thought", '{"text": "starting"}') + _frame(
        "error", '{"kind": "error", "message": "Model service unavailable"}'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    with api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    ) as api:
        events = list(api.chat_send_stream("hi"))

    assert events[-1].kind == "error"
    assert events[-1].payload["message"] == "Model service unavailable"


# ---------------------------------------------------------------------------
# Mid-stream retry: the retry_attempt frame

_REPLY_FRAME = _frame(
    "reply", '{"reply": "done", "user_message_id": "u", "assistant_message_id": "a"}'
)


def _stream_stderr(body: str, capsys) -> str:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    with api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    ) as api:
        assert cli._stream_one_turn(api, "hi") is True
    return capsys.readouterr().err


def test_retry_attempt_does_not_fuse_the_abandoned_thought(capsys) -> None:
    """The abandoned fragments end their line, the retry's start a fresh
    thought, and the block is not reprinted. Without the branch the screen
    held ``Check the calCheck the calendar first.`` on one line."""
    body = (
        _frame("model_thought", '{"text": "Check the cal"}')
        + _frame("retry_attempt", "{}")
        + _frame("model_thought", '{"text": "Check the calendar first."}')
        + _frame("model_thought_block", '{"text": "Check the calendar first."}')
        + _REPLY_FRAME
    )
    err_lines = [line for line in _stream_stderr(body, capsys).splitlines() if line.strip()]
    assert err_lines == ["· Check the cal", "· Check the calendar first."]


def test_retry_attempt_with_nothing_streamed_prints_nothing(capsys) -> None:
    """The common retry lands before any fragment: output is byte-identical
    to the same turn without the frame."""
    thought = _frame("model_thought", '{"text": "Thinking."}') + _frame(
        "model_thought_block", '{"text": "Thinking."}'
    )
    baseline = _stream_stderr(thought + _REPLY_FRAME, capsys)
    retried = _stream_stderr(_frame("retry_attempt", "{}") + thought + _REPLY_FRAME, capsys)
    assert retried == baseline


def test_chat_send_stream_names_the_app_on_screen_only_when_asked() -> None:
    import json

    bodies: list[dict] = []
    reply = _frame("reply", '{"reply": "ok"}')

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=reply, headers={"content-type": "text/event-stream"})

    with api_client.ApiClient(
        server_url="http://example.com", token=SecretStr("pep_pat_abc"), client=_mock(handler)
    ) as api:
        list(api.chat_send_stream("plain"))
        list(api.chat_send_stream("todos", subject="app:todo"))
        list(api.chat_send_stream("note", subject="app:notes", client_context={"note_id": "n-1"}))

    assert "subject" not in bodies[0]
    assert "client_context" not in bodies[0]
    assert bodies[1]["subject"] == "app:todo"
    assert "client_context" not in bodies[1]
    assert bodies[2]["subject"] == "app:notes"
    assert bodies[2]["client_context"] == {"note_id": "n-1"}
