"""Pure logic of the terminal UI: ordering, the unread cache, cursors, cadence, backoff.

Nothing here imports Textual or touches the network, so it is unit-testable on its own and
carries most of the package's coverage (``planning/terminal-ui.md`` §11). The rules are ported
from the web client: ``groups_read_marks.js`` (read marks; unread itself is the server's),
``group_room.js`` (the poll cursor, pepper#4829) and ``group_poll_cadence.js`` (cadence).

Every timestamp comparison goes through :func:`parse_ts`. The server spells the same instant
as ``...Z`` in one route and ``...+00:00`` in another, so comparing the strings would
disagree with comparing the moments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urljoin, urlsplit

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from collections.abc import Set as AbstractSet

    from pepper_cli.api_client import (
        ChatHistoryEntry,
        GroupMessage,
        GroupRosterMember,
        GroupSummary,
        GroupTopicSummary,
        GroupUnreadRow,
    )

#: How far behind the newest ``created_at`` a topic poll asks (§6.2, pepper#4829). A row that
#: committed late but carries an earlier timestamp than one already seen still lands.
LOOKBACK = timedelta(seconds=10)
#: The topic poll re-reads the recent window on every Nth tick, to catch edits, deletes and
#: any row the lookback missed.
RECONCILE_EVERY = 5
#: Topics shown under the open room before "Show more" (§15 F).
TOPIC_LIMIT = 5
#: Seconds since the last keypress for the three cadence tiers, and the interval of each.
ACTIVE_WINDOW = 60.0
COOLING_WINDOW = 120.0
ACTIVE_INTERVAL = 3.0
COOLING_INTERVAL = 10.0
IDLE_INTERVAL = 15.0
#: Rail (sidebar) and Pepper DM history poll interval.
RAIL_INTERVAL = 15.0
#: The same polls while the person event stream is healthy: a safety net, not the source. The
#: stream says when to fetch (a message, a read on another device), so these only cover a
#: frame the stream dropped.
RAIL_INTERVAL_LIVE = 120.0
TOPIC_INTERVAL_LIVE = 60.0
DM_INTERVAL = 15.0
#: Seconds to wait after the Nth consecutive failure: 3, 6, 12, then 30 and no more.
BACKOFF_STEPS = (3.0, 6.0, 12.0, 30.0)
#: After a DM stream drops, how long history is re-read for the reply before giving up (§6.5).
#: Also how long an answered turn waits for its rows to show before it is retired (a turn
#: that straddles midnight never shows its user row in today's history).
DM_RECONCILE_WINDOW = 120.0
#: History cadence while waiting for a dropped turn's reply.
DM_RECONCILE_INTERVAL = 3.0
#: A room whose newest topic activity is older than this folds under "Show inactive (N)".
#: The server's own rail default (``groups_rail_inactive_after_days``); ``/api/v1/groups`` does
#: not send the ``inactive`` flag, so the client computes it from ``topics[].last_activity_at``.
INACTIVE_AFTER = timedelta(days=7)
#: The reaction picker: key ``1``..``6`` picks the matching emoji.
EMOJI_CHOICES = ("👍", "❤️", "😂", "🎉", "😮", "🙏")
#: Messages asked for in a "load older" page (the server caps it at 100).
OLDER_PAGE = 50
#: Longest quote snippet shown above the composer and on a pending row (local, from the
#: quoted message's own body; never the server's ``quote_excerpt``).
QUOTE_SNIPPET_CHARS = 80

_EPOCH = datetime.min.replace(tzinfo=UTC)


def parse_ts(value: str | None) -> datetime | None:
    """An ISO-8601 instant as an aware datetime, or ``None`` if it is absent or unparseable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _ts_or_epoch(value: str | None) -> datetime:
    return parse_ts(value) or _EPOCH


# ---------------------------------------------------------------------------
# Conversations


@dataclass(frozen=True)
class Conversation:
    """What the right-hand pane shows: the Pepper DM (both ids ``None``) or one room topic."""

    room_id: str | None = None
    topic_id: str | None = None

    @property
    def is_pepper(self) -> bool:
        return self.room_id is None


PEPPER = Conversation()


# ---------------------------------------------------------------------------
# Read marks and unread


