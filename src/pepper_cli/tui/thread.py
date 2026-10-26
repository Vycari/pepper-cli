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
        Binding("plus", "react", "React"),
        Binding("e", "edit", "Edit"),
        Binding("d", "delete", "Delete"),
        Binding("o", "open_link", "Open link"),
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

    class React(Message):
        """``+``: pick an emoji for the selected message."""

    class Edit(Message):
        """``e``: edit the selected message (your own)."""

    class Delete(Message):
        """``d``: delete the selected message (your own), after a confirmation."""

    class Open(Message):
        """``o``: open the first link, attachment or card of the selected message."""

    def action_select(self, step: int) -> None:
        self.post_message(self.Select(step))

    def action_reply(self) -> None:
        self.post_message(self.Reply())

    def action_unselect(self) -> None:
        self.post_message(self.Unselect())

    def action_react(self) -> None:
        self.post_message(self.React())

    def action_edit(self) -> None:
        self.post_message(self.Edit())

    def action_delete(self) -> None:
        self.post_message(self.Delete())

    def action_open_link(self) -> None:
        self.post_message(self.Open())


class ThreadView(Vertical):
    """Messages plus a "N new" marker when rows arrive while you are reading further up."""

    class AtTop(Message):
        """The view reached its first line: the cue to load an older page."""

    class PinChanged(Message):
        """The view reached its last line (the newest row is on screen: the read cue, §5.2) or
        left it (scrolled up: rows below are unseen)."""

        def __init__(self, pinned: bool) -> None:
            super().__init__()
            self.pinned = pinned

    def __init__(self) -> None:
        super().__init__(id="thread")
        self._unseen = 0
        self._was_pinned = True

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

    def show(
        self,
        body: RenderableType,
        *,
        new_rows: int = 0,
        reset: bool = False,
        prepended: bool = False,
    ) -> None:
        """Replace the content. ``reset`` (a different conversation) always lands at the end.

        ``prepended`` says rows were added above what was shown (an older page): the view
        keeps showing the same lines, so the added height is scrolled past.
        """
        stick = reset or (self.pinned and not prepended)
        scroller = self.scroller
        old_height, old_y = scroller.virtual_size.height, scroller.scroll_y
        self.query_one("#thread-body", Static).update(body)
        if prepended and not reset:

            def keep_place() -> None:
                grown = scroller.virtual_size.height - old_height
                scroller.scroll_to(y=old_y + max(grown, 0), animate=False)

            scroller.call_after_refresh(keep_place)
        if stick:
            self._unseen = 0
            self.scroller.call_after_refresh(self.scroller.scroll_end, animate=False)
        else:
            self._unseen += new_rows
        self._paint_marker()

    def on_mount(self) -> None:
        # However the bottom is reached (keys, wheel, scrollbar drag), the marker clears.
        self.watch(self.scroller, "scroll_y", self._scrolled, init=False)

    def _scrolled(self, _old: float, new: float) -> None:
        if new <= 0 and self.scroller.max_scroll_y > 0:
            self.post_message(self.AtTop())
        pinned = self.pinned
        if pinned != self._was_pinned:
            self._was_pinned = pinned
            self.post_message(self.PinChanged(pinned))
        if pinned and self._unseen:
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
