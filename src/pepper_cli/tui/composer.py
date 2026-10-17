"""The composer: a multi-line input whose Enter sends (``planning/terminal-ui.md`` §5, §9).

Keys: Enter sends, Alt+Enter or Ctrl+J inserts a newline, Esc is handed to the app (cancel
the quote, then leave the composer). Typing ``@`` after a space opens a menu of the room's
roster; ↑/↓ move in it, Tab or Enter picks, Esc closes it.

The keys are taken in ``_on_key`` rather than as bindings: ``TextArea`` inserts Enter as a
newline in its own key handler, which runs before any non-priority binding would.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.message import Message
from textual.widgets import OptionList, TextArea
from textual.widgets.option_list import Option

from pepper_cli.terminal_text import sanitize_line
from pepper_cli.tui import state

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual import events

    from pepper_cli.api_client import GroupRosterMember

PLACEHOLDER = "Message  (Enter sends · Alt+Enter new line · Esc leaves)"


class MentionMenu(OptionList):
    """The ``@`` completion list. Never takes focus: the composer drives it."""

    can_focus = False

    def __init__(self) -> None:
        super().__init__(id="mentions")
        self.members: list[GroupRosterMember] = []
        self.display = False

    def offer(self, members: list[GroupRosterMember]) -> None:
        self.members = members
        self.clear_options()
        self.add_options(
            Option(Text(sanitize_line(m.display_name) or "?"), id=str(i))
            for i, m in enumerate(members)
        )
        self.display = bool(members)
        if members:
            self.highlighted = 0

    def close(self) -> None:
        self.members = []
        self.clear_options()
        self.display = False

    @property
    def choice(self) -> GroupRosterMember | None:
        index = self.highlighted
        if index is None or not 0 <= index < len(self.members):
            return None
        return self.members[index]


class Composer(TextArea):
    """The message input for whichever conversation is open."""

    class Submitted(Message):
        """Enter was pressed; ``text`` is the composer's content (not yet cleared)."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class Escaped(Message):
        """Esc with no menu open."""

    def __init__(
        self, menu: MentionMenu, *, key_hook: Callable[[events.Key], bool] | None = None
    ) -> None:
        super().__init__(id="composer", soft_wrap=True, show_line_numbers=False)
        self.placeholder = PLACEHOLDER
        self.menu = menu
        #: Room members ``@`` offers; empty in the Pepper DM.
        self.roster: list[GroupRosterMember] = []
        #: Runs first for every key; True means the key was consumed (the app's fatal state).
        self._key_hook = key_hook

    async def _on_key(self, event: events.Key) -> None:
        if self._key_hook is not None and self._key_hook(event):
            event.stop()
            event.prevent_default()
            return
        if self.menu.display and self._menu_key(event.key):
            event.stop()
            event.prevent_default()
            return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self.text))
            return
        if event.key in ("alt+enter", "ctrl+j"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.post_message(self.Escaped())
            return
        await super()._on_key(event)

    def _menu_key(self, key: str) -> bool:
        if key == "up":
            self.menu.action_cursor_up()
        elif key == "down":
            self.menu.action_cursor_down()
        elif key in ("tab", "enter"):
            self.pick()
        elif key == "escape":
            self.menu.close()
        else:
            return False
        return True

    def _before_cursor(self) -> str:
        row, col = self.cursor_location
        return self.get_text_range((row, 0), (row, col))

    def refresh_menu(self) -> None:
        """Offer the roster members matching the ``@`` word at the cursor, if any."""
        query = state.mention_query(self._before_cursor()) if self.roster else None
        if query is None:
            self.menu.close()
            return
        self.menu.offer(state.roster_matches(query, self.roster))

    def pick(self) -> None:
        """Replace the ``@partial`` at the cursor with the highlighted ``@Display Name ``."""
        member = self.menu.choice
        query = state.mention_query(self._before_cursor())
        self.menu.close()
        if member is None or query is None:
            return
        row, col = self.cursor_location
        self.replace(f"@{member.display_name} ", (row, col - len(query) - 1), (row, col))

    def on_text_area_changed(self, _event: TextArea.Changed) -> None:
        self.refresh_menu()

    def on_text_area_selection_changed(self, _event: TextArea.SelectionChanged) -> None:
        self.refresh_menu()
