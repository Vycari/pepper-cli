"""The blocking half of the poll loops: one function per request, no Textual and no timers.

Each call runs on a worker thread and returns plain data; the app applies it on the UI thread
(so the shared read marks and message store are only ever touched there). Keeping the requests
here, apart from the scheduling, is what lets the cursor and ETag rules be tested with a mocked
transport instead of a running app.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pepper_cli.api_client import ApiError

if TYPE_CHECKING:
    from pepper_cli.api_client import (
        ApiClient,
        ChatHistoryEntry,
        GroupList,
        GroupMessage,
        GroupSummary,
    )
    from pepper_cli.tui.state import Conversation

#: Concurrent read-mark fetches on the first load (one request per room).
MARK_FETCH_WORKERS = 4


type _Signature = tuple[tuple[str, str, str | None], ...]


@dataclass
class RailUpdate:
    """What one rail poll learned. ``groups`` is ``None`` on a 304 (nothing changed).

    ``etag`` and ``seen`` are the poller state this result *would* move to. They are not in
    effect until the app has applied the result and called :meth:`RailPoller.commit`, so a
    result that fails, or is never applied, cannot leave a saved ETag that hides it behind a
    304 on the next poll.
    """

    groups: GroupList | None
    #: ``topic_id -> read_through`` for the rooms whose topics moved since the last 200.
    server_marks: dict[str, str] = field(default_factory=dict)
    etag: str | None = None
    seen: dict[str, _Signature] | None = None


def _signature(room: GroupSummary) -> _Signature:
    return tuple((t.topic_id, t.last_activity_at, t.archived_at) for t in room.topics)


class RailPoller:
    """Polls the room list with ``If-None-Match`` and refetches marks for rooms that moved.

    :meth:`step` runs on a worker thread and only reads the saved state; :meth:`commit` runs
    on the UI thread once the result has been applied. The app keeps one rail request in
    flight, so the two never overlap.
    """

    def __init__(self, api: ApiClient) -> None:
        self._api = api
        self._etag: str | None = None
        self._seen: dict[str, _Signature] = {}

    def step(self) -> RailUpdate:
        groups, etag = self._api.list_groups_conditional(if_none_match=self._etag)
        if groups is None:
            return RailUpdate(groups=None, etag=self._etag)
        if not groups.enabled or groups.degraded:
            # "Could not ask" is not "all caught up": the app holds what it has.
            return RailUpdate(groups=groups, etag=etag)
        moved = [r for r in groups.groups if self._seen.get(r.group_id) != _signature(r)]
        moved_ids = {r.group_id for r in moved}
        fetched = self._fetch_marks(moved)  # a transport error raises here, before any commit
        seen: dict[str, _Signature] = {}
        for room in groups.groups:
            if room.group_id in fetched or room.group_id not in moved_ids:
                seen[room.group_id] = _signature(room)
            elif (old := self._seen.get(room.group_id)) is not None:
                seen[room.group_id] = old  # its marks failed: it still counts as moved
        merged: dict[str, str] = {}
        for marks in fetched.values():
            merged.update(marks)
        complete = len(fetched) == len(moved)
        # A room whose marks could not be read is asked again on the next poll. That needs a
        # 200, so the ETag is only kept when every room's marks came back.
        return RailUpdate(
            groups=groups, server_marks=merged, etag=etag if complete else None, seen=seen
        )

    def commit(self, update: RailUpdate) -> None:
        """Adopt the state of an applied result (UI thread)."""
        self._etag = update.etag
        if update.seen is not None:
            self._seen = update.seen

    def forget_etag(self) -> None:
        """Ask for the full list next time (after a failed poll, §6.6)."""
        self._etag = None

    def _fetch_marks(self, rooms: list[GroupSummary]) -> dict[str, dict[str, str]]:
        """``group_id -> marks`` for each room whose marks could be read."""
        if not rooms:
            return {}
        with ThreadPoolExecutor(max_workers=MARK_FETCH_WORKERS) as pool:
            answers = list(pool.map(lambda r: self._marks_or_none(r.group_id), rooms))
        return {
            room.group_id: marks
            for room, marks in zip(rooms, answers, strict=True)
            if marks is not None
        }

    def _marks_or_none(self, group_id: str) -> dict[str, str] | None:
        # One room's marks failing must not hide the rest of the rail. A room whose marks
        # cannot be read is seeded read on first sight and asked again on the next poll. A
        # transport error is not caught: the whole step fails and nothing is committed.
        try:
            return self._api.get_group_read_marks(group_id)
        except ApiError as exc:
            if exc.status == 401:
                raise
            return None


def fetch_topic(api: ApiClient, conv: Conversation, *, since: str | None) -> list[GroupMessage]:
    """The open topic's rows: the newest page when ``since`` is ``None``, else the window."""
    if conv.room_id is None or conv.topic_id is None:
        return []
    return api.list_group_messages(conv.room_id, conv.topic_id, since=since).messages


def fetch_dm(api: ApiClient) -> list[ChatHistoryEntry]:
    """Today's Pepper thread."""
    return api.chat_history()


def publish_read_mark(api: ApiClient, conv: Conversation, read_through: str) -> str:
    """``PUT .../read``; returns the mark the server kept (monotonic, so maybe a later one)."""
    if conv.room_id is None or conv.topic_id is None:
        return read_through
    answer = api.set_group_read_mark(conv.room_id, conv.topic_id, read_through=read_through)
    kept = answer.get("read_through") if isinstance(answer, dict) else None
    return kept if isinstance(kept, str) else read_through
