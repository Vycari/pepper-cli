"""The right-hand pane: a scrolling thread that stays pinned to the bottom until you scroll up."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

if TYPE_CHECKING:
    from rich.console import RenderableType
    from textual.app import ComposeResult
    from textual.widget import Widget


class ThreadView(Vertical):
    """Messages plus a "N new" marker when rows arrive while you are reading further up."""

    def __init__(self) -> None:
        super().__init__(id="thread")
        self._unseen = 0

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="thread-scroll"):
            yield Static("", id="thread-body")
        yield Static("", id="thread-new")

    @property
    def scroller(self) -> VerticalScroll:
        return self.query_one("#thread-scroll", VerticalScroll)

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
