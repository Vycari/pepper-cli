"""The Todos and Notes pane: the widget that takes the thread's place, and what it draws.

Widget glue and rendering only. What the rows are, their order and the selection all come from
``tui.state`` (``pane_rows``, ``todo_sections``, ``move_selection``), and the network reads are
the app's. Every string that came from the server is drawn as a ``Text`` after ``sanitize_line``
(``sanitize_text`` for a body) and none is ever parsed as Rich markup.

The pane knows an *owner* (a room, or ``None`` for your own), not "a room", so your own panes
(``planning/tui-room-todos-notes.md`` P1b) reuse all of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Static

from pepper_cli.terminal_text import sanitize_line, sanitize_text
from pepper_cli.tui import rows, state

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from rich.console import RenderableType
    from textual.app import ComposeResult
    from textual.widget import Widget

    from pepper_cli.api_client import TodoEntry

#: ``(row key or None, renderable)``: what the pane shows, top to bottom. A key marks a row the
#: selection can land on.
type Item = tuple[str | None, RenderableType]


class PaneScroll(VerticalScroll):
    """The scrolling, focusable body. ↑/↓ move the selection; every key posts a message the
    app answers, as :class:`~pepper_cli.tui.thread.ThreadScroll` does."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move(-1)", "Up", show=False),
        Binding("down", "move(1)", "Down", show=False),
        Binding("enter", "activate", "Open", show=False),
        Binding("escape", "back", "Back"),
        Binding("m", "asks", "Asks of me"),
        Binding("r", "refresh", "Refresh"),
        Binding("o", "open_link", "Open link"),
    ]

    class Move(Message):
        def __init__(self, step: int) -> None:
            super().__init__()
            self.step = step

    class Activate(Message):
        """Enter: open the selected note, fold or unfold Closed, or show a todo's details."""

    class Back(Message):
        """Esc: from an open note to the list, from a list out of the pane."""

    class Asks(Message):
        """``m``: only the todos someone asked of you."""

    class Refresh(Message):
        """``r``: read again now."""

    class OpenLink(Message):
        """``o``: a todo's source link, or the first link in the open note."""

    #: ``todo`` or ``notes``; ``m`` is a Todos key and is hidden elsewhere.
    slug: str = "todo"

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        return not (action == "asks" and self.slug != "todo")

    def action_move(self, step: int) -> None:
        self.post_message(self.Move(step))

    def action_activate(self) -> None:
        self.post_message(self.Activate())

    def action_back(self) -> None:
        self.post_message(self.Back())

    def action_asks(self) -> None:
        self.post_message(self.Asks())

    def action_refresh(self) -> None:
        self.post_message(self.Refresh())

    def action_open_link(self) -> None:
        self.post_message(self.OpenLink())


class AppPane(Vertical):
    """Replaces the thread while a Todos or Notes pane is open."""

    def __init__(self) -> None:
        super().__init__(id="apppane")
        self.display = False
        #: The selection last scrolled into view: a redraw that leaves it where it was (a
        #: background re-read) keeps the user's scroll position.
        self._revealed: str | None = None

    def compose(self) -> ComposeResult:
        with PaneScroll(id="pane-scroll"):
            yield Static("", id="pane-body")

    @property
    def scroller(self) -> PaneScroll:
        return self.query_one("#pane-scroll", PaneScroll)

    def focus_scroller(self) -> Widget:
        self.scroller.focus()
        return self.scroller

    def scroll_page(self, *, down: bool) -> None:
        (self.scroller.scroll_page_down if down else self.scroller.scroll_page_up)(animate=False)

    def show(self, items: Sequence[Item], selected: str | None, *, slug: str, reset: bool) -> None:
        """Draw ``items``. A re-read keeps the scroll position; ``reset`` goes back to the top.

        The selection is scrolled into view when it moves (or on ``reset``), never just because
        the list was drawn again: paging past it must survive the 30s poll."""
        scroller = self.scroller
        scroller.slug = slug
        scroller.refresh_bindings()
        body = self.query_one("#pane-body", Static)
        body.update(Group(*(renderable for _, renderable in items)))
        if reset:
            scroller.scroll_home(animate=False)
        keys = [key for key, _ in items]
        reveal = reset or selected != self._revealed
        # A selection that is not drawn (an open note, a list still loading) is forgotten, so
        # the list it comes back to scrolls to it again.
        self._revealed = selected if selected in keys else None
        if reveal and selected in keys:
            index = keys.index(selected)
            width = max(scroller.scrollable_content_region.width, 1)
            options = self.app.console.options.update_width(width)

            def height(parts: list[RenderableType]) -> int:
                return len(self.app.console.render_lines(Group(*parts), options, pad=False))

            top = height([r for _, r in items[:index]]) if index else 0
            bottom = top + height([items[index][1]])
            view = scroller.scrollable_content_region.height
            if top < scroller.scroll_y:
                scroller.scroll_to(y=top, animate=False)
            elif bottom > scroller.scroll_y + view:
                scroller.scroll_to(y=max(top, bottom - view), animate=False)


# ---------------------------------------------------------------------------
# What the pane draws


def _line(label: str, right: str, *, style: str = "") -> Table:
    """One row: ``label`` filling the line, ``right`` flush to the edge (a due date, a day)."""
    grid = Table.grid(expand=True)
    grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    grid.add_column(justify="right", no_wrap=True)
    grid.add_row(Text(label), Text(right, style="dim"), style=style)
    return grid


