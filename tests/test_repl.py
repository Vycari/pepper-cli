"""Tests for the interactive ``pepper chat`` REPL.

We mock ``prompt_toolkit.PromptSession.prompt`` to return scripted
lines (and then ``EOFError`` to exit). The API client is replaced
with a fake that records calls to ``chat_send_stream`` so the test
can assert what dispatch fired per line.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Self

import httpx
import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr


@pytest.fixture
def logged_in(tmp_home):
    """Persist a credentials file so the REPL can load them."""
    credentials.save(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
    )


class _FakeApi:
    """Captures REPL→API dispatch.

    ``chat_send_stream`` yields whatever the test pre-programmed via
    ``stream_responses``. Each call records the message text so tests
    can assert dispatch ordering."""

    instances: list[_FakeApi] = []

    def __init__(self, *, server_url: str, token: SecretStr) -> None:
        self.server_url = server_url
        self.token = token
        self.messages: list[str] = []
        # What ``--root`` declared on each turn, if anything.
        self.client_tools: list[list[dict] | None] = []
        self.stream_responses: list[Any] = []
        # ``list_skills`` returns whatever was pre-programmed; default
        # to an empty list so existing tests don't have to know about
        # the call. Tests that exercise skill completion override
        # this attribute before invoking the REPL.
        self.skills_response: list[api_client.SkillSummary] | Exception = []
        _FakeApi.instances.append(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def list_skills(self) -> list[api_client.SkillSummary]:
        if isinstance(self.skills_response, Exception):
            raise self.skills_response
        return self.skills_response

    def chat_send_stream(
        self,
        message: str,
        *,
        client_tools: list[dict] | None = None,
        **_: object,
    ) -> Iterator[api_client.StreamEvent]:
        self.messages.append(message)
        self.client_tools.append(client_tools)
        if not self.stream_responses:
            return iter([])
        nxt = self.stream_responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return iter(nxt)


@pytest.fixture
def patch_api_factory(monkeypatch):
    """Install a factory that produces ``_FakeApi`` instances when
    ``cli`` constructs an ``ApiClient``. Returns a setter for the
    pre-programmed stream sequence."""
    _FakeApi.instances.clear()

    def factory(**kwargs: Any) -> _FakeApi:
        return _FakeApi(**kwargs)

    monkeypatch.setattr(api_client, "ApiClient", factory)
    return factory


def _patch_prompt(monkeypatch, lines: list[str], eof_after: bool = True) -> list[str]:
    """Make ``PromptSession.prompt`` return ``lines`` in order, then
    raise ``EOFError`` (clean Ctrl-D exit). Returns the list of
    prompts the REPL actually wrote, in order — useful for assertions
    about prompt count."""
    from prompt_toolkit import PromptSession

    prompts_seen: list[str] = []
    queue = list(lines)

    def fake_prompt(self, message: str = "", **_: Any) -> str:
        prompts_seen.append(message)
        if queue:
            return queue.pop(0)
        if eof_after:
            raise EOFError
        raise KeyboardInterrupt

    monkeypatch.setattr(PromptSession, "prompt", fake_prompt)
    return prompts_seen


# ---------------------------------------------------------------------------


def test_repl_exits_immediately_on_eof(logged_in, patch_api_factory, monkeypatch) -> None:
    _patch_prompt(monkeypatch, lines=[])  # no input → EOF on first prompt
    rc = cli.main(["chat"])
    assert rc == 0
    # No turns dispatched.
    assert _FakeApi.instances[0].messages == []


def test_repl_dispatches_each_nonblank_line(logged_in, patch_api_factory, monkeypatch) -> None:
    _patch_prompt(monkeypatch, lines=["hello", "and another"])
    rc = cli.main(["chat"])
    assert rc == 0
    assert _FakeApi.instances[0].messages == ["hello", "and another"]


def test_repl_ignores_blank_lines(logged_in, patch_api_factory, monkeypatch) -> None:
    _patch_prompt(monkeypatch, lines=["", "   ", "real message"])
    rc = cli.main(["chat"])
    assert rc == 0
    assert _FakeApi.instances[0].messages == ["real message"]


def test_repl_quit_command_exits(logged_in, patch_api_factory, monkeypatch) -> None:
    # ``/quit`` should terminate before the EOF fallback; lines after
    # it must never be prompted for.
    _patch_prompt(monkeypatch, lines=["one", "/quit", "should not be sent"])
    rc = cli.main(["chat"])
    assert rc == 0
    assert _FakeApi.instances[0].messages == ["one"]


def test_repl_exit_alias_quits(logged_in, patch_api_factory, monkeypatch) -> None:
    _patch_prompt(monkeypatch, lines=["/exit"])
    rc = cli.main(["chat"])
    assert rc == 0
    assert _FakeApi.instances[0].messages == []


def test_repl_unknown_slash_command_warns_but_continues(
    logged_in, patch_api_factory, monkeypatch, capsys
) -> None:
    _patch_prompt(monkeypatch, lines=["/whatever", "real message"])
    rc = cli.main(["chat"])
    assert rc == 0
    err = " ".join(capsys.readouterr().err.split())
    assert "Unknown command" in err
    # The unknown command must NOT be sent to the server.
    assert _FakeApi.instances[0].messages == ["real message"]


def test_repl_help_command_prints_help(logged_in, patch_api_factory, monkeypatch, capsys) -> None:
    _patch_prompt(monkeypatch, lines=["/help"])
    rc = cli.main(["chat"])
    assert rc == 0
    err = " ".join(capsys.readouterr().err.split())
    assert "/quit" in err
    assert "/clear" in err


def test_stream_one_turn_dispatches_kinds(
    logged_in, patch_api_factory, monkeypatch, capsys
) -> None:
    """Exercise ``_stream_one_turn`` directly with a fake api whose
    ``chat_send_stream`` yields one event per supported kind. Asserts
    that each kind routes to a different render helper (by checking
    stderr / stdout for the expected fragment)."""
    fake = _FakeApi(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    fake.stream_responses = [
        [
            api_client.StreamEvent(kind="model_thought", payload={"text": "thinking now"}),
            api_client.StreamEvent(
                kind="tool_call", payload={"name": "list_events", "args": {"when": "today"}}
            ),
            api_client.StreamEvent(
                kind="tool_result",
                payload={"name": "list_events", "status": "ok", "text": "3 events"},
            ),
            api_client.StreamEvent(kind="reply", payload={"reply": "here you go"}),
        ]
    ]
    cli._stream_one_turn(fake, "hi")  # type: ignore[arg-type]

    out_streams = capsys.readouterr()
    # The reply lands on stdout (pipeable).
    assert "here you go" in out_streams.out
    # Thoughts / tool calls / tool results land on stderr.
    err = " ".join(out_streams.err.split())
    assert "thinking now" in err
    assert "list_events" in err
    # ``3 events`` is the tool_result text; should appear once for the
    # result, not for the call (call has only args).
    assert "3 events" in err


def test_stream_one_turn_renders_a_thought_as_one_line(
    logged_in, patch_api_factory, monkeypatch, capsys
) -> None:
    """A thought arrives as fragments plus a terminal block; it must render as
    one line, not one line per fragment.

    ``model_thought`` frames are whatever the backend chunks at — a paragraph
    on Gemini, a single word on the OpenAI and Anthropic backends — so a line
    per frame turned a sentence of planning into a column of words. The
    fragments stream onto the same line and ``model_thought_block`` ends it.
    """
    fake = _FakeApi(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    fake.stream_responses = [
        [
            api_client.StreamEvent(kind="model_thought", payload={"text": "Check "}),
            api_client.StreamEvent(kind="model_thought", payload={"text": "the calendar "}),
            api_client.StreamEvent(kind="model_thought", payload={"text": "first."}),
            api_client.StreamEvent(
                kind="model_thought_block", payload={"text": "Check the calendar first."}
            ),
            api_client.StreamEvent(kind="reply", payload={"reply": "here you go"}),
        ]
    ]
    cli._stream_one_turn(fake, "hi")  # type: ignore[arg-type]

    err = capsys.readouterr().err
    thought_lines = [line for line in err.splitlines() if line.strip()]
    assert thought_lines == ["· Check the calendar first."], err


def test_stream_one_turn_renders_a_block_with_no_fragments(
    logged_in, patch_api_factory, monkeypatch, capsys
) -> None:
    """A block that arrives with nothing streamed before it still prints the
    thought — the CLI must not depend on having seen the fragments."""
    fake = _FakeApi(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    fake.stream_responses = [
        [
            api_client.StreamEvent(kind="model_thought_block", payload={"text": "One whole one."}),
            api_client.StreamEvent(kind="reply", payload={"reply": "ok"}),
        ]
    ]
    cli._stream_one_turn(fake, "hi")  # type: ignore[arg-type]

    err = capsys.readouterr().err
    assert [line for line in err.splitlines() if line.strip()] == ["· One whole one."], err


def test_stream_one_turn_reprints_a_thought_a_retry_replaced(
    logged_in, patch_api_factory, monkeypatch, capsys
) -> None:
    """A transient failure mid-thought leaves fused text on screen; the block
    is authoritative and gets the last word.

    The abandoned attempt gets no terminating block by design (see
    the server's generation loop), so its fragments and the
    retry's arrive as one unbroken run and the line reads as a partial thought
    welded to the real one. A terminal cannot un-write that, so the correct
    paragraph is printed after it rather than left implied.
    """
    fake = _FakeApi(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    fake.stream_responses = [
        [
            # The attempt that died mid-stream.
            api_client.StreamEvent(kind="model_thought", payload={"text": "Check the cal"}),
            # The retry, streaming onto the same open line.
            api_client.StreamEvent(kind="model_thought", payload={"text": "Check the calendar "}),
            api_client.StreamEvent(kind="model_thought", payload={"text": "first."}),
            api_client.StreamEvent(
                kind="model_thought_block", payload={"text": "Check the calendar first."}
            ),
            api_client.StreamEvent(kind="reply", payload={"reply": "ok"}),
        ]
    ]
    cli._stream_one_turn(fake, "hi")  # type: ignore[arg-type]

    lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    # The fused line is what the terminal already showed; the thought the model
    # actually had is the line the reader is left with.
    assert lines[-1] == "· Check the calendar first."
    assert len(lines) == 2, lines


def test_repl_continues_after_non_401_api_error(
    logged_in, patch_api_factory, monkeypatch, capsys
) -> None:
    """A 502 (server hiccup) on one turn must not kill the REPL —
    the user can retry. Uses the same factory-override pattern as
    the 401 / transport-error tests since pre-programming the fake
    must happen before ``cli.main`` constructs the ApiClient."""

    def factory(**kwargs: Any) -> _FakeApi:
        inst = _FakeApi(**kwargs)
        inst.stream_responses = [
            api_client.ApiError(status=502, body="bad gateway", detail=None),
        ]
        return inst

    monkeypatch.setattr(api_client, "ApiClient", factory)

    _patch_prompt(monkeypatch, lines=["first", "second"])
    rc = cli.main(["chat"])
    # Clean Ctrl-D exit after the two prompts — the 502 didn't kill
    # the REPL.
    assert rc == 0
    err = " ".join(capsys.readouterr().err.split())
    assert "502" in err


def test_repl_bails_out_on_401(logged_in, patch_api_factory, monkeypatch, capsys) -> None:
    """401 mid-REPL is special-cased: the token is bad, every
    subsequent turn would also 401, so the REPL exits with a hint."""
    # Replace the factory with one that ships a 401 on the first
    # chat_send_stream call. ``_FakeApi`` already supports pre-queued
    # exceptions via ``stream_responses``.
    raised = api_client.ApiError(status=401, body="", detail="invalid bearer token")

    def factory(**kwargs: Any) -> _FakeApi:
        inst = _FakeApi(**kwargs)
        inst.stream_responses = [raised]
        return inst

    monkeypatch.setattr(api_client, "ApiClient", factory)

    _patch_prompt(monkeypatch, lines=["something"], eof_after=False)
    rc = cli.main(["chat"])
    assert rc == 1
    err = " ".join(capsys.readouterr().err.split()).lower()
    assert "pepper login" in err


def test_repl_continues_after_transport_error(
    logged_in, patch_api_factory, monkeypatch, capsys
) -> None:
    """Transient network failure (DNS, connection refused) on one
    turn should leave the REPL running — the user retries."""

    def factory(**kwargs: Any) -> _FakeApi:
        inst = _FakeApi(**kwargs)
        inst.stream_responses = [httpx.ConnectError("nope")]
        return inst

    monkeypatch.setattr(api_client, "ApiClient", factory)

    _patch_prompt(monkeypatch, lines=["one", "two"])
    rc = cli.main(["chat"])
    # Clean exit (Ctrl-D after 'two'); the transport error didn't
    # propagate out of the REPL.
    assert rc == 0
    err = " ".join(capsys.readouterr().err.split())
    assert "Couldn't reach" in err


def test_chat_one_shot_path_streams_final_reply_only(
    logged_in, patch_api_factory, monkeypatch, capsys
) -> None:
    """One-shot chat uses SSE while keeping the final-only output contract."""

    def factory(**kwargs: Any) -> _FakeApi:
        inst = _FakeApi(**kwargs)
        inst.stream_responses = [
            [
                api_client.StreamEvent(kind="model_thought", payload={"text": "private"}),
                api_client.StreamEvent(kind="reply", payload={"reply": "from one-shot"}),
            ]
        ]
        return inst

    monkeypatch.setattr(api_client, "ApiClient", factory)
    rc = cli.main(["chat", "hello"])
    assert rc == 0
    assert _FakeApi.instances[-1].messages == ["hello"]
    assert _FakeApi.instances[-1].client_tools == [None]
    assert capsys.readouterr().out.strip() == "from one-shot"


def test_repl_tty_sets_and_restores_window_title(
    logged_in, patch_api_factory, monkeypatch, capsys
) -> None:
    """A TTY REPL run pushes the title stack, sets ``Pepper (host)`` (the
    ``logged_in`` fixture's server isn't the default), then pops it back
    on exit — in that order."""
    monkeypatch.setattr(cli.window_title, "_stdout_is_tty", lambda: True)
    _patch_prompt(monkeypatch, lines=[])  # immediate EOF

    rc = cli.main(["chat"])
    assert rc == 0

    out = capsys.readouterr().out
    push_at = out.index("\x1b[22;0t")
    set_at = out.index("\x1b]0;Pepper (example.com)\x07")
    pop_at = out.index("\x1b[23;0t")
    assert push_at < set_at < pop_at


def test_repl_default_server_title_has_no_host_suffix(
    tmp_home: Path, patch_api_factory, monkeypatch, capsys
) -> None:
    credentials.save(server_url=cli._DEFAULT_SERVER, token=SecretStr("pep_pat_abc"))
    monkeypatch.setattr(cli.window_title, "_stdout_is_tty", lambda: True)
    _patch_prompt(monkeypatch, lines=[])

    rc = cli.main(["chat"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "\x1b]0;Pepper\x07" in out
    assert "Pepper (" not in out


@pytest.mark.parametrize(
    ("server_url", "expected"),
    [
        ("http://localhost:8000", "Pepper (localhost:8000)"),
        ("https://pepper.example.com", "Pepper (pepper.example.com)"),
        ("https://alice:hunter2@pepper.example.com:8443/x", "Pepper (pepper.example.com:8443)"),
        ("https://alice@pepper.example.com", "Pepper (pepper.example.com)"),
        ("http://[::1]:8000", "Pepper ([::1]:8000)"),
        ("not a url", "Pepper"),
    ],
)
def test_repl_title_uses_host_and_port_only(server_url: str, expected: str) -> None:
    """Only hostname (+ port) reaches the title: userinfo in a ``--server``
    URL must never be written to the window title."""
    title = cli._repl_title(server_url)
    assert title == expected
    assert "hunter2" not in title
    assert "alice" not in title


def test_repl_title_does_not_leak_credentials_end_to_end(
    tmp_home: Path, patch_api_factory, monkeypatch, capsys
) -> None:
    credentials.save(
        server_url="https://alice:hunter2@pepper.example.com", token=SecretStr("pep_pat_abc")
    )
    monkeypatch.setattr(cli.window_title, "_stdout_is_tty", lambda: True)
    _patch_prompt(monkeypatch, lines=[])

    rc = cli.main(["chat"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "\x1b]0;Pepper (pepper.example.com)\x07" in out
    assert "hunter2" not in out


def test_repl_non_tty_emits_no_title_sequences(
    logged_in, patch_api_factory, monkeypatch, capsys
) -> None:
    """Stdout isn't a TTY under pytest's own capture by default — no
    escape sequences should show up even though the REPL opened."""
    _patch_prompt(monkeypatch, lines=[])

    rc = cli.main(["chat"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "\x1b[22;0t" not in out
    assert "\x1b]0;" not in out


def test_chat_one_shot_emits_no_title_sequences(
    logged_in, patch_api_factory, monkeypatch, capsys
) -> None:
    """One-shot ``pepper chat MESSAGE`` never touches the window title,
    even when stdout claims to be a TTY."""
    monkeypatch.setattr(cli.window_title, "_stdout_is_tty", lambda: True)

    def factory(**kwargs: Any) -> _FakeApi:
        inst = _FakeApi(**kwargs)
        inst.stream_responses = [[api_client.StreamEvent(kind="reply", payload={"reply": "hi"})]]
        return inst

    monkeypatch.setattr(api_client, "ApiClient", factory)

    rc = cli.main(["chat", "hello"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "\x1b[22;0t" not in out
    assert "\x1b]0;" not in out


def test_repl_pops_title_on_api_client_construction_error(
    logged_in, patch_api_factory, monkeypatch, capsys
) -> None:
    """An exception raised while the REPL is open still pops the title —
    the context manager's ``finally`` runs even when something above it
    (here, the skills fetch) blows up unexpectedly."""
    monkeypatch.setattr(cli.window_title, "_stdout_is_tty", lambda: True)

    def factory(**kwargs: Any) -> _FakeApi:
        inst = _FakeApi(**kwargs)
        inst.skills_response = RuntimeError("boom")
        return inst

    monkeypatch.setattr(api_client, "ApiClient", factory)
    _patch_prompt(monkeypatch, lines=[], eof_after=False)

    with pytest.raises(RuntimeError):
        cli.main(["chat"])

    out = capsys.readouterr().out
    assert "\x1b[23;0t" in out


def test_chat_repl_requires_credentials(tmp_home: Path, capsys, monkeypatch) -> None:
    """No credentials → no REPL. Same error path as the one-shot
    ``chat MESSAGE`` flow."""
    # No login.
    rc = cli.main(["chat"])
    assert rc == 1
    err = " ".join(capsys.readouterr().err.split())
    assert "pepper login" in err.lower()


# ---------------------------------------------------------------------------
# Skill autocomplete + dispatch


def test_repl_skill_command_sends_run_skill_message(
    logged_in, patch_api_factory, monkeypatch
) -> None:
    """Typing ``/morning-brief<Enter>`` must dispatch the templated
    "Run the morning-brief skill." message, not the raw ``/morning-brief``
    slash command (which would otherwise hit the "Unknown command" path)."""

    def factory(**kwargs: Any) -> _FakeApi:
        inst = _FakeApi(**kwargs)
        inst.skills_response = [
            api_client.SkillSummary(name="morning-brief", description="Daily summary"),
        ]
        return inst

    monkeypatch.setattr(api_client, "ApiClient", factory)
    _patch_prompt(monkeypatch, lines=["/morning-brief"])

    rc = cli.main(["chat"])
    assert rc == 0
    assert _FakeApi.instances[0].messages == ["Run the morning-brief skill."]


def test_repl_unknown_slash_command_does_not_match_skill(
    logged_in, patch_api_factory, monkeypatch, capsys
) -> None:
    """Skill dispatch must only fire for *exact* skill-name matches —
    a typo (``/morning-brieff``) should hit the existing unknown-
    command warning path, not silently send a "Run the morning-brieff
    skill." message that the model can't satisfy."""

    def factory(**kwargs: Any) -> _FakeApi:
        inst = _FakeApi(**kwargs)
        inst.skills_response = [
            api_client.SkillSummary(name="morning-brief", description="Daily summary"),
        ]
        return inst

    monkeypatch.setattr(api_client, "ApiClient", factory)
    _patch_prompt(monkeypatch, lines=["/morning-brieff"])

    rc = cli.main(["chat"])
    assert rc == 0
    err = " ".join(capsys.readouterr().err.split())
    assert "Unknown command" in err
    # The bad command must NOT be sent to the model.
    assert _FakeApi.instances[0].messages == []


def test_repl_skill_list_401_at_startup_bails(
    logged_in, patch_api_factory, monkeypatch, capsys
) -> None:
    """A 401 from the skills endpoint at REPL startup means the token
    is bad — every subsequent turn would also 401. Bail loudly,
    don't open the REPL with broken state."""

    def factory(**kwargs: Any) -> _FakeApi:
        inst = _FakeApi(**kwargs)
        inst.skills_response = api_client.ApiError(
            status=401, body="", detail="invalid bearer token"
        )
        return inst

    monkeypatch.setattr(api_client, "ApiClient", factory)
    # Prompt is never called — but patch it so the test fails loudly
    # if the bail-out logic regresses (otherwise prompt_toolkit would
    # try to read from stdin).
    _patch_prompt(monkeypatch, lines=[], eof_after=False)

    rc = cli.main(["chat"])
    assert rc == 1
    err = " ".join(capsys.readouterr().err.split()).lower()
    assert "pepper login" in err


def test_repl_skill_list_5xx_at_startup_degrades(
    logged_in, patch_api_factory, monkeypatch, capsys
) -> None:
    """Non-401 errors from the skills endpoint must NOT kill the REPL.
    A REPL with static-only completion is more useful than no REPL
    at all."""

    def factory(**kwargs: Any) -> _FakeApi:
        inst = _FakeApi(**kwargs)
        inst.skills_response = api_client.ApiError(status=502, body="bad gateway", detail=None)
        return inst

    monkeypatch.setattr(api_client, "ApiClient", factory)
    _patch_prompt(monkeypatch, lines=["hello"])

    rc = cli.main(["chat"])
    assert rc == 0
    err = " ".join(capsys.readouterr().err.split()).lower()
    assert "couldn't load skills" in err
    # The REPL ran and handled the chat line normally.
    assert _FakeApi.instances[0].messages == ["hello"]


def test_repl_skill_list_transport_error_degrades(
    logged_in, patch_api_factory, monkeypatch, capsys
) -> None:
    """Same as the 5xx case but for transport errors — the REPL still
    opens, with static-only completion."""

    def factory(**kwargs: Any) -> _FakeApi:
        inst = _FakeApi(**kwargs)
        inst.skills_response = httpx.ConnectError("nope")
        return inst

    monkeypatch.setattr(api_client, "ApiClient", factory)
    _patch_prompt(monkeypatch, lines=["hi"])

    rc = cli.main(["chat"])
    assert rc == 0
    err = " ".join(capsys.readouterr().err.split()).lower()
    assert "couldn't load skills" in err


# ---------------------------------------------------------------------------
# _SlashCompleter unit tests


def test_completer_emits_static_commands_on_bare_slash() -> None:
    """Typing just ``/`` should propose every built-in command."""
    from pepper_cli.cli import _SlashCompleter

    completer = _SlashCompleter([])
    completions = list(_completions_for(completer, "/"))
    words = {c.text for c in completions}
    assert {"/quit", "/exit", "/clear", "/help"} <= words


def test_completer_emits_skills_alongside_static_commands() -> None:
    from pepper_cli.cli import _SlashCompleter

    completer = _SlashCompleter(
        [
            api_client.SkillSummary(name="morning-brief", description="Daily summary"),
            api_client.SkillSummary(name="inbox-triage", description="Sort the inbox"),
        ]
    )
    completions = list(_completions_for(completer, "/"))
    words = {c.text for c in completions}
    assert "/morning-brief" in words
    assert "/inbox-triage" in words


def test_completer_filters_by_prefix() -> None:
    """Typing ``/m`` should only match commands / skills starting
    with ``/m``, not return the whole set."""
    from pepper_cli.cli import _SlashCompleter

    completer = _SlashCompleter(
        [
            api_client.SkillSummary(name="morning-brief", description="x"),
            api_client.SkillSummary(name="inbox-triage", description="y"),
        ]
    )
    completions = list(_completions_for(completer, "/m"))
    words = {c.text for c in completions}
    assert "/morning-brief" in words
    assert "/inbox-triage" not in words
    # No static command starts with /m either.
    assert all(w.startswith("/m") for w in words)


def test_completer_does_not_fire_on_free_text() -> None:
    """If the buffer doesn't start with ``/``, the completer must
    stay silent — completion popping up while the user types
    free-form chat would fight their typing speed."""
    from pepper_cli.cli import _SlashCompleter

    completer = _SlashCompleter([api_client.SkillSummary(name="morning-brief", description="x")])
    completions = list(_completions_for(completer, "hello"))
    assert completions == []


def test_completer_replaces_whole_slash_prefix_on_commit() -> None:
    """The ``Completion.start_position`` must be ``-len(text)`` so
    selecting a completion replaces the typed prefix, not appends
    to it (which would produce ``/m/morning-brief``)."""
    from pepper_cli.cli import _SlashCompleter

    completer = _SlashCompleter([api_client.SkillSummary(name="morning-brief", description="x")])
    completions = list(_completions_for(completer, "/m"))
    assert all(c.start_position == -2 for c in completions)


def _completions_for(completer, text: str):
    """Build a prompt_toolkit ``Document`` for ``text`` and yield the
    completer's results — used by the completer unit tests."""
    from prompt_toolkit.document import Document

    return completer.get_completions(Document(text=text, cursor_position=len(text)), None)


async def test_completer_async_surface_matches_sync():
    """Regression for the AttributeError reported when pressing Tab in
    the REPL: prompt_toolkit's buffer calls ``get_completions_async``
    directly via ``async for`` and does NOT fall back to the sync
    method on duck-typed completers. The async surface must exist and
    must yield the same set the sync surface would. Loss of this
    method shipped once as a runtime crash on first Tab press; the
    fix is cheap, the test pins it."""
    from prompt_toolkit.document import Document

    from pepper_cli.cli import _SlashCompleter

    completer = _SlashCompleter(
        [api_client.SkillSummary(name="morning-brief", description="Daily summary")]
    )
    document = Document(text="/", cursor_position=1)

    sync_words = {c.text for c in completer.get_completions(document, None)}
    async_words: set[str] = set()
    async for completion in completer.get_completions_async(document, None):
        async_words.add(completion.text)

    assert async_words == sync_words
    assert "/morning-brief" in async_words
    assert {"/quit", "/exit", "/clear", "/help"} <= async_words