@dataclass
class ReadMarks:
    """The session's cache of the server's read state (§6.3, read-state-sync §6).

    The server decides what is unread (its floor, your own messages, tombstones, a carried
    topic's reach) and answers it as ``unread`` rows; this holds the latest answer per topic
    and the read cursors that came with it. The one thing the client adds is that a cursor can
    only ever clear: a mark that moved after the server counted, and is at or after the topic's
    newest activity, leaves nothing in it unread under any rule, so :meth:`settle` drops the
    cached row until the server's next answer.
    Nothing here ever *raises* a count, and a topic the server has no row for is not unread.
    Mark merges are "take the max", so a late answer cannot walk a mark backwards.
    """

    marks: dict[str, datetime] = field(default_factory=dict)
    #: ``topic_id -> the server's unread row`` (zero-count rows are kept: they say "read").
    unread: dict[str, GroupUnreadRow] = field(default_factory=dict)
    #: ``topic_id -> the cursor the server counted that row from`` (``None``: it had none).
    counted_from: dict[str, datetime | None] = field(default_factory=dict)
    #: The server's deduped total from the last unread answer. Any count shown is this, never a
    #: sum of ``unread``: a message listed under a superseded room and its successor is one.
    unread_total: int | None = None

    def merge(self, topic_id: str, at: datetime | None) -> bool:
        """Raise the mark for ``topic_id`` to ``at`` if that is newer. True when it moved."""
        if at is None:
            return False
        current = self.marks.get(topic_id)
        if current is not None and current >= at:
            return False
        self.marks[topic_id] = at
        return True

    def merge_server(self, server_marks: Mapping[str, str]) -> None:
        for topic_id, raw in server_marks.items():
            self.merge(topic_id, parse_ts(raw))

    def merge_unread(
        self,
        rows: Iterable[GroupUnreadRow],
        marks: Mapping[str, str],
        *,
        total: int | None = None,
    ) -> None:
        """Take the server's unread rows (and the cursors answered with them).

        Every row is kept, even one counted from a cursor older than one held here (an answer
        that crossed a read on this device or another): it may count a message newer than that
        read, and dropping it would hide the message until the room moved again. Each row
        remembers the cursor it was counted from, and :meth:`settle` applies only a cursor that
        moved after it.
        """
        self.merge_server(marks)
        for row in rows:
            self.unread[row.topic_id] = row
            self.counted_from[row.topic_id] = parse_ts(marks.get(row.topic_id))
        if total is not None:
            self.unread_total = total

    def merge_advanced(self, payload: object) -> str | None:
        """Fold a ``read_state.advanced`` event payload (``{topic_id, group_id, last_read_at}``)
        into the marks. Forward only, and at the server's own precision: ``last_read_at`` is a
        stored ``created_at`` with microseconds, and ``datetime`` compares at microseconds, so a
        mark one microsecond behind the message it names still leaves that message unread.

        Returns the topic id when the mark moved (the sidebar should repaint), else ``None``;
        a malformed payload is ignored.
        """
        if not isinstance(payload, dict):
            return None
        topic_id, raw = payload.get("topic_id"), payload.get("last_read_at")
        if not isinstance(topic_id, str) or not isinstance(raw, str):
            return None
        return topic_id if self.merge(topic_id, parse_ts(raw)) else None

    def settle(self, rooms: Iterable[GroupSummary]) -> bool:
        """Clear the cached row of every topic whose mark has passed its newest activity.

        Only a mark newer than the cursor the row was counted from can clear it: ``rooms`` is
        read before the rows (the rail asks ``/groups``, then the unread route), so its activity can
        be older than the answer, and a cursor the server already counted from says nothing
        the row does not. True when a row was cleared. A mark short of the activity (a partial
        read) clears nothing: the count is the server's, and the caller asks it again.
        """
        cleared = False
        for room in rooms:
            for topic in room.topics:
                row = self.unread.get(topic.topic_id)
                if row is None or (row.unread_count == 0 and not row.has_unread_mention):
                    continue
                mark, activity = self.marks.get(topic.topic_id), parse_ts(topic.last_activity_at)
                if mark is None or activity is None or mark < activity:
                    continue
                since = self.counted_from.get(topic.topic_id)
                if since is None or mark > since:
                    self.unread[topic.topic_id] = replace(
                        row, unread_count=0, has_unread_mention=False
                    )
                    cleared = True
        return cleared

    def is_unread(self, topic: GroupTopicSummary) -> bool:
        """The server says this topic has unread messages. Archived topics are not shown as
        unread (the server counts them; the rail hides them)."""
        row = self.unread.get(topic.topic_id)
        return bool(row and row.unread_count > 0 and not topic.archived_at)

    def is_mentioned(self, topic: GroupTopicSummary) -> bool:
        """The server says an unread message in this topic mentions you."""
        row = self.unread.get(topic.topic_id)
        return bool(row and row.has_unread_mention and self.is_unread(topic))

    def room_unread(self, room: GroupSummary) -> bool:
        return any(self.is_unread(t) for t in room.topics)

    def room_mentioned(self, room: GroupSummary) -> bool:
        return any(self.is_mentioned(t) for t in room.topics)

    def mentioned_topics(self, rooms: Iterable[GroupSummary]) -> set[str]:
        """Ids of the topics shown as mentioned (for spotting a new one; archived ones are not)."""
        return {t.topic_id for room in rooms for t in room.topics if self.is_mentioned(t)}


