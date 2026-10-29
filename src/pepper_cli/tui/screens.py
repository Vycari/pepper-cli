"""Modal screens: the emoji picker, yes/no confirmation, a one-line prompt, the Ctrl+K jump
list and the room-info card.

Every string that came from the server is drawn as a ``Text`` after ``sanitize_line``; none is
ever parsed as Rich markup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from rich.text import Text
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from pepper_cli.terminal_text import sanitize_line
from pepper_cli.tui import state

if TYPE_CHECKING:
    from collections.abc import Sequence

    from textual import events
    from textual.app import ComposeResult


class PickerScreen(ModalScreen[str | None]):
    """A short list to pick one entry from: Enter (or its number) chooses, Esc cancels."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, choices: Sequence[tuple[str, str]]) -> None:
        """``choices`` are ``(value, label)`` pairs; the value is what the screen answers."""
        super().__init__()
        self._title = title
        self._choices = list(choices)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text(self._title, style="bold"))
            yield OptionList(
                *(
                    Option(Text(f"{i}  {sanitize_line(label)}"), id=str(i - 1))
                    for i, (_, label) in enumerate(self._choices, start=1)
                ),
                id="picker-list",
            )
            yield Static(Text("Enter picks · 1-9 picks directly · Esc cancels", style="dim"))

    def on_mount(self) -> None:
        self.query_one(OptionList).highlighted = 0
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(self._choices[event.option_index][0])

    def on_key(self, event: events.Key) -> None:
        if event.character and event.character.isdigit():
            index = int(event.character) - 1
            if 0 <= index < len(self._choices):
                event.stop()
                self.dismiss(self._choices[index][0])

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no question. ``y`` confirms; ``n`` or Esc backs out."""

    AUTO_FOCUS = "#confirm-no"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "yes", "Yes"),
        Binding("n,escape", "no", "No"),
    ]

    def __init__(self, question: str, detail: str = "") -> None:
        super().__init__()
        self._question = question
        self._detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text(self._question, style="bold"), classes="dialog-title")
            if self._detail:
                yield Static(Text(sanitize_line(self._detail)), classes="dialog-detail")
            yield Static(Text("y confirms · n or Esc cancels", style="dim"))
            with Horizontal(classes="dialog-buttons"):
                yield Button("Yes (y)", variant="primary", id="confirm-yes")
                yield Button("Cancel (n)", variant="default", id="confirm-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "confirm-yes":
            self.dismiss(True)
        else:
            self.dismiss(False)

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class PromptScreen(ModalScreen[str | None]):
    """A one-line text prompt. Enter answers the stripped text; Esc cancels (``None``)."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        title: str,
        *,
        placeholder: str = "",
        max_length: int = 0,
        allow_blank: bool = False,
    ) -> None:
        """``max_length`` stops typing there (0: no cap). ``allow_blank`` lets Enter answer an
        empty line (``""``), for a step that may be skipped; Esc still cancels (``None``)."""
        super().__init__()
        self._title = title
        self._placeholder = placeholder
        self._max_length = max_length
        self._allow_blank = allow_blank

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text(self._title, style="bold"))
            yield Input(
                placeholder=self._placeholder, max_length=self._max_length, id="prompt-input"
            )
            yield Static(Text("Enter confirms · Esc cancels", style="dim"))

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        value = event.value.strip()
        if value or self._allow_blank:
            self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class JumpScreen(ModalScreen[state.Conversation | state.OwnApp | None]):
    """``Ctrl+K``: type to filter your own Todos and Notes, Pepper and every room and topic;
    Enter opens the highlighted one."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("up", "move(-1)", "Up", show=False),
        Binding("down", "move(1)", "Down", show=False),
    ]

    def __init__(self, targets: Sequence[state.JumpTarget]) -> None:
        super().__init__()
        self._targets = list(targets)
        self._shown: list[state.JumpTarget] = list(targets)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Input(placeholder="Jump to a room or topic…", id="jump-input")
            yield OptionList(id="jump-list")

    def on_mount(self) -> None:
        self._refill("")
        self.query_one(Input).focus()

    def _refill(self, query: str) -> None:
        self._shown = state.fuzzy_filter(query, self._targets)
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options(Option(Text(sanitize_line(t.label))) for t in self._shown)
        options.highlighted = 0 if self._shown else None

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._refill(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._choose(self.query_one(OptionList).highlighted)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._choose(event.option_index)

    def _choose(self, index: int | None) -> None:
        if index is not None and 0 <= index < len(self._shown):
            self.dismiss(self._shown[index].conversation)

    def action_move(self, step: int) -> None:
        options = self.query_one(OptionList)
        (options.action_cursor_down if step > 0 else options.action_cursor_up)()

    def action_cancel(self) -> None:
        self.dismiss(None)


def room_info_text(info: dict[str, Any]) -> Text:
    """The room-info card from ``GET /groups/{id}``. Every field is read defensively: the
    answer is a plain dict, and a missing or odd value is skipped rather than raised on."""
    out = Text()
    out.append(sanitize_line(str(info.get("display_name") or "Room")), style="bold")
    kind = sanitize_line(str(info.get("kind") or ""))
    if kind:
        out.append(f"  ({kind})", style="dim")
    members = [m for m in info.get("members") or [] if isinstance(m, dict)]
    out.append(f"\n\nMembers ({len(members)})", style="bold")
    for member in members:
        name = sanitize_line(str(member.get("display_name") or "?"))
        role = sanitize_line(str(member.get("role") or ""))
        you = " (you)" if member.get("is_you") else ""
        out.append(f"\n  {name}{you}" + (f" · {role}" if role else ""))
    topics = [t for t in info.get("topics") or [] if isinstance(t, dict)]
    live = [t for t in topics if not t.get("archived")]
    out.append(f"\n\nTopics ({len(live)} active, {len(topics) - len(live)} archived)", style="bold")
    apps = info.get("apps")
    apps = apps if isinstance(apps, dict) else {}
    out.append("\n\nApps", style="bold")
    if apps.get("unavailable"):
        out.append("\n  unavailable right now", style="dim")
    else:
        installed = [a for a in apps.get("installed") or [] if isinstance(a, dict)]
        if not installed:
            out.append("\n  none installed", style="dim")
        for app in installed:
            label = app.get("title") or app.get("name") or app.get("app_key") or "app"
            out.append("\n  " + sanitize_line(str(label)))
    return out


class InfoScreen(ModalScreen[None]):
    """The room-info card. Esc, q or Enter closes it."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,q,enter", "close", "Close")]

    def __init__(self, body: Text) -> None:
        super().__init__()
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            # A big room's member list is taller than the screen: the body scrolls (↑/↓,
            # PgUp/PgDn, the wheel) and the closing hint stays in view under it.
            with VerticalScroll(id="info-body"):
                yield Static(self._body)
            yield Static(Text("Esc closes", style="dim"))

    def action_close(self) -> None:
        self.dismiss(None)
