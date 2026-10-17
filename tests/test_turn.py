"""The shared SSE dispatch seam (``pepper_cli.turn``), used by the REPL and the TUI."""

from __future__ import annotations

from typing import Any

import httpx

from pepper_cli import turn
from pepper_cli.api_client import ApiError, StreamEvent


class Recorder(turn.TurnSink):
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def frame(self, event: StreamEvent) -> None:
        self.calls.append(("frame", event.kind))

    def thought_delta(self, text: str, *, first: bool) -> None:
        self.calls.append(("delta", text, first))

    def thought_block(self, text: str, *, streamed: str) -> None:
        self.calls.append(("block", text, streamed))

    def retry(self, *, streamed: str) -> None:
        self.calls.append(("retry", streamed))

    def tool_call(self, payload: dict[str, Any]) -> None:
        self.calls.append(("tool_call", payload["name"]))

    def tool_result(self, payload: dict[str, Any]) -> None:
        self.calls.append(("tool_result", payload["name"]))

    def client_tools(self, payload: dict[str, Any]) -> None:
        self.calls.append(("client_tools",))

    def client_tool_call(self, payload: dict[str, Any]) -> None:
        self.calls.append(("client_tool_call", payload["call_id"]))

    def reply(self, payload: dict[str, Any]) -> None:
        self.calls.append(("reply", payload["reply"]))

    def error(self, payload: dict[str, Any]) -> None:
        self.calls.append(("error", payload["message"]))


def ev(kind: str, **payload: Any) -> StreamEvent:
    return StreamEvent(kind=kind, payload=payload)


def kinds(calls: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    return [c for c in calls if c[0] != "frame"]


def test_dispatch_assembles_thoughts_and_stops_at_the_reply() -> None:
    sink = Recorder()
    events = [
        ev("model_thought", text="a"),
        ev("model_thought", text=""),
        ev("model_thought", text="b"),
        ev("retry_attempt"),
        ev("model_thought", text="c"),
        ev("model_thought_block", text="cd"),
        ev("tool_call", name="search"),
        ev("tool_result", name="search"),
        ev("client_tools"),
        ev("client_tool_call", call_id="c1"),
        ev("something_new", x=1),
        ev("reply", reply="done"),
        ev("model_thought", text="never read"),
    ]
    assert turn.dispatch(events, sink) == "reply"
    assert kinds(sink.calls) == [
        ("delta", "a", True),
        ("delta", "b", False),
        ("retry", "ab"),
        ("delta", "c", True),
        ("block", "cd", "c"),
        ("tool_call", "search"),
        ("tool_result", "search"),
        ("client_tools",),
        ("client_tool_call", "c1"),
        ("reply", "done"),
    ]
    # Every frame, the unknown one included, reaches ``frame`` first.
    assert ("frame", "something_new") in sink.calls


def test_dispatch_outcomes_error_ended_and_stopped() -> None:
    assert turn.dispatch([ev("error", message="no")], Recorder()) == "error"
    assert turn.dispatch([ev("model_thought", text="x")], Recorder()) == "ended"
    sink = Recorder()
    assert turn.dispatch([ev("reply", reply="r")], sink, should_stop=lambda: True) == "stopped"
    assert sink.calls == []


class FakeApi:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail
        self.results: list[dict[str, Any]] = []
        self.sent: list[tuple[str, Any]] = []
        self.closed = False

    def post_tool_result(self, **kwargs: Any) -> None:
        if self.fail is not None:
            raise self.fail
        self.results.append(kwargs)

    def chat_send_stream(self, message: str, *, client_tools: Any = None):
        self.sent.append((message, client_tools))
        try:
            yield ev("reply", reply="ok")
            yield ev("model_thought", text="after")
        finally:
            self.closed = True


class FakeProvider:
    def manifest(self) -> list[dict[str, Any]]:
        return [{"name": "fs_read"}]

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        return f"{name}:{arguments.get('path')}", False


def test_run_turn_sends_the_manifest_and_closes_the_stream() -> None:
    api = FakeApi()
    assert turn.run_turn(api, "hi", Recorder(), provider=FakeProvider()) == "reply"
    assert api.sent == [("hi", [{"name": "fs_read"}])]
    assert api.closed
    api = FakeApi()
    turn.run_turn(api, "hi", Recorder())
    assert api.sent == [("hi", None)]  # no provider, no manifest


def test_answer_client_tool_call_runs_the_tool_and_posts_its_result() -> None:
    api = FakeApi()
    payload = {"call_id": "c1", "name": "fs_read", "arguments": {"path": "a.md"}}
    answer = turn.answer_client_tool_call(api, payload, provider=FakeProvider())
    assert answer == turn.ToolAnswer("fs_read", {"path": "a.md"}, "fs_read:a.md", False)
    assert api.results == [
        {"call_id": "c1", "content": [{"type": "text", "text": "fs_read:a.md"}], "is_error": False}
    ]


def test_answer_client_tool_call_without_a_provider_or_call_id() -> None:
    api = FakeApi()
    assert turn.answer_client_tool_call(api, {"name": "x"}, provider=None) is None
    answer = turn.answer_client_tool_call(
        api,
        {"call_id": "c2", "name": "fs_read", "arguments": "not a dict"},
        provider=None,
    )
    assert answer is not None
    assert (answer.text, answer.is_error, answer.arguments) == (turn.NO_LOCAL_TOOLS, True, {})


def test_a_failed_post_is_returned_not_raised() -> None:
    for exc in (ApiError(404, "gone"), httpx.ConnectError("down")):
        api = FakeApi(fail=exc)
        answer = turn.answer_client_tool_call(
            api,
            {"call_id": "c3", "name": "fs_read", "arguments": {}},
            provider=FakeProvider(),
        )
        assert answer is not None and answer.post_error is exc
