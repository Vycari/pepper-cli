"""The full-screen app: layout, key bindings, and the scheduling around the poll functions.

Everything that talks to the network runs on a worker thread (``pepper_cli.tui.poller``) and
hands plain data back through ``call_from_thread``; the shared state (read marks, the open
topic's rows, the sidebar rows, the rail's ETag) is only touched on the UI thread.

There are five poll loops: ``startup`` (whoami, until it succeeds), ``rail``, ``dm``, ``topic``
(the open topic) and ``publish`` (owed read marks). Posting adds three more that run only on
demand: ``send`` (one room POST at a time, oldest first), ``roster`` (the open room's ``@``
list) and ``turn`` (the streamed Pepper turn). Each is a :class:`_Loop`, and the rules are the
same for all of them:

- At most one request per loop is in flight. ``_run`` is a no-op while one is, so a timer, a
  focus return or a key press can never start a second one.
- A loop's ``generation`` changes only when what it is about changes (switching topics). A
  result from an older generation is dropped. Nothing a worker returns has been committed
  yet, so dropping it loses nothing: the rail's ETag is adopted only when its result is
  applied, and a topic's cursor moves only when its rows are merged.
- Success resets the failure count and arms the next timer at the normal cadence; failure
  counts up and arms the next timer at the backoff delay. A 401 or 426 stops everything.

The ``turn`` loop is the exception to "runs on a Textual worker". A streamed turn can sit in
one read for minutes, and Textual's thread workers run on asyncio's default executor, which
``asyncio.run`` joins on exit: quitting mid-turn would hang until the read timed out. So the
turn runs on its own daemon thread, posts back through :meth:`PepperApp._post` (which drops
everything once the app is closing), and is simply abandoned on quit. It never resends.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

import httpx
from rich.console import Group
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult, ScreenStackError
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Static

from pepper_cli import turn as turns
from pepper_cli.api_client import ApiError, UpgradeRequiredError
from pepper_cli.terminal_text import sanitize_line
from pepper_cli.tui import poller, rows, state
from pepper_cli.tui.composer import Composer, MentionMenu
from pepper_cli.tui.screens import (
    ConfirmScreen,
    InfoScreen,
    JumpScreen,
    PickerScreen,
    PromptScreen,
    room_info_text,
)
from pepper_cli.tui.sidebar import Sidebar
from pepper_cli.tui.thread import ThreadScroll, ThreadView

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.driver import Driver
    from textual.timer import Timer
    from textual.widgets import Tree

    from pepper_cli.api_client import (
        ApiClient,
        ChatHistoryEntry,
        GroupList,
        GroupMessage,
        GroupMessagePage,
        GroupRosterMember,
        GroupSummary,
        GroupTopicSummary,
    )
    from pepper_cli.provider.fs import FilesystemProvider

LOGIN_HINT = "Authentication failed (401). Run 'pepper login' to refresh your token."
#: What a request can fail with and be handled by ``_failed``: retried with backoff, or (401,
#: 426) shown as a fatal notice. Anything else is a bug and should surface as a traceback
#: rather than as an endless "offline" banner.
_EXPECTED = (ApiError, UpgradeRequiredError, httpx.RequestError, ValueError, KeyError, TypeError)
#: Loops whose failures put "offline, retrying" in the header.
_LOOPS = ("startup", "rail", "dm", "topic", "publish")
#: On-demand loops. A failed send is not retried by itself (it waits for ``r``), so it does not
#: say "retrying"; the roster retries quietly; the turn reports in the status line.
#: P3 adds one-at-a-time requests started by a key: an older page, a reaction, an edit or
#: delete (``mutate``), a new topic, the room-info card, and a room's full topic rail.
_ON_DEMAND = (
    "send",
    "roster",
    "turn",
    "older",
    "react",
    "mutate",
    "newtopic",
    "info",
    "topics",
    "mentions",
)
#: Transport failures that mean a turn's request never reached the server, so it is safe to
#: give the text back for the user to send again.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout)
#: Client errors a read-mark PUT will keep getting however often it is retried (no access to
#: the topic any more, the topic is gone, the request is refused). The mark is dropped.
_PUBLISH_PERMANENT = frozenset({400, 403, 404, 409, 410, 422})


def utcnow() -> datetime:
    """The wall clock, in one place so a test can replace it (or pass ``now=`` to the app)."""
    return datetime.now(UTC)


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
    """Terminal client for Pepper's messages: rooms, topics and the Pepper DM."""

    CSS_PATH = "styles.tcss"
    TITLE = "Pepper"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+n", "next_unread", "Next unread"),
        Binding("ctrl+p", "previous_unread", "Prev unread"),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
        Binding("pageup", "page_up", "Page up", show=False),
        Binding("pagedown", "page_down", "Page down", show=False),
        Binding("slash", "focus_composer", "Compose", show=False),
        # Priority: a focused TextArea would otherwise take Ctrl+K as "delete to line end".
        Binding("ctrl+k", "jump", "Jump", priority=True),
        Binding("i", "room_info", "Room info"),
        Binding("ctrl+q", "quit_app", "Quit", priority=True),
    ]

    def get_driver_class(self) -> type[Driver]:
        """Textual's driver, except under macOS Terminal.app.

        Terminal.app answers the in-band resize query (mode 2048), which Textual takes as
        leave to switch the mouse to pixel coordinates (mode 1016). Terminal.app ignores 1016
        and keeps reporting cells, so Textual divides cell numbers by the cell's pixel size and
        every click lands near the top-left corner: the composer cannot be clicked. Skipping
        the query keeps the mouse in cells; resizes still arrive by SIGWINCH.
        """
        driver = super().get_driver_class()
        if os.environ.get("TERM_PROGRAM") != "Apple_Terminal":
            return driver
        from textual.drivers.linux_driver import LinuxDriver  # imports termios: POSIX only

        if driver is not LinuxDriver:
            return driver

        class CellMouseDriver(LinuxDriver):
            def _query_in_band_window_resize(self) -> None:
                pass

        return CellMouseDriver

    def __init__(
        self,
        api: ApiClient,
        *,
        provider: FilesystemProvider | None = None,
        open_url: Callable[[str], object] = webbrowser.open,
        now: Callable[[], datetime] | None = None,
        bell: bool = False,
    ) -> None:
        super().__init__()
        self._api = api
        #: Ring the terminal bell when a mention is first found in another topic (§15 C,
        #: opt-in only).
        self._bell = bell
        #: Hands a link to the system browser (``o``); a test passes a recorder.
        self._open_url = open_url
        #: The clock for the "inactive rooms" fold.
        self._now = now or utcnow
        #: The ``--root`` filesystem toolset served to Pepper's client-tool calls, if any.
        self._provider = provider
        self._rail = poller.RailPoller(api)
        self.rooms: list[GroupSummary] = []
        self.groups_enabled = True
        self.groups_degraded = False
        self.marks = state.ReadMarks()
        #: Unread topics that mention you, found one fetch at a time (§15 C).
        self.mentions = state.Mentions()
        self.current = state.PEPPER
        self.store = state.MessageStore()
        self.dm = state.DmTracker()
        self.manual: dict[str, bool] = {}
        self.show_all: set[str] = set()
        #: Which archived/inactive folds are open, and the topic rails read for the archived ones.
        self.folds = state.SidebarFolds()
        #: Rooms opened while another room's topic rail was being read; read next, in order.
        self._topics_waiting: list[str] = []
        self._topics_reading: str | None = None
        #: The message being edited in the composer: ``(conversation, message id)``.
        self.editing: tuple[state.Conversation, str] | None = None
        #: Your groups person id: what ``mentions`` and ``author_person_id`` carry. Not the
        #: core user id (``whoami().user_id``), which never appears in a group payload.
        self.you_person_id: str | None = None
        self.hour24 = False
        self.loaded_topic = False
        self.fatal: str | None = None
        self._loops = {kind: _Loop() for kind in _LOOPS + _ON_DEMAND}
        self._topic_error: str | None = None
        self._last_input = time.monotonic()
        self._blurred = False
        #: ``topic_id -> read_through`` the server has acknowledged.
        self._published: dict[str, str] = {}
        self._started = False
        #: Marks still to send, oldest first: ``topic_id -> (conversation, read_through)``.
        self._owed: dict[str, tuple[state.Conversation, str]] = {}
        #: Room messages being posted, from Enter until a poll returns them.
        self.pending = state.PendingSends()
        #: What the composer held for each destination, and the message it quotes.
        self.drafts: dict[state.Conversation, state.Draft] = {}
        #: ``room_id -> roster`` for ``@`` mentions, fetched when a room is first opened.
        self.rosters: dict[str, list[GroupRosterMember]] = {}
        #: The selected thread row (``msg:<id>`` or ``pending:<key>``), the target of ``r``.
        self.selected: str | None = None
        #: Pepper turns, oldest first. At most one is streaming or being checked at a time.
        self.turns: list[state.DmTurn] = []
        #: Every history id a reply frame has named this session. Each new turn's snapshot
        #: holds them, so a dropped turn never takes an earlier turn's reply for its own,
        #: even once that turn is retired before a history read has caught up with it.
        self._answered_ids: set[str] = set()
        #: The DM status line when no turn is running (an error, "no reply yet").
        self.dm_note = ""
        #: Set once quitting starts; workers and the turn thread then post nothing back.
        self._quitting = False

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        with Vertical(id="left"):
            yield Sidebar()
            yield Static("", id="subline")
        with Vertical(id="right"):
            yield Static("Pepper · Today", id="header")
            yield Static("", id="notice", classes="empty")
            yield ThreadView()
            with Vertical(id="compose-box"):
                yield Static("", id="dm-status", classes="empty")
                yield Static("", id="quote", classes="empty")
                menu = MentionMenu()
                yield menu
                yield Composer(menu, key_hook=self._composer_key)
        yield Footer()

    @property
    def sidebar(self) -> Sidebar:
        return self.query_one(Sidebar)

    @property
    def thread(self) -> ThreadView:
        return self.query_one(ThreadView)

    @property
    def composer(self) -> Composer:
        return self.query_one(Composer)

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
                self._post(self._failed, kind, exc, generation, on_error)
            else:
                self._post(self._delivered, kind, deliver, result, generation)

        self.run_worker(run, thread=True, group=kind, exit_on_error=True)

    def _post(self, callback: Callable[..., object], *args: Any) -> None:
        """Run ``callback`` on the UI thread, from a worker; dropped once the app is closing.

        ``call_from_thread`` raises once the app's event loop is gone, and a worker (above
        all the turn thread) can outlive it. A result that arrives after quit has nowhere
        to go and nothing waiting on it, so it is dropped rather than raised.
        """
        if self._quitting:
            return
        with contextlib.suppress(RuntimeError):
            self.call_from_thread(callback, *args)

    def exit(self, *args: Any, **kwargs: Any) -> None:
        self._quitting = True
        super().exit(*args, **kwargs)

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

    def _composer_key(self, _event: events.Key) -> bool:
        """Every key the composer gets: counts as activity; in the fatal state, exits."""
        self._last_input = time.monotonic()
        if self.fatal:
            self.exit(return_code=1)
            return True
        return False

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
            if any(turn.phase == "checking" for turn in self.turns):
                return state.DM_RECONCILE_INTERVAL
            return state.DM_INTERVAL
        if kind == "topic":
            return state.poll_interval(time.monotonic() - self._last_input, hidden=self._blurred)
        # startup runs once; publish and send run when there is something to send; the
        # roster is fetched when a room opens; the turn runs when one is composed.
        return None

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
            if self._loops["turn"].busy:
                return  # paused while a turn streams; the turn's end polls (see _turn_ended)
            self._start(kind, lambda: poller.fetch_dm(self._api), self._on_dm)
        elif kind == "publish":
            self._send_next_mark()
        elif kind == "send":
            self._send_next()
        elif kind == "roster":
            self._fetch_roster()
        elif kind == "mentions":
            self._check_mentions()
        elif kind == "turn":
            self._start_turn()
        elif kind == "topic" and not self.current.is_pepper:
            conv = self.current
            reconcile = self.store.next_is_reconcile()
            since = None if reconcile else self.store.since()
            self._start(
                kind,
                lambda: poller.fetch_topic(self._api, conv, since=since),
                lambda page: self._on_topic_page(page, newest=since is None),
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
        return who.person_id, who.time_format == "24h"

    def _on_identity(self, result: tuple[str | None, bool]) -> None:
        person_id, self.hour24 = result
        if person_id:
            self._learn_person_id(person_id)
        self._started = True
        self._run("rail")
        self._run("dm")

    # ------------------------------------------------------------------ results

    def _learn_person_id(self, person_id: str) -> None:
        """Keep your person id for the session; topics asked about before it was known are
        asked again."""
        if self.you_person_id is not None:
            return
        self.you_person_id = person_id
        self.mentions.checked.clear()
        if self._started:
            self._run("mentions")

    def _learn_from_messages(self, messages: list[GroupMessage]) -> None:
        """Until whoami carries ``person_id``: your own message names you."""
        if self.you_person_id is not None:
            return
        for m in messages:
            if m.is_you and m.author_person_id:
                self._learn_person_id(m.author_person_id)
                return

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
        # A failing mention loop keeps its backoff: new room activity waits for the retry.
        mentions = self._loops["mentions"]
        if mentions.failures == 0:
            self._run("mentions")
        elif mentions.timer is None and not mentions.busy:
            self._schedule("mentions")

    def _check_mentions(self) -> None:
        """Read the newest page of the next unread topic not yet asked about (one in flight).

        Each delivery asks again, so a burst of unread topics is worked off one request at a
        time; a topic is asked about once per new activity (``Mentions.due``).
        """
        if self.you_person_id is None:
            return
        due = self.mentions.due(self.rooms, self.marks, open_topic_id=self.current.topic_id)
        if not due:
            return
        room_id, topic = due[0]
        conv = state.Conversation(room_id, topic.topic_id)
        self._start(
            "mentions",
            lambda: poller.fetch_topic(self._api, conv, since=None),
            lambda page: self._mentions_checked(topic, page),
            on_error=lambda exc: self._mentions_failed(topic, exc),
        )

    def _mentions_checked(self, topic: GroupTopicSummary, page: GroupMessagePage) -> None:
        found = self.mentions.record(
            topic,
            page.messages,
            you_id=self.you_person_id,
            mark=self.marks.marks.get(topic.topic_id),
        )
        # A topic opened while this check was in flight has not raised its read mark yet
        # (its page is still loading), so ``record`` can call it new: no bell for it.
        if found and self._bell and topic.topic_id != self.current.topic_id:
            self.bell()
        self._run("mentions")

    def _mentions_failed(self, topic: GroupTopicSummary, exc: Exception) -> bool:
        """A topic this token cannot read is dropped; anything else retries with backoff,
        behind the other due topics so one failing topic does not hold them up."""
        if isinstance(exc, ApiError) and exc.status in _PUBLISH_PERMANENT:
            self.mentions.skip(topic)
            return True
        self.mentions.defer(topic)
        return False

    def _on_dm(self, history: list[ChatHistoryEntry]) -> None:
        new_ids = self.dm.update(history, is_open=self.current.is_pepper)
        self._settle_turns()
        if self.current.is_pepper:
            self._render_dm(new_rows=len(new_ids), reset=False)

    def _on_topic_page(self, page: GroupMessagePage, *, newest: bool) -> None:
        """A poll's page. Only a read of the newest page says where the older ones start."""
        if newest:
            self.store.note_newest_page(page.next_before)
        self._on_topic(page.messages)

    def _on_topic(self, messages: list[GroupMessage]) -> None:
        self._learn_from_messages(messages)
        new_ids = self.store.add(messages)
        # A send the poll has now returned is the store's row from here on; a selection on
        # its pending row is dropped with it.
        for key in self.pending.confirm(self.current, self.store.rows):
            if self.selected == f"pending:{key}":
                self.selected = None
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
            rows.dm_renderable(
                self.dm.ordered(), hour24=self.hour24, provisional=self._provisional()
            ),
            new_rows=new_rows,
            reset=reset,
        )
        self._paint_dm_status()

    def _provisional(self) -> list[tuple[str, str, str]]:
        """The turns of this session that today's history does not show yet."""
        out: list[tuple[str, str, str]] = []
        entries = self.dm.entries
        for turn in self.turns:
            if not turn.user_landed(entries):
                label = {"queued": "waiting to send", "streaming": "sending…"}.get(turn.phase, "")
                out.append(("user", turn.text, label))
            if turn.reply is not None and not turn.reply_landed(entries):
                out.append(("assistant", turn.reply, ""))
        return out

    def _topic_items(self) -> list[tuple[str | None, Any]]:
        return rows.thread_items(
            self.store.ordered(),
            you_id=self.you_person_id,
            pending=self.pending.in_conversation(self.current),
            selected=self.selected,
        )

    def _render_topic(self, *, new_rows: int, reset: bool, prepended: bool = False) -> None:
        if not self.loaded_topic:
            self.thread.show(Text("Loading…", style="dim"), reset=True)
            return
        items = self._topic_items()
        body = Group(*(r for _, r in items)) if items else Text("(no messages)", style="dim")
        self.thread.show(body, new_rows=new_rows, reset=reset, prepended=prepended)

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
                folds=self.folds,
                now=self._now(),
                mentions=self.mentions,
            ),
            current_key=current_key,
        )
        header = sanitize_line(state.header_text(self.current, shown))
        if any(self._loops[kind].failures for kind in _LOOPS):
            header += "  · offline, retrying"
        self.query_one("#header", Static).update(Text(header))
        notice = self._notice()
        box = self.query_one("#notice", Static)
        box.update(Text(notice))
        box.set_class(not notice, "empty")
        self._update_subline()
        unread = state.any_unread(shown, self.marks, current=self.current, pepper_dot=self.dm.dot)
        mention = state.any_mention(shown, self.marks, self.mentions, current=self.current)
        self.title = ("@ " if mention else "● " if unread else "") + "Pepper"

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
            if not row.expanded:
                self._ensure_topics(row.room_id)
            self.refresh_chrome()
        elif row.kind == "more" and row.room_id:
            self.show_all.add(row.room_id)
            self.refresh_chrome()
        elif row.kind == "archived" and row.room_id:
            archived = set(self.folds.archived) ^ {row.room_id}
            self.folds = replace(self.folds, archived=frozenset(archived))
            self.refresh_chrome()
        elif row.kind == "inactive":
            self.folds = replace(self.folds, inactive=not self.folds.inactive)
            self.refresh_chrome()
        elif row.kind == "newtopic" and row.room_id:
            self._prompt_topic(row.room_id)
        elif (conv := row.conversation) is not None:
            self.open_conversation(conv)

    def on_sidebar_room_toggled(self, event: Sidebar.RoomToggled) -> None:
        self.manual[event.room_id] = event.expanded
        if event.expanded:
            self._ensure_topics(event.room_id)
        self.refresh_chrome()

    def open_conversation(self, conv: state.Conversation) -> None:
        """Switch the right pane. Leaving a topic first publishes what was read of it.

        The composer's text is kept per destination; a send still in flight for the topic
        being left keeps going, and its row is there when the topic is opened again.
        """
        if conv == self.current:
            return
        self._note_viewed()
        self._run("publish")
        self._end_edit()
        self._draft().text = self.composer.text
        self.current = conv
        self.selected = None
        self._load_draft()
        self.store = state.MessageStore()
        self.loaded_topic = False
        self._topic_error = None
        # A new subject for the topic loop: cancel its timer, and let a request still in
        # flight for the old topic land as stale rather than block or overwrite this one.
        topic = self._loops["topic"]
        topic.stop_timer()
        topic.failures = 0
        topic.generation += 1
        self._loops["older"].generation += 1
        if conv.room_id is not None:
            self.manual.setdefault(conv.room_id, True)
            self._ensure_topics(conv.room_id)
        if conv.is_pepper:
            self.dm.dot = False
            self._render_dm(new_rows=0, reset=True)
        else:
            self._render_topic(new_rows=0, reset=True)
            self._run("topic")
            self._run("roster")
        self._paint_dm_status()
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

    def action_focus_composer(self) -> None:
        self.composer.focus()

    def action_jump(self) -> None:
        """``Ctrl+K``: fuzzy-pick a room or topic to open."""
        if self.fatal or isinstance(self.screen, ModalScreen):
            return
        shown = self.rooms if self.groups_enabled else []
        self.push_screen(JumpScreen(state.jump_targets(shown)), self._jumped)

    def _jumped(self, conv: state.Conversation | None) -> None:
        if conv is None:
            return
        self.open_conversation(conv)
        self.sidebar.select_key("pepper" if conv.is_pepper else f"topic:{conv.topic_id}")

    def action_room_info(self) -> None:
        """``i``: members, topics and apps of the room under the cursor (or the open one)."""
        if isinstance(self.screen, ModalScreen):
            return
        row = self.sidebar.highlighted_row if self.sidebar.has_focus else None
        room_id = (row.room_id if row is not None else None) or self.current.room_id
        if room_id is None:
            self.notify("Open a room first.", timeout=2)
            return
        if self._loops["info"].busy:
            return
        self._start(
            "info",
            lambda: poller.fetch_room_info(self._api, room_id),
            self._show_info,
            on_error=lambda exc: self._action_failed("load the room info", exc),
        )

    def _show_info(self, info: dict[str, Any]) -> None:
        self.push_screen(InfoScreen(room_info_text(info)))

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
        self._owe(conv, newest)

    def _owe(self, conv: state.Conversation, through: str) -> None:
        """Queue ``through`` as ``conv``'s read mark unless a later one is already queued."""
        stamp = state.parse_ts(through)
        if conv.topic_id is None or stamp is None:
            return
        owed = self._owed.get(conv.topic_id)
        if owed is None or (state.parse_ts(owed[1]) or stamp) < stamp:
            self._owed[conv.topic_id] = (conv, through)

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

    # ------------------------------------------------------------------ composer and drafts

    def _draft(self) -> state.Draft:
        return self.drafts.setdefault(self.current, state.Draft())

    def _load_draft(self) -> None:
        """Put the open conversation's draft (text, quote, roster) into the composer."""
        composer = self.composer
        composer.load_text(self._draft().text)
        composer.move_cursor(composer.document.end)
        room = self.current.room_id
        composer.roster = self.rosters.get(room, []) if room else []
        composer.menu.close()
        self._paint_quote()

    def _paint_quote(self) -> None:
        draft = self._draft()
        box = self.query_one("#quote", Static)
        if self.editing is not None:
            line = Text("✎ Editing your message", style="bold")
            line.append("  (Enter saves · Esc cancels)", style="dim italic")
            box.update(line)
            box.set_class(False, "empty")
            return
        if draft.quote_id is None:
            box.update("")
            box.set_class(True, "empty")
            return
        line = Text("┃ Replying to ", style="dim")
        line.append(sanitize_line(draft.quote_name) or "message", style="bold")
        line.append(": " + sanitize_line(draft.quote_text), style="dim")
        line.append("  (Esc cancels)", style="dim italic")
        box.update(line)
        box.set_class(False, "empty")

    def _paint_dm_status(self) -> None:
        """The dim line above the composer: what the running turn is doing (DM only)."""
        text = ""
        if self.current.is_pepper:
            active = self._active_turn()
            text = active.status if active is not None and active.status else self.dm_note
        box = self.query_one("#dm-status", Static)
        box.update(Text(sanitize_line(text), style="dim italic"))
        box.set_class(not text, "empty")

    def on_composer_submitted(self, event: Composer.Submitted) -> None:
        text = event.text
        if not text.strip():
            return
        if text.strip() == "/quit":
            self.action_quit_app()
            return
        if self.fatal:
            return
        self._last_input = time.monotonic()
        if self.editing is not None:
            self._submit_edit(text)
            return
        if self.current.is_pepper:
            self._send_turn(text)
        else:
            self._send_room(text)
        draft = self._draft()
        draft.text, draft.quote_id, draft.quote_name, draft.quote_text = "", None, "", ""
        self.composer.load_text("")
        self._paint_quote()

    def on_composer_escaped(self, _event: Composer.Escaped) -> None:
        """Esc cancels an edit, else a quote; the next Esc leaves the composer for the thread."""
        if self.editing is not None:
            self._end_edit()
            return
        draft = self._draft()
        if draft.quote_id is not None:
            draft.quote_id, draft.quote_name, draft.quote_text = None, "", ""
            self._paint_quote()
            return
        self.thread.focus_scroller()

    # ------------------------------------------------------------------ selection

    def _selectable(self) -> list[str]:
        if self.current.is_pepper:
            return []
        return [key for key, _ in self._topic_items() if key is not None]

    def on_thread_scroll_select(self, event: ThreadScroll.Select) -> None:
        keys = self._selectable()
        if not keys:
            return
        if self.selected not in keys:
            index = len(keys) - 1  # the first ↑ or ↓ selects the newest row
        else:
            index = max(0, min(len(keys) - 1, keys.index(self.selected) + event.step))
        self.selected = keys[index]
        self._render_topic(new_rows=0, reset=False)
        self._reveal_selected()
        if index == 0 and event.step < 0:
            self._load_older()  # ↑ past the first row asks for the page before it

    def on_thread_scroll_unselect(self, _event: ThreadScroll.Unselect) -> None:
        if self.selected is not None:
            self.selected = None
            self._render_topic(new_rows=0, reset=False)

    def _reveal_selected(self) -> None:
        """Scroll so the selected row is on screen, measuring the rows rendered above it."""
        items = self._topic_items()
        keys = [key for key, _ in items]
        if self.selected not in keys:
            return
        index = keys.index(self.selected)
        options = self.console.options.update_width(self.thread.body_width())

        def height(parts: list[Any]) -> int:
            return len(self.console.render_lines(Group(*parts), options, pad=False))

        top = height([r for _, r in items[:index]]) if index else 0
        self.thread.reveal_lines(top, top + height([items[index][1]]))

    def on_thread_scroll_reply(self, _event: ThreadScroll.Reply) -> None:
        """``r``: retry a failed send, or quote the selected message in the composer."""
        if self.current.is_pepper:
            self.notify("Quote-reply works in rooms.", timeout=2)
            return
        key = self.selected
        if key is None:
            self.notify("Select a message with ↑/↓ first.", timeout=2)
            return
        if key.startswith("pending:"):
            pending_key = key.removeprefix("pending:")
            if self.pending.retry(pending_key):
                self._render_topic(new_rows=0, reset=False)
                self._run("send")
            return
        message = self.store.rows.get(key.removeprefix("msg:"))
        if message is None or message.is_deleted or message.is_broken:
            return
        draft = self._draft()
        draft.quote_id = message.id
        draft.quote_name = "you" if message.is_you else message.display_name
        draft.quote_text = state.quote_snippet(message.raw_body)
        self._paint_quote()
        self.composer.focus()

    # ------------------------------------------------------------------ room sends (§6.4)

    def _send_room(self, text: str) -> None:
        conv = self.current
        draft = self._draft()
        roster = self.rosters.get(conv.room_id or "", [])
        self.pending.add(
            state.PendingSend(
                key=str(uuid.uuid4()),
                conversation=conv,
                body=text,
                mentions=state.mention_ids(text, roster),
                parent_id=draft.quote_id,
                quote_text=draft.quote_text,
            )
        )
        self._render_topic(new_rows=0, reset=True)
        self._run("send")

    def _send_next(self) -> None:
        """POST the oldest queued send. Its key goes with it, so a retry is the same send."""
        item = self.pending.next_queued()
        if item is None:
            return
        item.status = "sending"
        self._start(
            "send",
            lambda: poller.post_message(self._api, item),
            lambda message, key=item.key: self._sent(key, message),
            on_error=lambda exc, key=item.key: self._send_failed(key, exc),
        )

    def _sent(self, key: str, message: GroupMessage) -> None:
        """The server stored it. The row stays pending until a poll returns it; the topic
        counts as read through it, so an own message never lights its own topic."""
        item = self.pending.get(key)
        if item is not None:
            item.status, item.server_id, item.created_at = "sent", message.id, message.created_at
            conv = item.conversation
            if conv.topic_id is not None:
                self.marks.merge(conv.topic_id, state.parse_ts(message.created_at))
                self._owe(conv, message.created_at)
                self._run("publish")
            if conv == self.current:
                self.pending.confirm(conv, self.store.rows)
                self._last_input = time.monotonic()
                self._render_topic(new_rows=0, reset=False)
                self._run("topic")
        self._run("send")

    def _send_failed(self, key: str, exc: Exception) -> bool:
        """Mark the row failed (``r`` retries it with the same key) and go on to the next."""
        item = self.pending.get(key)
        if item is not None:
            item.status = "failed"
            item.error = _short_error(exc)
            if item.conversation == self.current:
                self._render_topic(new_rows=0, reset=False)
        return True

    # ------------------------------------------------------------------ roster

    def _fetch_roster(self) -> None:
        room = self.current.room_id
        if room is None or room in self.rosters:
            return
        self._start(
            "roster",
            lambda: poller.fetch_roster(self._api, room),
            lambda members: self._got_roster(room, members),
        )

    def _got_roster(self, room: str, members: list[GroupRosterMember]) -> None:
        self.rosters[room] = members
        if self.current.room_id == room:
            self.composer.roster = members
        # A room opened while this request ran was skipped (one roster request at a time):
        # fetch it now. A no-op when the open room's roster is already held.
        self._run("roster")

    # ------------------------------------------------------------------ P3: parity

    def _action_failed(self, what: str, exc: Exception) -> bool:
        """A key-started request failed: say so. Never retried by itself (False: back off)."""
        self.notify(f"Couldn't {what} ({_short_error(exc)}).", severity="error", timeout=4)
        return False

    def _selected_message(self) -> GroupMessage | None:
        """The selected sent message, or ``None`` after saying why not."""
        key = self.selected
        if self.current.is_pepper:
            self.notify("This works on room messages.", timeout=2)
        elif key is None:
            self.notify("Select a message with ↑/↓ first.", timeout=2)
        elif not key.startswith("msg:"):
            self.notify("That message hasn't been sent yet.", timeout=2)
        else:
            return self.store.rows.get(key.removeprefix("msg:"))
        return None

    def _own_message(self, verb: str) -> GroupMessage | None:
        message = self._selected_message()
        if message is not None and not state.is_own_editable(message):
            self.notify(f"You can only {verb} your own messages.", timeout=2)
            return None
        return message

    # reactions

    def on_thread_scroll_react(self, _event: ThreadScroll.React) -> None:
        message = self._selected_message()
        if message is None:
            return
        if message.is_deleted or message.is_broken:
            self.notify("Nothing to react to.", timeout=2)
            return
        mine = {c.get("emoji") for c in message.reactions if c.get("mine") is True}
        choices = [
            (e, e + ("   yours: picks it back" if e in mine else "")) for e in state.EMOJI_CHOICES
        ]
        self.push_screen(
            PickerScreen("React", choices), lambda emoji: self._react(message.id, emoji)
        )

    def _react(self, message_id: str, emoji: str | None) -> None:
        room = self.current.room_id
        message = self.store.rows.get(message_id)
        if emoji is None or room is None or message is None:
            return
        if self._loops["react"].busy:
            self.notify("Still sending the last reaction.", timeout=2)
            return
        clear = state.reaction_action(message.reactions, emoji) == "clear"
        self._start(
            "react",
            lambda: poller.react(self._api, room, message_id, emoji=emoji, clear=clear),
            lambda chips: self._reacted(message_id, chips),
            on_error=lambda exc: self._action_failed("react", exc),
        )

    def _reacted(self, message_id: str, chips: list[dict[str, Any]]) -> None:
        self.store.set_reactions(message_id, chips)
        self._render_topic(new_rows=0, reset=False)

    # edit and delete

    def on_thread_scroll_edit(self, _event: ThreadScroll.Edit) -> None:
        if (message := self._own_message("edit")) is None:
            return
        # One change at a time: Esc leaves edit mode while a save carries on, and a new edit
        # opened before its reply lands would be ended by that reply (``_mutated``), its
        # text lost. Wait for the save, which also starts the new edit from the saved text.
        if self.composer.read_only or self._loops["mutate"].busy:
            self.notify("Still saving the last change.", timeout=2)
            return
        draft = self._draft()
        draft.text = self.composer.text if self.editing is None else draft.text
        self.editing = (self.current, message.id)
        self.composer.load_text(message.raw_body)
        self.composer.move_cursor(self.composer.document.end)
        self._paint_quote()
        self.composer.focus()

    def _end_edit(self) -> None:
        """Leave edit mode and put the draft that was set aside back in the composer."""
        if self.editing is None:
            return
        self.editing = None
        self.composer.read_only = False
        self._load_draft()

    def _submit_edit(self, text: str) -> None:
        if self.editing is None:
            return
        conv, message_id = self.editing
        original = self.store.rows.get(message_id)
        if not text.strip() or (original is not None and text.strip() == original.raw_body.strip()):
            self._end_edit()  # nothing changed (an empty edit is not a delete)
            return
        if self._loops["mutate"].busy or conv.room_id is None:
            self.notify("Still saving the last change.", timeout=2)
            return
        room = conv.room_id
        # Hold the text still while it saves: whatever was typed meanwhile would be thrown
        # away when the reply ends edit mode. Esc still leaves (the save carries on).
        self.composer.read_only = True
        self._start(
            "mutate",
            lambda: poller.edit_message(self._api, room, message_id, body=text),
            lambda answer: self._mutated(message_id, answer, deleted=False),
            on_error=self._edit_failed,
        )

    def _edit_failed(self, exc: Exception) -> bool:
        """The edit didn't save: stay in edit mode with the text, editable again, to retry."""
        if self.editing is not None:
            self.composer.read_only = False
        return self._action_failed("save your edit", exc)

    def on_thread_scroll_delete(self, _event: ThreadScroll.Delete) -> None:
        if (message := self._own_message("delete")) is None:
            return
        snippet = state.quote_snippet(message.raw_body)
        self.push_screen(
            ConfirmScreen("Delete this message?", snippet),
            lambda yes: self._delete(message.id) if yes else None,
        )

    def _delete(self, message_id: str) -> None:
        room = self.current.room_id
        if room is None:
            return
        if self._loops["mutate"].busy:
            self.notify("Still saving the last change.", timeout=2)
            return
        self._start(
            "mutate",
            lambda: poller.delete_message(self._api, room, message_id),
            lambda answer: self._mutated(message_id, answer, deleted=True),
            on_error=lambda exc: self._action_failed("delete the message", exc),
        )

    def _mutated(self, message_id: str, answer: object, *, deleted: bool) -> None:
        """The server took an edit or delete: patch the row from its reply (it may be on an
        older page no poll re-reads), leave edit mode, and re-read the newest page."""
        reply = answer if isinstance(answer, dict) else {}
        if deleted:
            self.store.apply_delete(message_id, reply)
        else:
            self.store.apply_edit(message_id, reply)
            if self.editing is not None and self.editing[1] == message_id:
                self._end_edit()
        if not self.current.is_pepper:
            self._render_topic(new_rows=0, reset=False)
        self.store.expect_reconcile()
        self._run("topic")

    # open a link

    def on_thread_scroll_open(self, _event: ThreadScroll.Open) -> None:
        if (message := self._selected_message()) is None:
            return
        url = state.first_url(message, base=self._api.server_url)
        if url is None:
            self.notify("No link in this message.", timeout=2)
            return
        try:
            opened = bool(self._open_url(url))
        except webbrowser.Error:
            opened = False
        shown = sanitize_line(url)
        self.notify(
            f"Opened {shown}" if opened else f"Couldn't open a browser for {shown}",
            markup=False,
            timeout=3,
        )

    # load older

    def on_thread_view_at_top(self, _event: ThreadView.AtTop) -> None:
        self._load_older()

    def _load_older(self) -> None:
        conv, before = self.current, self.store.older
        if conv.is_pepper or before is None or self._loops["older"].busy:
            return
        self._start(
            "older",
            lambda: poller.fetch_older(self._api, conv, before=before, limit=state.OLDER_PAGE),
            self._got_older,
            on_error=lambda exc: self._action_failed("load older messages", exc),
        )

    def _got_older(self, page: GroupMessagePage) -> None:
        self._learn_from_messages(page.messages)
        added = self.store.add_older(page.messages, page.next_before)
        self._render_topic(new_rows=0, reset=False, prepended=bool(added))

    # new topic and the topic rail

    def _prompt_topic(self, room_id: str) -> None:
        self.push_screen(
            PromptScreen("New topic", placeholder="Topic name"),
            lambda name: self._create_topic(room_id, name),
        )

    def _create_topic(self, room_id: str, name: str | None) -> None:
        if not name:
            return
        if self._loops["newtopic"].busy:
            return
        self._start(
            "newtopic",
            lambda: poller.create_topic(self._api, room_id, name=name),
            lambda topic: self._topic_created(room_id, topic),
            on_error=lambda exc: self._action_failed("create the topic", exc),
        )

    def _topic_created(self, room_id: str, topic: dict[str, Any]) -> None:
        """Open the new topic at once; the sidebar lists it when the rail next answers."""
        self._rail.forget_etag()
        self._run("rail")
        topic_id = topic.get("topic_id") or topic.get("id")
        if isinstance(topic_id, str):
            self.manual[room_id] = True
            self.open_conversation(state.Conversation(room_id, topic_id))

    def _ensure_topics(self, room_id: str) -> None:
        """Read a room's whole topic rail once (archived topics included) when it is opened.

        One read at a time: a room opened while another's is in flight waits its turn.
        """
        if room_id in self.folds.fetched or not self._started:
            return
        if self._loops["topics"].busy:
            if room_id != self._topics_reading and room_id not in self._topics_waiting:
                self._topics_waiting.append(room_id)
            return
        self._topics_reading = room_id
        self._start(
            "topics",
            lambda: poller.fetch_topic_rail(self._api, room_id),
            lambda topics: self._got_topics(room_id, topics),
            on_error=self._topics_failed,
        )

    def _got_topics(self, room_id: str, topics: list[GroupTopicSummary]) -> None:
        self.folds = replace(self.folds, fetched={**self.folds.fetched, room_id: topics})
        self._next_topics()

    def _topics_failed(self, _exc: Exception) -> bool:
        """That room is left for its next opening to retry; the waiting ones still go."""
        self._next_topics()
        return False

    def _next_topics(self) -> None:
        while self._topics_waiting:
            room_id = self._topics_waiting.pop(0)
            if room_id not in self.folds.fetched:
                self._ensure_topics(room_id)
                return

    # ------------------------------------------------------------------ Pepper turns (§6.5)

    def _send_turn(self, text: str) -> None:
        self.dm_note = ""
        self.turns.append(state.DmTurn(text=text))
        if len(self.turns) > 1:
            self.notify("Sends when Pepper finishes this answer.", timeout=3)
        self._render_dm(new_rows=0, reset=True)
        self._run("turn")

    def _active_turn(self) -> state.DmTurn | None:
        return next((turn for turn in self.turns if turn.active), None)

    def _start_turn(self) -> None:
        """Start the first waiting turn if none is streaming or being checked."""
        if self._quitting or self._active_turn() is not None:
            return
        turn = next((t for t in self.turns if t.phase == "queued"), None)
        if turn is None:
            return
        loop = self._loops["turn"]
        loop.generation += 1
        generation = loop.generation
        loop.in_flight = generation
        # An earlier answered turn's rows may not be in history yet; they are not this turn's.
        turn.snapshot = frozenset(self.dm.entries) | self._answered_ids
        turn.phase, turn.status = "streaming", "Sending…"
        self._loops["dm"].stop_timer()
        self._paint_dm_status()
        worker = threading.Thread(
            target=self._turn_worker, args=(turn, generation), name="pepper-turn", daemon=True
        )
        worker.start()

    def _turn_worker(self, turn: state.DmTurn, generation: int) -> None:
        """The turn thread: stream, serve client tools, then hand the outcome back."""
        sink = _TurnStatus(
            lambda text: self._post(self._turn_status, turn, generation, text),
            self._api,
            self._provider,
        )
        outcome: str = "ended"
        error: Exception | None = None
        try:
            outcome = turns.run_turn(
                self._api,
                turn.text,
                sink,
                provider=self._provider,
                should_stop=lambda: self._quitting,
            )
        except (*_EXPECTED, OSError) as exc:
            error = exc
        self._post(self._turn_ended, turn, generation, outcome, sink.payload, error)

    def _turn_status(self, turn: state.DmTurn, generation: int, text: str) -> None:
        if generation != self._loops["turn"].generation or turn.phase != "streaming":
            return
        turn.status = text
        self._paint_dm_status()

    def _turn_ended(
        self,
        turn: state.DmTurn,
        generation: int,
        outcome: str,
        payload: dict[str, Any],
        error: Exception | None,
    ) -> None:
        loop = self._loops["turn"]
        if loop.in_flight == generation:
            loop.in_flight = None
        if not self._mounted() or self.fatal or turn not in self.turns:
            return
        if isinstance(error, ApiError) and error.status == 401:
            self.fail_fatal(LOGIN_HINT)
            return
        if isinstance(error, UpgradeRequiredError):
            self.fail_fatal(str(error))
            return
        if outcome == "reply" and error is None:
            turn.phase, turn.status = "done", ""
            turn.done_at = time.monotonic()
            turn.reply = str(payload.get("reply") or "")
            turn.user_id = _str_or_none(payload.get("user_message_id"))
            turn.assistant_id = _str_or_none(payload.get("assistant_message_id"))
            self._answered_ids |= turn.known_ids()
        elif outcome == "error" and error is None:
            self.turns.remove(turn)
            message = sanitize_line(str(payload.get("message") or "model service unavailable"))
            self.dm_note = f"Pepper couldn't answer: {message}"
        elif error is not None and isinstance(error, (ApiError, *_NOT_SENT)):
            # Refused before any frame, or never connected: the turn did not run, so the
            # text goes back to the composer to send again by hand.
            self.turns.remove(turn)
            self._give_back(turn.text)
            reason = _short_error(error)
            self.dm_note = f"Couldn't send ({reason}). Your message is back in the composer."
        else:
            # The stream dropped (or closed without a reply). The turn may well have run:
            # never send it again; re-read history until its reply shows or time runs out.
            turn.phase, turn.status = "checking", "Connection lost, checking…"
            turn.lost_at = time.monotonic()
        self._settle_turns()
        if self.current.is_pepper:
            self._render_dm(new_rows=0, reset=False)
        self._run("dm")

    def _give_back(self, text: str) -> None:
        """Put a refused turn's text back in the Pepper draft. A draft typed since is kept:
        the refused text goes above it, a blank line between, so neither is lost."""
        draft = self.drafts.setdefault(state.PEPPER, state.Draft())
        if self.current.is_pepper:
            draft.text = self.composer.text
        draft.text = f"{text}\n\n{draft.text}" if draft.text.strip() else text
        if self.current.is_pepper:
            self.composer.load_text(draft.text)
            self.composer.move_cursor(self.composer.document.end)

    def _settle_turns(self) -> None:
        """Retire turns whose rows history now shows, or that are past their window, and
        start the next queued turn once nothing is streaming or being checked.

        An answered turn is retired after the window even when history never shows both
        rows (its user row was stored before midnight, and today's history has dropped it).
        """
        entries = self.dm.entries
        now = time.monotonic()
        for turn in list(self.turns):
            if turn.phase == "done" and (
                (turn.reply_landed(entries) and turn.user_landed(entries)) or turn.gave_up(now)
            ):
                self.turns.remove(turn)
            elif turn.phase == "checking" and turn.reply_landed(entries):
                self.turns.remove(turn)
                self.dm_note = ""
            elif turn.phase == "checking" and turn.gave_up(now):
                self.turns.remove(turn)
                self.dm_note = "No reply yet. It may still arrive; check back in a moment."
        if any(turn.phase == "queued" for turn in self.turns):
            self._run("turn")
        self._paint_dm_status()


