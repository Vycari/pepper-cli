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

#: Refreshes an older page's scroll-past waits through for the new rows to be measured.
_KEEP_PLACE_TRIES = 5

#: Refreshes a stick-to-the-end waits through before it reads the end, as many as
#: ``scroll_end`` itself defers, so the layout has measured the rows it scrolls past.
_STICK_TRIES = 2


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
        Binding("s", "send_draft", "Send draft"),
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

    class SendDraft(Message):
        """``s``: send a draft Pepper staged in the thread, after a confirmation."""

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

    def action_send_draft(self) -> None:
        self.post_message(self.SendDraft())


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

    class Landed(Message):
        """A guarded scroll to the last line completed: the rows just rendered are on
        screen, so the topic can be marked read. Carries the render it landed for and the
        newest timestamp it rendered, so a message handled after another render replaced it,
        or after a poll appended rows, marks only what was on screen. Not sent when the
        reader moved first, unless they reached the new end themselves or a reset clamped
        them onto it."""

        def __init__(self, render_id: int, through: str | None) -> None:
            super().__init__()
            self.render_id = render_id
            self.through = through

    def __init__(self) -> None:
        super().__init__(id="thread")
        self._unseen = 0
        self._was_pinned = True
        #: Bumped when a different conversation is shown, so a keep-place retry, a stick
        #: callback or a queued ``Landed`` left over from the previous one stands down. Not
        #: by every ``show``: a live refresh of the same conversation must not cancel the
        #: scroll past an older page it has not reached yet.
        self._render_id = 0
        #: Bumped on every ``scroll_y`` change, so a deferred callback knows the reader moved
        #: even if they scrolled away and back to the offset it captured.
        self._scrolls = 0

    def compose(self) -> ComposeResult:
        with ThreadScroll(id="thread-scroll"):
            yield Static("", id="thread-body")
        yield Static("", id="thread-new")

    @property
    def going(self) -> bool:
        """Removal has begun. Textual sets this at once but takes the children out one await
        later, so for a moment the thread is in the tree with no body to paint (#177)."""
        return self._pruning or self._closing

    @property
    def scroller(self) -> ThreadScroll:
        return self.query_one("#thread-scroll", ThreadScroll)

    @property
    def pinned(self) -> bool:
        scroller = self.scroller
        return scroller.scroll_y >= scroller.max_scroll_y - 1

    @property
    def render_id(self) -> int:
        """The current render: a ``Landed`` that names an older one is stale."""
        return self._render_id

    def show(
        self,
        body: RenderableType,
        *,
        new_rows: int = 0,
        reset: bool = False,
        prepended: bool = False,
        through: str | None = None,
    ) -> None:
        """Replace the content. ``reset`` (a different conversation) always lands at the end.

        ``prepended`` says rows were added above what was shown (an older page): the view
        keeps showing the same lines, so the added height is scrolled past. ``through`` is
        the newest timestamp ``body`` renders, carried on ``Landed`` so a mark published
        after a later poll covers only the rows this render put on screen.
        """
        stick = reset or (self.pinned and not prepended)
        scroller = self.scroller
        old_height, old_y = scroller.virtual_size.height, scroller.scroll_y
        scrolls = self._scrolls
        if reset:
            self._render_id += 1
        render_id = self._render_id
        self.query_one("#thread-body", Static).update(body)
        if prepended and not reset:

            def keep_place(tries: int = _KEEP_PLACE_TRIES) -> None:
                if self._render_id != render_id or self._scrolls != scrolls:
                    # Another conversation was opened, or the reader scrolled, while this
                    # waited: either one owns the position now.
                    return
                grown = scroller.virtual_size.height - old_height
                if grown <= 0 and tries > 1:
                    # The refresh this waited for can land before the layout that measures
                    # the new rows (a loaded loop); scrolling by 0 would leave the reader at
                    # the top of the longer thread. Wait for the next one.
                    scroller.call_after_refresh(keep_place, tries - 1)
                    return
                scroller.scroll_to(y=old_y + max(grown, 0), animate=False)

            scroller.call_after_refresh(keep_place)
        if stick:
            self._unseen = 0

            def stick_to_end(tries: int = _STICK_TRIES) -> None:
                if self._render_id != render_id:
                    # Another conversation was opened while this waited: its own render owns
                    # the position and the count, and this one's rows are not on screen.
                    return
                if self._scrolls != scrolls:
                    # The reader moved while this waited, even if they came back to the same
                    # offset: they own the position now. Scrolling to the end here would drag
                    # them back down, and the read mark follows the screen (a Windows
                    # merge-queue run of pepper-cli#274 landed it after the reader had gone up).
                    if self.pinned:
                        # They are at the new end without this render's own scroll: either the
                        # reader reached it (the End key, a wheel to the bottom) or a shorter
                        # page clamped the old position there. The newest row is on screen, so
                        # this render landed.
                        self.post_message(self.Landed(render_id, through))
                    elif not reset and new_rows:
                        # Away from the end, the rows that arrived are below the screen: count
                        # them, or later polls deduplicate them and the cue is lost for good.
                        # Pinned means a sibling callback for the same render already reached
                        # the end: those rows are on screen and already counted as seen.
                        self._unseen += new_rows
                        self._paint_marker()
                    return
                if tries > 1:
                    # This refresh can land before the layout that measures the new rows;
                    # read the end after one more, as ``scroll_end`` does, but guard it too.
                    scroller.call_after_refresh(stick_to_end, tries - 1)
                    return
                scroller.scroll_end(animate=False, immediate=True)
                # The newest row is on screen now. A first page is marked read from this,
                # and so is a pinned poll: its own mark waits for this landing, so a reader
                # who moves first keeps the rows unread.
                self.post_message(self.Landed(render_id, through))

            scroller.call_after_refresh(stick_to_end)
        else:
            self._unseen += new_rows
        self._paint_marker()

    def on_mount(self) -> None:
        # However the bottom is reached (keys, wheel, scrollbar drag), the marker clears.
        self.watch(self.scroller, "scroll_y", self._scrolled, init=False)

    def _scrolled(self, _old: float, new: float) -> None:
        self._scrolls += 1
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
