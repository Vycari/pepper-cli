"""The ``pepper chat`` waiting spinner: shown for a person at a terminal, invisible otherwise."""

from __future__ import annotations

import argparse
import io
from collections.abc import Iterator
from typing import Any, Self

import pytest

from pepper_cli import api_client, cli, render


class _Api:
    def __init__(self, events: list[Any]) -> None:
        self.events = events

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def chat_send_stream(self, message: str, **_: object) -> Iterator[api_client.StreamEvent]:
        for event in self.events:
            if isinstance(event, BaseException):
                raise event
            yield event


def _events(*, phrases: bool = True) -> list[Any]:
    lead = (
        [api_client.StreamEvent(kind="status_phrase", payload={"text": "Counting electrons"})]
        if phrases
        else []
    )
    return [
        *lead,
        api_client.StreamEvent(kind="tool_call", payload={"name": "x", "args": {}}),
        api_client.StreamEvent(kind="tool_result", payload={"name": "x", "status": "ok"}),
        api_client.StreamEvent(kind="reply", payload={"reply": "here you go"}),
    ]


class _Spy(render.WaitStatus):
    log: list[str]

    def __init__(self) -> None:
        super().__init__()
        self.log = []
        _Spy.last = self

    last: _Spy

    def start(self) -> None:
        self.log.append("start")
        super().start()

    def stop(self) -> None:
        self.log.append("stop")
        super().stop()

    def set(self, text: str) -> None:
        self.log.append(text)
        super().set(text)


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """Pretend stderr is a terminal and capture what it is sent."""
    buf = io.StringIO()
    monkeypatch.setattr(render, "_stderr", render._make_console(file=buf, force_terminal=True))
    monkeypatch.setattr(render, "WaitStatus", _Spy)
    return buf


def _turn(*, trace: bool, json_mode: bool = False, phrases: bool = True) -> bool:
    json_args = argparse.Namespace(json=True) if json_mode else None
    api: Any = _Api(_events(phrases=phrases))
    return cli._stream_one_turn(api, "hi", trace=trace, json_args=json_args)


def test_no_spinner_when_stderr_is_not_a_terminal(capsys, monkeypatch) -> None:
    monkeypatch.setattr(render, "WaitStatus", lambda: pytest.fail("spinner built"))
    assert _turn(trace=False) is True
    captured = capsys.readouterr()
    assert captured.out == "here you go\n"
    assert captured.err == ""


def test_no_spinner_with_json_even_on_a_terminal(tty, capsys) -> None:
    assert _turn(trace=False, json_mode=True) is True
    assert "here you go" in capsys.readouterr().out
    assert tty.getvalue() == ""


def test_no_spinner_in_trace_mode_even_on_a_terminal(tty, capsys) -> None:
    assert _turn(trace=True) is True
    assert "x" in tty.getvalue()  # the trace chatter, not a spinner
    assert "Thinking…" not in tty.getvalue()
    assert "Counting electrons" not in tty.getvalue()
    assert capsys.readouterr().out == "here you go\n"


def test_spinner_on_a_terminal_follows_the_turn_and_stops_before_the_reply(tty, capsys) -> None:
    assert _turn(trace=False) is True
    spy = _Spy.last
    assert spy.log[0] == "start"
    assert spy.log[1:4] == ["Counting electrons", "Calling x…", "x → ok"]
    assert spy.log[-1] == "stop"
    assert capsys.readouterr().out == "here you go\n"
    assert "Thinking…" in tty.getvalue()


def test_spinner_is_stopped_on_error_and_on_ctrl_c(tty) -> None:
    api: Any = _Api([KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        cli._stream_one_turn(api, "hi", trace=False)
    assert _Spy.last.log == ["start", "stop"]
    api = _Api([api_client.ApiError(500, "boom")])
    with pytest.raises(api_client.ApiError):
        cli._stream_one_turn(api, "hi", trace=False)
    assert _Spy.last.log[-1] == "stop"


def test_an_older_server_without_phrases_shows_thinking_then_the_tool_statuses(tty, capsys) -> None:
    assert _turn(trace=False, phrases=False) is True
    assert _Spy.last.log[:3] == ["start", "Calling x…", "x → ok"]
    assert "Thinking…" in tty.getvalue()
    assert capsys.readouterr().out == "here you go\n"


def test_status_phrase_frames_leave_non_terminal_output_unchanged(capsys) -> None:
    assert _turn(trace=False) is True
    plain = capsys.readouterr()
    assert _turn(trace=False, phrases=False) is True
    assert capsys.readouterr() == plain
    assert plain.out == "here you go\n"
    assert plain.err == ""


def test_the_phrase_is_a_safe_single_line(tty) -> None:
    status = render.WaitStatus()
    status.set("a\x1b[31m\nb")
    assert "\x1b" not in status._text
    assert "\n" not in status._text


def test_wait_status_is_disabled_off_a_terminal() -> None:
    assert render.wait_status_enabled() is False  # pytest's stderr is captured
