"""``pepper next``: what is happening now, today's calendar, and your open todos.

Pure presentation of what the server returns; no decisions are made here. Todos are shown in
the order Pepper returns them (``GET /api/v1/todos?status=open``, or a room's own list for
``--room``); the CLI never ranks, reorders, hides or annotates them. Today's events come from the
schedule plan's ``your_day`` (``GET /api/v1/calendars/schedule-plan``); when the plan is missing
or fails, the calendar range read stands in, and when that fails too the result says so.
Everything here is a read.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from pepper_cli import api_client

#: Shown when neither the plan nor the calendar range could be read.
NO_CALENDAR_NOTE = "calendar unavailable: showing todos only"


def _clock(zone: tzinfo) -> datetime:
    return datetime.now(zone)


def user_now(api: Any) -> datetime:
    """Now in the account's timezone (whoami), never the host's; UTC when unset, as the server."""
    name = api.whoami().timezone
    try:
        zone: tzinfo = ZoneInfo(name) if name else UTC
    except (ZoneInfoNotFoundError, ValueError):
        zone = UTC
    return _clock(zone)


def _tolerate(call: Any) -> Any:
    try:
        return call()
    except (api_client.ApiError, httpx.RequestError):
        return None


@dataclasses.dataclass(frozen=True)
class Scope:
    """What ``--room`` and ``--source`` narrow the todos to; empty (falsy) keeps everything.

    ``room_id`` is a room's id, already resolved strictly by the caller: the todos are read
    from that room's list. ``source`` is a case-insensitive prefix of the todo's source
    (``user``, or a producer name).
    """

    room_id: str | None = None
    source: str | None = None

    def __bool__(self) -> bool:
        return bool(self.room_id or self.source)


def _source_ok(todo: api_client.TodoEntry, scope: Scope) -> bool:
    """``--source``: a plain case-insensitive prefix match on the todo's own ``source`` field."""
    return not scope.source or todo.source.lower().startswith(scope.source.lower())


def _minutes(delta_seconds: float) -> int:
    return max(0, int(delta_seconds // 60))


def _label(iso: str | None, tz: tzinfo | None) -> str | None:
    """A clock time like "2:30pm" for an ISO instant, in ``tz``.

    Built by hand rather than with ``%-I``: that no-padding flag is glibc's, and Windows'
    ``strftime`` rejects it with ``ValueError``.
    """
    if not iso:
        return None
    t = datetime.fromisoformat(iso)
    t = t.astimezone(tz) if tz else t
    return f"{t.hour % 12 or 12}:{t.minute:02d}{'am' if t.hour < 12 else 'pm'}"


def _venue(place: str | None) -> str | None:
    """The first part of an address: "Egan Junior High, 100 W Portola Ave" reads the school."""
    return (place or "").split(",")[0].strip() or None


def _event_row(ev: dict[str, Any], tz: tzinfo | None) -> dict[str, Any]:
    start, end = ev["start"], ev.get("end") or ev["start"]
    return {
        "id": ev.get("event_id") or ev.get("id"),
        "title": ev.get("title") or ev.get("summary") or "(no title)",
        "start": start,
        "end": end,
        "start_label": ev.get("start_label") or _label(start, tz),
        "end_label": ev.get("end_label") or _label(end, tz),
        "detail": _venue(ev.get("detail") or ev.get("location")),
    }


#: How many todos show without ``--all``.
CAP = 10


def _todo_row(t: api_client.TodoEntry) -> dict[str, Any]:
    """The server's own fields for one todo, unchanged."""
    return {
        "id": t.id,
        "title": t.title,
        "due": t.due_date,
        "due_is_date_only": t.due_is_date_only,
        "remind_at": t.remind_at,
        "priority": t.priority,
        "source": t.source,
    }


def _shape(
    now: datetime,
    events: list[dict[str, Any]],
    todos: list[api_client.TodoEntry],
    show_all: bool,
    note: str | None,
) -> dict[str, Any]:
    rows = [_event_row(e, now.tzinfo) for e in events]
    rows.sort(key=lambda r: datetime.fromisoformat(r["start"]))
    live = next(
        (
            r
            for r in rows
            if datetime.fromisoformat(r["start"]) <= now < datetime.fromisoformat(r["end"])
        ),
        None,
    )
    upcoming = [r for r in rows if datetime.fromisoformat(r["start"]) > now]
    now_row = (
        {
            **live,
            "minutes_left": _minutes((datetime.fromisoformat(live["end"]) - now).total_seconds()),
        }
        if live
        else None
    )
    nxt = (
        {
            **upcoming[0],
            "in_minutes": _minutes(
                (datetime.fromisoformat(upcoming[0]["start"]) - now).total_seconds()
            ),
        }
        if upcoming
        else None
    )
    items = [_todo_row(t) for t in todos]  # the server's order, untouched
    return {
        "source": "todos",
        "note": note,
        "now": now_row,
        "next_event": nxt,
        "calendar": [r for r in rows if r is not live],
        "todos": {"count": len(items), "items": items if show_all else items[:CAP]},
    }


def _plan_events(plan: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    return [
        e
        for e in (plan.get("your_day") or {}).get("items") or []
        if e.get("start") and datetime.fromisoformat(e.get("end") or e["start"]) > now
    ]


def _todays_events(api: Any, now: datetime) -> list[dict[str, Any]] | None:
    """Today's timed, busy events on your own calendars; ``None`` when the read failed."""
    payload = _tolerate(lambda: api.calendar_range(start=now.date().isoformat(), days=1))
    if payload is None:
        return None
    day = next(
        (d for d in payload.get("days") or [] if d.get("date") == now.date().isoformat()), {}
    )
    mine = {
        c["id"] for c in payload.get("calendars") or [] if c.get("is_primary") or c.get("writable")
    }
    timed = [
        e
        for e in day.get("events") or []
        if not e.get("all_day")
        and not e.get("kind")
        and e.get("busy", True)
        and e.get("start")
        and e.get("calendar_id") in mine
        and datetime.fromisoformat(e.get("end") or e["start"]) > now
    ]
    timed.sort(key=lambda e: e["start"])
    return timed


def assemble(api: Any, *, scope: Scope | None = None, show_all: bool = False) -> dict[str, Any]:
    """Build the ``pepper next`` result; the ``--json`` shape (see the README)."""
    scope = scope or Scope()
    plan = _tolerate(api.calendar_schedule_plan)
    if plan and plan.get("now"):
        now = datetime.fromisoformat(plan["now"])
        events: list[dict[str, Any]] | None = _plan_events(plan, now)
    else:
        now = user_now(api)
        events = _todays_events(api, now)
    todos = [
        t for t in api.list_todos(status="open", group_id=scope.room_id) if _source_ok(t, scope)
    ]
    note = None if events is not None else NO_CALENDAR_NOTE
    return {"generated_at": now.isoformat(), **_shape(now, events or [], todos, show_all, note)}
