"""The full-screen app: layout, key bindings, and the scheduling around the poll functions.

Everything that talks to the network runs on a worker thread (``pepper_cli.tui.poller``) and
hands plain data back through ``call_from_thread``; the shared state (read marks, the open
topic's rows, the sidebar rows) is only touched on the UI thread. One request per loop is ever
in flight: the next timer is armed when the previous result (or failure) has been applied.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, ClassVar

import httpx
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
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
#: What a poll can fail with and still be worth retrying. Anything else is a bug and should
#: surface as a traceback rather than as an endless "offline" banner.
_RETRYABLE = (ApiError, httpx.RequestError, ValueError, KeyError, TypeError)


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
        self._failures = {"rail": 0, "topic": 0, "dm": 0}
        self._topic_error: str | None = None
        self._loop_timers: dict[str, Timer] = {}
        self._generation = {"rail": 0, "topic": 0, "dm": 0}
        self._last_input = time.monotonic()
        self._blurred = False
        self._published: dict[str, str] = {}
        self._owed: dict[str, tuple[state.Conversation, str]] = {}
        self._publishing = False

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
        self._start("startup", self._load_identity, self._on_identity)

    # ------------------------------------------------------------------ workers

    def _start[T](
        self,
        kind: str,
        work: Callable[[], T],
        deliver: Callable[[T], None],
        *,
        generation: int | None = None,
    ) -> None:
        """Run ``work`` on a thread; apply its result (or failure) on the UI thread."""

        def run() -> None:
            try:
                result = work()
            except _RETRYABLE as exc:
                self.call_from_thread(self._failed, kind, exc, generation)
            else:
                self.call_from_thread(self._delivered, kind, deliver, result, generation)

        self.run_worker(run, thread=True, group=kind, exit_on_error=True)

    def _stale(self, kind: str, generation: int | None) -> bool:
        return generation is not None and generation != self._generation.get(kind)

    def _delivered[T](
        self,
        kind: str,
        deliver: Callable[[T], None],
        result: T,
        generation: int | None,
    ) -> None:
        if self.fatal or self._stale(kind, generation):
            return
        if kind in self._failures:
            self._failures[kind] = 0
            if kind == "topic":
                self._topic_error = None
        deliver(result)
        self.refresh_chrome()
        if kind in self._failures:
            self._schedule(kind)

    def _failed(self, kind: str, exc: Exception, generation: int | None) -> None:
        if self.fatal or self._stale(kind, generation):
            return
        if isinstance(exc, ApiError) and exc.status == 401:
            self.fail_fatal(LOGIN_HINT)
            return
        if isinstance(exc, UpgradeRequiredError):
            self.fail_fatal(str(exc))
            return
        if kind == "publish":
            self._publishing = False  # the mark stays owed; the next kick retries it
            return
        if kind == "startup":
            return
        self._failures[kind] += 1
        if kind == "topic" and isinstance(exc, ApiError):
            self._topic_error = "You can't open this topic." if exc.status in (403, 404) else None
        self.refresh_chrome()
        self._schedule(kind)

    def fail_fatal(self, message: str) -> None:
        """Stop every loop and wait for a keypress, then exit 1 (§10)."""
        self.fatal = message
        for timer in self._loop_timers.values():
            timer.stop()
        self._loop_timers.clear()
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
        self._blurred = True

    def on_app_focus(self, _event: events.AppFocus) -> None:
        self._blurred = False
        self._last_input = time.monotonic()
        self._poll_now()

    # ------------------------------------------------------------------ scheduling

    def _interval(self, kind: str) -> float | None:
        if self._failures[kind]:
            return state.backoff_delay(self._failures[kind])
        if kind == "rail":
            return state.RAIL_INTERVAL
        if kind == "dm":
            return state.DM_INTERVAL
        return state.poll_interval(time.monotonic() - self._last_input, hidden=self._blurred)

    def _schedule(self, kind: str) -> None:
        """Arm the next run of one loop. Open-topic polling is skipped while blurred (it is
        restarted by focus); the other two keep their slow cadence."""
        if (old := self._loop_timers.pop(kind, None)) is not None:
            old.stop()
        if self.fatal:
            return
        if kind == "topic" and self.current.is_pepper:
            return
        delay = self._interval(kind)
        if delay is None:
            return
        self._loop_timers[kind] = self.set_timer(delay, lambda: self._run(kind))

    def _run(self, kind: str) -> None:
        if self.fatal:
            return
        generation = self._generation[kind]
        if kind == "rail":
            self._start(kind, self._rail.step, self._on_rail, generation=generation)
        elif kind == "dm":
            self._start(
                kind, lambda: poller.fetch_dm(self._api), self._on_dm, generation=generation
            )
        elif not self.current.is_pepper:
            conv = self.current
            reconcile = self.store.next_is_reconcile()
            since = None if reconcile else self.store.since()
            self._start(
                kind,
                lambda: poller.fetch_topic(self._api, conv, since=since),
                self._on_topic,
                generation=generation,
            )

    def _poll_now(self) -> None:
        for kind in ("rail", "dm", "topic"):
            if self._failures[kind] == 0 and not self.fatal:
                if (timer := self._loop_timers.pop(kind, None)) is not None:
                    timer.stop()
                self._generation[kind] += 1
                self._run(kind)

    # ------------------------------------------------------------------ startup

    def _load_identity(self) -> tuple[str | None, bool]:
        who = self._api.whoami()
        return who.user_id, who.time_format == "24h"

    def _on_identity(self, result: tuple[str | None, bool]) -> None:
        self.you_id, self.hour24 = result
        self._run("rail")
        self._run("dm")

    # ------------------------------------------------------------------ results

    def _on_rail(self, update: poller.RailUpdate) -> None:
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
        self._note_viewed()
        self._kick_publish()

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
        if any(self._failures.values()):
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
        if not self.query(Sidebar):  # a highlight event can outlive the screen at shutdown
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
        self._kick_publish()
        self.current = conv
        self.store = state.MessageStore()
        self.loaded_topic = False
        self._topic_error = None
        self._failures["topic"] = 0
        self._generation["topic"] += 1
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

    def _kick_publish(self) -> None:
        """Send at most one PUT; the owed mark stays queued until a PUT for it succeeds."""
        if self._publishing or self.fatal:
            return
        for topic_id, (conv, through) in list(self._owed.items()):
            done = state.parse_ts(self._published.get(topic_id))
            if done is not None and (state.parse_ts(through) or done) <= done:
                del self._owed[topic_id]
                continue
            self._publishing = True
            self._start(
                "publish",
                lambda c=conv, t=through: poller.publish_read_mark(self._api, c, t),
                lambda kept, tid=topic_id, t=through: self._published_ok(tid, t, kept),
            )
            return

    def _published_ok(self, topic_id: str, through: str, kept: str) -> None:
        self._publishing = False
        self._published[topic_id] = through
        self.marks.merge(topic_id, state.parse_ts(kept))
        owed = self._owed.get(topic_id)
        if owed is not None and owed[1] == through:
            del self._owed[topic_id]
        self._kick_publish()
