"""HTTP client for Pepper's ``/api/v1/...`` surface.

This module is intentionally small and dependency-light. It is the
**de-facto API contract** that future iOS / Android ports will
reproduce: when you can't quite remember whether ``trajectory_id`` is
optional in the chat-send response, this file is the source of truth.

Two design rules drove the shape:

1. *Sync, not async.* The server side is asynchronous but the CLI is
   not a long-running event loop — one HTTP call per command. Sync
   ``httpx.Client`` avoids dragging an event loop into the call stack
   and keeps stack traces shallow when something fails.
2. *Raise on error, return data on success.* Each method returns a
   plain Python dict matching the server's response JSON (the server-
   side Pydantic models define the canonical schema). On a non-2xx response we raise
   :class:`ApiError` with the status + body so the CLI can render
   useful messages without parsing twice.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import email.utils
import json as _json
import math
import os
import re as _re
import uuid
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass, field
from typing import Any, NoReturn, Self, cast

import httpx

from pepper_cli._version import USER_AGENT
from pepper_cli.secret import SecretStr
from pepper_cli.terminal_text import CONTROL_CHAR_RE, sanitize_text, strip_sequences

#: Ordinary API request timeout (seconds). Chat streams may have long gaps
#: between frames while the agent works, so they get a separate read limit.
DEFAULT_TIMEOUT = 60.0
DEFAULT_CHAT_STREAM_READ_TIMEOUT = 600.0
#: Longest the agent event stream may go without a byte before the read is
#: abandoned and the caller reconnects. The server sends a ``: ping`` comment
#: about every 15 s while idle, so a minute of silence means a dead connection.
FEED_STREAM_READ_TIMEOUT = 60.0

#: Fallback output filename for ``ApiClient.download_takeout`` when the
#: caller didn't pass ``--out`` and the server's response somehow lacks
#: a ``Content-Disposition`` header (shouldn't happen against a real
#: server — the route always sets one — but a fallback here means a
#: malformed/mocked response still lands somewhere sensible).
DEFAULT_TAKEOUT_FILENAME = "pepper-takeout.zip"


class ApiError(Exception):
    """HTTP error response from the server.

    Distinct from ``httpx.HTTPError`` so the CLI can catch the
    "Pepper said no" case separately from transport-level failures
    (DNS, connection refused, TLS error) where there's no body to
    surface. Carries the status code and the response body — both
    matter to the CLI: 401 means "log in again", 502 means "server
    hiccup, try again", everything else gets the body printed.

    ``detail`` is server text bound for the terminal (every ``except
    ApiError`` handler prints it, directly or as ``str(exc)``), so it is
    cleaned here, once, with :func:`~pepper_cli.terminal_text.sanitize_text`:
    escape sequences, control characters other than newline and tab, and
    invisible format characters are gone before any handler sees it.
    ``body`` stays exactly as received; it is for parsing, never printed.
    """

    def __init__(
        self,
        status: int,
        body: str,
        *,
        detail: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.status = status
        self.body = body
        #: Seconds a ``Retry-After`` header asked the client to wait, when it sent one.
        self.retry_after = retry_after
        detail = sanitize_text(detail) if detail is not None else None
        self.detail = detail
        msg = f"HTTP {status}"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class UpgradeRequiredError(Exception):
    """A 426 from the server: this client version is no longer supported.

    Deliberately *not* an :class:`ApiError`: the many ``except ApiError``
    handlers in the CLI each map an error to one command's message, and none
    of them should swallow this. It propagates to :func:`pepper_cli.cli.main`,
    which prints ``message`` to stderr and exits non-zero. Never retried.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class ArtifactConflictError(ApiError):
    """A 412 from ``POST /api/v1/artifacts/{id}/edit`` — the ``If-Match``
    the caller sent no longer matches:
    someone else's write landed first. A subclass of :class:`ApiError`
    rather than a distinct type so a caller that only wants the generic
    "server said no" handling still catches it; ``pepper notes edit``'s
    conflict flow catches this specifically, before it would otherwise
    reach that generic handling.

    Carries the artifact's *current* body/etag/updated_at exactly as the
    412 response body did, so the CLI can diff and re-open without a
    second request.
    """

    def __init__(
        self,
        status: int,
        body: str,
        *,
        detail: str | None,
        current_body: str | None,
        current_etag: str | None,
        current_updated_at: str | None,
    ) -> None:
        super().__init__(status, body, detail=detail)
        self.current_body = current_body
        self.current_etag = current_etag
        self.current_updated_at = current_updated_at


# MIRROR: src/pepper/api/router.py::WhoamiResponse
# This package deliberately doesn't depend on pepper-server — the
# pydantic model on the server has a plain-dataclass twin here so
# the iOS/Android port reference stays dep-light. Field changes here
# MUST land at the mirror site in the same PR.
@dataclass(frozen=True)
class Whoami:
    """Response shape of ``GET /api/v1/whoami``.

    Mirrors the server's ``WhoamiResponse`` Pydantic model. Kept as a
    plain dataclass rather than re-using pydantic — the CLI's runtime
    just needs attribute access, and avoiding pydantic keeps the
    dependency tree tight for the iOS/Android port reference.
    """

    user_id: str
    email: str | None
    display_name: str
    timezone: str | None
    #: Every source-account id ever merged into this one (direct or
    #: chained). Always present; empty when this user was never a merge
    #: target.
    merged_from: list[str] = field(default_factory=list)
    #: The account's date order and clock, normalized server-side.
    #: Defaulted so a server older than the field still parses.
    date_format: str = "mdy"
    time_format: str = "12h"
    #: The caller's groups *person* id (what group messages carry in ``mentions`` and
    #: ``author_person_id``), distinct from ``user_id``. ``None`` when the server does not
    #: know it or is older than the field.
    person_id: str | None = None


# MIRROR: src/pepper/api/router.py::SkillSummary
# Same dual-definition rule as Whoami above — keep the field list in
# sync with the server's pydantic twin.
@dataclass(frozen=True)
class SkillSummary:
    """One entry from ``GET /api/v1/skills``.

    Mirrors the server's ``SkillSummary`` Pydantic model. Used by the
    REPL to build its slash-command completer and by ``pepper skills
    list``; the full skill body isn't part of the wire shape — fetch
    one skill with :meth:`ApiClient.get_skill` when you need it.
    ``builtin`` distinguishes Pepper's shared built-ins (editing one
    means creating a same-named override) from the user's own rows.
    """

    name: str
    description: str
    builtin: bool = False


# MIRROR: src/pepper/skills/router.py::SkillRead
# Plain-dataclass twin of the server's full skill shape (the CLI never
# depends on pepper-server). Field changes there MUST land here in the
# same PR. ``id``/``created_at``/``updated_at`` are deliberately
# dropped — the editor round-trip doesn't need them.
@dataclass(frozen=True)
class SkillDetail:
    """Full skill from ``GET /api/v1/skills/{name}``.

    ``model`` / ``toolsets`` / ``max_tool_rounds`` are the optional
    frontmatter extras; ``builtin`` marks a shared built-in served by
    the read-only fall-back (PUT/DELETE on it 404 — create an override
    instead).
    """

    name: str
    description: str
    body: str
    model: str | None
    toolsets: tuple[str, ...]
    max_tool_rounds: int | None
    builtin: bool


def _skill_detail_from_dict(row: dict) -> SkillDetail:
    return SkillDetail(
        name=row["name"],
        description=row["description"],
        body=row["body"],
        model=row.get("model"),
        toolsets=tuple(row.get("toolsets") or ()),
        max_tool_rounds=row.get("max_tool_rounds"),
        builtin=bool(row.get("builtin", False)),
    )


# MIRROR: src/pepper/api/group_skills.py::RoomSkillRow
# Same dual-definition rule as Whoami above — keep the field list in
# sync with the server's pydantic twin.
@dataclass(frozen=True)
class RoomSkill:
    """One skill shared with a room, from ``GET /api/v1/groups/{id}/skills``.

    A room never owns a skill: this is a *grant* — a live reference to a
    skill one of its members owns (or a built-in one, which a room owner can
    grant; ``builtin`` marks those) — and ``id`` is the **grant's** id, which
    is what show and revoke are addressed by. Two members can each share a
    skill of the same name, so ``name`` alone does not pick a row.

    ``state`` is ``"active"`` (a room turn can run it), ``"inactive"`` (it
    declares a toolset a room cannot run — ``ungrantable_toolsets`` names
    them) or ``"shadowed"`` (an earlier same-named grant wins).
    ``granted_by`` is a display name, ``None`` when that account is gone.
    ``can_revoke`` is the server's answer for this caller: their own skill,
    or a room they manage.
    """

    id: str
    name: str
    description: str
    granted_by: str | None
    owner_is_you: bool
    builtin: bool
    toolsets: tuple[str, ...]
    state: str
    ungrantable_toolsets: tuple[str, ...]
    can_revoke: bool


# MIRROR: src/pepper/api/group_skills.py::RoomSkillDetail
@dataclass(frozen=True)
class RoomSkillDetail:
    """``GET /api/v1/groups/{id}/skills/{grant_id}``: the row plus the skill
    itself, as the :class:`SkillDetail` ``get_skill`` answers — so the same
    code renders it as SKILL.md."""

    row: RoomSkill
    skill: SkillDetail


def _room_skill_from_dict(row: dict) -> RoomSkill:
    return RoomSkill(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        granted_by=row.get("granted_by"),
        owner_is_you=bool(row.get("owner_is_you", False)),
        builtin=bool(row.get("builtin", False)),
        toolsets=tuple(row.get("toolsets") or ()),
        state=row.get("state", "active"),
        ungrantable_toolsets=tuple(row.get("ungrantable_toolsets") or ()),
        can_revoke=bool(row.get("can_revoke", False)),
    )


# MIRROR: src/pepper/api/groups.py::agent_events_stream
# MIRROR: src/pepper/api/groups.py::room_events_stream
# MIRROR: src/pepper/api/groups.py::topic_events_stream
# (SSE routes; frames are not described by the snapshot)
@dataclass(frozen=True)
class FeedFrame:
    """One SSE frame from ``GET /api/v1/groups/events/stream`` (an agent member's
    event feed), or from one of its scoped forms,
    ``GET /api/v1/groups/{gid}/events/stream`` and
    ``GET /api/v1/groups/{gid}/topics/{tid}/events/stream``.

    ``kind`` is the frame's ``event:`` line: ``"cursor"`` (``payload`` is
    ``{"cursor": N}``, a resume point past events the agent was not in scope
    for), ``"error"`` (``payload`` is ``{"error": slug, "retry": bool}`` and
    the server closes after it), ``"scope"`` (scoped streams only: the first
    data frame, see :class:`WatchScope`), or an event kind such as
    ``"message.created"`` (``payload`` is the event: ``id``, ``kind``,
    ``group_id``, ``person_id``, ``payload``, ``created_at``, and on a scoped
    stream ``room_id``).

    ``"comment"`` (empty ``payload``) stands for an SSE comment line, the
    server's ``: connected`` / ``: ping`` heartbeat: it carries nothing to act
    on but tells the caller the connection is healthy.

    ``id`` is the frame's SSE ``id:``: for an event frame the event id, for a
    ``cursor`` frame the cursor, ``None`` on ``error`` and ``scope``. Either
    of the first two is a valid ``Last-Event-ID`` to resume after.
    """

    kind: str
    id: int | None
    payload: dict[str, Any]


# MIRROR: src/pepper/gateway/channels/groups_service/agent_feed.py::scope_frame
# (the ``scope`` frame's data; SSE frames are not described by the snapshot)
@dataclass(frozen=True)
class WatchScope:
    """What a scoped event stream says it resolved, before it sends any event.

    ``requested_room_id`` is the room id the client put in the path.
    ``room_id`` is the room the stream is actually scoped to: the same id,
    unless the requested room was merged into another, in which case it is
    the surviving room. ``service_group_id`` is the id event frames carry as
    ``group_id``. ``topic_id`` is ``None`` for a room-scoped stream.
    ``agent_person_id`` is the agent's own person id in the groups service.
    """

    room_id: str
    requested_room_id: str
    service_group_id: str
    topic_id: str | None
    agent_person_id: str


#: The ``v`` a ``scope`` frame must carry for :func:`watch_scope_from_frame`
#: to read it. A different version is a frame this client does not understand.
WATCH_SCOPE_VERSION = 1


def watch_scope_from_frame(frame: FeedFrame) -> WatchScope | None:
    """The :class:`WatchScope` a ``scope`` frame carries, or ``None`` when the
    frame is not a ``scope`` frame this client can read (another kind, another
    ``v``, a missing or non-string id). ``None`` is never a scope to trust."""
    if frame.kind != "scope" or frame.payload.get("v") != WATCH_SCOPE_VERSION:
        return None
    data = frame.payload
    ids = [data.get(k) for k in ("room_id", "requested_room_id", "service_group_id")]
    agent = data.get("agent_person_id")
    topic = data.get("topic_id")
    if not all(isinstance(v, str) and v for v in [*ids, agent]):
        return None
    if topic is not None and not (isinstance(topic, str) and topic):
        return None
    return WatchScope(
        room_id=data["room_id"],
        requested_room_id=data["requested_room_id"],
        service_group_id=data["service_group_id"],
        topic_id=topic,
        agent_person_id=data["agent_person_id"],
    )


# MIRROR: src/pepper/api/router.py::chat_send
# (SSE path of the chat route; frames are not described by the snapshot)
@dataclass(frozen=True)
class StreamEvent:
    """One SSE frame from ``POST /api/v1/chat/send`` (SSE path).

    ``kind`` is the SSE ``event:`` line value — one of:

    - ``"model_thought"`` — a *fragment* of a thought, of any size:
      a whole paragraph on the Gemini backend, a single token on the
      OpenAI and Anthropic ones. ``payload`` carries ``{"text": "..."}``.
      A client appends it to the thought it has open; it is never a whole
      thought and never marks a boundary.
    - ``"model_thought_block"`` — the terminal frame for one thought, whose
      ``payload`` ``{"text": "..."}`` is the assembled thought (the same text
      the trajectory stores). Exactly one arrives per thought on every
      backend, so this — not the shape of the fragments — is what closes a
      thought.
    - ``"retry_attempt"`` — the generation was abandoned by a transient
      provider failure and is being re-run. No payload; drop the
      thought accumulated so far, since the abandoned attempt sends no block.
    - ``"tool_call"`` — model invoked a tool. ``payload`` matches the
      server's ``_event_payload_for_sse`` output (``{"name", "args",
      ...}``).
    - ``"tool_result"`` — tool returned. ``payload`` carries
      ``{"name", "status", "text", "latency_ms", "error", ...}``.
    - ``"client_tools"`` — which of the tools this client declared the
      server accepted, and why it dropped the rest. ``payload`` carries
      ``{"accepted": [{"name", "exposed_name"}], "rejected": [{"name",
      "reason"}], "manifest_reason": str | None}``. Informational.
    - ``"client_tool_call"`` — the server is asking **this process** to run
      one of the tools it declared. ``payload`` carries ``{"call_id",
      "name", "exposed_name", "arguments", "deadline_ms"}``, and the answer
      goes back out of band via :meth:`ApiClient.post_tool_result` before
      ``deadline_ms`` elapses. The only frame in this list that is a request
      rather than a notification; ignoring it costs the turn that tool.
    - ``"error"`` — terminal failure (model error, policy denial).
      ``payload`` is ``{"kind": "error"|"denied", "message": "..."}``.
      No further events follow.
    - ``"reply"`` — terminal success. ``payload`` mirrors
      :class:`ChatReply` field-for-field (``reply``, ``trajectory_id``,
      ``user_message_id``, ``assistant_message_id``, ``steps``).
      No further events follow.

    Unknown ``kind`` values pass through unchanged so a forward-
    compatible client (or one talking to a newer server) sees novel
    events without crashing.
    """

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


# MIRROR: src/pepper/api/router.py::TrajectoryRef
@dataclass(frozen=True)
class TrajectoryRef:
    """Header row for a single trajectory.

    Mirrors the server's ``TrajectoryRef`` Pydantic model. ``meta``
    carries the system-stamped commit SHA + dirty flag, mirrored from
    the agent-token export endpoint so the CLI's render can match the
    bash-script fallback exactly.
    """

    id: str
    user_id: str
    channel: str
    #: Which conversation the turn belongs to; ``None`` for turns that
    #: predate the column. Rendered because a mechanism is transport, so
    #: ``channel`` no longer tells you which conversation this is.
    conversation_key: str | None = None
    created_at: str = ""
    closed_at: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


# MIRROR: src/pepper/api/router.py::TrajectoryEventRef
@dataclass(frozen=True)
class TrajectoryEvent:
    """One row from ``trajectory_events``.

    ``payload`` is opaque — its shape varies per ``event_type``, and
    the CLI doesn't try to type it.
    """

    id: str
    seq: int
    event_type: str
    payload: dict[str, Any]
    created_at: str


# MIRROR: src/pepper/api/router.py::TrajectoryDetailResponse
@dataclass(frozen=True)
class TrajectoryDetail:
    """Bundled trajectory + events for ``GET /api/v1/trajectories/{id}``."""

    trajectory: TrajectoryRef
    events: list[TrajectoryEvent]


# MIRROR: src/pepper/api/router.py::TrajectoryListEntry
@dataclass(frozen=True)
class TrajectoryListEntry:
    """One row in ``GET /api/v1/trajectories``."""

    id: str
    channel: str
    created_at: str
    closed_at: str | None
    meta: dict[str, Any]
    event_count: int
    first_user_message: str | None
    group_id: str | None
    group_name: str | None


# MIRROR: src/pepper/api/router.py::ChatHistoryEntry
@dataclass(frozen=True)
class ChatHistoryEntry:
    """One row of ``GET /api/v1/chat/history`` — today's live Pepper thread.

    Only the fields the terminal UI reads. ``role`` is ``"user"`` or
    ``"assistant"``; ``created_at`` is raw UTC ISO-8601 (``Z`` suffix), so a
    client localizes it itself. The mail, brief, draft and schedule-proposal
    cards the server also sends are not mirrored: the UI shows ``text`` only.
    """

    id: str
    role: str
    text: str
    channel: str
    created_at: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    reactions: list[dict[str, Any]] = field(default_factory=list)


# MIRROR: src/pepper/gateway/channels/client_api/adapter.py::ChatSendResponse
# Same dual-definition rule as the other ``/api/v1`` payload shapes.
@dataclass(frozen=True)
class ChatReply:
    """Response shape of ``POST /api/v1/chat/send`` (JSON path).

    Mirrors the server's ``ChatSendResponse``. The reply is raw
    markdown — the CLI renders it locally via
    :mod:`pepper_cli.render`. ``trajectory_id`` is nullable because
    a denied / errored turn never minted one (the SSE branch handles
    this differently, but the JSON branch surfaces those errors as
    :class:`ApiError` before they ever produce a reply).
    """

    reply: str
    trajectory_id: str | None
    user_message_id: str
    assistant_message_id: str
    steps: list[dict[str, Any]]
    #: Why a declared ``client_tools`` manifest was not honoured, or ``None``
    #: when none was declared. Always set on this path when one *was*:
    #: client tools need the streaming path, which is why ``--root`` makes
    #: ``pepper chat`` stream even for a one-shot message.
    client_tools_ignored: str | None = None


# MIRROR: src/pepper/api/router.py::TodoPerson
@dataclass(frozen=True)
class TodoPerson:
    """Who an ask is from or to: the server's ``{user_id, label}`` on a ``TodoEntry``.

    ``label`` is display text the asker's record carries; it is data, never an
    instruction, and is shown through the sanitising renderer. Both are nullable on the
    wire (a person with no account, a record with no display name) and stay ``None``
    here; the renderer supplies the display fallback."""

    user_id: str | None
    label: str | None


