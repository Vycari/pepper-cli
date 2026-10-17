"""Pure logic of the terminal UI: ordering, the unread rule, cursors, cadence, backoff.

Nothing here imports Textual or touches the network, so it is unit-testable on its own and
carries most of the package's coverage (``planning/terminal-ui.md`` §11). The rules are ported
from the web client: ``groups_unread.js`` / ``groups_read_marks.js`` (unread, read marks),
``group_room.js`` (the poll cursor, pepper#4829) and ``group_poll_cadence.js`` (cadence).

Every timestamp comparison goes through :func:`parse_ts`. The server spells the same instant
as ``...Z`` in one route and ``...+00:00`` in another, so comparing the strings would
disagree with comparing the moments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pepper_cli.api_client import (
        ChatHistoryEntry,
        GroupMessage,
        GroupSummary,
        GroupTopicSummary,
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
DM_INTERVAL = 15.0
#: Seconds to wait after the Nth consecutive failure: 3, 6, 12, then 30 and no more.
BACKOFF_STEPS = (3.0, 6.0, 12.0, 30.0)

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
    """Per-topic read marks held in memory for the session (§6.3).

    The server is the store of record; this is its mirror plus the first-sight seed. Merges
    are always "take the max", so a late answer can never walk a mark backwards.
    """

    marks: dict[str, datetime] = field(default_factory=dict)

    def merge(self, topic_id: str, at: datetime | None) -> bool:
        """Raise the mark for ``topic_id`` to ``at`` if that is newer. True when it moved."""
        if at is None:
            return False
        current = self.marks.get(topic_id)
        if current is not None and current >= at:
            return False
        self.marks[topic_id] = at
        return True

    def merge_server(self, server_marks: dict[str, str]) -> None:
        for topic_id, raw in server_marks.items():
            self.merge(topic_id, parse_ts(raw))

    def seed_first_sight(self, rooms: Iterable[GroupSummary]) -> None:
        """A topic with no mark is read as of its current activity, so a fresh install does
        not light up every room (the web does the same)."""
        for room in rooms:
            for topic in room.topics:
                if topic.topic_id not in self.marks:
                    self.merge(topic.topic_id, parse_ts(topic.last_activity_at))

    def is_unread(self, topic: GroupTopicSummary, *, open_topic_id: str | None) -> bool:
        """The unread rule: activity later than the mark, and not the topic on screen.

        Own pending sends (a P2 concern) will pass through ``open_topic_id``-style
        suppression too; P1 has no composer, so there is nothing pending to suppress.
        """
        if topic.topic_id == open_topic_id or topic.archived_at:
            return False
        activity = parse_ts(topic.last_activity_at)
        if activity is None:
            return False
        mark = self.marks.get(topic.topic_id)
        return mark is None or activity > mark

    def room_unread(self, room: GroupSummary, *, open_topic_id: str | None) -> bool:
        return any(self.is_unread(t, open_topic_id=open_topic_id) for t in room.topics)


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
    kind: str  # "pepper" | "room" | "topic" | "more"
    label: str
    depth: int
    room_id: str | None = None
    topic_id: str | None = None
    color: str | None = None
    unread: bool = False
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


def build_sidebar(
    rooms: list[GroupSummary],
    marks: ReadMarks,
    *,
    current: Conversation,
    manual: dict[str, bool],
    show_all: set[str],
    pepper_dot: bool,
) -> list[SidebarRow]:
    """The Messages tree as flat rows: Pepper first, then rooms in server (recency) order.

    A room is expanded when the user said so by hand (``manual``), else when it is the open
    room. An expanded room lists its 5 most recent topics plus a "Show more (N)" row (the
    open topic stays visible even past the fifth).
    """
    open_topic_id = current.topic_id
    rows = [SidebarRow(key="pepper", kind="pepper", label="Pepper", depth=0, unread=pepper_dot)]
    for room in rooms:
        expanded = manual.get(room.group_id, room.group_id == current.room_id)
        rows.append(
            SidebarRow(
                key=f"room:{room.group_id}",
                kind="room",
                label=room.display_name,
                depth=0,
                room_id=room.group_id,
                color=room.color,
                unread=marks.room_unread(room, open_topic_id=open_topic_id),
                expandable=True,
                expanded=expanded,
            )
        )
        if not expanded:
            continue
        topics = topics_by_recency(room)
        shown = topics if room.group_id in show_all else topics[:TOPIC_LIMIT]
        if open_topic_id and current.room_id == room.group_id and shown != topics:
            extra = next((t for t in topics if t.topic_id == open_topic_id), None)
            if extra is not None and extra not in shown:
                shown = [*shown, extra]
        rows.extend(
            SidebarRow(
                key=f"topic:{t.topic_id}",
                kind="topic",
                label=t.name,
                depth=1,
                room_id=room.group_id,
                topic_id=t.topic_id,
                unread=marks.is_unread(t, open_topic_id=open_topic_id),
            )
            for t in shown
        )
        if (hidden := len(topics) - len(shown)) > 0:
            rows.append(
                SidebarRow(
                    key=f"more:{room.group_id}",
                    kind="more",
                    label=f"Show more ({hidden})",
                    depth=1,
                    room_id=room.group_id,
                    hidden_count=hidden,
                )
            )
    return rows


def unread_conversations(
    rooms: list[GroupSummary], marks: ReadMarks, *, current: Conversation, pepper_dot: bool
) -> list[Conversation]:
    """Every unread conversation in sidebar order, whether or not its room is expanded."""
    found = [PEPPER] if pepper_dot else []
    for room in rooms:
        found.extend(
            Conversation(room.group_id, t.topic_id)
            for t in topics_by_recency(room)
            if marks.is_unread(t, open_topic_id=current.topic_id)
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


def any_unread(
    rooms: list[GroupSummary], marks: ReadMarks, *, current: Conversation, pepper_dot: bool
) -> bool:
    """Whether the terminal title should carry the ``●`` prefix."""
    return pepper_dot or any(
        marks.room_unread(room, open_topic_id=current.topic_id) for room in rooms
    )


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

    def add(self, incoming: Iterable[GroupMessage]) -> list[str]:
        """Merge server rows. Returns the ids that are new (an edit replaces, it is not new)."""
        new_ids: list[str] = []
        for message in incoming:
            if message.id not in self.rows:
                new_ids.append(message.id)
            self.rows[message.id] = message
            stamp = parse_ts(message.created_at)
            if stamp is not None and (self.cursor is None or stamp > self.cursor):
                self.cursor = stamp
        return new_ids

    def ordered(self) -> list[GroupMessage]:
        return sorted(self.rows.values(), key=lambda m: (_ts_or_epoch(m.created_at), m.id))

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

    def next_is_reconcile(self) -> bool:
        """Count a poll tick; True on every ``RECONCILE_EVERY``-th one."""
        self.ticks += 1
        return self.ticks % RECONCILE_EVERY == 0


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