def topics_by_recency(room: GroupSummary) -> list[GroupTopicSummary]:
    """Non-archived topics, most recent activity first (the server's order, made explicit)."""
    live = [t for t in room.topics if not t.archived_at]
    return sorted(live, key=lambda t: _ts_or_epoch(t.last_activity_at), reverse=True)


# ---------------------------------------------------------------------------
# Sidebar model


@dataclass(frozen=True)
class SidebarRow:
    """One line of the Messages tree. ``key`` is stable across rebuilds."""

    key: str
    kind: str  # "pepper" | "room" | "topic" | "more" | "archived" | "newtopic" | "inactive"
    label: str
    depth: int
    room_id: str | None = None
    topic_id: str | None = None
    color: str | None = None
    unread: bool = False
    #: Unread, and something in it mentions you (shown as ``@`` in place of the dot).
    mention: bool = False
    expandable: bool = False
    expanded: bool = False
    hidden_count: int = 0

    @property
    def conversation(self) -> Conversation | None:
        if self.kind == "pepper":
            return PEPPER
        if self.kind == "topic":
            return Conversation(self.room_id, self.topic_id)
        return None


def room_recency(room: GroupSummary) -> datetime | None:
    """The newest live-topic activity of a room, or ``None`` when it has none to go by."""
    stamps = [parse_ts(t.last_activity_at) for t in room.topics if not t.archived_at]
    return max((s for s in stamps if s is not None), default=None)


def room_inactive(room: GroupSummary, *, now: datetime) -> bool:
    """Whether a room has been quiet longer than :data:`INACTIVE_AFTER`.

    A room with no dated topic is not called inactive: the server also falls back to the
    room's creation time, which ``/api/v1/groups`` does not send, so this errs toward showing.
    """
    recency = room_recency(room)
    return recency is not None and now - recency > INACTIVE_AFTER


def archived_topics(
    room: GroupSummary, fetched: Sequence[GroupTopicSummary] = ()
) -> list[GroupTopicSummary]:
    """A room's archived topics, newest first: those the rooms list carries plus ``fetched``
    (the full topic rail, read when the room is opened), deduped by id."""
    found = {t.topic_id: t for t in room.topics if t.archived_at}
    found.update({t.topic_id: t for t in fetched if t.archived_at})
    return sorted(found.values(), key=lambda t: _ts_or_epoch(t.last_activity_at), reverse=True)


@dataclass(frozen=True)
class SidebarFolds:
    """What the user has unfolded: archived topics per room, and the inactive rooms."""

    archived: AbstractSet[str] = frozenset()
    inactive: bool = False
    #: Archived topics read from the topic rail, per room (the rooms list may omit them).
    fetched: Mapping[str, Sequence[GroupTopicSummary]] = field(default_factory=dict)


def build_sidebar(
    rooms: list[GroupSummary],
    marks: ReadMarks,
    *,
    current: Conversation,
    manual: dict[str, bool],
    show_all: set[str],
    pepper_dot: bool,
    folds: SidebarFolds | None = None,
    now: datetime | None = None,
) -> list[SidebarRow]:
    """The Messages tree as flat rows: Pepper first, then rooms in server (recency) order.

    A room is expanded when the user said so by hand (``manual``), else when it is the open
    room. An expanded room lists its 5 most recent topics plus a "Show more (N)" row (the
    open topic stays visible even past the fifth), then "Show archived (N)" and "+ New topic".
    With ``now`` given, rooms quiet for over a week fold behind "Show inactive (N)" at the end
    (never the open room, nor one with something unread); without it nothing folds.
    """
    folds = folds or SidebarFolds()
    rows = [SidebarRow(key="pepper", kind="pepper", label="Pepper", depth=0, unread=pepper_dot)]
    folded: set[str] = set()
    if now is not None:
        folded = {
            r.group_id
            for r in rooms
            if room_inactive(r, now=now)
            and r.group_id != current.room_id
            and not marks.room_unread(r)
        }
    for room in rooms:
        if room.group_id in folded and not folds.inactive:
            continue
        expanded = manual.get(room.group_id, room.group_id == current.room_id)
        rows.append(
            SidebarRow(
                key=f"room:{room.group_id}",
                kind="room",
                label=room.display_name,
                depth=0,
                room_id=room.group_id,
                color=room.color,
                unread=marks.room_unread(room),
                mention=marks.room_mentioned(room),
                expandable=True,
                expanded=expanded,
            )
        )
        if expanded:
            rows.extend(
                _room_children(
                    room,
                    marks,
                    current=current,
                    folds=folds,
                    show_all=room.group_id in show_all,
                )
            )
    if folded:
        verb = "Hide" if folds.inactive else "Show"
        rows.append(
            SidebarRow(
                key="inactive",
                kind="inactive",
                label=f"{verb} inactive ({len(folded)})",
                depth=0,
                expanded=folds.inactive,
                hidden_count=len(folded),
            )
        )
    return rows