# Nested in ``TodoEntry.next_action``; declared inline by the server, so nothing to mirror.
@dataclass(frozen=True)
class TodoNextAction:
    """A todo's GTD-style next action: the one concrete step that moves it on.

    ``by`` is ``"user"`` for text the person wrote and ``"model"`` for text Pepper wrote.
    The text is display data, shown through the sanitising renderer."""

    text: str
    by: str
    updated_at: str


# Nested in ``TodoEntry.next_action_suggestion``; likewise declared inline.
@dataclass(frozen=True)
class TodoNextActionSuggestion:
    """What Pepper proposes as the next action when the person's own text is already set;
    it replaces nothing until accepted (``todos edit --accept-suggestion``)."""

    text: str
    updated_at: str


# MIRROR: src/pepper/api/router.py::TodoEntry
# Same dual-definition rule as the other ``/api/v1`` payload shapes —
# keep the field list in sync with the server's pydantic twin.
@dataclass(frozen=True)
class TodoEntry:
    """One row from ``GET /api/v1/todos`` (and create/done/dismiss).

    Mirrors the server's ``TodoEntry``. Times are raw UTC ISO-8601
    strings (``due_date`` / ``created_at`` / ``done_at``); the CLI
    localizes ``due_date`` for display, unless ``due_is_date_only`` is
    ``true`` — then ``due_date`` is midnight UTC on a named day and must
    be shown as that bare day with no zone conversion. ``source`` is
    ``"user"`` or a producer name; ``source_ref`` is the producer's
    back-link (None for user todos). ``remind_at`` is the UTC instant of
    the todo's one pending reminder, or None.

    ``assignee`` and ``requested_by`` are set on an ask (a todo assigned to a
    room member): who it was asked of and who asked. Both are None on a plain
    todo. The ask's state (``pending``, ``declined``, ...) and a decline's
    reason ride in ``source_ref`` as display data.

    ``next_action`` and ``next_action_suggestion`` are None when the todo has none, and
    also when the server predates them and does not send the keys.
    """

    id: str
    title: str
    details: str | None
    status: str
    source: str
    priority: str | None
    due_date: str | None
    due_is_date_only: bool | None
    created_at: str
    done_at: str | None
    source_ref: dict[str, Any] | None
    remind_at: str | None = None
    assignee: TodoPerson | None = None
    requested_by: TodoPerson | None = None
    next_action: TodoNextAction | None = None
    next_action_suggestion: TodoNextActionSuggestion | None = None


# MIRROR: src/pepper/api/scheduled_tasks.py::TaskSummary
# Same dual-definition rule as the other ``/api/v1`` payload shapes.
@dataclass(frozen=True)
class ScheduledTaskSummary:
    """One row from ``GET /api/v1/scheduled-tasks`` (and every write
    response) — a task's configuration.

    Times are raw UTC ISO-8601 strings; the CLI localizes at display
    time. ``destinations`` has three states and a client must not
    coalesce the last two: a list pins those destinations, ``[]`` is a
    deliberately silent task, and ``None`` inherits the user's
    notification preference.
    """

    id: str
    name: str
    description: str | None
    cron: str | None
    timezone: str | None
    destinations: list[str] | None
    active: bool
    one_shot: bool
    last_run_at: str | None
    next_run_at: str


# MIRROR: src/pepper/api/group_scheduled_tasks.py::RoomTaskSummary
@dataclass(frozen=True)
class RoomScheduledTaskSummary(ScheduledTaskSummary):
    """One row from ``GET /api/v1/groups/{id}/scheduled-tasks`` (and every
    room write response) — the summary plus what a room listing adds.

    On a room's task ``destinations`` being ``None`` means *the room's
    default*, not the reader's notification preference, so ``delivery`` is
    the server's own one-line answer and the thing to print.
    ``last_run_status`` is how the newest run ended; only the listing
    computes it, a write answers ``None``.
    """

    delivery: str
    last_run_status: str | None


# MIRROR: src/pepper/api/scheduled_tasks.py::RunEntry
@dataclass(frozen=True)
class ScheduledTaskRunEntry:
    """One row from ``GET /api/v1/scheduled-tasks/{id}/runs``.

    ``status`` is ``running`` / ``succeeded`` / ``error`` /
    ``interrupted`` — the raw stored value. The server deliberately
    doesn't compute a "stale" label for JSON clients, so a run left
    ``running`` past its lease is the caller's judgment to make.
    """

    id: str
    fired_at: str
    finished_at: str | None
    status: str
    error: str | None
    delivered_channels: list[str] | None
    trajectory_id: str | None
    reply_preview: str | None


# MIRROR: src/pepper/api/scheduled_tasks.py::TaskDetailResponse
@dataclass(frozen=True)
class ScheduledTaskDetail(ScheduledTaskSummary):
    """``GET /api/v1/scheduled-tasks/{id}`` — the summary plus the most
    recent fire, so a caller can show a task card in one round-trip.
    ``latest_run`` is ``None`` when the task has never fired.

    The ``group_*`` fields are filled only for a *room* task (the room
    detail route): the room's name, its members' display names, and the
    server's own sentence for where the task delivers. A room task's
    ``destinations`` of ``None`` means the room's default, so
    ``group_delivery`` is the thing to print there."""

    latest_run: ScheduledTaskRunEntry | None
    group_name: str | None = None
    group_members: list[str] = field(default_factory=list)
    group_delivery: str | None = None


# MIRROR: src/pepper/api/scheduled_tasks.py::ThreadMessageEntry
@dataclass(frozen=True)
class ScheduledTaskMessage:
    """One row from ``GET /api/v1/scheduled-tasks/{id}/thread`` — a
    message in the task's own conversation, oldest first within a page."""

    id: str
    role: str
    text: str
    channel: str
    trajectory_id: str | None
    created_at: str
    attachments: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Research projects (``/api/v1/research-projects``)
#
# Mirrors src/pepper/api/research_projects.py, the personal read routes
# (``planning/research-projects.md``). Every shape is a typed response model, so the
# contract test checks each field below against the snapshot. An optional field the server
# omits reads as ``None``; a required one missing is a malformed response (``_fields``); an
# unknown one is ignored.
# ---------------------------------------------------------------------------


# MIRROR: src/pepper/api/research_projects.py::ResearchPersonOut
@dataclass(frozen=True)
class ResearchPerson:
    user_id: str
    name: str


# MIRROR: src/pepper/api/research_projects.py::ResearchScheduleOut
@dataclass(frozen=True)
class ResearchSchedule:
    """A project's schedule. ``delivery_topic_id`` is the room topic scheduled runs post to;
    ``None`` is a personal project or the room's default delivery. An older server leaves the
    field out: ``delivery_topic_reported`` is then False (it is not a wire field)."""

    cron: str | None
    timezone: str | None
    active: bool
    delivery_topic_id: str | None

    delivery_topic_reported = True  # class attribute, not a dataclass field


# MIRROR: src/pepper/api/research_projects.py::ResearchLastIterationOut
@dataclass(frozen=True)
class ResearchLastIteration:
    id: str
    status: str
    started_at: str
    finished_at: str | None


# MIRROR: src/pepper/api/research_projects.py::ResearchInflightRunOut
@dataclass(frozen=True)
class ResearchInflightRun:
    id: str
    status: str
    requested_at: str
    requested_by: ResearchPerson | None


# MIRROR: src/pepper/api/research_projects.py::ResearchQuestionOut
@dataclass(frozen=True)
class ResearchQuestion:
    id: str
    text: str


# MIRROR: src/pepper/api/research_projects.py::ResearchFilterOut
@dataclass(frozen=True)
class ResearchFilter:
    text: str
    author_user_id: str | None
    author_name: str | None
    set_at: str | None


# MIRROR: src/pepper/api/research_projects.py::ResearchOwnerOut
@dataclass(frozen=True)
class ResearchOwner:
    kind: str
    id: str


# MIRROR: src/pepper/api/research_projects.py::ResearchProjectSummary
@dataclass(frozen=True)
class ResearchProjectSummary:
    """One row of ``GET /api/v1/research-projects``. A queued or running manual run is
    ``inflight_run``; times are raw UTC ISO-8601 strings."""

    id: str
    name: str
    status: str
    delivery_policy: str
    brief_snippet: str
    skill: str
    scheduled_task_id: str | None
    schedule: ResearchSchedule | None
    last_iteration: ResearchLastIteration | None
    inflight_run: ResearchInflightRun | None
    created_at: str


# MIRROR: src/pepper/api/research_projects.py::ResearchProjectDetail
@dataclass(frozen=True)
class ResearchProjectDetail(ResearchProjectSummary):
    """``GET /api/v1/research-projects/{id}``: the summary plus brief, filters, ledger
    counts, pending steering and the report's open questions (``q1``..). ``pending_steering``
    is ``None`` when the server does not send it (an older server), so a renderer falls back to
    ``pending_steering_count``."""

    brief: str
    source_url: str | None
    filters: list[ResearchFilter]
    pending_steering_count: int
    item_counts: dict[str, int]
    report_artifact_id: str | None
    open_questions: list[ResearchQuestion]
    owner: ResearchOwner | None
    pending_steering: list[ResearchSteeringNote] | None


# MIRROR: src/pepper/api/research_projects.py::ResearchSteeringOut
@dataclass(frozen=True)
class ResearchSteeringNote:
    """One entry of a project detail's ``pending_steering``: a note waiting for the next run,
    oldest first. ``author_name`` is ``None`` when the server cannot name the author."""

    id: str
    text: str
    question_id: str | None
    author_user_id: str | None
    author_name: str | None
    created_at: str
    consumed_at: str | None


# MIRROR: src/pepper/api/research_projects.py::ResearchIterationOut
@dataclass(frozen=True)
class ResearchIteration:
    """One row of ``GET .../{id}/iterations``: what actually executed. ``trigger`` is
    ``manual`` or ``scheduled``; ``requested_by`` names who pressed run (manual only)."""

    id: str
    occurrence_key: str | None
    trigger: str
    requested_by: ResearchPerson | None
    run_id: str | None
    status: str
    started_at: str
    finished_at: str | None
    error: str | None
    source_failures: dict[str, Any] | None


# MIRROR: src/pepper/api/research_projects.py::ResearchChangelogOut
@dataclass(frozen=True)
class ResearchChangelogEntry:
    day: str
    text: str
    evidence_ids: list[str]
    iteration_id: str | None


# MIRROR: src/pepper/api/research_projects.py::ResearchReport
@dataclass(frozen=True)
class ResearchReport:
    """``GET .../{id}/report``: the report document (``body``, markdown) and the pieces
    the server parsed out of it."""

    artifact_id: str | None
    body: str
    summary: str
    current_recommendation: str
    open_questions: list[ResearchQuestion]
    changelog: list[ResearchChangelogEntry]
    collapsed_changelog: int
    updated_at: str | None


# MIRROR: src/pepper/api/research_projects.py::ResearchItemOut
@dataclass(frozen=True)
class ResearchItem:
    """One ledger row. ``seq`` is globally monotonic, not a per-project counter, so the CLI
    never shows it as a number to type (``planning/research-projects.md`` §5.2)."""

    id: str
    seq: int
    title: str
    state: str
    state_reason: str | None
    url: str | None
    summary: str | None
    first_seen_at: str
    state_changed_at: str


# MIRROR: src/pepper/api/research_projects.py::ResearchItemDetail
@dataclass(frozen=True)
class ResearchItemDetail(ResearchItem):
    source_type: str
    creator: str | None
    year: int | None
    published_at: str | None
    citations: list[Any]
    superseded_by: str | None


# MIRROR: src/pepper/api/research_projects.py::ResearchRunReceipt
@dataclass(frozen=True)
class ResearchRunReceipt:
    """``POST .../{id}/runs`` (202): the queued run to poll."""

    run_id: str
    project_id: str
    status: str
    requested_at: str


# MIRROR: src/pepper/api/research_projects.py::ResearchRunOut
@dataclass(frozen=True)
class ResearchRun:
    """A manual run receipt. ``status`` is ``queued``, ``running``, ``succeeded``, ``error``
    or ``interrupted``; the last three are terminal."""

    id: str
    status: str
    requested_by_user_id: str
    requested_at: str
    started_at: str | None
    finished_at: str | None
    attempts: int
    error: str | None


# MIRROR: src/pepper/api/research_projects.py::ResearchRunDetail
@dataclass(frozen=True)
class ResearchRunDetail(ResearchRun):
    """``GET .../{id}/runs/{run_id}``: the receipt, the iteration it produced (once it has
    started one) and the change-log lines that iteration wrote."""

    iteration: ResearchIteration | None
    changelog: list[ResearchChangelogEntry]


# MIRROR: src/pepper/api/research_projects.py::ResearchSteeringOut
@dataclass(frozen=True)
class ResearchSteering:
    id: str
    text: str
    question_id: str | None
    author_user_id: str | None
    author_name: str | None
    created_at: str
    consumed_at: str | None


#: A run in one of these states is over (the server's ``RunDetail``).
RESEARCH_RUN_TERMINAL = frozenset({"succeeded", "error", "interrupted"})


class ResearchRunInFlightError(ApiError):
    """A 409 whose body names the run already queued or running (server G1): not a failure,
    the run to attach to. ``run_id`` is in canonical lowercase."""

    def __init__(self, exc: ApiError, run_id: str) -> None:
        super().__init__(exc.status, exc.body, detail=exc.detail)
        self.run_id = run_id


class ResearchAlreadyScheduledError(ApiError):
    """A 409 whose body carries ``scheduled_task_id``: the project already has a schedule.
    The server points at ``PATCH /api/v1/scheduled-tasks/{id}`` to change it."""

    def __init__(self, exc: ApiError, scheduled_task_id: str) -> None:
        super().__init__(exc.status, exc.body, detail=exc.detail)
        self.scheduled_task_id = scheduled_task_id


@dataclass(frozen=True)
class ResearchPage[T]:
    """A ``{items, next_before}`` page; ``next_before`` is the server's opaque cursor."""

    items: list[T]
    next_before: str | None


# MIRROR: src/pepper/web/auth/device_router.py::DeviceStartResponse
# Same dual-definition rule as the other wire shapes in this module.
@dataclass(frozen=True)
class DeviceStart:
    """Response shape of ``POST /auth/device/start`` (unauthenticated).

    ``device_code`` is the secret the client polls with — wrapped in
    :class:`SecretStr` like every other bearer-shaped value in this
    module, even though it isn't a login token itself. ``user_code``
    is the short human-facing pair (``XXXX-XXXX``) meant to be printed
    and typed/confirmed by a person, so it stays a plain string.
    """

    device_code: SecretStr
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


# MIRROR: src/pepper/web/auth/device_router.py::device_poll (response shapes)
@dataclass(frozen=True)
class DevicePoll:
    """Result of one ``POST /auth/device/poll`` attempt (unauthenticated).

    ``status`` is one of ``"pending"``, ``"denied"``, ``"expired"``,
    ``"approved"``, or ``"slow_down"``. The last one is synthesized by
    :func:`device_poll` from the server's HTTP 429 response rather than
    raised as an :class:`ApiError` — a poll loop treats "you're polling
    too fast" as an ordinary backoff signal, not a failure. ``token`` /
    ``user`` are only populated when ``status == "approved"``;
    ``interval`` (seconds the server suggests backing off by) only when
    ``status == "slow_down"``.
    """

    status: str
    token: SecretStr | None = None
    user: Whoami | None = None
    interval: int | None = None


def device_start(
    server_url: str,
    device_name: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    client: httpx.Client | None = None,
) -> DeviceStart:
    """``POST /auth/device/start``. Begins a device-authorization login.

    Deliberately a free function, not an ``ApiClient`` method: this
    call (and :func:`device_poll`) happen *before* there's a token to
    authenticate with, so there's no ``ApiClient`` instance to hang it
    off of yet. ``device_name`` is a free-text label (1-80 chars) shown
    to the human on the web approval page — ``pepper login`` passes
    ``user@hostname``.

    ``client`` is the test seam (pass an ``httpx.Client`` wrapping a
    ``MockTransport``); when omitted a throwaway client is constructed
    and closed before returning. Raises :class:`ApiError` on a non-2xx
    response (422 for a bad ``device_name``, 5xx for a server hiccup);
    ``httpx.RequestError`` propagates for transport failures, same as
    every other call in this module.
    """
    server_url = server_url.rstrip("/")
    owns_client = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        resp = http.post(
            f"{server_url}/auth/device/start",
            json={"client": "cli", "device_name": device_name},
            headers={"User-Agent": USER_AGENT},
        )
        if resp.status_code >= 400:
            _raise_for_error(resp)
        body = resp.json()
        return DeviceStart(
            device_code=SecretStr(body["device_code"]),
            user_code=body["user_code"],
            verification_url=body["verification_url"],
            expires_in=body["expires_in"],
            interval=body["interval"],
        )
    finally:
        if owns_client:
            http.close()


def device_poll(
    server_url: str,
    device_code: SecretStr,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    client: httpx.Client | None = None,
) -> DevicePoll:
    """``POST /auth/device/poll``. One poll attempt — the caller loops.

    A 429 response ("you're polling too fast") maps to
    ``DevicePoll(status="slow_down", interval=...)`` rather than an
    :class:`ApiError` — see :class:`DevicePoll`. Every other non-2xx
    response still raises :class:`ApiError` normally.
    """
    server_url = server_url.rstrip("/")
    owns_client = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        resp = http.post(
            f"{server_url}/auth/device/poll",
            json={"device_code": device_code.get_secret_value()},
            headers={"User-Agent": USER_AGENT},
        )
        if resp.status_code == 429:
            interval: int | None = None
            try:
                slow_body = resp.json()
            except ValueError:
                slow_body = {}
            if isinstance(slow_body, dict):
                interval = slow_body.get("interval")
            return DevicePoll(status="slow_down", interval=interval)
        if resp.status_code >= 400:
            _raise_for_error(resp)
        body = resp.json()
        status_value = body.get("status", "")
        if status_value == "approved":
            user_row = body["user"]
            return DevicePoll(
                status="approved",
                token=SecretStr(body["token"]),
                user=Whoami(
                    user_id=user_row["user_id"],
                    email=user_row.get("email"),
                    display_name=user_row["display_name"],
                    timezone=user_row.get("timezone"),
                    merged_from=user_row.get("merged_from", []),
                    date_format=user_row.get("date_format", "mdy"),
                    time_format=user_row.get("time_format", "12h"),
                ),
            )
        return DevicePoll(status=status_value)
    finally:
        if owns_client:
            http.close()


# ---------------------------------------------------------------------------
# Groups
#
# MIRROR: src/pepper/api/groups.py — the route module that the server's
# other front doors share. Field changes on the server MUST be matched here:
# this package is the canonical reference a native port reproduces, and a
# reference client one release behind stops being a reference.
#
# Where a shape below is a plain ``dict`` rather than a dataclass, the
# docstring says why. The rule is: anything a *caller* reads field by field
# gets a dataclass; a blob a caller hands straight to a renderer (room info,
# an activity row) stays a dict, because typing thirty fields that nothing in
# this package reads would be a second description to drift from rather than a
# contract.
# ---------------------------------------------------------------------------


# MIRROR: src/pepper/api/groups.py::room_topics
# (route handler; the response is an untyped dict, so no snapshot schema)
@dataclass(frozen=True)
class GroupTopicSummary:
    """One topic, as the rooms list and the topic rail both send it.

    ``last_activity_at`` is the *recency* key, not the raw field — a topic the
    service never bumped carries its creation time here rather than ``None``,
    so a client can sort without a special case.
    """

    topic_id: str
    name: str
    last_activity_at: str
    dedicated_conversation: bool = False
    archived_at: str | None = None
    imported_history: bool = False
    #: Whether this is the room's default topic — the one an unprompted post
    #: lands in. Only the topic rail
    #: (``GET .../topics``) says so; the rooms list leaves it ``False``.
    is_default: bool = False


