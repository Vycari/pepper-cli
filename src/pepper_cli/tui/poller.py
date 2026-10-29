"""The blocking half of the poll loops: one function per request, no Textual and no timers.

Each call runs on a worker thread and returns plain data; the app applies it on the UI thread
(so the shared read marks and message store are only ever touched there). Keeping the requests
here, apart from the scheduling, is what lets the cursor and ETag rules be tested with a mocked
transport instead of a running app.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from pepper_cli.api_client import ApiError, GroupMessagePage, group_message_from_dict

if TYPE_CHECKING:
    from pepper_cli.api_client import (
        ApiClient,
        ArtifactDetail,
        ArtifactListResponse,
        ChatHistoryEntry,
        GroupAppTile,
        GroupList,
        GroupMessage,
        GroupRosterMember,
        GroupSummary,
        GroupsUnread,
        GroupTopicSummary,
        GroupUnreadRow,
        RoomTodos,
        TodoEntry,
    )
    from pepper_cli.tui.state import Conversation, PendingSend

#: How long the rail leaves ``GET /api/v1/groups/unread`` alone after the server answered 404
#: (a server without the route), before one poll asks again.
UNREAD_MISSING_RETRY_SECONDS = 600.0

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
    #: ``topic_id -> read_through`` from the unread request (empty when none was made).
    server_marks: dict[str, str] = field(default_factory=dict)
    #: The server's unread rows for every room (read-state-sync §6).
    unread: list[GroupUnreadRow] = field(default_factory=list)
    #: The server's deduped total, to show as it is; never a sum of ``unread``.
    unread_total: int | None = None
    #: The server answered the unread request, so ``unread`` is its whole snapshot: a topic it
    #: no longer lists is no longer unread. ``False`` when none was made or it failed, where an
    #: empty ``unread`` means "not asked", not "nothing unread".
    unread_answered: bool = False
    etag: str | None = None
    seen: dict[str, _Signature] | None = None
    #: The newest :meth:`RailPoller.refetch_all` request this step answered by reading every
    #: room's marks (0: none).
    full: int = 0


def _signature(room: GroupSummary) -> _Signature:
    return tuple((t.topic_id, t.last_activity_at, t.archived_at) for t in room.topics)


class RailPoller:
    """Polls the room list with ``If-None-Match`` and rereads the unread state when a room moved.

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
        #: Monotonic time to ask for unread rows again after a 404 (``None``: the route exists).
        self._unread_retry_at: float | None = None

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
        # A 404 on the unread route holds for UNREAD_MISSING_RETRY_SECONDS; the poll that ends
        # it is a full one, so the room list is not what decides whether the route is asked.
        retry_at = self._unread_retry_at
        missing = retry_at is not None and time.monotonic() < retry_at
        full = asked > self._full_done or (retry_at is not None and not missing)
        groups, etag = self._api.list_groups_conditional(if_none_match=None if full else self._etag)
        if groups is None:
            return RailUpdate(groups=None, etag=self._etag)
        if not groups.enabled or groups.degraded:
            # "Could not ask" is not "all caught up": the app holds what it has.
            return RailUpdate(groups=groups, etag=etag)
        moved = [r for r in groups.groups if full or self._seen.get(r.group_id) != _signature(r)]
        # One request answers every room, so it is made only when a room moved (or on a full
        # step). A transport error raises here, before any commit.
        asking = bool(moved) and not missing
        if retry_at is not None and not missing and not moved:
            # An expired back-off with no room to ask about (an empty list): the route was not
            # tried, so end the back-off rather than force a 200 on every poll.
            self._unread_retry_at = None
        fetched = self._fetch_unread() if asking else None
        # A server without the route (404) is answered: asking again would only fail again.
        # Only a 404 from this very request counts: a stale deadline must not excuse a 500.
        answered_404 = self._unread_retry_at is not None and self._unread_retry_at != retry_at
        complete = fetched is not None or not asking or answered_404
        seen: dict[str, _Signature] = {}
        moved_ids = {r.group_id for r in moved}
        for room in groups.groups:
            if complete or room.group_id not in moved_ids:
                seen[room.group_id] = _signature(room)
            elif (old := self._seen.get(room.group_id)) is not None:
                seen[room.group_id] = old  # its unread state failed: it still counts as moved
        # A failed unread request is asked again on the next poll. That needs a 200, so the
        # ETag is only kept when the request came back (or none was needed, or the server has
        # no such route).
        return RailUpdate(
            groups=groups,
            server_marks=fetched.marks if fetched else {},
            unread=fetched.unread if fetched else [],
            unread_total=fetched.unread_total if fetched else None,
            unread_answered=fetched is not None,
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

    def _fetch_unread(self) -> GroupsUnread | None:
        # The unread state failing must not hide the rail. The cache keeps whatever it held
        # (nothing, on first sight: not unread) and the next poll asks again. A transport error
        # is not caught: the whole step fails and nothing is committed.
        try:
            fetched = self._api.get_groups_unread()
        except ApiError as exc:
            if exc.status == 401:
                raise
            if exc.status == 404:
                # No such route: stop asking for a while, and let the ETag and a pending
                # ``refetch_all`` settle as they would for an answer.
                self._unread_retry_at = time.monotonic() + UNREAD_MISSING_RETRY_SECONDS
            return None
        self._unread_retry_at = None
        return fetched


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


def fetch_room_shelf(api: ApiClient, room_id: str) -> tuple[list[GroupAppTile], bool]:
    """A room's installed apps in install order, and whether the registry was unavailable
    (which is not the same as an empty shelf)."""
    return api.group_app_launcher(room_id)


def fetch_todos(api: ApiClient, owner: str | None, *, mine: bool) -> list[TodoEntry]:
    """Every todo of one owner, open and closed, in one call: a room's (``owner`` is its id) or
    your own (``None``). ``mine`` keeps what someone asked of you."""
    return api.list_todos(status="all", group_id=owner, mine=mine)


@dataclass(frozen=True)
class OwnTodos:
    """Your own list and, beside it, your rooms' (``None`` when that read failed)."""

    todos: list[TodoEntry]
    rooms: list[RoomTodos] | None


def fetch_own_todos(api: ApiClient, *, mine: bool) -> OwnTodos:
    """Your own todos plus each room's (``GET /todos/rooms``) for your own Todos pane.

    The rooms' read is a second request and the pane does not depend on it: when it fails
    (or the server says it is degraded) ``rooms`` is ``None`` and your own items still show.
    A failure of your own list raises, as any pane read does; a 401 on either one too, so
    the app takes its login path (an upgrade-required answer is not caught here either).
    """
    todos = api.list_todos(status="all", mine=mine)
    try:
        listed = api.list_room_todos(mine=mine)
    except ApiError as exc:
        if exc.status == 401:
            raise
        return OwnTodos(todos, None)
    except (httpx.RequestError, ValueError, KeyError, TypeError):
        return OwnTodos(todos, None)
    return OwnTodos(todos, None if listed.degraded else listed.rooms)


def todo_verb(api: ApiClient, verb: str, todo_id: str, owner: str | None) -> TodoEntry:
    """``done`` / ``dismiss`` / ``reopen`` one todo of ``owner`` (a room's id, or ``None`` for
    your own list); the row as the server now has it."""
    if verb == "done":
        return api.complete_todo(todo_id, group_id=owner)
    if verb == "dismiss":
        return api.dismiss_todo(todo_id, group_id=owner)
    return api.reopen_todo(todo_id, group_id=owner)


def add_todo(
    api: ApiClient,
    owner: str | None,
    *,
    title: str,
    due_date: str | None,
    due_day: str | None,
    assignee_person_id: str | None,
) -> TodoEntry:
    """``POST`` one todo to ``owner``'s list (a room's, or your own); the created row."""
    return api.create_todo(
        title=title,
        due_date=due_date,
        due=due_day,
        group_id=owner,
        assignee_person_id=assignee_person_id,
    )


def fetch_notes(api: ApiClient, owner: str | None) -> ArtifactListResponse:
    """The owner's notes (``inline_text`` artifacts), up to the route's cap of 200."""
    return api.list_artifacts(kind="inline_text", limit=200, principal=owner)


def fetch_note(api: ApiClient, note_id: str) -> ArtifactDetail:
    """One note's full text. Anything that is not a note is refused, as ``pepper notes`` does."""
    detail = api.get_artifact_detail(note_id)
    if detail.kind != "inline_text":
        raise ValueError("That is not a note.")
    return detail
