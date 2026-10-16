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


@dataclass
class RailUpdate:
    """What one rail poll learned. ``groups`` is ``None`` on a 304 (nothing changed)."""

    groups: GroupList | None
    #: ``topic_id -> read_through`` for the rooms whose topics moved since the last 200.
    server_marks: dict[str, str] = field(default_factory=dict)


def _signature(room: GroupSummary) -> tuple[tuple[str, str, str | None], ...]:
    return tuple((t.topic_id, t.last_activity_at, t.archived_at) for t in room.topics)


class RailPoller:
    """Polls the room list with ``If-None-Match`` and refetches marks for rooms that moved."""

    def __init__(self, api: ApiClient) -> None:
        self._api = api
        self._etag: str | None = None
        self._seen: dict[str, tuple[tuple[str, str, str | None], ...]] = {}

    def step(self) -> RailUpdate:
        groups, etag = self._api.list_groups_conditional(if_none_match=self._etag)
        if groups is None:
            return RailUpdate(groups=None)
        self._etag = etag
        if not groups.enabled or groups.degraded:
            # "Could not ask" is not "all caught up": the app holds what it has.
            return RailUpdate(groups=groups)
        moved = [r for r in groups.groups if self._seen.get(r.group_id) != _signature(r)]
        self._seen = {r.group_id: _signature(r) for r in groups.groups}
        return RailUpdate(groups=groups, server_marks=self._fetch_marks(moved))

    def _fetch_marks(self, rooms: list[GroupSummary]) -> dict[str, str]:
        if not rooms:
            return {}
        merged: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=MARK_FETCH_WORKERS) as pool:
            for marks in pool.map(lambda r: self._marks_or_empty(r.group_id), rooms):
                merged.update(marks)
        return merged

    def _marks_or_empty(self, group_id: str) -> dict[str, str]:
        # One room's marks failing must not hide the rest of the rail; a room whose marks
        # cannot be read is simply treated as "no marks yet" (seeded read on first sight).
        try:
            return self._api.get_group_read_marks(group_id)
        except ApiError as exc:
            if exc.status == 401:
                raise
            return {}


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
