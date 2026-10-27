"""The blocking half of the poll loops: one function per request, no Textual and no timers.

Each call runs on a worker thread and returns plain data; the app applies it on the UI thread
(so the shared read marks and message store are only ever touched there). Keeping the requests
here, apart from the scheduling, is what lets the cursor and ETag rules be tested with a mocked
transport instead of a running app.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pepper_cli.api_client import ApiError, GroupMessagePage, group_message_from_dict

if TYPE_CHECKING:
    from pepper_cli.api_client import (
        ApiClient,
        ChatHistoryEntry,
        GroupList,
        GroupMessage,
        GroupReadState,
        GroupRosterMember,
        GroupSummary,
        GroupTopicSummary,
        GroupUnreadRow,
    )
    from pepper_cli.tui.state import Conversation, PendingSend

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
    #: The server's unread rows for those rooms (read-state-sync §6).
    unread: list[GroupUnreadRow] = field(default_factory=list)
    etag: str | None = None
    seen: dict[str, _Signature] | None = None
    #: The newest :meth:`RailPoller.refetch_all` request this step answered by reading every
    #: room's marks (0: none).
    full: int = 0


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
        #: Requests for a step that reads every room's marks, moved or not (see
        #: :meth:`refetch_all`), and the newest one a committed step has answered.
        self._full_asked = 0
        self._full_done = 0

    def refetch_all(self) -> None:
        """Make the next step a 200 that reads every room's marks (UI thread).

        A mark can move with no change to the room list (a read on another device), and the
        signature a step compares is activity only. When the person stream starts at the feed's
        head, a read committed just before it is in neither the stream nor the last poll, so the
        catch-up asks for every mark once. Requests are counted: a step answers only those
        made before it started, so committing a step already in flight (it read the old
        state) cannot cancel a request made after it.
        """
        self._full_asked += 1

    def step(self) -> RailUpdate:
        asked = self._full_asked
        full = asked > self._full_done
        groups, etag = self._api.list_groups_conditional(if_none_match=None if full else self._etag)
        if groups is None:
            return RailUpdate(groups=None, etag=self._etag)
        if not groups.enabled or groups.degraded:
            # "Could not ask" is not "all caught up": the app holds what it has.
            return RailUpdate(groups=groups, etag=etag)
        moved = [r for r in groups.groups if full or self._seen.get(r.group_id) != _signature(r)]
        moved_ids = {r.group_id for r in moved}
        fetched = self._fetch_marks(moved)  # a transport error raises here, before any commit
        seen: dict[str, _Signature] = {}
        for room in groups.groups:
            if room.group_id in fetched or room.group_id not in moved_ids:
                seen[room.group_id] = _signature(room)
            elif (old := self._seen.get(room.group_id)) is not None:
                seen[room.group_id] = old  # its marks failed: it still counts as moved
        merged: dict[str, str] = {}
        rows: list[GroupUnreadRow] = []
        for answer in fetched.values():
            merged.update(answer.marks)
            rows.extend(answer.unread)
        complete = len(fetched) == len(moved)
        # A room whose marks could not be read is asked again on the next poll. That needs a
        # 200, so the ETag is only kept when every room's marks came back.
        return RailUpdate(
            groups=groups,
            server_marks=merged,
            unread=rows,
            etag=etag if complete else None,
            seen=seen,
            full=asked if full and complete else 0,
        )

    def commit(self, update: RailUpdate) -> None:
        """Adopt the state of an applied result (UI thread)."""
        self._etag = update.etag
        if update.seen is not None:
            self._seen = update.seen
        self._full_done = max(self._full_done, update.full)

    def forget_etag(self) -> None:
        """Ask for the full list next time (after a failed poll, §6.6)."""
        self._etag = None

    def _fetch_marks(self, rooms: list[GroupSummary]) -> dict[str, GroupReadState]:
        """``group_id -> read state`` (marks and unread rows) for each room that answered."""
        if not rooms:
            return {}
        with ThreadPoolExecutor(max_workers=MARK_FETCH_WORKERS) as pool:
            answers = list(pool.map(lambda r: self._marks_or_none(r.group_id), rooms))
        return {
            room.group_id: marks
            for room, marks in zip(rooms, answers, strict=True)
            if marks is not None
        }

    def _marks_or_none(self, group_id: str) -> GroupReadState | None:
        # One room's read state failing must not hide the rest of the rail. A room that cannot
        # be read keeps whatever the cache held (nothing, on first sight: not unread) and is
        # asked again on the next poll. A transport error is not caught: the whole step fails
        # and nothing is committed.
        try:
            return self._api.get_group_read_state(group_id)
        except ApiError as exc:
            if exc.status == 401:
                raise
            return None


def fetch_topic(api: ApiClient, conv: Conversation, *, since: str | None) -> GroupMessagePage:
    """The open topic's page: the newest one when ``since`` is ``None``, else the window.

    The whole page comes back (not just its rows) because ``next_before`` says whether older
    messages exist.
    """
    if conv.room_id is None or conv.topic_id is None:
        return GroupMessagePage()
    return api.list_group_messages(conv.room_id, conv.topic_id, since=since)


def fetch_older(api: ApiClient, conv: Conversation, *, before: str, limit: int) -> GroupMessagePage:
    """The page older than ``before`` (an opaque ``next_before`` the server handed out)."""
    if conv.room_id is None or conv.topic_id is None:
        return GroupMessagePage()
    return api.list_group_messages(conv.room_id, conv.topic_id, before=before, limit=limit)


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


def post_message(api: ApiClient, pending: PendingSend) -> GroupMessage:
    """``POST`` one room message under its idempotency key; the stored row the server answers.

    The same key on a retry makes the server answer with the row it already stored, so a
    send whose first answer was lost (a timeout after the server committed) is not doubled.
    """
    conv = pending.conversation
    if conv.room_id is None or conv.topic_id is None:
        raise ValueError("a room message needs a room and a topic")
    row = api.post_group_message(
        conv.room_id,
        conv.topic_id,
        message=pending.body,
        mentions=list(pending.mentions) or None,
        parent_message_id=pending.parent_id,
        idempotency_key=pending.key,
    )
    return group_message_from_dict(row)


def fetch_roster(api: ApiClient, room_id: str) -> list[GroupRosterMember]:
    """The room's ``@``-mention roster (Pepper first)."""
    return api.group_roster(room_id)


