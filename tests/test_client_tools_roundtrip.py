"""``pepper chat --root …`` end to end, against a fake server.

Drives the whole client half of the client-tools round trip through
``httpx.MockTransport``: the manifest rides up on the chat request, a
``client_tool_call`` frame comes down the SSE stream, the CLI runs the tool
locally and posts the answer to ``/api/v1/chat/tool-result`` **while the
stream is still open**, and the reply frame lands last.

The two negative shapes matter as much as the happy one: without ``--root``
the streamed request declares no local tools, and a server that never emits
client-tool frames is a server the CLI works against unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr


def _frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


class _FakeServer:
    """A chat/send + chat/tool-result pair that records what it was sent."""

    def __init__(self, *, frames_before_reply: list[str] | None = None) -> None:
        self.chat_bodies: list[dict] = []
        self.results: list[dict] = []
        self._extra = frames_before_reply or []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/chat/send":
            self.chat_bodies.append(json.loads(request.content))
            body = "".join(
                [
                    *self._extra,
                    _frame(
                        "reply",
                        {
                            "reply": "done",
                            "trajectory_id": None,
                            "user_message_id": "u",
                            "assistant_message_id": "a",
                            "steps": [],
                        },
                    ),
                ]
            )
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
        if request.url.path == "/api/v1/chat/tool-result":
            self.results.append(json.loads(request.content))
            return httpx.Response(204)
        raise AssertionError(f"unexpected path {request.url.path}")


def _client(server: _FakeServer) -> api_client.ApiClient:
    return api_client.ApiClient(
        server_url="http://test",
        token=SecretStr("pep_pat_x"),
        client=httpx.Client(transport=httpx.MockTransport(server.handler)),
    )


@pytest.fixture
def notes(tmp_path: Path) -> Path:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "plan.md").write_bytes(b"Ship phase one.\n")  # bytes: no CRLF translation on Windows
    return root


def test_a_declared_tool_is_called_and_answered(notes: Path) -> None:
    server = _FakeServer(
        frames_before_reply=[
            _frame(
                "client_tools",
                {
                    "accepted": [{"name": "fs_read", "exposed_name": "client_fs_read"}],
                    "rejected": [],
                    "manifest_reason": None,
                },
            ),
            _frame(
                "client_tool_call",
                {
                    "call_id": "abc",
                    "name": "fs_read",
                    "exposed_name": "client_fs_read",
                    "arguments": {"path": "plan.md"},
                    "deadline_ms": 20000,
                },
            ),
        ]
    )
    provider = cli.provider_fs.FilesystemProvider([notes])
    with _client(server) as api:
        cli._stream_one_turn(api, "what's in my notes?", provider=provider, trace=False)

    sent = server.chat_bodies[0]["client_tools"]
    assert [t["name"] for t in sent] == ["fs_list", "fs_read", "fs_grep"]
    assert server.results == [
        {
            "call_id": "abc",
            "content": [{"type": "text", "text": "notes/plan.md:\nShip phase one.\n"}],
            "is_error": False,
        }
    ]


def test_a_refused_path_is_returned_as_an_error_not_a_silence(notes: Path) -> None:
    server = _FakeServer(
        frames_before_reply=[
            _frame(
                "client_tool_call",
                {
                    "call_id": "abc",
                    "name": "fs_read",
                    "arguments": {"path": "../../etc/passwd"},
                    "deadline_ms": 20000,
                },
            )
        ]
    )
    provider = cli.provider_fs.FilesystemProvider([notes])
    with _client(server) as api:
        cli._stream_one_turn(api, "read that", provider=provider, trace=False)
    assert server.results[0]["is_error"] is True


def test_a_call_with_no_provider_is_answered_immediately(notes: Path) -> None:
    """A frame for a tool this process never declared still holds a call the
    server is waiting on — saying so beats burning the whole 20s budget."""
    server = _FakeServer(
        frames_before_reply=[
            _frame(
                "client_tool_call",
                {"call_id": "abc", "name": "fs_read", "arguments": {}, "deadline_ms": 20000},
            )
        ]
    )
    with _client(server) as api:
        cli._stream_one_turn(api, "hi", provider=None, trace=False)
    assert server.results[0]["is_error"] is True
    assert server.chat_bodies[0].get("client_tools") is None


def test_without_a_root_the_request_is_unchanged_on_a_terminal(monkeypatch) -> None:
    monkeypatch.setattr(api_client.sys.stdout, "isatty", lambda: True, raising=False)
    server = _FakeServer()
    with _client(server) as api:
        cli._stream_one_turn(api, "hi", provider=None, trace=False)
    assert server.chat_bodies == [{"message": "hi"}]


def test_without_a_root_a_piped_request_only_adds_the_plain_declaration(monkeypatch) -> None:
    monkeypatch.setattr(api_client.sys.stdout, "isatty", lambda: False, raising=False)
    server = _FakeServer()
    with _client(server) as api:
        cli._stream_one_turn(api, "hi", provider=None, trace=False)
    assert server.chat_bodies == [
        {"message": "hi", "capabilities": api_client.turn_capabilities(False)}
    ]


def test_a_server_that_never_sends_the_new_frames_still_works(notes: Path) -> None:
    """An older server ignores the manifest field and emits no client frames;
    the CLI must simply take the reply."""
    server = _FakeServer()
    provider = cli.provider_fs.FilesystemProvider([notes])
    with _client(server) as api:
        cli._stream_one_turn(api, "hi", provider=provider, trace=False)
    assert server.results == []


def test_an_unknown_frame_kind_is_ignored(notes: Path) -> None:
    server = _FakeServer(frames_before_reply=[_frame("some_future_kind", {"x": 1})])
    provider = cli.provider_fs.FilesystemProvider([notes])
    with _client(server) as api:
        cli._stream_one_turn(api, "hi", provider=provider, trace=False)
    assert server.results == []


def test_a_404_on_the_result_post_does_not_abort_the_stream(notes: Path, capsys) -> None:
    """The call already timed out or the turn moved on. There is nothing to
    recover, and the reply is still coming."""

    class _Refusing(_FakeServer):
        def handler(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/chat/tool-result":
                return httpx.Response(404, json={"detail": "unknown call_id"})
            return super().handler(request)

    server = _Refusing(
        frames_before_reply=[
            _frame(
                "client_tool_call",
                {
                    "call_id": "abc",
                    "name": "fs_read",
                    "arguments": {"path": "plan.md"},
                    "deadline_ms": 20000,
                },
            )
        ]
    )
    provider = cli.provider_fs.FilesystemProvider([notes])
    with _client(server) as api:
        cli._stream_one_turn(api, "hi", provider=provider, trace=False)
    assert "done" in capsys.readouterr().out


def test_cmd_chat_without_a_root_streams_without_local_tools(monkeypatch, capsys) -> None:
    """A long one-shot turn streams, but grants no filesystem capability."""
    calls: list[tuple[str, object]] = []

    class _Api:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def chat_send_stream(self, message: str, *, client_tools=None):
            calls.append((message, client_tools))
            yield api_client.StreamEvent(kind="model_thought", payload={"text": "private"})
            yield api_client.StreamEvent(kind="reply", payload={"reply": "hi back"})

    monkeypatch.setattr(
        cli,
        "_load_or_error",
        lambda *_: credentials.Credentials(server_url="http://test", token=SecretStr("t")),
    )
    monkeypatch.setattr(cli.api_client, "ApiClient", lambda **kw: _Api())
    args = cli._build_parser()[0].parse_args(["chat", "hello"])
    assert cli.cmd_chat(args) == 0
    assert calls == [("hello", None)]
    assert capsys.readouterr().out.strip() == "hi back"


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        ([api_client.StreamEvent(kind="error", payload={"message": "turn failed"})], "turn failed"),
        ([], "ended without a reply"),
    ],
)
def test_cmd_chat_requires_terminal_reply(monkeypatch, capsys, events, expected) -> None:
    calls: list[str] = []

    class _Api:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def chat_send_stream(self, message: str, *, client_tools=None):
            calls.append(message)
            yield from events

    monkeypatch.setattr(
        cli,
        "_load_or_error",
        lambda *_: credentials.Credentials(server_url="http://test", token=SecretStr("t")),
    )
    monkeypatch.setattr(cli.api_client, "ApiClient", lambda **kw: _Api())
    args = cli._build_parser()[0].parse_args(["chat", "hello"])
    assert cli.cmd_chat(args) == 1
    assert calls == ["hello"]
    assert expected in capsys.readouterr().err


def test_cmd_chat_with_a_root_streams_instead(monkeypatch, notes: Path) -> None:
    """``--root`` adds a local provider to the one-shot stream."""
    streamed: list[tuple[str, bool]] = []

    class _Api:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(
        cli,
        "_load_or_error",
        lambda *_: credentials.Credentials(server_url="http://test", token=SecretStr("t")),
    )
    monkeypatch.setattr(cli.api_client, "ApiClient", lambda **kw: _Api())
    monkeypatch.setattr(
        cli,
        "_stream_one_turn",
        lambda api, message, *, provider, trace, json_args=None: (
            streamed.append((message, trace)) or True
        ),
    )
    args = cli._build_parser()[0].parse_args(["chat", "--root", str(notes), "hello"])
    assert cli.cmd_chat(args) == 0
    assert streamed == [("hello", False)]


def test_a_bad_root_fails_before_any_request(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        cli,
        "_load_or_error",
        lambda *_: credentials.Credentials(server_url="http://test", token=SecretStr("t")),
    )
    args = cli._build_parser()[0].parse_args(["chat", "--root", str(tmp_path / "nope"), "hello"])
    assert cli.cmd_chat(args) == 2


def test_tools_root_is_accepted_as_an_alias(notes: Path) -> None:
    args = cli._build_parser()[0].parse_args(["chat", "--tools-root", str(notes), "hi"])
    assert args.root == [str(notes)]
