"""The editor and conflict helpers shared by ``pepper notes`` and ``pepper tui``."""

from __future__ import annotations

import stat
import sys

import pytest

from pepper_cli import note_edit

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the fake editor is a shell script")


def _editor(tmp_path, script: str) -> str:
    path = tmp_path / "ed.sh"
    path.write_text(f"#!/bin/sh\n{script}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@posix_only
def test_open_in_editor_returns_the_edited_text_and_names_the_temp_file_by_prefix(
    monkeypatch, tmp_path
) -> None:
    log = tmp_path / "log"
    monkeypatch.setenv(
        "VISUAL", _editor(tmp_path, f'basename "$1" > {log}; printf "edited" >> "$1"')
    )
    text, error = note_edit.open_in_editor("start-", prefix="pepper-note-")
    assert (text, error) == ("start-edited", None)
    name = log.read_text().strip()
    assert name.startswith("pepper-note-")
    assert name.endswith(".md")


@posix_only
def test_open_in_editor_reports_a_nonzero_exit_as_an_error_string(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("VISUAL", _editor(tmp_path, "exit 3"))
    assert note_edit.open_in_editor("x") == (None, "editor exited with status 3")


def test_open_in_editor_reports_an_editor_that_cannot_be_launched(monkeypatch) -> None:
    monkeypatch.setenv("VISUAL", "/no/such/editor-binary")
    text, error = note_edit.open_in_editor("x")
    assert text is None
    assert error is not None
    assert error.startswith("couldn't launch editor '/no/such/editor-binary'")


def test_open_in_editor_reports_an_unparseable_or_empty_editor(monkeypatch) -> None:
    monkeypatch.setenv("VISUAL", "vi 'unterminated")
    text, error = note_edit.open_in_editor("x")
    assert text is None
    assert error is not None
    assert error.startswith("couldn't parse $VISUAL/$EDITOR")
    monkeypatch.setenv("VISUAL", "  ")
    assert note_edit.open_in_editor("x") == (
        None,
        "$VISUAL/$EDITOR is empty — set one to edit skills",
    )


def test_texts_match_tolerates_one_trailing_newline_only() -> None:
    assert note_edit.texts_match("a", "a")
    assert note_edit.texts_match("a", "a\n")
    assert not note_edit.texts_match("a", "a\n\n")
    assert not note_edit.texts_match("a\n", "a")


def test_note_diff_is_from_the_servers_text_to_yours_with_controls_spelled_out() -> None:
    diff = note_edit.note_diff("hello\nworld\n", "hello\x1b[2J\nworld\r\n")
    assert diff.startswith("--- server (current)\n+++ yours\n")
    assert "\x1b" not in diff
    assert "\\x1b" in diff
    assert note_edit.note_diff("same\n", "same\n") == ""