def _room_children(
    room: GroupSummary,
    marks: ReadMarks,
    *,
    current: Conversation,
    folds: SidebarFolds,
    show_all: bool,
) -> list[SidebarRow]:
    open_topic_id = current.topic_id
    topics = topics_by_recency(room)
    shown = topics if show_all else topics[:TOPIC_LIMIT]
    if open_topic_id and current.room_id == room.group_id and shown != topics:
        extra = next((t for t in topics if t.topic_id == open_topic_id), None)
        if extra is not None and extra not in shown:
            shown = [*shown, extra]
    out = [
        SidebarRow(
            key=f"topic:{t.topic_id}",
            kind="topic",
            label=t.name,
            depth=1,
            room_id=room.group_id,
            topic_id=t.topic_id,
            unread=marks.is_unread(t),
            mention=marks.is_mentioned(t),
        )
        for t in shown
    ]
    if (hidden := len(topics) - len(shown)) > 0:
        out.append(
            SidebarRow(
                key=f"more:{room.group_id}",
                kind="more",
                label=f"Show more ({hidden})",
                depth=1,
                room_id=room.group_id,
                hidden_count=hidden,
            )
        )
    archived = archived_topics(room, folds.fetched.get(room.group_id, ()))
    unfolded = room.group_id in folds.archived
    if archived:
        verb = "Hide" if unfolded else "Show"
        out.append(
            SidebarRow(
                key=f"archived:{room.group_id}",
                kind="archived",
                label=f"{verb} archived ({len(archived)})",
                depth=1,
                room_id=room.group_id,
                expanded=unfolded,
                hidden_count=len(archived),
            )
        )
        if unfolded:
            out.extend(
                SidebarRow(
                    key=f"topic:{t.topic_id}",
                    kind="topic",
                    label=t.name,
                    depth=1,
                    room_id=room.group_id,
                    topic_id=t.topic_id,
                )
                for t in archived
            )
    out.append(
        SidebarRow(
            key=f"newtopic:{room.group_id}",
            kind="newtopic",
            label="+ New topic",
            depth=1,
            room_id=room.group_id,
        )
    )
    return out


def unread_conversations(
    rooms: list[GroupSummary], marks: ReadMarks, *, current: Conversation, pepper_dot: bool
) -> list[Conversation]:
    """Every unread conversation in sidebar order, whether or not its room is expanded."""
    found = [PEPPER] if pepper_dot else []
    for room in rooms:
        found.extend(
            Conversation(room.group_id, t.topic_id)
            for t in topics_by_recency(room)
            if marks.is_unread(t)
        )
    return found


def next_unread(
    rooms: list[GroupSummary],
    marks: ReadMarks,
    *,
    current: Conversation,
    pepper_dot: bool,
    step: int = 1,
) -> Conversation | None:
    """The next (``step=1``) or previous (``-1``) unread conversation, wrapping; ``None`` if
    nothing is unread."""
    order = [PEPPER] + [
        Conversation(room.group_id, t.topic_id) for room in rooms for t in topics_by_recency(room)
    ]
    unread = set(unread_conversations(rooms, marks, current=current, pepper_dot=pepper_dot))
    if not unread:
        return None
    start = order.index(current) if current in order else -1
    for offset in range(1, len(order) + 1):
        candidate = order[(start + step * offset) % len(order)]
        if candidate in unread:
            return candidate
    return None


def header_text(current: Conversation, rooms: list[GroupSummary]) -> str:
    """``Room › Topic`` (``[archived]`` where it applies), or ``Pepper · Today`` for the DM."""
    if current.is_pepper:
        return "Pepper · Today"
    for room in rooms:
        if room.group_id != current.room_id:
            continue
        for topic in room.topics:
            if topic.topic_id == current.topic_id:
                tail = " [archived]" if topic.archived_at else ""
                return f"{room.display_name} › {topic.name}{tail}"
        return room.display_name
    return ""


def any_mention(rooms: list[GroupSummary], marks: ReadMarks) -> bool:
    """Whether any unread topic holds a mention of you (the terminal title shows ``@``)."""
    return any(marks.room_mentioned(room) for room in rooms)


def any_unread(rooms: list[GroupSummary], marks: ReadMarks, *, pepper_dot: bool) -> bool:
    """Whether the terminal title should carry the ``●`` prefix."""
    return pepper_dot or any(marks.room_unread(room) for room in rooms)


# ---------------------------------------------------------------------------
# Open-topic message store (pepper#4829)


