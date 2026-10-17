"""The full-screen app: layout, key bindings, and the scheduling around the poll functions.

Everything that talks to the network runs on a worker thread (``pepper_cli.tui.poller``) and
hands plain data back through ``call_from_thread``; the shared state (read marks, the open
topic's rows, the sidebar rows, the rail's ETag) is only touched on the UI thread.

There are five loops: ``startup`` (whoami, until it succeeds), ``rail``, ``dm``, ``topic`` (the
open topic) and ``publish`` (owed read marks). Each is a :class:`_Loop`, and the rules are the
same for all of them:

- At most one request per loop is in flight. ``_run`` is a no-op while one is, so a timer, a
  focus return or a key press can never start a second one.
- A loop's ``generation`` changes only when what it is about changes (switching topics). A
  result from an older generation is dropped. Nothing a worker returns has been committed
  yet, so dropping it loses nothing: the rail's ETag is adopted only when its result is
  applied, and a topic's cursor moves only when its rows are merged.
- Success resets the failure count and arms the next timer at the normal cadence; failure
  counts up and arms the next timer at the backoff delay. A 401 or 426 stops everything.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

import httpx
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult, ScreenStackError
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.widgets import Footer, Static

from pepper_cli.api_client import ApiError, UpgradeRequiredError
from pepper_cli.terminal_text import sanitize_line
from pepper_cli.tui import poller, rows, state
from pepper_cli.tui.sidebar import Sidebar
from pepper_cli.tui.thread import ThreadView

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.timer import Timer
    from textual.widgets import Tree

    from pepper_cli.api_client import (
        ApiClient,
        ChatHistoryEntry,
        GroupList,
        GroupMessage,
        GroupSummary,
    )

LOGIN_HINT = "Authentication failed (401). Run 'pepper login' to refresh your token."
#: What a request can fail with and be handled by ``_failed``: retried with backoff, or (401,
#: 426) shown as a fatal notice. Anything else is a bug and should surface as a traceback
#: rather than as an endless "offline" banner.
_EXPECTED = (ApiError, UpgradeRequiredError, httpx.RequestError, ValueError, KeyError, TypeError)
#: Loops whose failures put "offline, retrying" in the header.
_LOOPS = ("startup", "rail", "dm", "topic", "publish")
#: Client errors a read-mark PUT will keep getting however often it is retried (no access to
#: the topic any more, the topic is gone, the request is refused). The mark is dropped.
_PUBLISH_PERMANENT = frozenset({400, 403, 404, 409, 410, 422})


@dataclass
class _Loop:
    """Scheduling state of one poll loop (see the module docstring)."""

    failures: int = 0
    generation: int = 0
    #: The generation of the request in flight, or ``None``.
    in_flight: int | None = None
    timer: Timer | None = None

    @property
    def busy(self) -> bool:
        return self.in_flight is not None and self.in_flight == self.generation

    def stop_timer(self) -> None:
        if self.timer is not None:
            self.timer.stop()
            self.timer = None


class PepperApp(App[None]):
    """Read-only terminal client for Pepper's messages."""

    CSS_PATH = "styles.tcss"
    TITLE = "Pepper"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+n", "next_unread", "Next unread"),
        Binding("ctrl+p", "previous_unread", "Prev unread"),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
        Binding("pageup", "page_up", "Page up", show=False),
        Binding("pagedown", "page_down", "Page down", show=False),
        Binding("ctrl+q", "quit_app", "Quit", priority=True),
    ]

    def __init__(self, api: ApiClient) -> None:
        super().__init__()
        self._api = api
        self._rail = poller.RailPoller(api)
        self.rooms: list[GroupSummary] = []
        self.groups_enabled = True
        self.groups_degraded = False
        self.marks = state.ReadMarks()
        self.current = state.PEPPER
        self.store = state.MessageStore()
        self.dm = state.DmTracker()
        self.manual: dict[str, bool] = {}
        self.show_all: set[str] = set()
        self.you_id: str | None = None
        self.hour24 = False
        self.loaded_topic = False
        self.fatal: str | None = None
        self._loops = {kind: _Loop() for kind in _LOOPS}
        self._topic_error: str | None = None
        self._last_input = time.monotonic()
        self._blurred = False
        #: ``topic_id -> read_through`` the server has acknowledged.
        self._published: dict[str, str] = {}
        self._started = False
        #: Marks still to send, oldest first: ``topic_id -> (conversation, read_through)``.
        self._owed: dict[str, tuple[state.Conversation, str]] = {}

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        with Vertical(id="left"):
            yield Sidebar()
            yield Static("", id="subline")
        with Vertical(id="right"):
            yield Static("Pepper · Today", id="header")
            yield Static("", id="notice", classes="empty")
            yield ThreadView()
        yield Footer()

    @property
    def sidebar(self) -> Sidebar:
        return self.query_one(Sidebar)

    @property
    def thread(self) -> ThreadView:
        return self.query_one(ThreadView)

    def on_mount(self) -> None:
        self.refresh_chrome()
        self.sidebar.focus()
        self._run("startup")

    # ------------------------------------------------------------------ workers

    def _start[T](
        self,
        kind: str,
        work: Callable[[], T],
        deliver: Callable[[T], None],
        *,
        on_error: Callable[[Exception], bool] | None = None,
    ) -> None:
        """Run ``work`` on a thread; apply its result (or failure) on the UI thread.

        ``on_error`` runs for a non-fatal failure of a current request. It returns True when
        that failure was final for the item and the loop should move on at once instead of
        backing off (the publish loop drops a mark the server will never accept).
        """
        loop = self._loops[kind]
        generation = loop.generation
        loop.in_flight = generation

        def run() -> None:
            try:
                result = work()
            except _EXPECTED as exc:
                self.call_from_thread(self._failed, kind, exc, generation, on_error)
            else:
                self.call_from_thread(self._delivered, kind, deliver, result, generation)

        self.run_worker(run, thread=True, group=kind, exit_on_error=True)

    def _finish(self, kind: str, generation: int) -> bool:
        """Clear the in-flight token; True when the result should be applied."""
        loop = self._loops[kind]
        if loop.in_flight == generation:
            loop.in_flight = None
        if not self._mounted():
            return False  # a worker can finish after quit has torn the screen down
        return not self.fatal and generation == loop.generation

    def _mounted(self) -> bool:
        try:
            return bool(self.query(Sidebar))
        except ScreenStackError:
            return False

    def _delivered[T](
        self,
        kind: str,
        deliver: Callable[[T], None],
        result: T,
        generation: int,
    ) -> None:
        if not self._finish(kind, generation):
            return
        self._loops[kind].failures = 0
        if kind == "topic":
            self._topic_error = None
        deliver(result)
        self.refresh_chrome()
        self._schedule(kind)

    def _failed(
        self,
        kind: str,
        exc: Exception,
        generation: int,
        on_error: Callable[[Exception], bool] | None,
    ) -> None:
        if not self._finish(kind, generation):
            return
        if isinstance(exc, ApiError) and exc.status == 401:
            self.fail_fatal(LOGIN_HINT)
            return
        if isinstance(exc, UpgradeRequiredError):
            self.fail_fatal(str(exc))
            return
        if on_error is not None and on_error(exc):
            # The server answered, so the connection is fine: no backoff, no offline banner.
            self._loops[kind].failures = 0
            self.refresh_chrome()
            self._run(kind)
            return
        self._loops[kind].failures += 1
        if kind == "topic" and isinstance(exc, ApiError):
            self._topic_error = "You can't open this topic." if exc.status in (403, 404) else None
        if kind == "rail":
            self._rail.forget_etag()
        self.refresh_chrome()
        self._schedule(kind)

    def fail_fatal(self, message: str) -> None:
        """Stop every loop and wait for a keypress, then exit 1 (§10)."""
        self.fatal = message
        for loop in self._loops.values():
            loop.stop_timer()
        self.refresh_chrome()

    def on_key(self, event: events.Key) -> None:
        self._last_input = time.monotonic()
        if self.fatal:
            event.stop()
            self.exit(return_code=1)

    def on_mouse_down(self, _event: events.MouseDown) -> None:
        self._last_input = time.monotonic()

    def on_mouse_scroll_down(self, _event: events.MouseScrollDown) -> None:
        self._last_input = time.monotonic()

    def on_mouse_scroll_up(self, _event: events.MouseScrollUp) -> None:
        self._last_input = time.monotonic()

    def on_app_blur(self, _event: events.AppBlur) -> None:
        """Pause the open-topic poll. A request already in flight still lands, but its rows
        are not marked read until focus returns (§6.3: open *and focused* = read). A backoff
        retry stays armed: recovering from a failure does not wait for focus."""
        self._blurred = True
        topic = self._loops["topic"]
        if topic.failures == 0:
            topic.stop_timer()

    def on_app_focus(self, _event: events.AppFocus) -> None:
        self._blurred = False
        self._last_input = time.monotonic()
        self._poll_now()

    # ------------------------------------------------------------------ scheduling

    def _interval(self, kind: str) -> float | None:
        failures = self._loops[kind].failures
        if failures:
            return state.backoff_delay(failures)
        if kind == "rail":
            return state.RAIL_INTERVAL
        if kind == "dm":
            return state.DM_INTERVAL
        if kind == "topic":
            return state.poll_interval(time.monotonic() - self._last_input, hidden=self._blurred)
        return None  # startup runs once; publish runs when there is something owed

    def _schedule(self, kind: str) -> None:
        """Arm the next run of one loop. Open-topic polling is skipped while blurred (it is
        restarted by focus); the rail and DM keep their slow cadence."""
        loop = self._loops[kind]
        loop.stop_timer()
        if self.fatal:
            return
        if kind == "topic" and self.current.is_pepper:
            return
        delay = self._interval(kind)
        if delay is None:
            return
        loop.timer = self.set_timer(delay, lambda: self._run(kind))

    def _run(self, kind: str) -> None:
        """Start one request for ``kind`` now, unless one is already in flight."""
        loop = self._loops[kind]
        if self.fatal or loop.busy:
            return
        loop.stop_timer()
        if kind == "startup":
            self._start(kind, self._load_identity, self._on_identity)
        elif kind == "rail":
            self._start(kind, self._rail.step, self._on_rail)
        elif kind == "dm":
            self._start(kind, lambda: poller.fetch_dm(self._api), self._on_dm)
        elif kind == "publish":
            self._send_next_mark()
        elif not self.current.is_pepper:
            conv = self.current
            reconcile = self.store.next_is_reconcile()
            since = None if reconcile else self.store.since()
            self._start(
                kind, lambda: poller.fetch_topic(self._api, conv, since=since), self._on_topic
            )

    def _poll_now(self) -> None:
        """Focus came back: poll every healthy loop now, except one whose request is already
        in flight (its result will land and re-arm it). A failing loop keeps its backoff, and
        is re-armed if nothing is pending for it. Started only once ``whoami`` has."""
        if not self._started:
            return
        for kind in ("rail", "dm", "topic", "publish"):
            loop = self._loops[kind]
            if loop.failures == 0:
                self._run(kind)
            elif loop.timer is None and not loop.busy:
                self._schedule(kind)

    # ------------------------------------------------------------------ startup

    def _load_identity(self) -> tuple[str | None, bool]:
        who = self._api.whoami()
        return who.user_id, who.time_format == "24h"

    def _on_identity(self, result: tuple[str | None, bool]) -> None:
        self.you_id, self.hour24 = result
        self._started = True
        self._run("rail")
        self._run("dm")

    # ------------------------------------------------------------------ results

    def _on_rail(self, update: poller.RailUpdate) -> None:
        self._rail.commit(update)
        groups = update.groups
        if groups is None:
            return
        self.groups_enabled = groups.enabled
        self.groups_degraded = groups.degraded
        if groups.enabled and not groups.degraded:
            self._apply_groups(groups, update.server_marks)

    def _apply_groups(self, groups: GroupList, server_marks: dict[str, str]) -> None:
        self.rooms = groups.groups
        self.marks.merge_server(server_marks)
        self.marks.seed_first_sight(self.rooms)

    def _on_dm(self, history: list[ChatHistoryEntry]) -> None:
        new_ids = self.dm.update(history, is_open=self.current.is_pepper)
        if self.current.is_pepper:
            self._render_dm(new_rows=len(new_ids), reset=False)

    def _on_topic(self, messages: list[GroupMessage]) -> None:
        new_ids = self.store.add(messages)
        fresh = [m for m in messages if m.id in new_ids and not m.is_you]
        first = not self.loaded_topic
        self.loaded_topic = True
        self._render_topic(new_rows=len(fresh), reset=first)
        if not self._blurred:
            self._note_viewed()
            self._run("publish")

    # ------------------------------------------------------------------ rendering

    def _render_dm(self, *, new_rows: int, reset: bool) -> None:
        self.thread.show(
            rows.dm_renderable(self.dm.ordered(), hour24=self.hour24),
            new_rows=new_rows,
            reset=reset,
        )

    def _render_topic(self, *, new_rows: int, reset: bool) -> None:
        if not self.loaded_topic:
            self.thread.show(Text("Loading…", style="dim"), reset=True)
            return
        body = rows.thread_renderable(self.store.ordered(), you_id=self.you_id)
        self.thread.show(body, new_rows=new_rows, reset=reset)

    def refresh_chrome(self) -> None:
        """Sidebar, header, notice, subline and terminal title from the current state."""
        shown = self.rooms if self.groups_enabled else []
        current_key = "pepper" if self.current.is_pepper else f"topic:{self.current.topic_id}"
        self.sidebar.set_rows(
            state.build_sidebar(
                shown,
                self.marks,
                current=self.current,
                manual=self.manual,
                show_all=self.show_all,
                pepper_dot=self.dm.dot,
            ),
            current_key=current_key,
        )
        header = sanitize_line(state.header_text(self.current, shown))
        if any(loop.failures for loop in self._loops.values()):
            header += "  · offline, retrying"
        self.query_one("#header", Static).update(Text(header))
        notice = self._notice()
        box = self.query_one("#notice", Static)
        box.update(Text(notice))
        box.set_class(not notice, "empty")
        self._update_subline()
        unread = state.any_unread(shown, self.marks, current=self.current, pepper_dot=self.dm.dot)
        self.title = ("● " if unread else "") + "Pepper"

    def _notice(self) -> str:
        if self.fatal:
            return f"{self.fatal}  Press any key to exit."
        if not self.groups_enabled:
            return "Rooms aren't enabled on this server; only your Pepper chat is available."
        if self.groups_degraded:
            return "Rooms are temporarily unavailable; showing the last known list."
        if self._topic_error and not self.current.is_pepper:
            return self._topic_error
        return ""

    def _update_subline(self) -> None:
        if not self._mounted():  # a highlight event can outlive the screen at shutdown
            return
        row = self.sidebar.highlighted_row
        text = ""
        if row is not None and row.room_id:
            room = next((r for r in self.rooms if r.group_id == row.room_id), None)
            if room is not None:
                text = f"{room.member_count} people · {room.sub}"
        self.query_one("#subline", Static).update(Text(sanitize_line(text)))

    # ------------------------------------------------------------------ navigation

    def on_tree_node_highlighted(self, _event: Tree.NodeHighlighted[state.SidebarRow]) -> None:
        self._update_subline()

    def on_tree_node_selected(self, event: Tree.NodeSelected[state.SidebarRow]) -> None:
        row = event.node.data
        if row is None:
            return
        if row.kind == "room" and row.room_id:
            self.manual[row.room_id] = not row.expanded
            self.refresh_chrome()
        elif row.kind == "more" and row.room_id:
            self.show_all.add(row.room_id)
            self.refresh_chrome()
        elif (conv := row.conversation) is not None:
            self.open_conversation(conv)

    def on_sidebar_room_toggled(self, event: Sidebar.RoomToggled) -> None:
        self.manual[event.room_id] = event.expanded
        self.refresh_chrome()

    def open_conversation(self, conv: state.Conversation) -> None:
        """Switch the right pane. Leaving a topic first publishes what was read of it."""
        if conv == self.current:
            return
        self._note_viewed()
        self._run("publish")
        self.current = conv
        self.store = state.MessageStore()
        self.loaded_topic = False
        self._topic_error = None
        # A new subject for the topic loop: cancel its timer, and let a request still in
        # flight for the old topic land as stale rather than block or overwrite this one.
        topic = self._loops["topic"]
        topic.stop_timer()
        topic.failures = 0
        topic.generation += 1
        if conv.room_id is not None:
            self.manual.setdefault(conv.room_id, True)
        if conv.is_pepper:
            self.dm.dot = False
            self._render_dm(new_rows=0, reset=True)
        else:
            self._render_topic(new_rows=0, reset=True)
            self._run("topic")
        self.refresh_chrome()

    def _jump_unread(self, step: int) -> None:
        target = state.next_unread(
            self.rooms, self.marks, current=self.current, pepper_dot=self.dm.dot, step=step
        )
        if target is None:
            self.notify("Nothing unread.", timeout=2)
            return
        self.open_conversation(target)
        key = "pepper" if target.is_pepper else f"topic:{target.topic_id}"
        self.sidebar.select_key(key)

    def action_next_unread(self) -> None:
        self._jump_unread(1)

    def action_previous_unread(self) -> None:
        self._jump_unread(-1)

    def action_toggle_sidebar(self) -> None:
        self.query_one("#left").toggle_class("hidden")

    def action_page_up(self) -> None:
        self.thread.scroll_page(down=False)

    def action_page_down(self) -> None:
        self.thread.scroll_page(down=True)

    def action_quit_app(self) -> None:
        self.exit(return_code=0)

    # ------------------------------------------------------------------ read marks

    def _note_viewed(self) -> None:
        """The open topic counts as read through its newest row; publish that mark."""
        conv = self.current
        if conv.is_pepper or conv.topic_id is None:
            return
        newest = self.store.newest_created_at()
        stamp = state.parse_ts(newest)
        if newest is None or stamp is None:
            return
        self.marks.merge(conv.topic_id, stamp)
        owed = self._owed.get(conv.topic_id)
        if owed is None or (state.parse_ts(owed[1]) or stamp) < stamp:
            self._owed[conv.topic_id] = (conv, newest)

    def _send_next_mark(self) -> None:
        """Send the oldest owed mark (one PUT in flight at a time, across all topics).

        The queue is independent of the open conversation: a failed PUT is retried by the
        publish loop's own backoff timer, and a mark that failed goes to the back of the
        queue so one bad topic cannot hold up the others.
        """
        for topic_id, (conv, through) in list(self._owed.items()):
            done = state.parse_ts(self._published.get(topic_id))
            if done is not None and (state.parse_ts(through) or done) <= done:
                del self._owed[topic_id]
                continue
            self._start(
                "publish",
                lambda c=conv, t=through: poller.publish_read_mark(self._api, c, t),
                lambda kept, tid=topic_id, t=through: self._published_ok(tid, t, kept),
                on_error=lambda exc, tid=topic_id, t=through: self._publish_failed(tid, t, exc),
            )
            return
        loop = self._loops["publish"]
        if loop.failures:  # nothing left to retry, so nothing is failing
            loop.failures = 0
            loop.stop_timer()
            self.refresh_chrome()

    def _published_ok(self, topic_id: str, through: str, kept: str) -> None:
        self._published[topic_id] = through
        self.marks.merge(topic_id, state.parse_ts(kept))
        owed = self._owed.get(topic_id)
        if owed is not None and owed[1] == through:
            del self._owed[topic_id]
        self._run("publish")

    def _publish_failed(self, topic_id: str, through: str, exc: Exception) -> bool:
        """Drop a mark the server will never accept (True); requeue any other at the back."""
        owed = self._owed.pop(topic_id, None)
        if isinstance(exc, ApiError) and exc.status in _PUBLISH_PERMANENT:
            if owed is not None and owed[1] != through:
                self._owed[topic_id] = owed  # a newer mark arrived meanwhile; let it try
            return True
        if owed is not None:
            self._owed[topic_id] = owed
        return False
