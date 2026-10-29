"""The Todos and Notes pane: the widget that takes the thread's place, and what it draws.

Widget glue and rendering only. What the rows are, their order and the selection all come from
``tui.state`` (``pane_rows``, ``todo_sections``, ``move_selection``), and the network reads are
the app's. Every string that came from the server is drawn as a ``Text`` after ``sanitize_line``
(``sanitize_text`` for a body) and none is ever parsed as Rich markup.

The pane knows an *owner* (a room, or ``None`` for your own), not "a room", so your own panes
(``planning/tui-room-todos-notes.md`` P1b) reuse all of it. Under your own pane the Pepper
composer stays and a :class:`ReplyStrip` at the pane's foot shows the last two exchanges.
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


#: The actions of the Todos list; they are hidden (and do nothing) over Notes.
TODO_ACTIONS = frozenset({"asks", "done", "dismiss", "reopen", "add"})


class PaneScroll(VerticalScroll):
    """The scrolling, focusable body. ↑/↓ move the selection; every key posts a message the
    app answers, as :class:`~pepper_cli.tui.thread.ThreadScroll` does."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move(-1)", "Up", show=False),
        Binding("down", "move(1)", "Down", show=False),
        Binding("enter", "activate", "Open", show=False),
        Binding("escape", "back", "Back"),
        # ``space`` is two bindings, one per app; check_action leaves exactly one live.
        Binding("space", "done", "Done"),
        Binding("x", "done", "Done", show=False),
        Binding("d", "dismiss", "Dismiss"),
        Binding("u", "reopen", "Reopen"),
        Binding("n", "add", "Add"),
        Binding("c", "new_note", "New"),
        Binding("e", "edit_note", "Edit"),
        Binding("D", "delete_note", "Delete"),
        Binding("space", "tick", "Tick"),
        Binding("m", "asks", "Asks of me"),
        Binding("r", "refresh", "Refresh"),
        Binding("o", "open_link", "Open link"),
        Binding("tab", "composer", "Composer"),
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

    class Done(Message):
        """``space`` / ``x``: mark the selected open todo done."""

    class Dismiss(Message):
        """``d``: dismiss the selected open todo."""

    class Reopen(Message):
        """``u``: put the selected closed todo back."""

    class Add(Message):
        """``n``: add a todo."""

    class Refresh(Message):
        """``r``: read again now."""

    class OpenLink(Message):
        """``o``: a todo's source link, or the first link in the open note."""

    class NewNote(Message):
        """``c``: a new note, written in ``$EDITOR``."""

    class EditNote(Message):
        """``e``: the selected (or open) note, in ``$EDITOR``."""

    class DeleteNote(Message):
        """``D``: delete the selected (or open) note, after asking."""

    class Tick(Message):
        """``space``: tick or untick a checklist line of the open note."""

    #: ``todo`` or ``notes``; ``m`` is a Todos key and is hidden elsewhere.
    slug: str = "todo"
    #: A note is open (``space`` ticks only there).
    note_open: bool = False
    #: Your own pane: the Pepper composer is the next stop, and the footer says so.
    composer_below: bool = False

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "composer":
            return self.composer_below
        if action in TODO_ACTIONS:
            return self.slug == "todo"
        if action in ("new_note", "edit_note", "delete_note"):
            return self.slug == "notes"
        if action == "tick":
            return self.slug == "notes" and self.note_open
        return True

    def action_composer(self) -> None:
        self.app.action_focus_next()

    def action_move(self, step: int) -> None:
        self.post_message(self.Move(step))

    def action_activate(self) -> None:
        self.post_message(self.Activate())

    def action_back(self) -> None:
        self.post_message(self.Back())

    def action_done(self) -> None:
        self.post_message(self.Done())

    def action_dismiss(self) -> None:
        self.post_message(self.Dismiss())

    def action_reopen(self) -> None:
        self.post_message(self.Reopen())

    def action_add(self) -> None:
        self.post_message(self.Add())

    def action_asks(self) -> None:
        self.post_message(self.Asks())

    def action_refresh(self) -> None:
        self.post_message(self.Refresh())

    def action_open_link(self) -> None:
        self.post_message(self.OpenLink())

    def action_new_note(self) -> None:
        self.post_message(self.NewNote())

    def action_edit_note(self) -> None:
        self.post_message(self.EditNote())

    def action_delete_note(self) -> None:
        self.post_message(self.DeleteNote())

    def action_tick(self) -> None:
        self.post_message(self.Tick())