def _dim(text: str, *, style: str = "") -> Text:
    return Text(
        sanitize_line(text), style=f"dim {style}".strip(), no_wrap=True, overflow="ellipsis"
    )


def _heading(left: str, right: str = "") -> Text:
    line = Text(sanitize_line(left), style="bold")
    if right:
        line.append("  " + sanitize_line(right), style="dim")
    return line


def _status(error: str | None, *, stale: bool) -> list[Item]:
    if error is None:
        return []
    note = "  (showing the last read)" if stale else ""
    return [(None, Text(sanitize_line(error) + note, style="yellow"))]


def todo_items(
    pane: state.AppPaneState,
    *,
    you_ids: set[str],
    now: datetime,
    hour24: bool,
) -> list[Item]:
    """The Todos list: title and count, open todos (with their ask line), the Closed fold."""
    count = state.open_todo_count(pane)
    tail = f"{count} open" + ("  ·  only asks of you" if pane.mine else "")
    items: list[Item] = [(None, _heading("Todos", tail)), (None, Text(""))]
    items.extend(_status(pane.error, stale=pane.loaded))
    if not pane.loaded:
        if pane.error is None:
            items.append((None, Text("Loading…", style="dim")))
        return items
    prow = state.pane_rows(pane)
    if not any(r.kind == "todo" and r.todo and r.todo.status == "open" for r in prow):
        items.append((None, Text("Nothing open.", style="dim")))
    for row in prow:
        selected = row.key == pane.selected
        style = rows.SELECTED if selected else ""
        if row.kind == "fold":
            arrow = "▾" if pane.closed_open else "▸"
            items.append((row.key, Text(f"{arrow} Closed ({row.count})", style=f"dim {style}")))
        elif row.todo is not None:
            items.append((row.key, _todo_row(row.todo, you_ids, now, hour24, style=style)))
    return items


def _todo_row(
    entry: TodoEntry,
    you_ids: set[str],
    now: datetime,
    hour24: bool,
    *,
    style: str,
) -> RenderableType:
    if entry.status == "open":
        mark, right = "○", state.due_label(entry, now=now, hour24=hour24)
    else:
        mark = "✓" if entry.status == "done" else "✗"
        right = ""
    title = sanitize_line(entry.title) or "(untitled)"
    parts: list[RenderableType] = [
        _line(
            f"{mark} {title}", sanitize_line(right), style=style or ("dim" if mark != "○" else "")
        )
    ]
    notes = [state.ask_line(entry, you_ids), state.todo_source_label(entry)]
    for note in (n for n in notes if n):
        parts.append(Text(f"  {sanitize_line(note)}", style=f"dim {style}".strip(), no_wrap=True))
    return Group(*parts)


def todo_detail_text(entry: TodoEntry, *, you_ids: set[str], now: datetime, hour24: bool) -> Text:
    """The Enter card for a todo: title, details, due date, source and asks."""
    out = Text(sanitize_line(entry.title), style="bold")
    out.append(f"  ({sanitize_line(entry.status)})", style="dim")
    if entry.details:
        out.append("\n\n" + sanitize_text(entry.details))
    facts = [
        state.due_label(entry, now=now, hour24=hour24),
        state.todo_source_label(entry),
        state.ask_line(entry, you_ids) or "",
    ]
    for fact in (f for f in facts if f):
        out.append("\n" + sanitize_line(fact), style="dim")
    return out


def notes_items(pane: state.AppPaneState, *, now: datetime) -> list[Item]:
    """The Notes list: name and edited day, the summary under each, the "Showing 200" line."""
    notes = pane.notes or []
    items: list[Item] = [
        (None, _heading("Notes", str(len(notes)) if pane.loaded else "")),
        (None, Text("")),
    ]
    items.extend(_status(pane.error, stale=pane.loaded))
    if not pane.loaded:
        if pane.error is None:
            items.append((None, Text("Loading…", style="dim")))
        return items
    if not notes:
        items.append((None, Text("No notes yet.", style="dim")))
    for row in state.pane_rows(pane):
        if row.kind == "limit":
            text = state.NOTES_TRUNCATED.format(limit=state.NOTES_LIMIT)
            items.append((None, _dim(text)))
        elif row.note is not None:
            style = rows.SELECTED if row.key == pane.selected else ""
            note = row.note
            edited = sanitize_line(state.edited_label(note.updated_at, now=now))
            parts: list[RenderableType] = [
                _line(sanitize_line(note.name) or "(untitled)", edited, style=style or "bold")
            ]
            if note.summary:
                parts.append(_dim("  " + note.summary, style=style))
            items.append((row.key, Group(*parts)))
    return items


def note_items(pane: state.AppPaneState, *, now: datetime) -> list[Item]:
    """An open note: its name and edited day, then its text rendered."""
    detail = pane.note
    name = sanitize_line(pane.note_name) or "Note"
    if detail is None:
        items: list[Item] = [(None, _heading(name)), (None, Text(""))]
        items.extend(_status(pane.note_error, stale=False))
        if pane.note_error is None:
            items.append((None, Text("Loading…", style="dim")))
        return items
    edited = state.edited_label(detail.updated_at, now=now)
    return [
        (None, _line(name, f"edited {sanitize_line(edited)}" if edited else "", style="bold")),
        (None, Text("")),
        # The list behind the note is what notices an edit, so its failure shows here too.
        *(_status(pane.note_error, stale=False) or _status(pane.error, stale=True)),
        (None, rows.note_body_renderable(detail.body or "")),
    ]