# MIRROR: src/pepper/api/groups.py::list_groups
# (route handler; untyped dict response, no snapshot schema)
@dataclass(frozen=True)
class GroupSummary:
    """One room on ``GET /api/v1/groups``.

    ``sub`` is a preformatted relative label ("active 2h ago") and ``color``
    a hex accent — both server-rendered, both to be shown rather than
    re-derived. ``archived`` is a *per-viewer* preference, not a
    groups-service concept.
    """

    group_id: str
    display_name: str
    color: str
    sub: str
    member_count: int
    archived: bool
    topics: list[GroupTopicSummary] = field(default_factory=list)


# MIRROR: src/pepper/api/groups.py::list_groups
# (route handler; untyped dict response, no snapshot schema)
@dataclass(frozen=True)
class GroupList:
    """``GET /api/v1/groups`` — and the capability signal for the surface.

    ``enabled`` is ``False`` when the deployment has no groups service
    configured: there is no per-user flag, so this is how a client decides
    whether to offer a Groups surface at all. ``degraded`` means something
    different — "could not ask" — and a client tracking unread must hold its
    last-known state rather than read it as "all caught up".

    ``features`` names what the server can do beyond the base surface. A
    server that predates the field sends none, which reads as "nothing
    extra": ``"watch_scope"`` (the room- and topic-scoped event streams) is
    used only when the server lists it.
    """

    enabled: bool
    degraded: bool
    total: int
    archived_total: int
    groups: list[GroupSummary] = field(default_factory=list)
    features: tuple[str, ...] = ()


# MIRROR: src/pepper/api/groups.py::topic_messages
# (route handler; untyped dict response, no snapshot schema)
@dataclass(frozen=True)
class GroupCard:
    """One structured app card carried on a message (``cards[]``).

    Replaces the web door's ``card_html``: a native client cannot inject a
    host-rendered fragment into a native stream. ``kind`` is ``"card"`` for an
    app summary and ``"conversation"`` for the room's in-app thread with
    Pepper — a client that does not know a kind degrades to the markdown link
    the body still carries, which is what the web page does too.
    """

    kind: str
    app_key: str
    slug: str
    title: str
    principal: str
    url: str
    view: str | None = None
    summary_lines: list[str] = field(default_factory=list)


# MIRROR: src/pepper/api/groups.py::topic_messages
# (route handler; untyped dict response, no snapshot schema)
@dataclass(frozen=True)
class GroupMessage:
    """One message in a topic.

    Every field the server sends, including the ones this package does not
    read: the point of the mirror is that a native port can see the whole
    shape here. Three things are worth knowing before reproducing it:

    - ``raw_body`` is the markdown source and is what a native client
      renders; ``rendered_html`` is the web door's render, kept on both doors
      so there is one shape, and ignored by a native client.
    - ``created_at_display``, ``day_label``, ``day_kicker`` and
      ``day_heading`` are **preformatted server-side** in the user's own
      timezone and date format. Show them; never re-derive them. A client
      computes only the *grouping* (which messages share a day, which start a
      run) from ``day_key`` and ``run_start``.
    - ``reactions`` is server-authoritative, ``mine`` included — a client
      never computes whether a reaction is its own.
    """

    id: str
    is_pepper: bool
    is_you: bool
    is_deleted: bool
    is_broken: bool
    run_start: bool
    display_name: str
    initial: str
    avatar_tint: str
    raw_body: str
    rendered_html: str
    created_at: str
    created_at_display: str
    day_key: str
    day_kicker: str
    day_heading: str
    day_label: str
    quote_excerpt: str
    picture_url: str | None = None
    #: The bridge the message arrived through ("via X"). The server stamps Pepper here
    #: on every ``/api/v1`` post as an audit marker, so a client shows the chip only when
    #: ``is_bridged`` is also true — the web does the same.
    via_display_name: str | None = None
    #: True when ``via_display_name`` names a real bridge (an email or chat mirror), not
    #: the audit marker. ``False`` on rows from an older server that does not send it.
    is_bridged: bool = False
    #: True when the person wrote this in Pepper's composer and Pepper posted it for them
    #: (groups#279's ``composed_via``): a native post, not a bridge. ``False`` on rows from
    #: an older server that does not send it.
    composed_via: bool = False
    is_edited: bool = False
    edited_at: str | None = None
    deleted_at: str | None = None
    reactions: list[dict[str, Any]] = field(default_factory=list)
    link_previews: list[dict[str, Any]] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    mention_names: list[str] = field(default_factory=list)
    quote: dict[str, Any] | None = None
    cards: list[GroupCard] = field(default_factory=list)
    is_permalink_target: bool = False
    #: Structured ids alongside the display fields (``display_name``,
    #: ``mention_names``, ``quote``): the groups service's own ids for the
    #: author, the mentioned persons, and the parent. A caller
    #: deciding *who* wrote a message or *whom* it addresses — an
    #: agent-member watcher, say — needs ids, not names.
    #: ``None``/``[]`` on rows from an older server that does not send them.
    #: Nothing in this package reads them.
    author_person_id: str | None = None
    mentions: list[str] = field(default_factory=list)
    parent_message_id: str | None = None
    #: An agent member's post, and whom it was
    #: answering — "*agent* · for *requester*". ``False``/``None`` on rows
    #: from an older server.
    is_agent: bool = False
    for_display_name: str | None = None


# MIRROR: src/pepper/api/groups.py::topic_messages
# (route handler; untyped dict response, no snapshot schema)
@dataclass(frozen=True)
class GroupMessagePage:
    """One page of a topic's messages, oldest-first.

    ``next_before`` is the cursor for the page *older* than this one, and
    ``None`` once there is nothing older. ``permalink_target_missing`` says an
    ``at=`` anchor was not found and this is the newest page instead — a note,
    never an error: the room is real, only the anchor was not.
    """

    messages: list[GroupMessage] = field(default_factory=list)
    next_before: str | None = None
    permalink_target_missing: bool = False


# MIRROR: src/pepper/api/groups.py::room_roster
# (route handler; untyped dict response, no snapshot schema)
@dataclass(frozen=True)
class GroupRosterMember:
    """One addressable member. Pepper is prepended by the server and flagged
    ``is_pepper`` — she is never a membership row, so a roster built from the
    member list alone would never offer her."""

    person_id: str
    display_name: str
    picture_url: str | None = None
    is_pepper: bool = False
    #: An agent member, so ``groups roster``
    #: never presents one as a person.
    is_agent: bool = False
    #: The harness profile the adder picked for an agent member in this room
    #: ``None`` for a person and for Pepper.
    agent_profile: str | None = None


# MIRROR: src/pepper/api/groups.py::app_launcher
# (route handler; untyped dict response, no snapshot schema)
@dataclass(frozen=True)
class GroupAppTile:
    """One tile of a room's launcher. ``principal`` is the room, so opening
    the tile opens the *room's* copy of the app rather than the caller's."""

    app_key: str
    slug: str
    title: str
    principal: str
    icon: str | None = None


def _group_card_from_dict(row: dict[str, Any]) -> GroupCard:
    summary = row.get("summary") or {}
    return GroupCard(
        kind=row.get("kind", "card"),
        app_key=row["app_key"],
        slug=row["slug"],
        title=row["title"],
        principal=row["principal"],
        url=row["url"],
        view=row.get("view"),
        summary_lines=list(summary.get("lines") or []),
    )


def _mention_ids(raw: object) -> list[str]:
    """Bare person-id strings from a ``mentions`` field, whichever of the
    two shapes the service uses (a bare id, or a dict carrying
    ``person_id``); anything else is dropped rather than guessed at."""
    if not isinstance(raw, list):
        return []
    ids: list[str] = []
    for item in raw:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict) and isinstance(item.get("person_id"), str):
            ids.append(item["person_id"])
    return ids


def group_message_from_dict(row: dict[str, Any]) -> GroupMessage:
    """Parse one message. A free function so the page read and any later
    single-message read parse through one place and cannot drift."""
    return GroupMessage(
        id=row["id"],
        is_pepper=row["is_pepper"],
        is_you=row["is_you"],
        is_deleted=row["is_deleted"],
        is_broken=row["is_broken"],
        run_start=row["run_start"],
        display_name=row["display_name"],
        initial=row["initial"],
        avatar_tint=row["avatar_tint"],
        raw_body=row["raw_body"],
        rendered_html=row["rendered_html"],
        created_at=row["created_at"],
        created_at_display=row["created_at_display"],
        day_key=row["day_key"],
        day_kicker=row["day_kicker"],
        day_heading=row["day_heading"],
        day_label=row["day_label"],
        quote_excerpt=row["quote_excerpt"],
        picture_url=row.get("picture_url"),
        via_display_name=row.get("via_display_name"),
        is_bridged=bool(row.get("is_bridged", False)),
        # ``composed_via`` is groups' name; ``is_composed`` is the render-dict spelling that
        # sits beside ``is_bridged``. Accept either so the label shows whichever pepper serves.
        composed_via=bool(row.get("composed_via") or row.get("is_composed")),
        is_edited=row.get("is_edited", False),
        edited_at=row.get("edited_at"),
        deleted_at=row.get("deleted_at"),
        reactions=list(row.get("reactions") or []),
        link_previews=list(row.get("link_previews") or []),
        attachments=list(row.get("attachments") or []),
        mention_names=list(row.get("mention_names") or []),
        quote=row.get("quote"),
        cards=[_group_card_from_dict(c) for c in row.get("cards") or []],
        is_permalink_target=row.get("is_permalink_target", False),
        author_person_id=row.get("author_person_id"),
        mentions=_mention_ids(row.get("mentions")),
        is_agent=bool(row.get("is_agent", False)),
        for_display_name=row.get("for_display_name"),
        parent_message_id=row.get("parent_message_id"),
    )


def _group_list_from_dict(body: dict[str, Any]) -> GroupList:
    return GroupList(
        enabled=body["enabled"],
        degraded=body["degraded"],
        total=body["total"],
        archived_total=body["archived_total"],
        groups=[_group_summary_from_dict(g) for g in body.get("groups") or []],
        features=tuple(f for f in body.get("features") or () if isinstance(f, str)),
    )


def _group_summary_from_dict(row: dict[str, Any]) -> GroupSummary:
    return GroupSummary(
        group_id=row["group_id"],
        display_name=row["display_name"],
        color=row["color"],
        sub=row["sub"],
        member_count=row["member_count"],
        archived=row["archived"],
        topics=[_group_topic_from_dict(t) for t in row.get("topics") or []],
    )


def _group_topic_from_dict(row: dict[str, Any]) -> GroupTopicSummary:
    return GroupTopicSummary(
        topic_id=row["topic_id"],
        name=row["name"],
        last_activity_at=row["last_activity_at"],
        dedicated_conversation=row.get("dedicated_conversation", False),
        archived_at=row.get("archived_at"),
        imported_history=row.get("imported_history", False),
        is_default=row.get("is_default", False),
    )


# ---------------------------------------------------------------------------
# Artifacts / notes
#
# MIRROR: src/pepper/api/artifacts.py::ArtifactListItem / ArtifactListResponse
# (``GET /api/v1/artifacts``) and the server's artifact-detail reader
# (``GET /api/v1/artifacts/{id}/detail``). The detail route answers a plain
# ``JSONResponse``, not a declared Pydantic model, so :class:`ArtifactDetail`
# below is a curated subset of that function's returned dict — the fields
# ``pepper notes`` reads — rather than a field-for-field mirror. Field
# changes to either MUST be matched here.
#
# ``pepper notes`` is the one caller today and
# always passes ``kind="inline_text"``, but these shapes are kind-agnostic
# like the routes themselves — a future ``images``/``links`` surface would
# read the same types.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactListItem:
    """One row of ``GET /api/v1/artifacts``.

    No ``body`` — a shelf of a hundred notes costs a hundred summaries, not a
    hundred bodies. :meth:`ApiClient.get_artifact_detail` reads one artifact's
    full text once it's open.
    """

    id: str
    name: str
    kind: str
    updated_at: str
    etag: str
    labels: list[str] = field(default_factory=list)
    summary: str | None = None


@dataclass(frozen=True)
class ArtifactListResponse:
    """The page, plus whether it's a page.

    ``truncated`` comes from the server fetching one row past ``limit`` — see
    ``list_artifacts_route``'s own docstring — not from a separate count.
    """

    truncated: bool
    items: list[ArtifactListItem] = field(default_factory=list)


# MIRROR: src/pepper/agent/artifacts/applet.py::read_detail
# (the detail route answers an untyped JSONResponse, no snapshot schema)
@dataclass(frozen=True)
class ArtifactDetail:
    """One opened artifact from ``GET /api/v1/artifacts/{id}/detail``.

    ``body`` is the markdown exactly as written — ``None`` for a non-text
    artifact, never the rendered HTML (``read_detail``'s own docstring: an
    editor needs what was typed, and rendering to HTML and back is not a
    round trip). ``shared`` is true when the artifact belongs to a group the
    caller is a member of rather than to the caller themself.
    """

    id: str
    name: str
    kind: str
    updated_at: str
    etag: str
    shared: bool
    body: str | None = None
    labels: list[str] = field(default_factory=list)
    summary: str | None = None


def _artifact_list_item_from_dict(row: dict[str, Any]) -> ArtifactListItem:
    return ArtifactListItem(
        id=str(row["id"]),
        name=row["name"],
        kind=row["kind"],
        updated_at=row["updated_at"],
        etag=row["etag"],
        labels=list(row.get("labels") or []),
        summary=row.get("summary"),
    )


def _artifact_detail_from_dict(body: dict[str, Any]) -> ArtifactDetail:
    """Parse a detail-shaped dict into :class:`ArtifactDetail`.

    One place for this rather than inlining it at each call site: the same
    shape comes back from ``GET /{id}/detail`` (:meth:`ApiClient.get_artifact_detail`)
    and from the ``artifact`` field of ``POST /{id}/edit``'s success response
    (:meth:`ApiClient.edit_artifact`) — both routes build it from the
    server's own ``read_detail`` — so parsing it once here means the two
    callers can't drift on which fields are required versus defaulted.
    """
    return ArtifactDetail(
        id=str(body["id"]),
        name=body["name"],
        kind=body["kind"],
        updated_at=body["updated_at"],
        etag=body["etag"],
        shared=bool(body.get("shared", False)),
        body=body.get("body"),
        labels=list(body.get("labels") or []),
        summary=body.get("summary"),
    )


# MIRROR: src/pepper/api/artifacts.py::ArtifactCreateResponse
@dataclass(frozen=True)
class ArtifactCreateResult:
    """Response of ``POST /api/v1/artifacts/new`` (:meth:`ApiClient.create_artifact`).

    Deliberately not a full :class:`ArtifactDetail` — the create route
    answers only ``id``/``name``/``etag`` (``ArtifactCreateResponse``
    server-side; a caller that wants the rest re-reads ``/detail``, which
    is exactly what a client that immediately opens what it just created
    would do anyway).
    """

    id: str
    name: str
    etag: str


