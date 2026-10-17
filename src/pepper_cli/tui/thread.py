"""The right-hand pane: a scrolling thread that stays pinned to the bottom until you scroll up."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Static

if TYPE_CHECKING:
    from rich.console import RenderableType
    from textual.app import ComposeResult
    from textual.widget import Widget


class ThreadScroll(VerticalScroll):
    """The scrolling body. ↑/↓ select a message (for ``r``) instead of scrolling a line;
    Page Up/Down and the wheel still scroll."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "select(-1)", "Select", show=False),
        Binding("down", "select(1)", "Select", show=False),
        Binding("r", "reply", "Reply / retry"),
        Binding("escape", "unselect", "Unselect", show=False),
    ]

    class Select(Message):
        """Move the selection ``step`` rows (negative is up)."""

        def __init__(self, step: int) -> None:
            super().__init__()
            self.step = step

    class Reply(Message):
        """``r``: quote-reply to the selected message, or retry a failed send."""

    class Unselect(Message):
        """Esc in the thread."""

    def action_select(self, step: int) -> None:
        self.post_message(self.Select(step))

    def action_reply(self) -> None:
        self.post_message(self.Reply())

    def action_unselect(self) -> None:
        self.post_message(self.Unselect())


class ThreadView(Vertical):
    """Messages plus a "N new" marker when rows arrive while you are reading further up."""

    def __init__(self) -> None:
        super().__init__(id="thread")
        self._unseen = 0

    def compose(self) -> ComposeResult:
        with ThreadScroll(id="thread-scroll"):
            yield Static("", id="thread-body")
        yield Static("", id="thread-new")

    @property
    def scroller(self) -> ThreadScroll:
        return self.query_one("#thread-scroll", ThreadScroll)

    @property
    def pinned(self) -> bool:
        scroller = self.scroller
        return scroller.scroll_y >= scroller.max_scroll_y - 1

    def show(self, body: RenderableType, *, new_rows: int = 0, reset: bool = False) -> None:
        """Replace the content. ``reset`` (a different conversation) always lands at the end."""
        stick = reset or self.pinned
        self.query_one("#thread-body", Static).update(body)
        if stick:
            self._unseen = 0
            self.scroller.call_after_refresh(self.scroller.scroll_end, animate=False)
        else:
            self._unseen += new_rows
        self._paint_marker()

    def on_mount(self) -> None:
        # However the bottom is reached (keys, wheel, scrollbar drag), the marker clears.
        self.watch(self.scroller, "scroll_y", self._scrolled, init=False)

    def _scrolled(self, _old: float, _new: float) -> None:
        if self._unseen and self.pinned:
            self._unseen = 0
            self._paint_marker()

    def scroll_page(self, *, down: bool) -> None:
        (self.scroller.scroll_page_down if down else self.scroller.scroll_page_up)(animate=False)

    def _paint_marker(self) -> None:
        marker = self.query_one("#thread-new", Static)
        marker.update(f"↓ {self._unseen} new" if self._unseen else "")
        marker.display = bool(self._unseen)

    def focus_scroller(self) -> Widget:
        self.scroller.focus()
        return self.scroller

    def body_width(self) -> int:
        """Columns the body is laid out in (for measuring where a row starts)."""
        return max(self.query_one("#thread-body", Static).size.width, 1)

    def reveal_lines(self, top: int, bottom: int) -> None:
        """Scroll the least distance that shows body lines ``top`` up to ``bottom``."""
        scroller = self.scroller
        height = scroller.scrollable_content_region.height
        y = scroller.scroll_y
        if top < y:
            scroller.scroll_to(y=top, animate=False)
        elif bottom > y + height:
            scroller.scroll_to(y=max(top, bottom - height), animate=False)