@dataclass
class MessageStore:
    """The rows of the open topic, and the poll cursor that goes with them.

    The cursor is the newest ``created_at`` the **server returned**, and nothing else; rows are
    deduped by id and kept in ``(created_at, id)`` order rather than appended. P2's pending
    (own, not yet echoed) rows will live outside this store, so they cannot move the cursor.
    """

    rows: dict[str, GroupMessage] = field(default_factory=dict)
    cursor: datetime | None = None
    ticks: int = 0
    #: The ``next_before`` of the oldest page held: what "load older" asks for next. ``None``
    #: means nothing is older (or the first page has not landed yet).
    older: str | None = None
    #: A "load older" page has been merged, so the newest page's cursor no longer applies.
    paged_back: bool = False

    def add(self, incoming: Iterable[GroupMessage]) -> list[str]:
        """Merge server rows. Returns the ids that are new (an edit replaces, it is not new)."""
        new_ids: list[str] = []
        for message in incoming:
            held = self.rows.get(message.id)
            if held is None:
                new_ids.append(message.id)
            if held is None or not _is_stale_copy(message, held):
                self.rows[message.id] = message
            stamp = parse_ts(message.created_at)
            if stamp is not None and (self.cursor is None or stamp > self.cursor):
                self.cursor = stamp
        return new_ids

    def note_newest_page(self, next_before: str | None) -> None:
        """A read of the newest page told us where the older ones start. Once the user has
        paged back, a later reconcile read of the newest page must not move that cursor."""
        if not self.paged_back:
            self.older = next_before

    def add_older(self, incoming: Iterable[GroupMessage], next_before: str | None) -> list[str]:
        """Merge a "load older" page. Rows are deduped by id; the poll cursor is untouched
        because older rows are never newer than it. Returns the ids that are new."""
        new_ids = self.add(incoming)
        self.older = next_before
        self.paged_back = True
        return new_ids

    @property
    def has_older(self) -> bool:
        return self.older is not None

    def ordered(self) -> list[GroupMessage]:
        return sorted(self.rows.values(), key=lambda m: (_ts_or_epoch(m.created_at), m.id))

    def set_reactions(self, message_id: str, reactions: list[dict[str, Any]]) -> None:
        """Adopt the server's chip set for a message (it is authoritative, ``mine`` included)."""
        if (row := self.rows.get(message_id)) is not None:
            self.rows[message_id] = replace(row, reactions=list(reactions))

    def apply_edit(self, message_id: str, answer: dict[str, Any]) -> None:
        """Swap in the body the server answered an edit with.

        The row may be on an older page that no poll re-reads, so the reply is what puts the
        server's version on screen. Its ``edited_at`` is the version marker that stops a poll
        already in flight (carrying the row as it was) from undoing this (see :meth:`add`).
        """
        row = self.rows.get(message_id)
        if row is None:
            return
        raw_body = answer.get("raw_body")
        rendered = answer.get("rendered_html")
        edited_at = answer.get("edited_at")
        self.rows[message_id] = replace(
            row,
            raw_body=raw_body if isinstance(raw_body, str) else row.raw_body,
            rendered_html=rendered if isinstance(rendered, str) else row.rendered_html,
            is_edited=True,
            edited_at=edited_at if isinstance(edited_at, str) else row.edited_at,
        )

    def apply_delete(self, message_id: str, answer: dict[str, Any]) -> None:
        """Turn a row the server confirmed deleted into its tombstone (as a poll would show it)."""
        row = self.rows.get(message_id)
        if row is None:
            return
        deleted_at = answer.get("deleted_at")
        self.rows[message_id] = replace(
            row,
            is_deleted=True,
            raw_body="",
            rendered_html="",
            deleted_at=deleted_at if isinstance(deleted_at, str) else row.deleted_at,
        )

    def newest_created_at(self) -> str | None:
        """The ``created_at`` to publish as the read mark: the newest row's own spelling."""
        rows = self.ordered()
        return rows[-1].created_at if rows else None

    def since(self) -> str | None:
        """The ``since`` query value: the cursor minus the lookback, or ``None`` before the
        first page (the first read is the newest page, unconditioned)."""
        if self.cursor is None:
            return None
        return (self.cursor - LOOKBACK).isoformat()

    def expect_reconcile(self) -> None:
        """Make the next poll a full re-read (after an edit or delete: the newest page comes
        back as the server holds it, whatever was patched in from the reply)."""
        self.ticks = RECONCILE_EVERY - 1

    def next_is_reconcile(self) -> bool:
        """Count a poll tick; True on every ``RECONCILE_EVERY``-th one."""
        self.ticks += 1
        return self.ticks % RECONCILE_EVERY == 0


def _is_stale_copy(incoming: GroupMessage, held: GroupMessage) -> bool:
    """Whether ``incoming`` is an older version of ``held`` than the one already shown.

    Edits only move forward and a delete is final, so a read that started before the change
    landed (and carries the row as it was) must not put the old text back.
    """
    if held.is_deleted and not incoming.is_deleted:
        return True
    held_at = parse_ts(held.edited_at)
    if held_at is None:
        return False
    incoming_at = parse_ts(incoming.edited_at)
    return incoming_at is None or incoming_at < held_at