class ReplyStrip(VerticalScroll):
    """Pepper's answers at the foot of your own pane: the exchange in flight and the one before.

    Scrolls with the wheel when longer than its cap (about eight lines, in the stylesheet) and
    is never a focus stop: the Tab cycle is sidebar, pane, composer."""

    can_focus = False

    def __init__(self) -> None:
        super().__init__(id="reply-strip")
        self.display = False

    def compose(self) -> ComposeResult:
        yield Static("", id="reply-body")


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
        yield ReplyStrip()

    @property
    def scroller(self) -> PaneScroll:
        return self.query_one("#pane-scroll", PaneScroll)

    def focus_scroller(self) -> Widget:
        self.scroller.focus()
        return self.scroller

    def show_strip(self, parts: Sequence[RenderableType]) -> None:
        """Draw the reply strip (nothing: hide it). It follows the newest line."""
        strip = self.query_one(ReplyStrip)
        strip.display = bool(parts)
        self.query_one("#reply-body", Static).update(Group(*parts))
        if parts:
            strip.scroll_end(animate=False)

    def scroll_page(self, *, down: bool) -> None:
        (self.scroller.scroll_page_down if down else self.scroller.scroll_page_up)(animate=False)

    def show(
        self,
        items: Sequence[Item],
        selected: str | None,
        *,
        slug: str,
        reset: bool,
        composer_below: bool = False,
    ) -> None:
        """Draw ``items``. A re-read keeps the scroll position; ``reset`` goes back to the top.

        The selection is scrolled into view when it moves (or on ``reset``), never just because
        the list was drawn again: paging past it must survive the 30s poll."""
        scroller = self.scroller
        scroller.slug = slug
        scroller.composer_below = composer_below
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


#: Drawn in place of a todo's mark while a write to it is in flight.
PENDING_MARK = "…"

#: Shown on your own Todos when the rooms' read failed: your own items are still drawn.
ROOMS_FAILED = "Couldn't load your rooms' todos."


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
    if pane.rooms_error:
        items.append((None, Text(ROOMS_FAILED, style="yellow")))
    prow = state.pane_rows(pane)
    if not any(r.kind == "todo" and r.todo and r.todo.status == "open" for r in prow):
        items.append((None, Text("Nothing open.", style="dim")))
    for row in prow:
        selected = row.key == pane.selected
        style = rows.SELECTED if selected else ""
        if row.kind == "section":
            items.append((None, Text("")))
            items.append((None, Text(sanitize_line(row.label), style="bold", no_wrap=True)))
        elif row.kind == "fold":
            arrow = "▾" if pane.closed_open else "▸"
            items.append((row.key, Text(f"{arrow} Closed ({row.count})", style=f"dim {style}")))
        elif row.todo is not None:
            pending = row.todo.id in pane.pending
            items.append(
                (row.key, _todo_row(row.todo, you_ids, now, hour24, style=style, pending=pending))
            )
    return items


def _todo_row(
    entry: TodoEntry,
    you_ids: set[str],
    now: datetime,
    hour24: bool,
    *,
    style: str,
    pending: bool = False,
) -> RenderableType:
    if entry.status == "open":
        mark, right = "○", state.due_label(entry, now=now, hour24=hour24)
    else:
        mark = "✓" if entry.status == "done" else "✗"
        right = ""
    if pending:
        mark = PENDING_MARK  # a write is in flight: the row is as it was until the answer
    title = sanitize_line(entry.title) or "(untitled)"
    parts: list[RenderableType] = [
        _line(
            f"{mark} {title}",
            sanitize_line(right),
            style=style or ("dim" if mark != "○" else ""),
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


def strip_items(turns: Sequence[state.DmTurn]) -> list[RenderableType]:
    """The reply strip: for each turn, your line and Pepper's answer (or what it is waiting on).

    Everything is sanitized into ``Text`` or a markdown body, never parsed as Rich markup."""
    out: list[RenderableType] = []
    for turn in turns:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(no_wrap=True, width=6)
        grid.add_column(ratio=1)
        grid.add_row(Text("you", style="bold"), Text(sanitize_text(turn.text)))
        answer: RenderableType
        if turn.reply is not None:
            answer = rows.reply_renderable(turn.reply)
        else:
            wait = {
                "queued": "waiting to send…",
                "streaming": "…",
                "checking": "checking for the answer…",
            }.get(turn.phase, f"({turn.status})" if turn.status else "(no answer)")
            answer = Text(wait, style="dim italic")
        grid.add_row(Text("Pepper", style="bold"), answer)
        out.append(grid)
    return out
