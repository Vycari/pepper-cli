"""The person event stream behind the TUI (read-state sync phase 4).

``GET /api/v1/groups/events/stream`` on the PAT carries metadata only (ids and kinds), so the
app re-fetches through its existing paths. This module is the blocking half: one thread that
opens the stream, hands frames to the app, and reconnects on its own. It holds no Textual and
no timers; the app decides what a frame means.

The rules (``Vycari/groups`` ``docs/design/read-state-sync.md`` §9, and the web client's
``groups_stream.js`` that this mirrors):

- A connection with no position starts at the feed's head and opens with a ``cursor`` frame.
  Events committed between the app's last fetch and that head are in neither, so the app is told
  to catch up once (:attr:`StreamHandler.on_resync`).
- A drop reconnects with ``Last-Event-ID`` set to the last id seen, so nothing is missed or
  re-sent beyond what the server replays; the app tolerates a repeat.
- The server's ``cursor_ahead`` error (``retry: false``, with ``next_after_id``) means the saved
  position is past every id the groups service issued. The app catches up, and a new stream
  opens with ``?since=`` that id.
- ``retry: true`` errors and transport failures back off and retry. Any other ``retry: false``
  error, or a 4xx that says this token or server has no such stream, ends it for good: the
  caller's polling was never turned off, only slowed, and speeds up again.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from pepper_cli.api_client import ApiError, UpgradeRequiredError

if TYPE_CHECKING:
    from collections.abc import Callable

    from pepper_cli.api_client import ApiClient, FeedFrame

#: Seconds to wait after the Nth consecutive failed connection: 1, 2, 4, ... capped.
RECONNECT_FIRST = 1.0
RECONNECT_MAX = 60.0
#: Doublings past which the delay is the cap whatever the base (``2 ** 16`` seconds is a day).
_MAX_DOUBLINGS = 16
#: Client errors that mean the stream will not come back by retrying: 401 (the app's own
#: polls report that), 403 (feature or token kind), 404/405/501 (an older server).
_GIVE_UP_STATUSES = frozenset({401, 403, 404, 405, 501})
#: Error slugs a ``retry: false`` frame may carry that are not ``cursor_ahead``.
CURSOR_AHEAD = "cursor_ahead"


@dataclass
class StreamHandler:
    """What the stream thread calls. Each runs on the stream thread; the app posts to its own."""

    #: One feed event (``read_state.advanced``, ``message.created``, ...), or ``chat.message``.
    on_frame: Callable[[FeedFrame], None]
    #: ``True`` once the server has said something on a connection, ``False`` when it drops.
    on_live: Callable[[bool], None]
    #: Catch up with one fetch: the stream started at the head, or the server said ``cursor_ahead``.
    on_resync: Callable[[], None]


def reconnect_delay(failures: int) -> float:
    """Seconds before the next connection after ``failures`` in a row (``0`` when none)."""
    if failures <= 0:
        return 0.0
    # The exponent is clamped first: ``2 ** n`` for an outage of a thousand failures is too big
    # for a float, and an OverflowError here would end the stream thread for the session.
    return min(RECONNECT_FIRST * 2 ** min(failures - 1, _MAX_DOUBLINGS), RECONNECT_MAX)


class PersonStream:
    """Runs the reconnect loop on a daemon thread until :meth:`stop` or the stream ends for good."""

    def __init__(
        self,
        api: ApiClient,
        handler: StreamHandler,
        *,
        delay: Callable[[int], float] = reconnect_delay,
    ) -> None:
        self._api = api
        self._handler = handler
        self._delay = delay
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: The last event or cursor id seen: the ``Last-Event-ID`` of the next connection.
        self.last_id: int | None = None
        #: Set after ``cursor_ahead``: the position to open at, used once.
        self._since: int | None = None
        #: True once the loop has ended for good.
        self.gave_up = False
        self._from_head = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self.run, name="pepper-person-stream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            live = False
            retry = True
            # A connection that names no position starts at the feed's head.
            self._from_head = self.last_id is None and self._since is None
            try:
                for frame in self._api.watch_person_events(
                    since=self._since if self.last_id is None else None,
                    last_event_id=self.last_id,
                ):
                    if self._stop.is_set():
                        return
                    if not live and frame.kind not in ("error", "comment"):
                        # Opening proves little (the server may still send a retryable error),
                        # and a bare ``: connected`` comment says nothing of the feed: only a
                        # frame from the feed itself counts as live.
                        live = True
                        self._handler.on_live(True)
                    verdict = self._frame(frame)
                    if verdict is not None:
                        retry = verdict
                        break
            except ApiError as exc:
                if exc.status in _GIVE_UP_STATUSES:
                    retry = False
            except UpgradeRequiredError:
                retry = False
            except (httpx.RequestError, ValueError):
                pass
            if live:
                self._handler.on_live(False)
            if not retry:
                self.gave_up = True
                return
            failures = 1 if live else failures + 1
            if self._stop.wait(self._delay(failures)):
                return

    def _frame(self, frame: FeedFrame) -> bool | None:
        """Handle one frame. ``None`` keeps reading; a bool ends this connection, saying
        whether to open another."""
        kind = frame.kind
        if kind == "error":
            return self._error(frame.payload)
        if kind in ("comment", "heartbeat", "scope"):
            return None
        if frame.id is not None:
            self.last_id = frame.id
            self._since = None
        if kind == "cursor":
            if self._from_head:
                self._from_head = False
                self._handler.on_resync()
            return None
        self._handler.on_frame(frame)
        return None

    def _error(self, payload: dict[str, Any]) -> bool:
        slug = payload.get("error")
        if slug == CURSOR_AHEAD:
            restart = payload.get("next_after_id")
            if isinstance(restart, int) and not isinstance(restart, bool) and restart >= 0:
                self.last_id = None
                self._since = restart
                self._handler.on_resync()
                return True
            return False
        return bool(payload.get("retry"))