# ---------------------------------------------------------------------------
# The Pepper DM


@dataclass
class DmTracker:
    """History rows of the Pepper DM, diffed by id, and the session-local unread dot (§15 D).

    The first history read is the baseline: it never lights the dot. After that, an assistant
    row not seen before lights it, unless the DM is the open conversation.
    """

    entries: dict[str, ChatHistoryEntry] = field(default_factory=dict)
    dot: bool = False
    _primed: bool = False

    def update(self, history: Iterable[ChatHistoryEntry], *, is_open: bool) -> list[str]:
        """Replace the rows with a history read. Returns the ids not in the previous read.

        The read is the whole of today's thread, so it replaces what was held rather than
        merging into it: after midnight, yesterday's rows drop out instead of staying under
        the "Today" heading.
        """
        previous = self.entries
        self.entries = {entry.id: entry for entry in history}
        new_ids = [eid for eid in self.entries if eid not in previous]
        fresh_assistant = any(self.entries[eid].role == "assistant" for eid in new_ids)
        if is_open:
            self.dot = False
        elif self._primed and fresh_assistant:
            self.dot = True
        self._primed = True
        return new_ids

    def ordered(self) -> list[ChatHistoryEntry]:
        return sorted(self.entries.values(), key=lambda e: (_ts_or_epoch(e.created_at), e.id))


# ---------------------------------------------------------------------------
# Cadence and backoff


def poll_interval(idle_seconds: float, *, hidden: bool = False) -> float | None:
    """Seconds until the next open-topic poll, or ``None`` while paused (§3, §6.2).

    3s while the user interacted within a minute, 10s through two minutes, 15s after that.
    """
    if hidden:
        return None
    if idle_seconds < ACTIVE_WINDOW:
        return ACTIVE_INTERVAL
    if idle_seconds < COOLING_WINDOW:
        return COOLING_INTERVAL
    return IDLE_INTERVAL


def backoff_delay(failures: int) -> float:
    """Wait after ``failures`` consecutive errors: 3 -> 6 -> 12 -> 30s, capped."""
    if failures <= 0:
        return 0.0
    return BACKOFF_STEPS[min(failures, len(BACKOFF_STEPS)) - 1]


# ---------------------------------------------------------------------------
# Posting (P2): pending room sends, drafts, mentions, the DM turn


type SendStatus = Literal["queued", "sending", "sent", "failed"]


@dataclass
class PendingSend:
    """One room message this session is posting, from Enter until a poll returns it.

    ``key`` is the idempotency key: minted once, when the message is composed, and reused by
    every retry, so a POST whose answer was lost and is sent again is stored once (§6.4).
    The row never enters :class:`MessageStore`; the store's copy, when the poll brings it,
    replaces this one (matched by ``server_id``), so the poll cursor never moves on an echo.
    """

    key: str
    conversation: Conversation
    body: str
    mentions: tuple[str, ...] = ()
    parent_id: str | None = None
    quote_text: str = ""
    status: SendStatus = "queued"
    error: str = ""
    #: The stored message's id and ``created_at``, once the POST has answered.
    server_id: str | None = None
    created_at: str | None = None


@dataclass
class PendingSends:
    """Room sends in the order they were composed. One POST is in flight at a time."""

    items: list[PendingSend] = field(default_factory=list)

    def add(self, item: PendingSend) -> None:
        self.items.append(item)

    def get(self, key: str) -> PendingSend | None:
        return next((p for p in self.items if p.key == key), None)

    def next_queued(self) -> PendingSend | None:
        return next((p for p in self.items if p.status == "queued"), None)

    def in_conversation(self, conv: Conversation) -> list[PendingSend]:
        return [p for p in self.items if p.conversation == conv]

    def retry(self, key: str) -> bool:
        """Queue a failed send again, with the key it already had. True when it was failed."""
        item = self.get(key)
        if item is None or item.status != "failed":
            return False
        item.status, item.error = "queued", ""
        return True

    def confirm(self, conv: Conversation, store_ids: Iterable[str]) -> list[str]:
        """Drop sends whose stored copy the poll has returned; their keys."""
        present = set(store_ids)
        gone = [
            p
            for p in self.items
            if p.conversation == conv and p.server_id is not None and p.server_id in present
        ]
        for p in gone:
            self.items.remove(p)
        return [p.key for p in gone]


@dataclass
class Draft:
    """What is in the composer for one destination: text, and the message it quotes."""

    text: str = ""
    quote_id: str | None = None
    quote_name: str = ""
    quote_text: str = ""


