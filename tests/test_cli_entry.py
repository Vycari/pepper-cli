"""The entry-point matrix of planning/terminal-ui.md §10: which invocation opens what."""

from __future__ import annotations

import pytest

from pepper_cli import cli


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the handlers that would open something; record which one ran."""
    ran: list[str] = []
    monkeypatch.setattr(cli, "cmd_tui", lambda _a: ran.append("tui") or 0)
    monkeypatch.setattr(cli, "cmd_chat", lambda a: ran.append(f"chat:{' '.join(a.message)}") or 0)
    monkeypatch.setattr(cli, "cmd_whoami", lambda _a: ran.append("whoami") or 0)
    monkeypatch.delenv("PEPPER_NO_TUI", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    return ran


def _tty(monkeypatch: pytest.MonkeyPatch, *, stdin: bool = True, stdout: bool = True) -> None:
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: stdin)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: stdout, raising=False)


def test_bare_pepper_on_a_terminal_opens_the_tui(monkeypatch, seen) -> None:
    _tty(monkeypatch)
    assert cli.main([]) == 0
    assert seen == ["tui"]


def test_bare_pepper_with_only_a_profile_opens_the_tui(monkeypatch, seen) -> None:
    _tty(monkeypatch)
    assert cli.main(["--profile", "work"]) == 0
    assert seen == ["tui"]


@pytest.mark.parametrize(("stdin", "stdout"), [(False, True), (True, False), (False, False)])
def test_bare_pepper_without_a_terminal_on_both_ends_stays_the_repl(
    monkeypatch, seen, stdin, stdout
) -> None:
    _tty(monkeypatch, stdin=stdin, stdout=stdout)
    assert cli.main([]) == 0
    assert seen == ["chat:"]


def test_term_dumb_stays_the_repl(monkeypatch, seen) -> None:
    _tty(monkeypatch)
    monkeypatch.setenv("TERM", "dumb")
    assert cli.main([]) == 0
    assert seen == ["chat:"]


@pytest.mark.parametrize("value", ["1", "true", "yes", "anything"])
def test_pepper_no_tui_stays_the_repl(monkeypatch, seen, value) -> None:
    _tty(monkeypatch)
    monkeypatch.setenv("PEPPER_NO_TUI", value)
    assert cli.main([]) == 0
    assert seen == ["chat:"]


@pytest.mark.parametrize("value", ["", "0", "false", "no"])
def test_pepper_no_tui_falsy_spellings_leave_the_tui_on(monkeypatch, seen, value) -> None:
    _tty(monkeypatch)
    monkeypatch.setenv("PEPPER_NO_TUI", value)
    assert cli.main([]) == 0
    assert seen == ["tui"]


def test_term_unset_is_not_dumb(monkeypatch, seen) -> None:
    """Windows consoles carry no TERM; that must not turn the TUI off."""
    _tty(monkeypatch)
    monkeypatch.delenv("TERM", raising=False)
    assert cli.main([]) == 0
    assert seen == ["tui"]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["chat"], "chat:"),
        (["chat", "hi", "there"], "chat:hi there"),
        (["hello", "pepper"], "chat:hello pepper"),
        (["whoami"], "whoami"),
        (["tui"], "tui"),
    ],
)
def test_every_other_invocation_is_unchanged_on_a_terminal(
    monkeypatch, seen, argv, expected
) -> None:
    _tty(monkeypatch)
    assert cli.main(argv) == 0
    assert seen == [expected]


def test_the_terminal_never_changes_what_a_non_bare_command_does(monkeypatch, seen) -> None:
    for stdin, stdout in [(True, True), (False, False)]:
        _tty(monkeypatch, stdin=stdin, stdout=stdout)
        seen.clear()
        assert cli.main(["chat"]) == 0
        assert cli.main(["whoami"]) == 0
        assert seen == ["chat:", "whoami"]