def react(
    api: ApiClient, room_id: str, message_id: str, *, emoji: str, clear: bool
) -> list[dict[str, Any]]:
    """Add your ``emoji`` to a message, or take it back; the server's whole chip set."""
    if clear:
        return api.clear_group_reaction(room_id, message_id, emoji=emoji)
    return api.react_to_group_message(room_id, message_id, emoji=emoji)


def edit_message(api: ApiClient, room_id: str, message_id: str, *, body: str) -> dict[str, Any]:
    """``POST .../edit`` on one of your own messages."""
    return api.edit_group_message(room_id, message_id, body=body)


def delete_message(api: ApiClient, room_id: str, message_id: str) -> dict[str, Any]:
    """``DELETE`` one of your own messages (the server leaves a tombstone row)."""
    return api.delete_group_message(room_id, message_id)


def create_topic(api: ApiClient, room_id: str, *, name: str) -> dict[str, Any]:
    """``POST .../topics``; the new topic as the server sends it."""
    return api.create_group_topic(room_id, name=name)


def fetch_room_info(api: ApiClient, room_id: str) -> dict[str, Any]:
    """``GET /groups/{id}``: members, topics and apps for the room-info screen."""
    return api.get_group(room_id)


def fetch_topic_rail(api: ApiClient, room_id: str) -> list[GroupTopicSummary]:
    """Every topic of a room, archived included (the rooms list may carry only live ones)."""
    return api.list_group_topics(room_id)