def quote_snippet(body: str) -> str:
    """The first line of a message, cut to :data:`QUOTE_SNIPPET_CHARS`."""
    first = next((line.strip() for line in body.splitlines() if line.strip()), "")
    if len(first) > QUOTE_SNIPPET_CHARS:
        return first[: QUOTE_SNIPPET_CHARS - 1].rstrip() + "…"
    return first


def mention_ids(text: str, roster: Iterable[GroupRosterMember]) -> tuple[str, ...]:
    """Person ids of roster members whose ``@Display Name`` is in ``text``, longest name first.

    Decided at send time from the text, so a mention picked from the menu and then deleted is
    not sent, and one typed out in full is. The ``@`` must start the text or follow whitespace,
    as in :func:`mention_query`, so ``help@ada.com`` mentions no one. ``@Al`` does not match
    inside ``@Allen``: the name must end at a non-word character. Each matched span is
    consumed, so "@Ada Lovelace" does not also mention a member called "Ada".
    """
    remaining = text
    found: list[str] = []
    members = sorted(roster, key=lambda m: len(m.display_name), reverse=True)
    for member in members:
        name = member.display_name.strip()
        if not name or member.person_id in found:
            continue
        pattern = re.compile(r"(?<!\S)@" + re.escape(name) + r"(?!\w)", re.IGNORECASE)
        if pattern.search(remaining):
            found.append(member.person_id)
            remaining = pattern.sub(" ", remaining)
    return tuple(found)


def mention_query(before_cursor: str) -> str | None:
    """The partial name after an ``@`` the cursor is in, or ``None`` when not in a mention.

    The ``@`` must start the line or follow whitespace (so an e-mail address does not open
    the menu). Names have spaces, so the query runs to the cursor, up to 32 characters.
    """
    match = re.search(r"(?:^|\s)@([^@\n]{0,32})$", before_cursor)
    return match.group(1) if match else None


def roster_matches(
    query: str, roster: Iterable[GroupRosterMember], *, limit: int = 6
) -> list[GroupRosterMember]:
    """Roster members whose name (or any word of it) starts with ``query``, roster order kept."""
    q = query.casefold()
    out = []
    for member in roster:
        name = member.display_name.casefold()
        if not name:
            continue
        if name.startswith(q) or any(word.startswith(q) for word in name.split()):
            out.append(member)
    return out[:limit]


type TurnPhase = Literal["queued", "streaming", "checking", "done"]


@dataclass
class DmTurn:
    """One message to Pepper, from Enter until its rows are in today's history (§6.5).

    ``snapshot`` holds the history ids on screen when the turn started. A reply frame names
    its rows (``user_message_id``/``assistant_message_id``); a dropped stream does not, and
    then the reply is "an assistant row that was not in the snapshot". The turn is never
    sent twice: a drop only ever re-reads history.

    Only a ``streaming`` or ``checking`` turn holds back the next one. A ``done`` turn stays
    just to show its rows until history does, and is retired after
    :data:`DM_RECONCILE_WINDOW` even if history never shows them.
    """

    text: str
    snapshot: frozenset[str] = frozenset()
    phase: TurnPhase = "queued"
    status: str = ""
    reply: str | None = None
    user_id: str | None = None
    assistant_id: str | None = None
    #: ``time.monotonic()`` when the stream was found dropped.
    lost_at: float | None = None
    #: ``time.monotonic()`` when the reply frame arrived.
    done_at: float | None = None

    @property
    def active(self) -> bool:
        """Streaming, or re-reading history after a drop: the next turn waits for this one."""
        return self.phase in ("streaming", "checking")

    def known_ids(self) -> set[str]:
        """The history ids the reply frame named."""
        return {i for i in (self.user_id, self.assistant_id) if i is not None}

    def user_landed(self, entries: dict[str, ChatHistoryEntry]) -> bool:
        if self.user_id is not None:
            return self.user_id in entries
        if self.phase == "queued":
            return False
        return any(e.role == "user" and eid not in self.snapshot for eid, e in entries.items())

    def reply_landed(self, entries: dict[str, ChatHistoryEntry]) -> bool:
        if self.assistant_id is not None:
            return self.assistant_id in entries
        if self.phase != "checking":
            return False
        return any(e.role == "assistant" and eid not in self.snapshot for eid, e in entries.items())

    def gave_up(self, now: float) -> bool:
        """A dropped turn whose reply, or an answered turn whose rows, did not show within
        :data:`DM_RECONCILE_WINDOW`."""
        since = {"checking": self.lost_at, "done": self.done_at}.get(self.phase)
        return since is not None and now - since >= DM_RECONCILE_WINDOW


# ---------------------------------------------------------------------------
# Parity (P3): reactions, own messages, links, jump


