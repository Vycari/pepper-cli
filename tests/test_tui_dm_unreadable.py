"""The TUI says, once per change, that today's Pepper thread had rows or drafts it left out."""

from __future__ import annotations

from typing import Any

from pepper_cli.api_client import ChatHistory, ChatHistoryEntry
from pepper_cli.tui.app import PepperApp


class _Stub:
    def __init__(self) -> None:
        self._dm_unreadable = 0
        self.notices: list[str] = []

    def notify(self, message: str, **_: Any) -> None:
        self.notices.append(message)


def _history(skipped: int, skipped_drafts: int = 0) -> ChatHistory:
    history = ChatHistory(
        [ChatHistoryEntry("a1", "user", "", "", "z", skipped_drafts=skipped_drafts)]
    )
    history.skipped = skipped
    return history


def test_unreadable_rows_and_drafts_are_noted_once_and_again_only_when_more_appear() -> None:
    app: Any = _Stub()
    PepperApp._note_dm_unreadable(app, _history(0))
    assert app.notices == []
    PepperApp._note_dm_unreadable(app, _history(1, skipped_drafts=1))
    assert app.notices == [
        "2 item(s) in today's Pepper thread could not be read and are not shown."
    ]
    PepperApp._note_dm_unreadable(app, _history(1, skipped_drafts=1))
    assert len(app.notices) == 1, "the same count on the next poll is not said again"
    PepperApp._note_dm_unreadable(app, _history(2, skipped_drafts=1))
    assert len(app.notices) == 2


def test_a_plain_list_from_an_older_caller_counts_no_skipped_rows() -> None:
    app: Any = _Stub()
    PepperApp._note_dm_unreadable(app, [ChatHistoryEntry("a1", "user", "", "", "z")])
    assert app.notices == []