def _str_or_none(value: object) -> str | None:
    return str(value) if value else None


def _short_error(exc: Exception) -> str:
    """A one-line reason for a status line or a failed row (never a token: see ApiError)."""
    if isinstance(exc, ApiError):
        return f"HTTP {exc.status}"
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    if isinstance(exc, httpx.RequestError):
        return "network error"
    return sanitize_line(type(exc).__name__)


class _TurnStatus(turns.TurnSink):
    """Draws a streaming turn in the TUI's status line. Runs on the turn thread, so
    ``status`` must hand its text to the UI thread (the app passes one that does)."""

    def __init__(
        self,
        status: Callable[[str], None],
        api: ApiClient,
        provider: FilesystemProvider | None,
    ) -> None:
        self._status = status
        self.api = api
        self.provider = provider
        self.thought = ""
        #: The terminal frame's payload (reply or error), read by the app when the turn ends.
        self.payload: dict[str, Any] = {}

    def thought_delta(self, text: str, *, first: bool) -> None:
        self.thought = text if first else self.thought + text
        self._status("Thinking: " + _tail(self.thought))

    def thought_block(self, text: str, *, streamed: str) -> None:
        self.thought = ""
        self._status("Thinking: " + _tail(text))

    def retry(self, *, streamed: str) -> None:
        self.thought = ""
        self._status("Retrying…")

    def tool_call(self, payload: dict[str, Any]) -> None:
        self._status(f"Using {payload.get('name') or 'a tool'}…")

    def tool_result(self, payload: dict[str, Any]) -> None:
        self._status("Thinking…")

    def client_tools(self, payload: dict[str, Any]) -> None:
        if payload.get("manifest_reason"):
            self._status(f"Local tools refused: {payload['manifest_reason']}")

    def client_tool_call(self, payload: dict[str, Any]) -> None:
        self._status(f"Running local tool {payload.get('name') or ''}…")
        answer = turns.answer_client_tool_call(self.api, payload, provider=self.provider)
        if answer is None or answer.post_error is None:
            return
        error = answer.post_error
        if isinstance(error, ApiError) and error.status == 401:
            # The token stopped working: no later request will either. Raised out of the
            # stream so the turn ends and the app takes its fatal login path (_turn_ended).
            raise error
        # A call the server no longer waits on (404) or a network blip: the reply may still
        # come, so the stream is read on.
        self._status(f"Couldn't return {answer.name!r} to the server.")

    def reply(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def error(self, payload: dict[str, Any]) -> None:
        self.payload = payload


def _tail(text: str, width: int = 70) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else "…" + flat[-(width - 1) :]