def reaction_action(reactions: Iterable[dict[str, Any]], emoji: str) -> Literal["add", "clear"]:
    """What picking ``emoji`` does: take your own back when the chip is already yours, else add.

    ``mine`` is the server's word, never computed here. Reactions are per emoji, so picking a
    different one adds a second chip rather than swapping the first.
    """
    for chip in reactions:
        if chip.get("emoji") == emoji and chip.get("mine") is True:
            return "clear"
    return "add"


def is_own_editable(message: GroupMessage) -> bool:
    """Whether ``e``/``d`` apply: the caller's own live message (the server enforces it too)."""
    return message.is_you and not message.is_deleted and not message.is_broken


# A bare link in a body. A ``(...)`` or ``[...]`` pair inside it is kept (Wikipedia's
# ``Pixel_(computer)``, an IPv6 host ``[::1]``), one level deep; an unmatched closer ends it,
# so ``(see https://x.com/a)`` and markdown's ``[text](https://x.com/a)`` stop before the
# ``)``. Sentence punctuation after it is trimmed separately (``_URL_TRAILING``).
_URL_IN_TEXT = re.compile(r"""https?://(?:[^\s<>"'()\[\]]|\([^\s<>"'()]*\)|\[[^\s<>"'\[\]]*\])+""")
_URL_TRAILING = ".,;:!?"


def safe_http_url(url: str | None, *, base: str) -> str | None:
    """``url`` resolved against the server, or ``None`` unless it is http(s).

    A message can carry any string as a link; handing a ``javascript:`` or ``file:`` URL to
    the system browser is how a chat message becomes a local exploit, so only http and https
    pass.
    """
    if not url or not url.strip():
        return None
    try:
        absolute = urljoin(base.rstrip("/") + "/", url.strip())
        parts = urlsplit(absolute)
        host = parts.hostname
    except ValueError:  # a malformed address ("https://[") is not a link
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    return absolute


def message_urls(message: GroupMessage, *, base: str) -> list[str]:
    """Every openable URL of a message: links (previews, then bare links in the body), then
    attachments, then card urls, in that order, each at most once."""
    candidates: list[object] = [p.get("url") for p in message.link_previews]
    candidates.extend(
        url.rstrip(_URL_TRAILING) for url in _URL_IN_TEXT.findall(message.raw_body or "")
    )
    candidates.extend(a.get("url") for a in message.attachments)
    candidates.extend(c.url for c in message.cards)
    found: list[str] = []
    for raw in candidates:
        url = safe_http_url(raw if isinstance(raw, str) else None, base=base)
        if url is not None and url not in found:
            found.append(url)
    return found


def first_url(message: GroupMessage, *, base: str) -> str | None:
    """The URL ``o`` opens, or ``None`` when the message has none."""
    urls = message_urls(message, base=base)
    return urls[0] if urls else None


def url_domain(url: str) -> str:
    """The host of a link, without ``www.`` (empty when it has none or cannot be parsed)."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host.removeprefix("www.")


@dataclass(frozen=True)
class JumpTarget:
    """One entry of the ``Ctrl+K`` list: a label and the conversation it opens."""

    label: str
    conversation: Conversation


def jump_targets(rooms: list[GroupSummary]) -> list[JumpTarget]:
    """Pepper, every room (opens its newest topic) and every live topic, in sidebar order."""
    targets = [JumpTarget("Pepper", PEPPER)]
    for room in rooms:
        topics = topics_by_recency(room)
        if topics:
            newest = Conversation(room.group_id, topics[0].topic_id)
            targets.append(JumpTarget(room.display_name, newest))
        targets.extend(
            JumpTarget(f"{room.display_name} › {t.name}", Conversation(room.group_id, t.topic_id))
            for t in topics
        )
    return targets


def fuzzy_score(query: str, text: str) -> int | None:
    """How well ``query`` matches ``text`` as an in-order subsequence, or ``None`` if it does
    not. Higher is better: consecutive letters and word starts count extra, and a shorter
    haystack beats a longer one with the same hits."""
    needle = query.casefold().replace(" ", "")
    hay = text.casefold()
    if not needle:
        return 0
    score = 0
    position = 0
    previous = -2
    for ch in needle:
        found = hay.find(ch, position)
        if found < 0:
            return None
        score += 10
        if found == previous + 1:
            score += 15
        if found == 0 or not hay[found - 1].isalnum():
            score += 12
        position = found + 1
        previous = found
    return score - len(hay)


def fuzzy_filter(query: str, targets: Sequence[JumpTarget]) -> list[JumpTarget]:
    """Targets matching ``query``, best first (stable on ties); all of them for an empty query."""
    scored = [(fuzzy_score(query, t.label), i, t) for i, t in enumerate(targets)]
    hits = [(sc, i, t) for sc, i, t in scored if sc is not None]
    hits.sort(key=lambda h: (-h[0], h[1]))
    return [t for _, _, t in hits]
