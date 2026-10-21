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


#: A calendar read that failed: an error status, no connection, or a 200 whose body is not JSON
#: (a captive portal's HTML page, say), which ``resp.json()`` raises as ``ValueError``.
_READ_FAILED = (api_client.ApiError, httpx.RequestError, ValueError)


def _tolerate(call: Any) -> Any:
    try:
        return call()
    except _READ_FAILED:
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
    """A subset of the server's fields for one todo, values unchanged; ``due`` is ``due_date``."""
    return {
        "id": t.id,
        "title": t.title,
        "due": t.due_date,
        "due_is_date_only": t.due_is_date_only,
        "remind_at": t.remind_at,
        "priority": t.priority,
        "source": t.source,
        "next_action": dataclasses.asdict(t.next_action) if t.next_action else None,
        "next_action_suggestion": (
            dataclasses.asdict(t.next_action_suggestion) if t.next_action_suggestion else None
        ),
    }


def _event_view(now: datetime, events: list[dict[str, Any]]) -> dict[str, Any]:
    """Now, the next event and today's calendar from the event rows; raises on a bad timestamp."""
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
    return {"now": now_row, "next_event": nxt, "calendar": [r for r in rows if r is not live]}


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


def _zone_name(name: Any) -> str | None:
    """``name`` when it is an IANA zone this machine knows, else ``None``."""
    if not isinstance(name, str) or not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def _whoami_zone(api: Any) -> str | None:
    """The account's zone from whoami; UTC when unset (as the server); ``None`` when unreadable."""
    try:
        name = api.whoami().timezone
    except _READ_FAILED:
        return None
    return _zone_name(name) or "UTC"


#: What a calendar payload that is not the shape the CLI reads raises while being parsed: a
#: timestamp that is not ISO, a naive one compared with an aware one, a missing key.
_PARSE_FAILED = (ValueError, TypeError, KeyError, AttributeError)


def assemble(api: Any, *, scope: Scope | None = None, show_all: bool = False) -> dict[str, Any]:
    """Build the ``pepper next`` result; the ``--json`` shape (see the README).

    ``timezone`` is the account's IANA zone, from the plan or else whoami; every time is shown
    through it. When neither can be read it is ``None``: the calendar is skipped (the note says
    so) and the renderer shows times in UTC, labelled, rather than guess from the machine's zone.
    """
    scope = scope or Scope()
    plan = _tolerate(api.calendar_schedule_plan)
    if not isinstance(plan, dict):
        plan = None
    tz_name = _zone_name(plan.get("timezone")) if plan else None
    tz_name = tz_name or _whoami_zone(api)
    zone: tzinfo = ZoneInfo(tz_name) if tz_name else UTC
    now = _clock(zone)
    events: list[dict[str, Any]] | None = None
    try:
        if plan and plan.get("now"):
            now = datetime.fromisoformat(plan["now"]).astimezone(zone)
            events = _plan_events(plan, now)
        elif tz_name:
            events = _todays_events(api, now)
        shaped_events = _event_view(now, events or [])
    except _PARSE_FAILED:
        # A calendar that can't be parsed is a calendar that couldn't be read.
        events, shaped_events = None, _event_view(now, [])
    todos = [
        t for t in api.list_todos(status="open", group_id=scope.room_id) if _source_ok(t, scope)
    ]
    items = [_todo_row(t) for t in todos]  # the server's order, untouched
    return {
        "generated_at": now.isoformat(),
        "timezone": tz_name,
        "source": "todos",
        "note": None if events is not None else NO_CALENDAR_NOTE,
        **shaped_events,
        "todos": {"count": len(items), "items": items if show_all else items[:CAP]},
    }