class ApiClient:
    """Bearer-token client for one (server_url, token) pair.

    Constructed once per CLI invocation. Holds a single
    ``httpx.Client`` so connection reuse + TLS session caching
    work — matters less for the one-shot ``pepper chat`` path and
    more for the REPL, which sends many turns over one client.

    Use as a context manager (``with ApiClient(...) as api:``) so the
    underlying httpx client closes deterministically; the CLI's
    top-level handler does exactly that.
    """

    def __init__(
        self,
        *,
        server_url: str,
        token: SecretStr,
        timeout: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        # ``server_url`` is stored without trailing slash (the
        # credentials loader already strips it, but defense in depth
        # here matters because tests pass URLs directly).
        self._server_url = server_url.rstrip("/")
        # ``client`` is the test seam — pass an ``httpx.Client`` with a
        # ``MockTransport`` to exercise this module without a real
        # server. Production code constructs one for itself.
        self._client = client or httpx.Client(
            timeout=DEFAULT_TIMEOUT if timeout is None else timeout
        )
        # Keep explicit caller/test timeouts intact. Only the normal CLI
        # default gets more time between SSE frames for a long model turn;
        # other API calls still use the ordinary 60-second limit.
        self._chat_stream_read_timeout = (
            client.timeout.read
            if client is not None
            else DEFAULT_CHAT_STREAM_READ_TIMEOUT
            if timeout is None
            else timeout
        )
        # Hold the :class:`SecretStr` rather than the pre-formatted
        # header — the unwrap happens at each request in
        # :meth:`_request` so the plaintext doesn't live on a long-
        # lived attribute that ``repr(api)`` could surface.
        self._token = token

    @property
    def server_url(self) -> str:
        """The server's base URL, without a trailing slash (a client resolves the
        server-relative links a message carries against it)."""
        return self._server_url

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Endpoint methods

    def whoami(self) -> Whoami:
        """``GET /api/v1/whoami``. Used by ``pepper login`` to validate
        a freshly-pasted token, and by ``pepper whoami`` to show the
        identity behind the current credentials."""
        resp = self._request("GET", "/api/v1/whoami")
        body = resp.json()
        return Whoami(
            user_id=body["user_id"],
            email=body.get("email"),
            display_name=body["display_name"],
            timezone=body.get("timezone"),
            merged_from=body.get("merged_from", []),
            date_format=body.get("date_format", "mdy"),
            time_format=body.get("time_format", "12h"),
            person_id=body.get("person_id"),
        )

    def list_skills(self) -> list[SkillSummary]:
        """``GET /api/v1/skills``. Returns the calling user's skills
        (name, description, and whether each is a shared built-in).

        The REPL calls this once at startup to seed its slash-command
        completer. Errors propagate as :class:`ApiError` so the REPL
        can choose to bail (401) or degrade to static-only completion
        (everything else)."""
        resp = self._request("GET", "/api/v1/skills")
        body = resp.json()
        return [
            SkillSummary(
                name=row["name"],
                description=row["description"],
                builtin=bool(row.get("builtin", False)),
            )
            for row in body
        ]

    def get_skill(self, name: str) -> SkillDetail:
        """``GET /api/v1/skills/{name}`` — full skill, yours or built-in.

        The server serves your row when you have one, else falls back to
        the shared built-in (``builtin=True`` in the result). 404 →
        :class:`ApiError`."""
        resp = self._request("GET", f"/api/v1/skills/{name}")
        return _skill_detail_from_dict(resp.json())

    def create_skill(
        self,
        *,
        name: str,
        description: str,
        body: str,
        model: str | None = None,
        toolsets: list[str] | None = None,
        max_tool_rounds: int | None = None,
    ) -> SkillDetail:
        """``POST /api/v1/skills``. 409 when you already have the name;
        422 with the server's issue list on invalid input."""
        payload: dict = {"name": name, "description": description, "body": body}
        if model is not None:
            payload["model"] = model
        if toolsets:
            payload["toolsets"] = toolsets
        if max_tool_rounds is not None:
            payload["max_tool_rounds"] = max_tool_rounds
        resp = self._request("POST", "/api/v1/skills", json=payload)
        return _skill_detail_from_dict(resp.json())

    def update_skill(
        self,
        *,
        name: str,
        description: str,
        body: str,
        model: str | None = None,
        toolsets: list[str] | None = None,
        max_tool_rounds: int | None = None,
    ) -> SkillDetail:
        """``PUT /api/v1/skills/{name}`` — full replace of YOUR row.

        Extras omitted are cleared server-side, so callers pass exactly
        what the edited document declares. A built-in-only name 404s
        (create a same-named skill to override it instead)."""
        payload: dict = {"description": description, "body": body}
        if model is not None:
            payload["model"] = model
        if toolsets:
            payload["toolsets"] = toolsets
        if max_tool_rounds is not None:
            payload["max_tool_rounds"] = max_tool_rounds
        resp = self._request("PUT", f"/api/v1/skills/{name}", json=payload)
        return _skill_detail_from_dict(resp.json())

    def delete_skill(self, name: str) -> None:
        """``DELETE /api/v1/skills/{name}`` — your row only; 404 otherwise."""
        self._request("DELETE", f"/api/v1/skills/{name}")

    def list_room_skills(self, group_id: str) -> list[RoomSkill]:
        """``GET /api/v1/groups/{id}/skills``. Every skill shared with the
        room, for any member — including ones a room turn cannot run now."""
        resp = self._request("GET", _room_skills_path(group_id))
        body = resp.json()
        if not isinstance(body, list):
            raise ApiError(status=resp.status_code, body=resp.text, detail="malformed response")
        return [_room_skill_from_dict(row) for row in body]

    def get_room_skill(self, group_id: str, grant_id: str) -> RoomSkillDetail:
        """``GET /api/v1/groups/{id}/skills/{grant_id}``. 404 for an id that
        is not one of this room's grants."""
        resp = self._request("GET", f"{_room_skills_path(group_id)}/{grant_id}")
        body = resp.json()
        if not isinstance(body, dict) or not isinstance(body.get("skill"), dict):
            raise ApiError(status=resp.status_code, body=resp.text, detail="malformed response")
        return RoomSkillDetail(
            row=_room_skill_from_dict(body), skill=_skill_detail_from_dict(body["skill"])
        )

    def share_room_skill(self, group_id: str, name: str) -> RoomSkill:
        """``POST /api/v1/groups/{id}/skills`` — share one of YOUR skills.

        Idempotent: sharing one the room already holds answers the same row.
        404 no such skill of yours · 409 a room that takes no skills · 422 a
        skill a room cannot run."""
        resp = self._request("POST", _room_skills_path(group_id), json={"name": name})
        body = resp.json()
        if not isinstance(body, dict):
            raise ApiError(status=resp.status_code, body=resp.text, detail="malformed response")
        return _room_skill_from_dict(body)

    def revoke_room_skill(self, group_id: str, grant_id: str) -> None:
        """``DELETE /api/v1/groups/{id}/skills/{grant_id}`` — stop sharing it.
        The skill is untouched. 403 unless it is your skill or you own the
        room; 404 for an id that is not one of this room's grants."""
        self._request("DELETE", f"{_room_skills_path(group_id)}/{grant_id}")

    def list_todos(
        self,
        *,
        status: str = "open",
        group_id: str | None = None,
        mine: bool = False,
        asked: bool = False,
    ) -> list[TodoEntry]:
        """``GET /api/v1/todos?status=`` (a room's: ``/api/v1/groups/{id}/todos``).

        ``status`` is one of ``open`` (default) / ``done`` / ``dismissed``
        / ``all``; the server 422s anything else. Open items come back
        ordered by due date (soonest first, undated last).

        ``mine`` keeps what someone asked of the caller: ``asked_of_me=true`` on
        the caller's own list (every room), ``assignee=me`` on a room's list.
        ``asked`` keeps what the caller asked (``requester=me``) and is a room
        list's filter only; the server does the filtering."""
        path = f"{_todos_path(group_id)}?status={status}"
        if mine:
            path += "&assignee=me" if group_id else "&asked_of_me=true"
        if asked:
            if not group_id:
                raise ValueError("asked is a room list's filter: pass group_id")
            path += "&requester=me"
        resp = self._request("GET", path)
        body = resp.json()
        if not isinstance(body, list):
            raise ApiError(status=resp.status_code, body=resp.text, detail="malformed response")
        return [_todo_from_dict(row) for row in body]

    def calendar_range(
        self, *, start: str, days: int, principal: str | None = None
    ) -> dict[str, Any]:
        """``GET /api/v1/calendars/range?start=YYYY-MM-DD&days=N[&principal=]`` (1-31 days).

        ``principal`` is a room id: the room's calendar view instead of the caller's.
        Returns the server's preformatted agenda payload as-is."""
        return self._calendar_object("range", start=start, days=days, principal=principal)

    def calendar_planning(
        self, *, start: str, days: int, principal: str | None = None
    ) -> dict[str, Any]:
        """``GET /api/v1/calendars/planning?start=&days=[&principal=]`` (1-7 days).

        ``{"cards": [...], "acted_on": [...]}``: Planning's detector cards."""
        return self._calendar_object("planning", start=start, days=days, principal=principal)

    def _calendar_object(
        self, view: str, *, start: str, days: int, principal: str | None
    ) -> dict[str, Any]:
        path = f"/api/v1/calendars/{view}?start={start}&days={days}"
        if principal is not None:
            path += f"&principal={principal}"
        resp = self._request("GET", path)
        body = resp.json()
        if not isinstance(body, dict):
            raise ApiError(status=resp.status_code, body=resp.text, detail="malformed response")
        return body

    def calendar_environment(self, *, dates: list[str], focus: str) -> dict[str, Any]:
        """``GET /api/v1/calendars/environment``: per-day weather and daylight (read-only)."""
        return self._get_object(
            f"/api/v1/calendars/environment?dates={','.join(dates)}&focus={focus}"
        )

    def calendar_travel(self, token: str) -> dict[str, Any]:
        """``GET /api/v1/calendars/travel``: drive facts for one signed leg from the range."""
        return self._get_object(f"/api/v1/calendars/travel?mode=drive&t={token}")

    def calendar_schedule_plan(self) -> dict[str, Any]:
        """``GET /api/v1/calendars/schedule-plan``: today's schedule plan, read-only."""
        return self._get_object("/api/v1/calendars/schedule-plan")

    def _get_object(self, path: str) -> dict[str, Any]:
        resp = self._request("GET", path)
        body = resp.json()
        if not isinstance(body, dict):
            raise ApiError(status=resp.status_code, body=resp.text, detail="malformed response")
        return body

    def create_todo(
        self,
        *,
        title: str,
        details: str | None = None,
        priority: str | None = None,
        due_date: str | None = None,
        due: str | None = None,
        group_id: str | None = None,
        assignee_person_id: str | None = None,
        next_action: str | None = None,
    ) -> TodoEntry:
        """``POST /api/v1/todos`` (a room's: ``/api/v1/groups/{id}/todos``).

        ``due_date`` is an ISO-8601 string (timezone-aware); ``due`` is a bare
        ``YYYY-MM-DD`` day, stored date-only. Both are a soft deadline for
        display/sort, never a trigger. ``assignee_person_id`` (a room's list
        only) is a member's roster person id: the todo becomes an ask of that
        member; the server refuses a non-member, an agent, or the caller, and an
        over-long title or details, with a 422 that says why. Returns the
        created row."""
        payload: dict[str, Any] = {"title": title}
        for key, value in (
            ("details", details),
            ("priority", priority),
            ("due_date", due_date),
            ("due", due),
            ("assignee_person_id", assignee_person_id),
            ("next_action", next_action),
        ):
            if value is not None:
                payload[key] = value
        resp = self._request("POST", _todos_path(group_id), json=payload)
        return _todo_from_dict(resp.json())

    def complete_todo(self, todo_id: str, *, group_id: str | None = None) -> TodoEntry:
        """``POST …/todos/{id}/done``. Mark done and release its held time (idempotent).

        Raises :class:`ApiError` (404) if the id is unknown or not the
        calling user's (or room's) — the CLI surfaces that as "not found, or
        not yours"."""
        return self._todo_verb(todo_id, "done", group_id)

    def dismiss_todo(
        self, todo_id: str, *, group_id: str | None = None, reason: str | None = None
    ) -> TodoEntry:
        """``POST …/todos/{id}/dismiss``. Close without completing.

        On an ask, the person asked turning it down (a decline) and the person
        who asked taking it back (a withdraw) both come here. ``reason`` is sent
        when given and is optional; any refusal is the server's, shown as it came."""
        if reason is None:
            return self._todo_verb(todo_id, "dismiss", group_id)
        resp = self._request(
            "POST", f"{_todos_path(group_id)}/{todo_id}/dismiss", json={"reason": reason}
        )
        return _todo_from_dict(resp.json())

    def edit_todo(
        self,
        todo_id: str,
        fields: dict[str, Any],
        *,
        group_id: str | None = None,
    ) -> TodoEntry:
        """``PATCH …/todos/{id}``: only the keys of ``fields`` change.

        ``fields`` is sent as the body: ``title``, ``details``, ``priority``,
        ``due`` (a ``YYYY-MM-DD`` day), ``due_date`` (an ISO-8601 instant) and,
        on a room's todo, ``assignee_person_id`` (ask that member, or reassign an
        ask). A key set to ``None`` clears that field (not ``title``). On an ask
        only the person who asked may edit, and both copies follow. A 409 means
        the todo changed under the edit (or the ask is not in a state that can
        be edited); a 403 is someone else's ask."""
        resp = self._request("PATCH", f"{_todos_path(group_id)}/{todo_id}", json=fields)
        return _todo_from_dict(resp.json())

    def reopen_todo(self, todo_id: str, *, group_id: str | None = None) -> TodoEntry:
        """``POST …/todos/{id}/reopen``. Put a done or dismissed todo back on the list."""
        return self._todo_verb(todo_id, "reopen", group_id)

    def _todo_verb(self, todo_id: str, verb: str, group_id: str | None) -> TodoEntry:
        resp = self._request("POST", f"{_todos_path(group_id)}/{todo_id}/{verb}")
        return _todo_from_dict(resp.json())

    # ------------------------------------------------------------------
    # Scheduled tasks (``/api/v1/scheduled-tasks``)
    #
    # The whole surface, reads and writes. Writes reach the same service
    # layer the agent tools and the web page use, so cron validation, the
    # next-fire recomputation and the destination eligibility gate behave
    # identically here — a task created from the CLI is not a second kind
    # of task.

    def list_scheduled_tasks(
        self, *, include_inactive: bool = False, group_id: str | None = None
    ) -> list[ScheduledTaskSummary]:
        """``GET /api/v1/scheduled-tasks``. The user's tasks, by name.

        Cancelled tasks and spent one-shots are excluded unless
        ``include_inactive`` — matching the web page's default.

        With ``group_id``, ``GET /api/v1/groups/{id}/scheduled-tasks``: that
        room's tasks, readable by any member, as
        :class:`RoomScheduledTaskSummary` rows."""
        query = "?include_inactive=true" if include_inactive else ""
        resp = self._request("GET", f"{_scheduled_tasks_path(group_id)}{query}")
        body = resp.json()
        if not isinstance(body, list):
            raise ApiError(status=resp.status_code, body=resp.text, detail="malformed response")
        if group_id:
            return [_room_scheduled_task_from_dict(row) for row in body]
        return [_scheduled_task_from_dict(row) for row in body]

    def get_scheduled_task(
        self, task_id: str, *, group_id: str | None = None
    ) -> ScheduledTaskDetail:
        """``GET /api/v1/scheduled-tasks/{id}``. Config + most recent fire.

        404 whether the id is unknown or belongs to another user — the
        server never distinguishes the two.

        With ``group_id``, ``GET /api/v1/groups/{id}/scheduled-tasks/{task}``:
        a room task, readable by any member, with the ``group_*`` fields set."""
        resp = self._request("GET", f"{_scheduled_tasks_path(group_id)}/{task_id}")
        row = resp.json()
        latest = row.get("latest_run")
        return ScheduledTaskDetail(
            **_scheduled_task_fields(row),
            latest_run=_scheduled_run_from_dict(latest) if latest else None,
            group_name=row.get("group_name"),
            group_members=row.get("group_members") or [],
            group_delivery=row.get("group_delivery"),
        )

    def list_scheduled_task_runs(
        self,
        task_id: str,
        *,
        limit: int = 20,
        before: str | None = None,
        before_id: str | None = None,
        group_id: str | None = None,
    ) -> list[ScheduledTaskRunEntry]:
        """``GET /api/v1/scheduled-tasks/{id}/runs``. Fire history, newest first.

        The server caps ``limit`` at 100. ``before`` (a ``fired_at``) and
        ``before_id`` (that row's id) are the keyset cursor for the next, older
        page; the server refuses ``before_id`` without ``before``.

        With ``group_id``, the room route under
        ``/api/v1/groups/{id}/scheduled-tasks`` — any member may read."""
        resp = self._request(
            "GET",
            f"{_scheduled_tasks_path(group_id)}/{task_id}/runs",
            params=_keyset_params(limit, before, before_id),
        )
        body = resp.json()
        if not isinstance(body, list):
            raise ApiError(status=resp.status_code, body=resp.text, detail="malformed response")
        return [_scheduled_run_from_dict(row) for row in body]

    def get_scheduled_task_thread(
        self,
        task_id: str,
        *,
        limit: int = 20,
        before: str | None = None,
        before_id: str | None = None,
        group_id: str | None = None,
    ) -> list[ScheduledTaskMessage]:
        """``GET /api/v1/scheduled-tasks/{id}/thread``. The newest ``limit``
        messages of the task's own conversation, oldest first within the
        page. The server caps ``limit`` at 100. ``before`` (a ``created_at``)
        and ``before_id`` (that message's id) page back to older messages.

        With ``group_id``, the room route under
        ``/api/v1/groups/{id}/scheduled-tasks`` — any member may read."""
        resp = self._request(
            "GET",
            f"{_scheduled_tasks_path(group_id)}/{task_id}/thread",
            params=_keyset_params(limit, before, before_id),
        )
        body = resp.json()
        if not isinstance(body, list):
            raise ApiError(status=resp.status_code, body=resp.text, detail="malformed response")
        return [
            ScheduledTaskMessage(
                id=row["id"],
                role=row["role"],
                text=row["text"],
                channel=row["channel"],
                trajectory_id=row.get("trajectory_id"),
                created_at=row["created_at"],
                attachments=row.get("attachments") or [],
            )
            for row in body
        ]

    def create_scheduled_task(
        self,
        *,
        name: str,
        prompt: str | None = None,
        cron: str | None = None,
        run_at: str | None = None,
        timezone: str | None = None,
        description: str | None = None,
        destinations: list[str] | None = None,
        group_id: str | None = None,
        topic: str | None = None,
        skill: str | None = None,
    ) -> ScheduledTaskSummary:
        """``POST /api/v1/scheduled-tasks``. Schedule a new task.

        Exactly one of ``cron`` (recurring) or ``run_at`` (a one-off, ISO
        8601 *with* an offset). ``destinations`` omitted inherits the
        user's notification preference; pass ``[]`` for a silent task.
        409 when an active task already holds ``name``; 422 for an invalid
        cron, a past ``run_at``, or an unreachable destination.

        With ``group_id``, ``POST /api/v1/groups/{id}/scheduled-tasks``: a
        task on that room — a room owner's write (403 otherwise). A room
        task takes exactly one of ``cron`` / ``run_at`` and at least one of
        ``prompt`` / ``skill`` (a room-granted skill's name; 422 otherwise),
        and has no description, so ``description`` is never sent. Keys the
        caller left unset are *omitted*, never sent as ``null`` (the server
        types ``prompt`` and ``cron`` as strings). ``destinations`` omitted
        leaves the delivery to the server, and ``topic`` (which needs
        ``destinations``) names the room topic a ``groups_service`` delivery
        posts into. 409 for a taken name or a room at its task budget.
        """
        if group_id:
            if topic is not None and destinations is None:
                msg = "topic needs explicit destinations"
                raise ValueError(msg)
            room_payload: dict[str, Any] = {"name": name}
            if prompt is not None:
                room_payload["prompt"] = prompt
            if skill is not None:
                room_payload["skill"] = skill
            if cron is not None:
                room_payload["cron"] = cron
            if run_at is not None:
                room_payload["run_at"] = run_at
            if timezone is not None:
                room_payload["timezone"] = timezone
            if destinations is not None:
                room_payload["delivery"] = _room_delivery(destinations, topic)
            resp = self._request("POST", _scheduled_tasks_path(group_id), json=room_payload)
            return _room_scheduled_task_from_dict(resp.json())
        if prompt is None or skill is not None:
            msg = "a personal task needs a prompt and takes no skill"
            raise ValueError(msg)
        payload: dict[str, Any] = {"name": name, "prompt": prompt}
        if cron is not None:
            payload["cron"] = cron
        if run_at is not None:
            payload["run_at"] = run_at
        if timezone is not None:
            payload["timezone"] = timezone
        if description is not None:
            payload["description"] = description
        if destinations is not None:
            payload["destinations"] = destinations
        resp = self._request("POST", "/api/v1/scheduled-tasks", json=payload)
        return _scheduled_task_from_dict(resp.json())

    def update_scheduled_task(self, task_id: str, *, edits: dict[str, Any]) -> ScheduledTaskSummary:
        """``PATCH /api/v1/scheduled-tasks/{id}``. Partial edit.

        ``edits`` is passed through verbatim so "the caller didn't mention
        this field" survives to the server — which is the whole contract
        of the endpoint. Building the dict is the caller's job precisely
        so a ``None`` default here can't turn an omission into a value.
        Legal keys: ``prompt``, ``cron``, ``run_at``, ``timezone``,
        ``description`` (the only one that accepts ``null``, to clear it).
        """
        resp = self._request("PATCH", f"/api/v1/scheduled-tasks/{task_id}", json=edits)
        return _scheduled_task_from_dict(resp.json())

    def cancel_scheduled_task(
        self, task_id: str, *, group_id: str | None = None
    ) -> ScheduledTaskSummary:
        """``POST /api/v1/scheduled-tasks/{id}/cancel``. Pause a task.

        A soft delete: the row stays (with its run history) and can be
        resumed. Idempotent.

        With ``group_id``, ``PATCH /api/v1/groups/{id}/scheduled-tasks/{task}``
        with ``{"active": false}`` — a room owner's write (403 otherwise)."""
        if group_id:
            return self._patch_room_scheduled_task(group_id, task_id, {"active": False})
        resp = self._request("POST", f"/api/v1/scheduled-tasks/{task_id}/cancel")
        return _scheduled_task_from_dict(resp.json())

    def resume_scheduled_task(
        self, task_id: str, *, group_id: str | None = None
    ) -> ScheduledTaskSummary:
        """``POST /api/v1/scheduled-tasks/{id}/resume``. Put it back on
        the schedule, next fire re-derived from the cron.

        422 for a one-off whose moment has passed — the server's message
        says so, and the CLI surfaces it verbatim.

        With ``group_id``, the room PATCH with ``{"active": true}`` — a room
        owner's write (403 otherwise); 409 when the room is at its task
        budget or the name has since been taken."""
        if group_id:
            return self._patch_room_scheduled_task(group_id, task_id, {"active": True})
        resp = self._request("POST", f"/api/v1/scheduled-tasks/{task_id}/resume")
        return _scheduled_task_from_dict(resp.json())

    def renew_scheduled_task(
        self, task_id: str, *, group_id: str | None = None
    ) -> ScheduledTaskSummary:
        """``POST /api/v1/scheduled-tasks/{id}/renew``. Keep a task going past its
        renewal warning ("Keep it going" in the web UI).

        422 when the server refuses (nothing to renew, a one-off, a paused task); its
        ``detail`` is written for the user and the CLI surfaces it verbatim.

        With ``group_id``, the room route — a room owner's write (403 otherwise)."""
        resp = self._request("POST", f"{_scheduled_tasks_path(group_id)}/{task_id}/renew")
        if group_id:
            return _room_scheduled_task_from_dict(resp.json())
        return _scheduled_task_from_dict(resp.json())

    def delete_scheduled_task(self, task_id: str, *, group_id: str | None = None) -> None:
        """``DELETE /api/v1/scheduled-tasks/{id}``. Remove a personal task for good
        (its run history goes with it); 204. 404 whether the id is unknown or
        another user's.

        With ``group_id``, ``DELETE /api/v1/groups/{id}/scheduled-tasks/{task}`` —
        a room owner's write (403 otherwise)."""
        self._request("DELETE", f"{_scheduled_tasks_path(group_id)}/{task_id}")

    def update_room_scheduled_task(
        self,
        group_id: str,
        task_id: str,
        *,
        edits: dict[str, Any],
        destinations: list[str] | None = None,
        topic: str | None = None,
    ) -> RoomScheduledTaskSummary:
        """``PATCH /api/v1/groups/{id}/scheduled-tasks/{task}`` with field edits
        and, optionally, a new delivery — all in ONE request, which the server
        applies atomically (a room owner's write; 403 otherwise).

        ``edits`` is passed through verbatim (legal keys: ``prompt``, ``cron``,
        ``timezone``, ``description`` — the only one that accepts ``null`` —
        and ``run_at``). ``destinations`` ``None`` leaves the delivery alone —
        a room has no preference to inherit — while a list (``[]`` is silent)
        sends ``delivery``; ``topic`` needs ``destinations``. ``active`` is
        never combined with these: the server answers 422."""
        if topic is not None and destinations is None:
            msg = "topic needs explicit destinations"
            raise ValueError(msg)
        change = dict(edits)
        if destinations is not None:
            change["delivery"] = _room_delivery(destinations, topic)
        return self._patch_room_scheduled_task(group_id, task_id, change)

    def _patch_room_scheduled_task(
        self, group_id: str, task_id: str, change: dict[str, Any]
    ) -> RoomScheduledTaskSummary:
        """``PATCH /api/v1/groups/{id}/scheduled-tasks/{task}``. ``change`` is
        either ``{"active": ...}`` alone or a set of edit fields (with
        ``delivery`` among them) applied atomically; mixing ``active`` with
        anything else is the server's 422."""
        resp = self._request("PATCH", f"{_scheduled_tasks_path(group_id)}/{task_id}", json=change)
        return _room_scheduled_task_from_dict(resp.json())

    def set_scheduled_task_destinations(
        self,
        task_id: str,
        *,
        destinations: list[str] | None,
        group_id: str | None = None,
        topic: str | None = None,
    ) -> ScheduledTaskSummary:
        """``PUT /api/v1/scheduled-tasks/{id}/destinations``. Replace the set.

        ``None`` inherits the user's notification preference, ``[]`` makes
        the task silent, a list pins exactly those. The field is required
        on the wire — this method always sends it — so an omission can
        never be read as "make it silent".

        With ``group_id``, the room PATCH with ``{"delivery": {...}}`` — a
        room owner's write (403 otherwise). A room's delivery is always
        explicit: ``[]`` is silent, a list pins those, and ``None`` is a
        ``ValueError`` — a room has no personal preference to inherit, and
        what "the default" stores server-side depends on a deployment flag.
        ``topic`` names the room topic a ``groups_service`` delivery posts
        into; it is a room-only field."""
        if group_id:
            if destinations is None:
                msg = "a room task's destinations must be an explicit list"
                raise ValueError(msg)
            return self._patch_room_scheduled_task(
                group_id, task_id, {"delivery": _room_delivery(destinations, topic)}
            )
        resp = self._request(
            "PUT",
            f"/api/v1/scheduled-tasks/{task_id}/destinations",
            json={"destinations": destinations},
        )
        return _scheduled_task_from_dict(resp.json())

    def trajectory_get(self, trajectory_id: str) -> TrajectoryDetail:
        """``GET /api/v1/trajectories/{id}``. Bundled trajectory + events.

        Visibility is scoped to the calling user's principal — own
        trajectories plus group ones — and 404s otherwise. The CLI's
        ``pepper trajectories show`` command surfaces that as "not found,
        or not yours".
        """
        return _trajectory_detail_from_dict(self.trajectory_get_raw(trajectory_id))

    def trajectory_get_raw(self, trajectory_id: str) -> dict[str, Any]:
        """Same call as :meth:`trajectory_get` but returns the raw JSON
        body so ``pepper trajectories show --json`` can pipe the wire
        format through unchanged."""
        resp = self._request("GET", f"/api/v1/trajectories/{trajectory_id}")
        body = resp.json()
        if not isinstance(body, dict):
            raise ApiError(status=resp.status_code, body=resp.text, detail="malformed response")
        return body

    def trajectory_list(
        self, *, limit: int = 20, offset: int = 0, principal: str | None = None
    ) -> list[TrajectoryListEntry]:
        """``GET /api/v1/trajectories?limit=N&offset=N``. Newest-first.

        Default ``limit`` is small (20) — the CLI is rendering rows to
        a terminal, not paging a web view. The server caps at 200.

        ``principal`` (a room's id) narrows the listing to that room's turns,
        filtered server-side so ``limit``/``offset`` page the filtered set;
        404 for a room the caller isn't in.
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if principal is not None:
            params["principal"] = principal
        resp = self._request("GET", "/api/v1/trajectories", params=params)
        body = resp.json()
        if not isinstance(body, list):
            raise ApiError(status=resp.status_code, body=resp.text, detail="malformed response")
        return [
            TrajectoryListEntry(
                id=row["id"],
                channel=row["channel"],
                created_at=row["created_at"],
                closed_at=row.get("closed_at"),
                meta=row.get("meta") or {},
                event_count=row["event_count"],
                first_user_message=row.get("first_user_message"),
                group_id=row.get("group_id"),
                group_name=row.get("group_name"),
            )
            for row in body
        ]

    def chat_history(self) -> list[ChatHistoryEntry]:
        """``GET /api/v1/chat/history`` — today's live Pepper thread, oldest first.

        No pagination and no ``since``: the whole day comes back each time.
        """
        rows = self._request("GET", "/api/v1/chat/history").json()
        return [
            ChatHistoryEntry(
                id=str(r["id"]),
                role=r["role"],
                text=r.get("text") or "",
                channel=r.get("channel") or "",
                created_at=r["created_at"],
                attachments=list(r.get("attachments") or []),
                reactions=list(r.get("reactions") or []),
            )
            for r in rows
            if isinstance(r, dict)
        ]

    def post_tool_result(
        self, *, call_id: str, content: list[dict[str, Any]], is_error: bool = False
    ) -> None:
        """``POST /api/v1/chat/tool-result`` — answer one ``client_tool_call``.

        The uplink half of the client-tools round trip. Issued **while the SSE stream is
        still open**, on a second connection from the same pool: the chat
        request body closed before the turn started, so there is nowhere else
        for an answer to go.

        204 on success. A 404 means the ``call_id`` is unknown, not ours, or
        already settled — the server deliberately does not say which — and is
        not worth retrying: the turn has already moved on or is about to time
        the call out. 413 means the result was too large to accept; the
        server settles the call as an error in that case, so the turn is not
        left waiting either way.
        """
        self._request(
            "POST",
            "/api/v1/chat/tool-result",
            json={"call_id": call_id, "content": content, "is_error": is_error},
        )

    def chat_send(self, message: str, *, attachment_ids: list[str] | None = None) -> ChatReply:
        """``POST /api/v1/chat/send`` (JSON path).

        Single turn, full reply. Returns when the server has finalized
        the assistant message into the trajectory. For the streaming
        variant — used by the interactive REPL — see
        :meth:`chat_send_stream`.

        ``attachment_ids`` reference artifacts (kind ``blob``) already
        uploaded elsewhere (e.g. ``POST /artifacts/upload``); an unknown
        or foreign id 422s server-side before any turn runs.
        """
        payload: dict[str, Any] = {"message": message}
        if attachment_ids:
            payload["attachment_ids"] = attachment_ids
        resp = self._request("POST", "/api/v1/chat/send", json=payload)
        body = resp.json()
        return ChatReply(
            reply=body["reply"],
            trajectory_id=body.get("trajectory_id"),
            user_message_id=body["user_message_id"],
            assistant_message_id=body["assistant_message_id"],
            steps=body.get("steps") or [],
            client_tools_ignored=body.get("client_tools_ignored"),
        )

    def chat_send_stream(
        self,
        message: str,
        *,
        attachment_ids: list[str] | None = None,
        client_tools: list[dict[str, Any]] | None = None,
    ) -> Iterator[StreamEvent]:
        """``POST /api/v1/chat/send`` (SSE path).

        Yields one :class:`StreamEvent` per server-sent event, in
        order. The stream terminates after either an ``"error"`` event
        or a ``"reply"`` event — the REPL uses the latter as the
        "done, ready for next line" signal.

        ``attachment_ids`` — same contract as :meth:`chat_send`.

        Implementation detail: the SSE wire format is line-based, but
        a single logical event spans multiple lines (``event:`` line,
        one or more ``data:`` lines, blank line terminator). The
        parser inside :func:`_iter_sse_events` accumulates lines into
        whole events so callers see a flat iterator.

        Errors:
        - Non-2xx HTTP response → :class:`ApiError` (raised before any
          events are yielded — the stream never opens).
        - Transport error → ``httpx.RequestError`` (propagates).
        - Malformed SSE frame → skipped silently; a defensive client
          should be tolerant of stray frames since SSE allows
          comments and unknown event types.
        - A successful ``application/json`` reply from an older server →
          one terminal reply event from the same request. An invalid JSON
          reply becomes a terminal error event, never a second POST.

        With the default client settings, each SSE read may wait up to ten
        minutes for the next frame. An explicit constructor ``timeout`` still
        applies to this stream; no timeout triggers an automatic resend.
        """
        url = f"{self._server_url}/api/v1/chat/send"
        headers = {
            "Authorization": f"Bearer {self._token.get_secret_value()}",
            "User-Agent": USER_AGENT,
            "Accept": "text/event-stream",
        }
        payload: dict[str, Any] = {"message": message}
        if attachment_ids:
            payload["attachment_ids"] = attachment_ids
        if client_tools:
            # The manifest this process is offering to serve for this one
            # turn. An older server ignores the unknown field — pydantic
            # models here are not ``extra="forbid"`` — so declaring it is
            # safe against a server that predates the feature, which is what
            # lets the CLI ship ahead of a deploy.
            payload["client_tools"] = client_tools
        base_timeout = self._client.timeout
        with self._client.stream(
            "POST",
            url,
            json=payload,
            headers=headers,
            timeout=httpx.Timeout(
                connect=base_timeout.connect,
                read=self._chat_stream_read_timeout,
                write=base_timeout.write,
                pool=base_timeout.pool,
            ),
        ) as resp:
            if resp.status_code >= 400:
                # ``read()`` materializes the streaming body so we can
                # build a useful ``ApiError`` even for the error path.
                # Without this the body would be a closed stream by the
                # time the caller sees the exception.
                resp.read()
                _raise_for_error(resp)
            media_type = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type == "application/json":
                # Older servers may ignore Accept and complete the turn as
                # a unary response. Decode that response in place; retrying
                # the POST could submit the user's turn twice.
                resp.read()
                try:
                    body = resp.json()
                except ValueError:
                    body = None
                if isinstance(body, dict) and isinstance(body.get("reply"), str):
                    yield StreamEvent(kind="reply", payload=body)
                else:
                    yield StreamEvent(
                        kind="error",
                        payload={
                            "message": "Server returned an invalid chat reply. "
                            "Check the web chat before sending again."
                        },
                    )
                return
            yield from _iter_sse_events(resp.iter_lines())

    def watch_agent_events(
        self, *, since: int = 0, last_event_id: int | None = None
    ) -> Iterator[FeedFrame]:
        """``GET /api/v1/groups/events/stream`` — an agent member's roster-scoped
        event feed, as :class:`FeedFrame`s until the server closes it.

        Resumes after ``last_event_id`` (sent as ``Last-Event-ID``, what a
        reconnect uses) when given, else after ``since`` (a first connect).
        Agent-token only: a PAT is a 403 and a revoked token a 401, both
        raised as :class:`ApiError` before any frame is yielded.

        The generator ends when the server closes the stream; a dropped or
        stalled connection raises ``httpx.RequestError`` instead. Either way
        the caller reconnects from the last cursor it saw. Heartbeat comments
        surface as ``kind="comment"`` frames so the caller can tell a quiet
        healthy connection from a dead one.
        """
        yield from self._stream_feed(
            "/api/v1/groups/events/stream", since=since, last_event_id=last_event_id
        )

    def watch_scoped_events(
        self,
        room_id: str,
        *,
        topic_id: str | None = None,
        since: int = 0,
        last_event_id: int | None = None,
    ) -> Generator[FeedFrame]:
        """``GET /api/v1/groups/{room_id}/events/stream`` (or, with
        ``topic_id``, ``.../topics/{topic_id}/events/stream``): the agent
        feed narrowed by the server to one room, or one topic in it.

        The same frames, cursor and resume rules as :meth:`watch_agent_events`,
        plus a ``scope`` frame (see :func:`watch_scope_from_frame`) ahead of
        the first event on every connection, naming the room and topic the
        server is actually serving. A room the caller cannot see is a 404,
        indistinguishable from one that does not exist; a topic that is not
        in the room is a 404 too. This never falls back to the roster-wide
        stream.
        """
        path = f"/api/v1/groups/{room_id}"
        if topic_id is not None:
            path += f"/topics/{topic_id}"
        yield from self._stream_feed(
            f"{path}/events/stream", since=since, last_event_id=last_event_id
        )

    def _stream_feed(
        self, path: str, *, since: int, last_event_id: int | None
    ) -> Generator[FeedFrame]:
        """Open one SSE feed connection at ``path`` and yield its frames."""
        headers = {
            "Authorization": f"Bearer {self._token.get_secret_value()}",
            "User-Agent": USER_AGENT,
            "Accept": "text/event-stream",
        }
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        base_timeout = self._client.timeout
        with self._client.stream(
            "GET",
            f"{self._server_url}{path}",
            params={"since": since},
            headers=headers,
            timeout=httpx.Timeout(
                connect=base_timeout.connect,
                read=FEED_STREAM_READ_TIMEOUT,
                write=base_timeout.write,
                pool=base_timeout.pool,
            ),
        ) as resp:
            if resp.status_code >= 400:
                resp.read()
                _raise_for_error(resp)
            yield from _iter_feed_frames(resp.iter_lines())

    # ------------------------------------------------------------------
    # Research projects (``/api/v1/research-projects``)
    #
    # A project, run or item that is not yours is the same 404 as one that does not exist.
    # With the feature switched off every route answers 403 with a ``code`` (see
    # :func:`error_code`). Every method takes ``group_id``: given, it addresses the room's
    # twin of the route under ``/api/v1/groups/{gid}/research-projects`` (same paths, bodies
    # and shapes; any member reads, steers, takes and reconsiders, the owner role does the
    # rest, ``planning/research-projects.md`` §9.3).

    def list_research_projects(
        self, *, status: str | None = None, limit: int = 200, group_id: str | None = None
    ) -> ResearchPage[ResearchProjectSummary]:
        """``GET /api/v1/research-projects``. Newest first; ``status`` (``active``,
        ``paused``, ``stopped``) is the server's filter, and none means every status.
        The server never issues a cursor here in v1 (a person or a room has a handful)."""
        params: dict[str, Any] = {"limit": limit}
        if status is not None:
            params["status"] = status
        body = self._research_get(_research_base(group_id), params)
        return _research_page(body, _research_summary)

    def get_research_project(
        self, project_id: str, *, group_id: str | None = None
    ) -> ResearchProjectDetail:
        """``GET /api/v1/research-projects/{id}``."""
        return _research_detail(self._research_get(_research_path(group_id, project_id)))

    def get_research_report(
        self, project_id: str, *, group_id: str | None = None
    ) -> ResearchReport:
        """``GET /api/v1/research-projects/{id}/report``."""
        return _research_report(self._research_get(_research_path(group_id, project_id, "report")))

    def list_research_iterations(
        self,
        project_id: str,
        *,
        limit: int = 50,
        before: str | None = None,
        group_id: str | None = None,
    ) -> ResearchPage[ResearchIteration]:
        """``GET /api/v1/research-projects/{id}/iterations``. What executed, newest first;
        ``before`` is a ``next_before`` the previous page returned."""
        body = self._research_get(
            _research_path(group_id, project_id, "iterations"), _page_params(limit, before)
        )
        return _research_page(body, _research_iteration)

    def list_research_items(
        self,
        project_id: str,
        *,
        states: list[str] | None = None,
        limit: int = 50,
        before: str | None = None,
        group_id: str | None = None,
    ) -> ResearchPage[ResearchItem]:
        """``GET /api/v1/research-projects/{id}/items``. Newest ``seq`` first; ``states``
        repeats the ``state`` query parameter (any of them matches)."""
        params = _page_params(limit, before)
        if states:
            params["state"] = list(states)
        body = self._research_get(_research_path(group_id, project_id, "items"), params)
        return _research_page(body, _research_item)

    def get_research_item(
        self, project_id: str, item_id: str, *, group_id: str | None = None
    ) -> ResearchItemDetail:
        """``GET /api/v1/research-projects/{id}/items/{item_id}``."""
        body = self._research_get(_research_path(group_id, project_id, "items", item_id))
        return _research_item_detail(body)

    # The write side. Every write answers the object it changed, as the server returns it.

    def create_research_project(
        self,
        *,
        name: str,
        brief: str,
        delivery_policy: str,
        skill_name: str | None = None,
        source_url: str | None = None,
        group_id: str | None = None,
    ) -> ResearchProjectDetail:
        """``POST /api/v1/research-projects``. A manual-only project; the server runs
        ``web-research`` when ``skill_name`` is omitted. In a room the skill must be one
        granted to the room (the server's 422 ``skill_not_granted`` says so)."""
        body: dict[str, Any] = {"name": name, "brief": brief, "delivery_policy": delivery_policy}
        if skill_name is not None:
            body["skill_name"] = skill_name
        if source_url is not None:
            body["source_url"] = source_url
        return _research_detail(self._research_write("POST", _research_base(group_id), body))

    def edit_research_project(
        self, project_id: str, *, brief: str, group_id: str | None = None
    ) -> ResearchProjectDetail:
        """``PATCH /api/v1/research-projects/{id}``: replace the brief."""
        path = _research_path(group_id, project_id)
        return _research_detail(self._research_write("PATCH", path, {"brief": brief}))

    def set_research_filters(
        self, project_id: str, filters: list[str], *, group_id: str | None = None
    ) -> ResearchProjectDetail:
        """``PUT /api/v1/research-projects/{id}/filters``: replace the whole list (``[]``
        clears it)."""
        path = _research_path(group_id, project_id, "filters")
        body = {"filters": [{"text": text} for text in filters]}
        return _research_detail(self._research_write("PUT", path, body))

    def run_research_project(
        self, project_id: str, *, group_id: str | None = None
    ) -> ResearchRunReceipt:
        """``POST /api/v1/research-projects/{id}/runs`` (202). Raises
        :class:`ResearchRunInFlightError` for the 409 that names the run already in flight."""
        path = _research_path(group_id, project_id, "runs")
        try:
            body = self._research_write("POST", path, None)
        except ApiError as exc:
            run_id = _in_flight_run_id(exc)
            if run_id is None:
                raise
            raise ResearchRunInFlightError(exc, run_id) from exc
        return _fields(ResearchRunReceipt, body)

    def enable_research_schedule(
        self,
        project_id: str,
        *,
        cron: str,
        timezone: str | None = None,
        topic_id: str | None = None,
        group_id: str | None = None,
    ) -> ResearchProjectDetail:
        """``POST /api/v1/research-projects/{id}/schedule`` (201): the project's first
        schedule. ``timezone`` is left out when ``None`` so the server uses the profile zone.
        ``topic_id`` (a room's only) is the topic each scheduled run posts to; left out, the
        room's default delivery applies. Raises :class:`ResearchAlreadyScheduledError` for
        the 409 that names the task."""
        body = _schedule_body(cron, timezone)
        if topic_id is not None:
            body["topic_id"] = topic_id
        path = _research_path(group_id, project_id, "schedule")
        try:
            return _research_detail(self._research_write("POST", path, body))
        except ApiError as exc:
            task_id = _scheduled_task_id(exc)
            if task_id is None:
                raise
            raise ResearchAlreadyScheduledError(exc, task_id) from exc

    def detach_research_schedule(
        self, project_id: str, *, group_id: str | None = None
    ) -> ResearchProjectDetail:
        """``DELETE /api/v1/research-projects/{id}/schedule``: manual-only again; the run
        history stays."""
        path = _research_path(group_id, project_id, "schedule")
        return _research_detail(self._research_write("DELETE", path, None))

    def change_research_schedule(
        self,
        task_id: str,
        *,
        cron: str,
        timezone: str | None = None,
        topic_id: str | None = None,
        group_id: str | None = None,
    ) -> None:
        """``PATCH /api/v1/scheduled-tasks/{id}``: the cadence of an existing schedule (the
        server keeps changes on the scheduled-tasks routes). Only the keys given are sent.

        In a room it is the room twin, ``PATCH /api/v1/groups/{gid}/scheduled-tasks/{id}``
        (owner role), and ``topic_id`` moves the delivery to that topic of the room."""
        body = _schedule_body(cron, timezone)
        if group_id is None:
            self._research_write("PATCH", f"/api/v1/scheduled-tasks/{task_id}", body)
            return
        if topic_id is not None:
            body["delivery"] = _room_delivery(["groups_service"], topic_id)
        self._research_write("PATCH", f"{_scheduled_tasks_path(group_id)}/{task_id}", body)

    def get_research_run(
        self, project_id: str, run_id: str, *, group_id: str | None = None
    ) -> ResearchRunDetail:
        """``GET /api/v1/research-projects/{id}/runs/{run_id}``."""
        return self.poll_research_run(project_id, run_id, group_id=group_id)[0]

    def poll_research_run(
        self,
        project_id: str,
        run_id: str,
        *,
        timeout: float | None = None,
        group_id: str | None = None,
    ) -> tuple[ResearchRunDetail, float | None]:
        """:meth:`get_research_run` plus the seconds a ``Retry-After`` header asked for.

        ``timeout`` bounds this one request in seconds (``None``: the client's own), so a
        ``--wait`` poll near its deadline cannot run a full :data:`DEFAULT_TIMEOUT` past it.
        """
        path = _research_path(group_id, project_id, "runs", run_id)
        resp = self._request("GET", path, timeout=timeout)
        body = resp.json()
        if not isinstance(body, dict):
            raise ApiError(status=200, body=str(body)[:200], detail="malformed response")
        return _research_run_detail(body), _retry_after(resp)

    def steer_research_project(
        self,
        project_id: str,
        *,
        text: str,
        question_id: str | None = None,
        group_id: str | None = None,
    ) -> ResearchSteering:
        """``POST /api/v1/research-projects/{id}/steering`` (201): queue guidance for the next
        run, optionally answering an open question (``q1``..)."""
        path = _research_path(group_id, project_id, "steering")
        body: dict[str, Any] = {"text": text}
        if question_id is not None:
            body["question_id"] = question_id
        return _fields(ResearchSteering, self._research_write("POST", path, body))

    def pause_research_project(
        self, project_id: str, *, group_id: str | None = None
    ) -> ResearchProjectDetail:
        """``POST /api/v1/research-projects/{id}/pause``. Already paused is a 200."""
        path = _research_path(group_id, project_id, "pause")
        return _research_detail(self._research_write("POST", path, None))

    def resume_research_project(
        self, project_id: str, *, group_id: str | None = None
    ) -> ResearchProjectDetail:
        """``POST /api/v1/research-projects/{id}/resume``. Resuming never runs the project."""
        path = _research_path(group_id, project_id, "resume")
        return _research_detail(self._research_write("POST", path, None))

    def take_research_item(
        self, project_id: str, item_id: str, *, group_id: str | None = None
    ) -> ResearchItem:
        """``POST .../{id}/items/{item_id}/take``: the caller had a delivered item."""
        path = _research_path(group_id, project_id, "items", item_id, "take")
        return _research_item(self._research_write("POST", path, None))

    def reconsider_research_item(
        self, project_id: str, item_id: str, *, reason: str, group_id: str | None = None
    ) -> ResearchItem:
        """``POST .../{id}/items/{item_id}/reconsider``: bring a rejected item back as a
        candidate. ``reason`` goes as given; the server refuses a blank one."""
        path = _research_path(group_id, project_id, "items", item_id, "reconsider")
        return _research_item(self._research_write("POST", path, {"reason": reason}))

    def _research_write(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> dict[str, Any]:
        resp = self._request(method, path, json=body)
        parsed = resp.json()
        if not isinstance(parsed, dict):
            raise ApiError(
                status=resp.status_code, body=str(parsed)[:200], detail="malformed response"
            )
        return parsed

    def _research_get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        body = self._request("GET", path, params=params).json()
        if not isinstance(body, dict):
            raise ApiError(status=200, body=str(body)[:200], detail="malformed response")
        return body

    # ------------------------------------------------------------------
    # Groups (MIRROR: src/pepper/api/groups.py)
    #
    # One method per route of the groups API, so a native port can walk
    # the two side by side.

    def list_groups(self, *, archived: bool = False) -> GroupList:
        """``GET /api/v1/groups`` — the caller's rooms, and the surface's
        capability signal. See :class:`GroupList`."""
        resp = self._request("GET", "/api/v1/groups", params={"archived": int(archived)})
        return _group_list_from_dict(resp.json())

    def get_group(self, group_id: str) -> dict[str, Any]:
        """``GET /api/v1/groups/{gid}`` — room info: kind, members, topics,
        apps, sources of context, this viewer's archive flag.

        A dict rather than a dataclass on purpose (see the section header):
        nothing in this package reads it field by field, and five nested
        shapes typed here would be a second description of the server's, not a
        contract. Note the three availability flags — ``apps.unavailable``,
        ``apps.catalog_unavailable`` and ``context.unavailable`` — which are
        *not* the same as an empty list and must not be rendered as one.
        """
        return self._request("GET", f"/api/v1/groups/{group_id}").json()

    def list_group_topics(self, group_id: str) -> list[GroupTopicSummary]:
        """``GET /api/v1/groups/{gid}/topics`` — every topic, archived
        included; hiding them is the client's fold over one full answer."""
        body = self._request("GET", f"/api/v1/groups/{group_id}/topics").json()
        return [_group_topic_from_dict(t) for t in body.get("topics") or []]

    def list_group_messages(
        self,
        group_id: str,
        topic_id: str,
        *,
        since: str | None = None,
        before: str | None = None,
        at: str | None = None,
        limit: int | None = None,
    ) -> GroupMessagePage:
        """``GET /api/v1/groups/{gid}/topics/{tid}/messages``.

        ``since`` (the poll), ``before`` (load older, paged by the returned
        ``next_before``) and ``at`` (the window around one message, for a
        permalink) are **mutually exclusive** — sending two is a 422, not a
        precedence rule.
        """
        params: dict[str, Any] = {}
        for name, value in (("since", since), ("before", before), ("at", at), ("limit", limit)):
            if value is not None:
                params[name] = value
        body = self._request(
            "GET", f"/api/v1/groups/{group_id}/topics/{topic_id}/messages", params=params
        ).json()
        return GroupMessagePage(
            messages=[group_message_from_dict(m) for m in body.get("messages") or []],
            next_before=body.get("next_before"),
            permalink_target_missing=body.get("permalink_target_missing", False),
        )

    def post_group_message(
        self,
        group_id: str,
        topic_id: str,
        *,
        message: str,
        attachment_ids: list[str] | None = None,
        mentions: list[str] | None = None,
        parent_message_id: str | None = None,
        idempotency_key: str | None = None,
        on_behalf_of_person_id: str | None = None,
    ) -> dict[str, Any]:
        """``POST /api/v1/groups/{gid}/topics/{tid}/messages`` — post as the
        caller, as themself.

        Runs no turn and streams no reply: Pepper answers through the room's
        own poller if her reply policy gives one, and the client sees it on
        its next :meth:`list_group_messages`.

        ``mentions`` are bare person-id strings — the roster's ``person_id``,
        not a display name. Send one only if its literal ``@Display Name``
        token is still in ``message`` at send time.

        ``on_behalf_of_person_id`` names the person an agent member is
        answering:
        the service shows the post as the agent's, "for" that person. Only an
        agent token may send it; Pepper refuses it from a signed-in person.
        """
        payload: dict[str, Any] = {"message": message}
        if attachment_ids:
            payload["attachment_ids"] = attachment_ids
        if mentions:
            payload["mentions"] = mentions
        if parent_message_id:
            payload["parent_message_id"] = parent_message_id
        if on_behalf_of_person_id:
            payload["on_behalf_of_person_id"] = on_behalf_of_person_id
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        resp = self._request(
            "POST",
            f"/api/v1/groups/{group_id}/topics/{topic_id}/messages",
            json=payload,
            extra_headers=headers,
        )
        return resp.json()["message"]

    def upload_group_attachment(
        self, group_id: str, *, filename: str, content: bytes, content_type: str
    ) -> dict[str, Any]:
        """``POST /api/v1/groups/{gid}/attachments`` — the room's own store,
        not Pepper's. The returned ``id`` is what a later
        :meth:`post_group_message` sends as one of ``attachment_ids``."""
        resp = self._request(
            "POST",
            f"/api/v1/groups/{group_id}/attachments",
            files={"file": (filename, content, content_type)},
        )
        return resp.json()

    def get_group_attachment(self, group_id: str, attachment_id: str) -> bytes:
        """``GET /api/v1/groups/{gid}/attachments/{aid}`` — raw bytes, member
        -gated. An attachment id is not a capability: a non-member gets the
        same 404 they get for the room."""
        return self._request(
            "GET", f"/api/v1/groups/{group_id}/attachments/{attachment_id}"
        ).content

    def edit_group_message(self, group_id: str, message_id: str, *, body: str) -> dict[str, Any]:
        """``POST /api/v1/groups/{gid}/messages/{mid}/edit`` — own messages
        only (the service enforces it). Answers the re-rendered body plus the
        ``edited_at`` version marker a reconcile pass compares against."""
        return self._request(
            "POST", f"/api/v1/groups/{group_id}/messages/{message_id}/edit", json={"body": body}
        ).json()

    def delete_group_message(self, group_id: str, message_id: str) -> dict[str, Any]:
        """``DELETE /api/v1/groups/{gid}/messages/{mid}`` — leaves a tombstone,
        which still renders as a row, which is why this answers a body.

        A real ``DELETE`` where the session door answers ``POST …/delete``: a
        form cannot send one and this door is JSON.
        """
        return self._request("DELETE", f"/api/v1/groups/{group_id}/messages/{message_id}").json()

    def react_to_group_message(
        self, group_id: str, message_id: str, *, emoji: str
    ) -> list[dict[str, Any]]:
        """``POST /api/v1/groups/{gid}/messages/{mid}/reactions``.

        Answers the whole chip set, ``mine`` included — server-authoritative,
        so a client never computes whether a reaction is its own.
        """
        resp = self._request(
            "POST",
            f"/api/v1/groups/{group_id}/messages/{message_id}/reactions",
            json={"emoji": emoji},
        )
        return resp.json()["reactions"]

    def clear_group_reaction(
        self, group_id: str, message_id: str, *, emoji: str
    ) -> list[dict[str, Any]]:
        """``POST /api/v1/groups/{gid}/messages/{mid}/reactions/clear`` — take
        the caller's own reaction back. Same answer as :meth:`react_to_group_message`."""
        resp = self._request(
            "POST",
            f"/api/v1/groups/{group_id}/messages/{message_id}/reactions/clear",
            json={"emoji": emoji},
        )
        return resp.json()["reactions"]

    def create_group_topic(
        self, group_id: str, *, name: str, origin_message_id: str | None = None
    ) -> dict[str, Any]:
        """``POST /api/v1/groups/{gid}/topics``. ``origin_message_id`` splits
        the topic out of an existing message rather than starting it empty."""
        payload: dict[str, Any] = {"name": name}
        if origin_message_id:
            payload["origin_message_id"] = origin_message_id
        resp = self._request("POST", f"/api/v1/groups/{group_id}/topics", json=payload)
        return resp.json()["topic"]

    def rename_group_topic(self, group_id: str, topic_id: str, *, name: str) -> dict[str, Any]:
        """``POST /api/v1/groups/{gid}/topics/{tid}/rename``."""
        return self._request(
            "POST", f"/api/v1/groups/{group_id}/topics/{topic_id}/rename", json={"name": name}
        ).json()

    def set_group_topic_archived(
        self, group_id: str, topic_id: str, *, archived: bool
    ) -> dict[str, Any]:
        """``POST /api/v1/groups/{gid}/topics/{tid}/archive`` or
        ``/unarchive`` — two server paths, one method, because they are exact
        inverses and a caller holding a boolean should not have to branch."""
        verb = "archive" if archived else "unarchive"
        return self._request("POST", f"/api/v1/groups/{group_id}/topics/{topic_id}/{verb}").json()

    def rename_group(self, group_id: str, *, name: str) -> dict[str, Any]:
        """``POST /api/v1/groups/{gid}/rename``."""
        return self._request(
            "POST", f"/api/v1/groups/{group_id}/rename", json={"name": name}
        ).json()

    def leave_group(self, group_id: str) -> dict[str, Any]:
        """``POST /api/v1/groups/{gid}/leave`` — **open rooms only**; the
        service refuses this on an email/text thread (wire ``kind: "sealed"``) with a 403."""
        return self._request("POST", f"/api/v1/groups/{group_id}/leave").json()

    def set_group_archived(self, group_id: str, *, archived: bool) -> dict[str, Any]:
        """``POST /api/v1/groups/{gid}/archive`` or ``/unarchive`` — a
        **per-viewer** preference with no groups-service call, which is why it
        is the one write here that still works during a groups outage."""
        verb = "archive" if archived else "unarchive"
        return self._request("POST", f"/api/v1/groups/{group_id}/{verb}").json()

    def invite_to_group(self, group_id: str, *, handle: str) -> dict[str, Any]:
        """``POST /api/v1/groups/{gid}/members`` — an email or a phone number.
        403 on an email/text thread (service-enforced), 422 on an unparseable
        handle."""
        return self._request(
            "POST", f"/api/v1/groups/{group_id}/members", json={"handle": handle}
        ).json()

    def remove_group_member(self, group_id: str, person_id: str) -> dict[str, Any]:
        """``DELETE /api/v1/groups/{gid}/members/{person_id}``."""
        return self._request("DELETE", f"/api/v1/groups/{group_id}/members/{person_id}").json()

    def set_group_member_role(self, group_id: str, person_id: str, *, role: str) -> dict[str, Any]:
        """``POST /api/v1/groups/{gid}/members/{person_id}/role`` — grant or
        revoke owner on ``person_id``. ``role`` is ``"owner"`` or ``"member"``.
        **Open rooms only**, owner-gated: a non-owner caller gets a 403, and
        the service's last-owner refusal comes back as a 409."""
        return self._request(
            "POST",
            f"/api/v1/groups/{group_id}/members/{person_id}/role",
            json={"role": role},
        ).json()

    def set_group_muted(self, group_id: str, *, muted: bool) -> dict[str, Any]:
        """``POST /api/v1/groups/{gid}/mute`` or ``/unmute`` — room push for this viewer only."""
        verb = "mute" if muted else "unmute"
        return self._request("POST", f"/api/v1/groups/{group_id}/{verb}").json()

    def set_group_read_mark(
        self, group_id: str, topic_id: str, *, read_through: str
    ) -> dict[str, Any]:
        """``PUT /api/v1/groups/{gid}/topics/{tid}/read`` — mark a topic read through an ISO
        timestamp. Monotonic: answers the mark that stuck."""
        return self._request(
            "PUT",
            f"/api/v1/groups/{group_id}/topics/{topic_id}/read",
            json={"read_through": read_through},
        ).json()

    def get_group_read_marks(self, group_id: str) -> dict[str, str]:
        """``GET /api/v1/groups/{gid}/read`` — ``{topic_id: read_through}`` for every
        topic the caller has a mark on. A topic with no mark is absent, not null."""
        # MIRROR: src/pepper/api/groups.py::read_marks
        # (route handler; the response is an untyped dict, so no snapshot schema)
        body = self._request("GET", f"/api/v1/groups/{group_id}/read").json()
        return {
            m["topic_id"]: m["read_through"]
            for m in body.get("marks") or []
            if isinstance(m.get("topic_id"), str) and isinstance(m.get("read_through"), str)
        }

    def list_groups_conditional(
        self, *, if_none_match: str | None = None
    ) -> tuple[GroupList | None, str | None]:
        """``GET /api/v1/groups`` with ``If-None-Match``: ``(None, etag)`` on a bodyless 304.

        The rail poll's form of :meth:`list_groups` (the server makes the list
        ETag-conditional for a client that polls it every 15 seconds). Returns the
        ``ETag`` the server sent, or ``if_none_match`` back on a 304.
        """
        headers = {"If-None-Match": if_none_match} if if_none_match else None
        resp = self._request("GET", "/api/v1/groups", params={"archived": 0}, extra_headers=headers)
        if resp.status_code == 304:
            return None, if_none_match
        return _group_list_from_dict(resp.json()), resp.headers.get("etag")

    def get_group_sharing(self, group_id: str) -> dict[str, Any]:
        """``GET /api/v1/groups/{gid}/sharing`` — which of your calendars this room sees."""
        return self._request("GET", f"/api/v1/groups/{group_id}/sharing").json()

    def set_group_sharing(self, group_id: str, *, calendars: dict[str, str]) -> dict[str, Any]:
        """``POST /api/v1/groups/{gid}/sharing`` — replaces your whole ``{calendar_id: level}``
        record for the room."""
        return self._request(
            "POST", f"/api/v1/groups/{group_id}/sharing", json={"calendars": calendars}
        ).json()

    def group_roster(self, group_id: str) -> list[GroupRosterMember]:
        """``GET /api/v1/groups/{gid}/roster`` — the ``@``-mention roster,
        Pepper prepended."""
        body = self._request("GET", f"/api/v1/groups/{group_id}/roster").json()
        return [
            GroupRosterMember(
                person_id=m["person_id"],
                display_name=m["display_name"],
                picture_url=m.get("picture_url"),
                is_pepper=m.get("is_pepper", False),
                is_agent=bool(m.get("is_agent", False)),
                agent_profile=m.get("agent_profile"),
            )
            for m in body.get("members") or []
        ]

    def create_group(self, *, people: list[str], name: str | None = None) -> dict[str, Any]:
        """``POST /api/v1/groups`` — start a room from a member set.

        ``people`` is a **list** of handles, where the web form takes one
        comma-joined string. The caller never names themself; the service adds
        them. ``name`` is required for a person: the service answers 422 to an
        unnamed create (email and text threads are made by the system, never
        by a person). ``name=None`` is still accepted here so the response
        shapes of older servers parse, but the CLI refuses an unnamed create
        before calling this.

        Answers ``{group_id, display_name, created}``. ``created`` is ``False``
        when the service handed back a room that already existed.
        """
        payload: dict[str, Any] = {"people": people}
        if name:
            payload["name"] = name
        return self._request("POST", "/api/v1/groups", json=payload).json()

    def group_activity(
        self, *, since: str, topic_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """``GET /api/v1/groups/activity`` — what moved, across the topics the
        caller names.

        ``topic_ids`` is the caller's own candidate set; omitting it answers
        empty. There is deliberately no "just tell me what's new" mode —
        naming the set is what stops the same few busiest topics winning every
        poll. Only *other-authored* messages are reported.
        """
        params: dict[str, Any] = {"since": since}
        if topic_ids:
            params["topic_ids"] = ",".join(topic_ids)
        return self._request("GET", "/api/v1/groups/activity", params=params).json()["topics"]

    def search_group_topics(self, q: str) -> list[dict[str, Any]]:
        """``GET /api/v1/groups/topics/search`` — case-insensitive substring
        across the caller's unarchived rooms. An archived topic matches only
        on its exact name."""
        return self._request("GET", "/api/v1/groups/topics/search", params={"q": q}).json()[
            "topics"
        ]

    def install_group_app(self, group_id: str, *, app_key: str) -> dict[str, Any]:
        """``POST /api/v1/groups/{gid}/apps``. A key this build does not know,
        and one this deployment does not install, are both 422s the person can
        act on rather than outages."""
        return self._request(
            "POST", f"/api/v1/groups/{group_id}/apps", json={"app_key": app_key}
        ).json()

    def uninstall_group_app(self, group_id: str, install_id: str) -> None:
        """``DELETE /api/v1/groups/{gid}/apps/{install_id}``. 204, no body. A
        row the registry no longer holds is a 404 — a stale list, not an
        outage."""
        self._request("DELETE", f"/api/v1/groups/{group_id}/apps/{install_id}")

    def group_app_launcher(self, group_id: str) -> tuple[list[GroupAppTile], bool]:
        """``GET /api/v1/groups/{gid}/apps/launcher``.

        Returns the tiles and ``installed_unavailable``, which is **not** the
        same as an empty shelf: both answer no tiles, and a client that treats
        a transient registry failure as "this room has no apps" wipes tiles it
        had already drawn correctly.
        """
        body = self._request("GET", f"/api/v1/groups/{group_id}/apps/launcher").json()
        tiles = [
            GroupAppTile(
                app_key=t["app_key"],
                slug=t["slug"],
                title=t["title"],
                principal=t["principal"],
                icon=t.get("icon"),
            )
            for t in body.get("tiles") or []
        ]
        return tiles, body.get("installed_unavailable", False)

    def group_app_chat(
        self,
        group_id: str,
        slug: str,
        *,
        message: str,
        topic_id: str,
        attachment_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """``POST /api/v1/groups/{gid}/apps/{slug}/chat`` (JSON path) — talk to
        Pepper about the room's copy of an app.

        The exchange is not posted into the topic; the topic learns of it
        through one conversation card instead. For the streaming variant the
        frames are byte-identical to ``/api/v1/chat/send``'s, so
        :func:`_iter_sse_events` parses them unchanged — see
        :meth:`chat_send_stream` for the shape of that loop.
        """
        payload: dict[str, Any] = {"message": message, "topic_id": topic_id}
        if attachment_ids:
            payload["attachment_ids"] = attachment_ids
        return self._request(
            "POST", f"/api/v1/groups/{group_id}/apps/{slug}/chat", json=payload
        ).json()

    def group_app_transcript(self, group_id: str, slug: str) -> list[dict[str, Any]]:
        """``GET /api/v1/groups/{gid}/apps/{slug}/chat`` — the last lines of
        the room's **shared** thread about this app, oldest-first."""
        return self._request("GET", f"/api/v1/groups/{group_id}/apps/{slug}/chat").json()[
            "exchanges"
        ]

    def search_contacts(self, q: str) -> list[dict[str, Any]]:
        """``GET /api/v1/people/contacts/search`` — the create-group field's
        autocomplete. Under ``people``, not ``groups``: what it returns is a
        Contacts search, and groups is merely its first caller. Under two
        characters answers empty rather than erroring.

        ``handle`` is the one field to fill into :meth:`create_group`.
        """
        return self._request("GET", "/api/v1/people/contacts/search", params={"q": q}).json()[
            "contacts"
        ]

    def list_artifacts(
        self,
        *,
        principal: str | None = None,
        kind: str | None = None,
        q: str | None = None,
        label: str | None = None,
        name: str | None = None,
        name_prefix: str | None = None,
        limit: int | None = None,
    ) -> ArtifactListResponse:
        """``GET /api/v1/artifacts`` — list and search one owner's artifacts.

        ``principal`` omitted (or equal to the caller) is self-access with no
        membership call; any other value must be a live group the caller
        belongs to, or the request 404s — an unanswerable membership check is
        a 503. ``q`` is a case-insensitive substring match over name and
        body, not semantic search; ``name`` and ``name_prefix`` are
        case-insensitive matches on the whole name and its start, for
        resolving one. ``limit`` defaults to 50 server-side and
        caps at 200.
        """
        params: dict[str, Any] = {}
        for key, value in (
            ("principal", principal),
            ("kind", kind),
            ("q", q),
            ("label", label),
            ("name", name),
            ("name_prefix", name_prefix),
            ("limit", limit),
        ):
            if value is not None:
                params[key] = value
        body = self._request("GET", "/api/v1/artifacts", params=params).json()
        return ArtifactListResponse(
            truncated=body["truncated"],
            items=[_artifact_list_item_from_dict(row) for row in body.get("items") or []],
        )

    def get_artifact_detail(self, artifact_id: str) -> ArtifactDetail:
        """``GET /api/v1/artifacts/{id}/detail`` — one artifact's full text.

        Viewer-scoped server-side (no ``?principal=`` here): widened to the
        caller's own artifacts plus their groups' shared documents, so a
        member reads a room's note without naming the room. 404 covers
        missing, deleted and inaccessible alike — the same non-distinction
        the web door makes, so a prober learns nothing about which ids exist.
        """
        body = self._request("GET", f"/api/v1/artifacts/{artifact_id}/detail").json()
        return _artifact_detail_from_dict(body)

    def create_artifact(
        self,
        *,
        name: str,
        body: str,
        principal: str | None = None,
    ) -> ArtifactCreateResult:
        """``POST /api/v1/artifacts/new`` — save a new note.

        Hardcodes ``kind="inline_text"``: this wraps the notes write path
        only, not the general
        artifact-creation route, which also takes links. ``principal``
        resolves the same as every other artifact call — omitted is
        self-access, a room name must be a group the caller belongs to or
        the request 404s.
        """
        params: dict[str, Any] = {}
        if principal is not None:
            params["principal"] = principal
        resp = self._request(
            "POST",
            "/api/v1/artifacts/new",
            json={"name": name, "kind": "inline_text", "body": body},
            params=params or None,
        ).json()
        return ArtifactCreateResult(id=str(resp["id"]), name=resp["name"], etag=resp["etag"])

    def edit_artifact(
        self,
        artifact_id: str,
        *,
        name: str | None = None,
        body: str | None = None,
        principal: str | None = None,
        if_match: str | None = None,
    ) -> ArtifactDetail:
        """``POST /api/v1/artifacts/{id}/edit`` — rewrite a note in place.

        "Only what arrives is touched" (``ArtifactEditRequest``'s own
        contract, server-side): a ``None`` field here is left alone, so a
        caller that only renames sends no ``body``, and a caller whose text
        is unchanged (``pepper notes edit``'s round trip) sends no
        ``body`` either, avoiding a spurious version row.

        ``if_match`` is the bare etag digest or ``None`` for last-write-wins
        (``--force``); a mismatch raises :class:`ArtifactConflictError`
        rather than the generic :class:`ApiError`, carrying the artifact's
        current body/etag/updated_at exactly as the 412 response did, so the
        CLI's conflict flow can diff and re-open without a second request.
        """
        params: dict[str, Any] = {}
        if principal is not None:
            params["principal"] = principal
        headers = {"If-Match": if_match} if if_match else None
        try:
            resp = self._request(
                "POST",
                f"/api/v1/artifacts/{artifact_id}/edit",
                json={"name": name, "body": body},
                params=params or None,
                extra_headers=headers,
            ).json()
        except ApiError as exc:
            if exc.status != 412:
                raise
            current: dict[str, Any] = {}
            with contextlib.suppress(_json.JSONDecodeError):
                current = _json.loads(exc.body)
            raise ArtifactConflictError(
                exc.status,
                exc.body,
                detail=exc.detail,
                current_body=current.get("body"),
                current_etag=current.get("etag"),
                current_updated_at=current.get("updated_at"),
            ) from exc
        return _artifact_detail_from_dict(resp["artifact"])

    def delete_artifact(
        self, artifact_id: str, *, principal: str | None = None, app: str = "notes"
    ) -> str:
        """``POST /api/v1/apps/notes/actions/delete`` — soft-delete a note.

        ``app="artifacts"`` is the same action under the generic Artifacts app, which is what
        ``pepper artifacts delete`` calls; both delete any artifact kind.

        The notes applet's declared ``delete`` action
        (``pepper.agent.artifacts.actions``), not a bespoke artifacts route:
        deleting is ``owner_scoped``, so ``principal`` reaches the same
        room-resolution the read/write routes use, and the action's audit
        trail (trajectory, policy gate) is the one every other delete in the
        product goes through. Returns the deleted note's name.
        """
        params: dict[str, Any] = {}
        if principal is not None:
            params["principal"] = principal
        resp = self._request(
            "POST",
            f"/api/v1/apps/{app}/actions/delete",
            json={"params": {"id": artifact_id}},
            params=params or None,
        ).json()
        result = resp.get("result") or {}
        return str(result.get("name", ""))

    def list_apps(self) -> list[dict[str, Any]]:
        """``GET /api/v1/apps``: every app's summary (``slug``, ``title``, ``description``,
        ``owner_scoped_actions``, ...). The list is the only place the server names an
        app's actions, and it names just the ``owner_scoped`` ones."""
        body = self._request("GET", "/api/v1/apps").json()
        if not isinstance(body, list):
            raise ApiError(status=200, body=str(body), detail="malformed response")
        return [row for row in body if isinstance(row, dict)]

    def get_artifact_detail_raw(self, artifact_id: str) -> dict[str, Any]:
        """``GET /api/v1/artifacts/{id}/detail`` as the server's own dict (``kind_label``,
        ``mime_type``, ``size_label``, ``text_body``, ``link``, ``viewer``, ... for every kind),
        where :meth:`get_artifact_detail` keeps only the note fields."""
        body = self._request("GET", f"/api/v1/artifacts/{artifact_id}/detail").json()
        if not isinstance(body, dict):
            raise ApiError(status=200, body=str(body), detail="malformed response")
        return body

    def download_artifact(self, artifact_id: str) -> bytes:
        """``GET /api/v1/artifacts/{id}/raw``: a file artifact's bytes. 404 for a missing,
        deleted or inaccessible artifact, and for one that is not a file (notes and links have
        no bytes). The server caps a file at ``artifact_max_blob_bytes`` (25 MiB by default),
        so the body is read whole."""
        return self._request("GET", f"/api/v1/artifacts/{artifact_id}/raw").content

    def upload_artifact(
        self,
        *,
        filename: str,
        content: bytes,
        content_type: str,
        name: str = "",
        summary: str = "",
        labels: str = "",
        principal: str | None = None,
    ) -> dict[str, Any]:
        """``POST /api/v1/artifacts/upload`` (multipart): store a file as a ``blob`` artifact.

        Answers ``{id, name, mime_type, byte_size}``. 415 for a type off the server's
        allowlist (or bytes that do not match the declared type), 413 over the size cap, 400
        for an empty file. ``labels`` is the form field's comma-separated string. The
        answer's ``id`` is also what :meth:`chat_send_stream` takes in ``attachment_ids``.
        """
        params = {"principal": principal} if principal is not None else None
        resp = self._request(
            "POST",
            "/api/v1/artifacts/upload",
            files={
                "file": (filename, content, content_type),
                "name": (None, name),
                "summary": (None, summary),
                "labels": (None, labels),
            },
            params=params,
        ).json()
        if not isinstance(resp, dict) or not resp.get("id"):
            raise ApiError(status=200, body=str(resp), detail="malformed response")
        return resp

    def get_calendar_preferences(self) -> dict[str, Any]:
        """``GET /api/v1/calendars/preferences``: the Calendar > Preferences page's values
        (addresses, timezone, work days and hours, meals, workout, weather switches) plus
        two page helpers, ``timezones`` (the allowed zone names) and ``maps``."""
        body = self._request("GET", "/api/v1/calendars/preferences").json()
        if not isinstance(body, dict):
            raise ApiError(status=200, body=str(body), detail="malformed response")
        return body

    def set_calendar_preferences(self, changes: dict[str, Any]) -> dict[str, Any]:
        """``POST /api/v1/calendars/preferences`` with only the keys given: the server keeps
        every field the body leaves out (``PreferencesIn.model_fields_set``). Answers the
        saved values. 422 for a value it refuses (unknown timezone, an address Maps cannot
        place, a bad time)."""
        body = self._request("POST", "/api/v1/calendars/preferences", json=changes).json()
        if not isinstance(body, dict):
            raise ApiError(status=200, body=str(body), detail="malformed response")
        return body

    def get_app_data(self, slug: str, *, principal: str | None = None) -> dict[str, Any]:
        """``GET /api/v1/apps/{slug}/data[?principal=]``: ``{"rev": int, "data": {...}}``,
        the app's document for the caller, or for the room ``principal``."""
        params = {"principal": principal} if principal is not None else None
        body = self._request("GET", f"/api/v1/apps/{slug}/data", params=params).json()
        if not isinstance(body, dict):
            raise ApiError(status=200, body=str(body), detail="malformed response")
        return body

    def run_app_action(
        self,
        slug: str,
        action: str,
        params: dict[str, Any],
        *,
        principal: str | None = None,
    ) -> dict[str, Any]:
        """``POST /api/v1/apps/{slug}/actions/{action}[?principal=]`` with ``{"params": ...}``:
        the action's ``{"result": {...}}``. An action the app does not declare is a 404."""
        query = {"principal": principal} if principal is not None else None
        body = self._request(
            "POST",
            f"/api/v1/apps/{slug}/actions/{action}",
            json={"params": params},
            params=query,
        ).json()
        if not isinstance(body, dict):
            raise ApiError(status=200, body=str(body), detail="malformed response")
        return body

    def download_takeout(self, *, out_path: str | None = None) -> str:
        """``GET /api/v1/takeout``. Streams the zip archive straight to
        disk — never buffers the whole body in memory, since an archive
        with blobs/embeddings can run to hundreds of MB.

        ``out_path`` is where to write the file. When omitted, the
        server's ``Content-Disposition`` filename is used if present,
        else :data:`DEFAULT_TAKEOUT_FILENAME`. Returns the path actually
        written to, so a caller that didn't pass ``out_path`` still
        knows where the file landed.

        The archive holds your data but none of your credentials — the
        server excludes OAuth tokens and API-token hashes, listing the
        accounts to reconnect in the manifest instead.

        Raises :class:`ApiError` on a non-2xx response before anything is
        written to disk.
        """
        url = f"{self._server_url}/api/v1/takeout"
        headers = {
            "Authorization": f"Bearer {self._token.get_secret_value()}",
            "User-Agent": USER_AGENT,
        }
        with self._client.stream("GET", url, headers=headers) as resp:
            # Strict 2xx here, not the >=400 the JSON paths use: httpx
            # doesn't follow redirects by default, and a 3xx body written
            # to disk would masquerade as a complete backup. The JSON
            # paths already fail loudly on 3xx (body doesn't parse); this
            # is the one path where a non-2xx body could pass silently.
            if not resp.is_success:
                # Materialize the body so the ApiError can carry a detail
                # message — same dance as the other error paths.
                resp.read()
                _raise_for_error(resp)
            resolved_path = (
                out_path
                or _filename_from_content_disposition(resp.headers.get("content-disposition"))
                or DEFAULT_TAKEOUT_FILENAME
            )
            # Stream into a sibling .part file and rename into place only
            # once the body is fully written — a connection dropped
            # mid-download must not leave a truncated archive at the final
            # path where it could later pass for a complete backup.
            part_path = f"{resolved_path}.part"
            try:
                with open(part_path, "wb") as fh:
                    fh.writelines(resp.iter_bytes())
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(part_path)
                raise
            os.replace(part_path, resolved_path)
        return resolved_path

    # ------------------------------------------------------------------
    # Plumbing

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        files: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """Issue one request with the bearer header. Raises
        :class:`ApiError` on a non-2xx response; lets transport-level
        errors (``httpx.RequestError`` subclasses) bubble up since
        those are caller-handled at the CLI boundary.

        ``params`` are query parameters, ``extra_headers`` merges over the
        bearer (``Idempotency-Key`` is the only caller today), and ``files``
        is a multipart body for the one upload endpoint. All three exist
        because ``/api/v1/groups`` needs them; they default to ``None`` so
        every caller written before them is untouched. ``timeout`` overrides the client's
        timeout for this request only; ``None`` keeps the client's (never "no timeout").
        """
        url = f"{self._server_url}{path}"
        # Build the Authorization header from the wrapped token on
        # every call — keeps the plaintext off ``self`` and confined
        # to this stack frame.
        headers = {
            "Authorization": f"Bearer {self._token.get_secret_value()}",
            "User-Agent": USER_AGENT,
        }
        if extra_headers:
            headers.update(extra_headers)
        resp = self._client.request(
            method,
            url,
            json=json,
            params=params,
            files=files,
            headers=headers,
            timeout=httpx.USE_CLIENT_DEFAULT if timeout is None else timeout,
        )
        if resp.status_code >= 400:
            _raise_for_error(resp)
        return resp


def _trajectory_detail_from_dict(body: dict[str, Any]) -> TrajectoryDetail:
    """Parse a ``TrajectoryDetailResponse`` JSON body into dataclasses.

    Kept as a free function (not an ``ApiClient`` method) so the
    raw-dict / parsed surfaces don't drift — both go through this
    when a caller wants the typed shape.
    """
    traj_row = body["trajectory"]
    return TrajectoryDetail(
        trajectory=TrajectoryRef(
            id=traj_row["id"],
            user_id=traj_row["user_id"],
            channel=traj_row["channel"],
            conversation_key=traj_row.get("conversation_key"),
            created_at=traj_row["created_at"],
            closed_at=traj_row.get("closed_at"),
            meta=traj_row.get("meta") or {},
        ),
        events=[
            TrajectoryEvent(
                id=ev["id"],
                seq=ev["seq"],
                event_type=ev["event_type"],
                payload=ev.get("payload") or {},
                created_at=ev["created_at"],
            )
            for ev in body.get("events") or []
        ],
    )


def _todos_path(group_id: str | None) -> str:
    return f"/api/v1/groups/{group_id}/todos" if group_id else "/api/v1/todos"


def _todo_from_dict(row: dict[str, Any]) -> TodoEntry:
    """Parse one ``TodoEntry`` JSON object into the dataclass.

    Free function (not an ``ApiClient`` method) so the list and the
    single-row create/done/dismiss responses all parse through one
    place and can't drift on field handling.
    """
    return TodoEntry(
        id=row["id"],
        title=row["title"],
        details=row.get("details"),
        status=row["status"],
        source=row["source"],
        priority=row.get("priority"),
        due_date=row.get("due_date"),
        due_is_date_only=row.get("due_is_date_only"),
        created_at=row["created_at"],
        done_at=row.get("done_at"),
        source_ref=row.get("source_ref"),
        remind_at=row.get("remind_at"),
        assignee=_todo_person(row.get("assignee")),
        requested_by=_todo_person(row.get("requested_by")),
        next_action=_todo_next_action(row.get("next_action")),
        next_action_suggestion=_todo_next_action_suggestion(row.get("next_action_suggestion")),
    )


def _todo_next_action(raw: object) -> TodoNextAction | None:
    """A ``TodoNextAction`` from the wire, or None when absent or not the expected shape."""
    if not isinstance(raw, dict) or not isinstance(raw.get("text"), str) or not raw["text"]:
        return None
    return TodoNextAction(
        text=raw["text"],
        by=str(raw.get("by") or "user"),
        updated_at=str(raw.get("updated_at") or ""),
    )


def _todo_next_action_suggestion(raw: object) -> TodoNextActionSuggestion | None:
    """A ``TodoNextActionSuggestion`` from the wire, or None when absent or malformed."""
    if not isinstance(raw, dict) or not isinstance(raw.get("text"), str) or not raw["text"]:
        return None
    return TodoNextActionSuggestion(text=raw["text"], updated_at=str(raw.get("updated_at") or ""))


def _todo_person(raw: object) -> TodoPerson | None:
    """A ``TodoPerson`` from the wire, or None when absent or not the expected shape."""
    if not isinstance(raw, dict) or "user_id" not in raw or "label" not in raw:
        return None
    return TodoPerson(user_id=_opt_str(raw["user_id"]), label=_opt_str(raw["label"]))


def _opt_str(value: object) -> str | None:
    """``None`` stays ``None`` (never the string ``"None"``); anything else is its text."""
    return None if value is None else str(value)


def _room_skills_path(group_id: str) -> str:
    return f"/api/v1/groups/{group_id}/skills"


def _scheduled_tasks_path(group_id: str | None) -> str:
    return f"/api/v1/groups/{group_id}/scheduled-tasks" if group_id else "/api/v1/scheduled-tasks"


def _room_delivery(destinations: list[str], topic: str | None) -> dict[str, Any]:
    """The room wire's ``delivery`` object. ``destinations`` is required there;
    ``topic`` is sent only when named, so an omission stays the server's choice."""
    delivery: dict[str, Any] = {"destinations": destinations}
    if topic is not None:
        delivery["topic"] = topic
    return delivery


def _scheduled_task_fields(row: dict[str, Any]) -> dict[str, Any]:
    """The ``ScheduledTaskSummary`` field mapping, as a kwargs dict.

    Split out from :func:`_scheduled_task_from_dict` so the detail
    response — which is the same shape plus ``latest_run`` — can reuse it
    instead of restating ten field names that would then drift.
    """
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row.get("description"),
        "cron": row.get("cron"),
        "timezone": row.get("timezone"),
        # ``.get`` would collapse a missing key onto ``None``, which is a
        # real state here (inherit) — but the server always sends the key,
        # so a missing one is a malformed response, not a silent task.
        "destinations": row["destinations"],
        "active": row["active"],
        "one_shot": row["one_shot"],
        "last_run_at": row.get("last_run_at"),
        "next_run_at": row["next_run_at"],
    }


def _scheduled_task_from_dict(row: dict[str, Any]) -> ScheduledTaskSummary:
    """Parse one task row — the list endpoint and every write response."""
    return ScheduledTaskSummary(**_scheduled_task_fields(row))


def _room_scheduled_task_from_dict(row: dict[str, Any]) -> RoomScheduledTaskSummary:
    """Parse one room task row — the room listing and every room write."""
    return RoomScheduledTaskSummary(
        **_scheduled_task_fields(row),
        delivery=row["delivery"],
        last_run_status=row.get("last_run_status"),
    )


def _keyset_params(limit: int, before: str | None, before_id: str | None) -> dict[str, Any]:
    """Query params for a ``before``/``before_id`` keyset page (None cursor parts dropped)."""
    params: dict[str, Any] = {"limit": limit}
    if before is not None:
        params["before"] = before
    if before_id is not None:
        params["before_id"] = before_id
    return params


#: Base path of the personal research-project routes.
RESEARCH_PROJECTS_PATH = "/api/v1/research-projects"


def _research_base(group_id: str | None) -> str:
    """The research-project routes: yours, or the room's twin under ``/groups/{gid}``."""
    if group_id is None:
        return RESEARCH_PROJECTS_PATH
    return f"/api/v1/groups/{group_id}/research-projects"


def _research_path(group_id: str | None, project_id: str, *parts: str) -> str:
    """``{base}/{project_id}[/part…]`` for the personal or the room route."""
    return "/".join([_research_base(group_id), project_id, *parts])


#: The 403 ``code`` values the server uses to say the feature is switched off, as opposed to
#: a role 403 (``planning/research-projects.md`` §8).
RESEARCH_DISABLED_CODES = frozenset(
    {"research_projects_disabled", "research_group_projects_disabled"}
)


def error_code(exc: ApiError) -> str | None:
    """The machine ``code`` an error body carries beside ``detail``, or ``None``."""
    try:
        body = _json.loads(exc.body)
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None


def _page_params(limit: int, before: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    if before is not None:
        params["before"] = before
    return params


def _fields[T](cls: type[T], row: dict[str, Any], **parsed: Any) -> T:
    """``cls`` built from the keys of ``row`` it declares. An unknown key is ignored (``/api/v1``
    is additive-only); an optional field (``X | None``) the server leaves out reads as ``None``.
    A required field that is missing or ``null`` is a malformed response, raised as an
    :class:`ApiError` naming it, so no renderer ever sees ``None`` where the type says it can't
    be. ``parsed`` overrides nested fields."""
    values: dict[str, Any] = {}
    for f in dataclasses.fields(cast("Any", cls)):
        if f.name in parsed:
            continue
        value = row.get(f.name)
        # ``from __future__ import annotations`` leaves ``f.type`` as the annotation's text.
        if value is None and "None" not in str(f.type):
            raise ApiError(
                status=200,
                body=str(row)[:200],
                detail=f"malformed response: {cls.__name__} has no {f.name!r}",
            )
        values[f.name] = value
    return cls(**{**values, **parsed})


def _research_schedule_from(row: object) -> ResearchSchedule | None:
    schedule = _nested(ResearchSchedule, row)
    if schedule is not None and isinstance(row, dict) and "delivery_topic_id" not in row:
        object.__setattr__(schedule, "delivery_topic_reported", False)
    return schedule


def _nested[T](cls: type[T], row: object) -> T | None:
    return _fields(cls, row) if isinstance(row, dict) else None


def _nested_list[T](cls: type[T], rows: object) -> list[T]:
    return [_fields(cls, r) for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _research_summary_parts(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "schedule": _research_schedule_from(row.get("schedule")),
        "last_iteration": _nested(ResearchLastIteration, row.get("last_iteration")),
        "inflight_run": _research_inflight(row.get("inflight_run")),
    }


def _research_inflight(row: object) -> ResearchInflightRun | None:
    if not isinstance(row, dict):
        return None
    return _fields(
        ResearchInflightRun, row, requested_by=_nested(ResearchPerson, row.get("requested_by"))
    )


def _research_summary(row: dict[str, Any]) -> ResearchProjectSummary:
    return _fields(ResearchProjectSummary, row, **_research_summary_parts(row))


def _research_detail(row: dict[str, Any]) -> ResearchProjectDetail:
    steering = row.get("pending_steering")
    return _fields(
        ResearchProjectDetail,
        row,
        pending_steering=_nested_list(ResearchSteeringNote, steering)
        if isinstance(steering, list)
        else None,
        **_research_summary_parts(row),
        filters=_nested_list(ResearchFilter, row.get("filters")),
        open_questions=_nested_list(ResearchQuestion, row.get("open_questions")),
        item_counts=row.get("item_counts") or {},
        owner=_nested(ResearchOwner, row.get("owner")),
    )


def _research_iteration(row: dict[str, Any]) -> ResearchIteration:
    return _fields(
        ResearchIteration, row, requested_by=_nested(ResearchPerson, row.get("requested_by"))
    )


def _research_report(row: dict[str, Any]) -> ResearchReport:
    return _fields(
        ResearchReport,
        row,
        open_questions=_nested_list(ResearchQuestion, row.get("open_questions")),
        changelog=_nested_list(ResearchChangelogEntry, row.get("changelog")),
    )


def _research_item(row: dict[str, Any]) -> ResearchItem:
    return _fields(ResearchItem, row)


def _research_item_detail(row: dict[str, Any]) -> ResearchItemDetail:
    return _fields(ResearchItemDetail, row, citations=row.get("citations") or [])


def _research_run_detail(row: dict[str, Any]) -> ResearchRunDetail:
    return _fields(
        ResearchRunDetail,
        row,
        iteration=(
            _research_iteration(row["iteration"])
            if isinstance(row.get("iteration"), dict)
            else None
        ),
        changelog=_nested_list(ResearchChangelogEntry, row.get("changelog")),
    )


def _in_flight_run_id(exc: ApiError) -> str | None:
    """The ``run_id`` a 409 body carries when the conflict is a run already in flight."""
    if exc.status != 409:
        return None
    try:
        body = _json.loads(exc.body)
    except ValueError:
        return None
    run_id = body.get("run_id") if isinstance(body, dict) else None
    if not isinstance(run_id, str):
        return None
    try:
        return str(uuid.UUID(run_id))
    except ValueError:
        return None


def _schedule_body(cron: str, timezone: str | None) -> dict[str, Any]:
    """The cron, and the zone only when one was given (else the server uses the profile's)."""
    return {"cron": cron} if timezone is None else {"cron": cron, "timezone": timezone}


def _scheduled_task_id(exc: ApiError) -> str | None:
    """The ``scheduled_task_id`` a 409 body carries when the project is already scheduled."""
    if exc.status != 409:
        return None
    try:
        body = _json.loads(exc.body)
    except ValueError:
        return None
    task_id = body.get("scheduled_task_id") if isinstance(body, dict) else None
    if not isinstance(task_id, str):
        return None
    try:
        return str(uuid.UUID(task_id))
    except ValueError:
        return None


def _research_page[T](
    body: dict[str, Any], parse: Callable[[dict[str, Any]], T]
) -> ResearchPage[T]:
    rows = body.get("items")
    if not isinstance(rows, list):
        raise ApiError(status=200, body=str(body)[:200], detail="malformed response")
    cursor = body.get("next_before")
    return ResearchPage(
        items=[parse(r) for r in rows if isinstance(r, dict)],
        next_before=cursor if isinstance(cursor, str) else None,
    )


def _scheduled_run_from_dict(row: dict[str, Any]) -> ScheduledTaskRunEntry:
    """Parse one run row — the runs listing and ``latest_run``."""
    return ScheduledTaskRunEntry(
        id=row["id"],
        fired_at=row["fired_at"],
        finished_at=row.get("finished_at"),
        status=row["status"],
        error=row.get("error"),
        delivered_channels=row.get("delivered_channels"),
        trajectory_id=row.get("trajectory_id"),
        reply_preview=row.get("reply_preview"),
    )


#: Printed on a 426 whose body carries no usable instruction of its own.
DEFAULT_UPGRADE_MESSAGE = (
    "This version of pepper is no longer supported. Upgrade with: uv tool upgrade vycari-pepper"
)

_MAX_UPGRADE_MESSAGE_CHARS = 300


def _sanitize_terminal_line(text: str, *, max_chars: int) -> str:
    """First non-blank line of server-supplied ``text``, safe to print.

    Server text is untrusted for terminal purposes: an escape sequence in it
    could clear the screen, move the cursor or retitle the window. This drops
    whole escape sequences and invisible format characters (bidirectional
    overrides, zero-width characters), turns every other control character
    into a space, collapses whitespace runs and caps the length. Returns
    ``""`` when nothing printable is left.
    """
    text = strip_sequences(text)
    text = CONTROL_CHAR_RE.sub(" ", text.replace("\r\n", "\n").replace("\r", "\n"))
    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        if line:
            return line[:max_chars].rstrip()
    return ""


def _upgrade_message(resp: httpx.Response) -> str:
    """The server's one-line upgrade instruction, or the built-in default.

    Takes the JSON ``detail`` string when there is one, else the first line of
    a plain-text body, else :data:`DEFAULT_UPGRADE_MESSAGE`. The text is
    printed straight to the terminal, so it goes through
    :func:`_sanitize_terminal_line` first. Needs a materialized body, like
    :func:`_extract_detail`.
    """
    text = _extract_detail(resp)
    if text is None:
        try:
            resp.json()
        except ValueError:
            # Not JSON at all: treat the body as the plain-text instruction.
            text = resp.text
    line = _sanitize_terminal_line(text or "", max_chars=_MAX_UPGRADE_MESSAGE_CHARS)
    return line or DEFAULT_UPGRADE_MESSAGE


def _raise_for_error(resp: httpx.Response) -> NoReturn:
    """Raise for an error response (the body must already be read).

    The one place a status >= 400 becomes an exception: 426 becomes
    :class:`UpgradeRequiredError`, everything else :class:`ApiError`.
    """
    if resp.status_code == 426:
        raise UpgradeRequiredError(_upgrade_message(resp))
    raise ApiError(
        status=resp.status_code,
        body=resp.text,
        detail=_extract_detail(resp),
        retry_after=_retry_after(resp),
    )


def _utcnow() -> dt.datetime:
    """The current UTC time, a seam for the ``Retry-After`` tests."""
    return dt.datetime.now(dt.UTC)


def _retry_after(resp: httpx.Response) -> float | None:
    """The ``Retry-After`` header as seconds to wait, or ``None`` when absent or unreadable.

    Both forms RFC 9110 §10.2.3 allows: delta-seconds, and an HTTP date (IMF-fixdate, or the
    obsolete RFC 850 and asctime forms), which is turned into the delay until then. A date
    already past is ``0.0`` (retry now), never negative. A number that is negative or not
    finite is not delta-seconds at all, so it is ``None``, like any other unreadable value.
    """
    value = resp.headers.get("Retry-After", "").strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        pass
    else:
        return seconds if seconds >= 0 and math.isfinite(seconds) else None
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:  # asctime carries no zone; HTTP dates are always GMT
        when = when.replace(tzinfo=dt.UTC)
    return max((when - _utcnow()).total_seconds(), 0.0)


def _extract_detail(resp: httpx.Response) -> str | None:
    """Pull ``{"detail": "..."}`` from a FastAPI-style error response.

    Returns ``None`` for non-JSON bodies, non-dict JSON payloads, and
    ``detail`` values that aren't a string (FastAPI's 422 validation
    error uses ``detail: [...]`` — a list — which we surface via
    ``body`` instead). Lets every error path build its ``ApiError``
    in one line without duplicating the extraction dance.

    The response must already have a materialized body — streaming
    responses need a ``resp.read()`` call before this for ``resp.json``
    / ``resp.text`` to be available.
    """
    try:
        payload = resp.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    detail = payload.get("detail")
    if isinstance(detail, str):
        return detail
    return None


def _filename_from_content_disposition(value: str | None) -> str | None:
    r"""Pull the ``filename="..."`` (or bare ``filename=...``) parameter
    out of a ``Content-Disposition`` header value.

    Returns ``None`` when the header is missing or has no filename
    parameter — the caller falls back to :data:`DEFAULT_TAKEOUT_FILENAME`
    in that case. Doesn't handle RFC 5987 ``filename*=`` (Pepper's server
    only ever sends plain ``filename=``, and this is a thin client, not a
    general-purpose HTTP library).

    The result is reduced to its final path segment (both ``/`` and ``\\``
    separators): the header is server-controlled, and without this a
    malicious or MITM'd ``--server`` target could point the write at
    ``../../.ssh/authorized_keys`` or an absolute path.
    """
    if not value:
        return None
    match = _re.search(r'filename="?([^";]+)"?', value)
    if not match:
        return None
    name = match.group(1).strip()
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if name in ("", ".", ".."):
        return None
    return name


def _iter_sse_events(lines: Iterator[str]) -> Iterator[StreamEvent]:
    r"""Parse a line-iterator into :class:`StreamEvent` frames.

    SSE wire format: each frame is one or more ``event:`` / ``data:``
    / ``id:`` / ``retry:`` / ``:`` (comment) lines terminated by a
    blank line. We only care about ``event:`` (frame type) and
    ``data:`` (JSON payload). Multiple ``data:`` lines in one frame
    are joined with ``\\n`` per the spec.

    Frames with no JSON-parseable ``data:`` payload are dropped
    silently — defends against the server emitting a comment line or
    a future heartbeat frame the client doesn't understand. Frames
    with no ``event:`` line default to ``"message"`` (the SSE
    default) so a non-pepper SSE stream wouldn't crash this parser.
    """
    event_type: str | None = None
    data_lines: list[str] = []
    for line in lines:
        if not line:
            # Blank line — end of frame. Emit if we have anything to
            # emit, then reset for the next frame.
            if data_lines:
                event = _build_stream_event(event_type, data_lines)
                if event is not None:
                    yield event
            event_type = None
            data_lines = []
            continue
        if line.startswith(":"):
            # SSE comment line — used for keep-alives. Ignore.
            continue
        field_name, _, value = line.partition(":")
        # Per spec, a single optional space after the colon is part of
        # the separator, not the value.
        value = value.removeprefix(" ")
        if field_name == "event":
            event_type = value
        elif field_name == "data":
            data_lines.append(value)
        # Other field names (id, retry) intentionally ignored — we
        # don't reconnect on disconnect and don't track event ids.

    # Flush a trailing frame that wasn't terminated with a blank line
    # (the server's last write before EOF). Real SSE always sends the
    # blank but httpx's iter_lines may not.
    if data_lines:
        event = _build_stream_event(event_type, data_lines)
        if event is not None:
            yield event


def _iter_feed_frames(lines: Iterator[str]) -> Iterator[FeedFrame]:
    r"""Parse a line-iterator into :class:`FeedFrame`s.

    Unlike :func:`_iter_sse_events` this keeps the ``id:`` field, because the
    feed's id *is* the resume cursor. A frame whose ``data:`` is not a JSON
    object, or whose ``id:`` is not an integer, is dropped rather than fatal;
    a comment line (``: ping``) becomes a ``"comment"`` frame.
    """
    event_type: str | None = None
    event_id: int | None = None
    data_lines: list[str] = []

    def build() -> FeedFrame | None:
        if not data_lines:
            return None
        try:
            payload = _json.loads("\n".join(data_lines))
        except _json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return FeedFrame(kind=event_type or "message", id=event_id, payload=payload)

    for line in lines:
        if not line:
            frame = build()
            if frame is not None:
                yield frame
            event_type, event_id, data_lines = None, None, []
            continue
        if line.startswith(":"):
            # ``: connected`` / ``: ping``: proof the connection is alive. The
            # text is kept so a caller can tell an accepted connection from one
            # that has stayed healthy long enough to send a heartbeat.
            yield FeedFrame(kind="comment", id=None, payload={"comment": line[1:].strip()})
            continue
        field_name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field_name == "event":
            event_type = value
        elif field_name == "data":
            data_lines.append(value)
        elif field_name == "id":
            event_id = int(value) if value.isdecimal() else None
    frame = build()
    if frame is not None:
        yield frame


def _build_stream_event(event_type: str | None, data_lines: list[str]) -> StreamEvent | None:
    """Build a :class:`StreamEvent` from the accumulated SSE fields,
    or return ``None`` if the payload isn't parseable JSON.

    Returning ``None`` (rather than raising) keeps a single malformed
    frame from killing the whole stream — the next valid frame still
    lands. Operationally we'd much rather drop one bad event than
    burn the user's whole REPL turn.
    """
    raw = "\n".join(data_lines)
    try:
        payload = _json.loads(raw)
    except _json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return StreamEvent(kind=event_type or "message", payload=payload)
