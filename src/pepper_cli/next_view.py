"""``pepper next``: what should I be doing now, and what is on my plate, in priority order.

Three parts: Now (the live event and the next commitment), today's calendar (commitments, never
work blocks), and one ranked list of todos with no times. Todos are never assigned to time slots.

Todos come from the todo list (``GET /api/v1/todos?status=open``, or a room's own list for
``--room``): the schedule plan's ``plans`` no longer carry them. Today's events come from the
schedule plan's ``your_day`` (``GET /api/v1/calendars/schedule-plan``); when the plan is missing
or fails, the calendar range read stands in, and when that fails too the result says so.
:func:`rank` is the one pure function holding the weights. Everything here is a read.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta, tzinfo
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


def _source_ok(item: dict[str, Any], scope: Scope) -> bool:
    if not scope.source:
        return True
    source = item.get("source")
    return isinstance(source, str) and source.lower().startswith(scope.source.lower())


def _minutes(delta_seconds: float) -> int:
    return max(0, int(delta_seconds // 60))


def _label(iso: str | None, tz: tzinfo | None) -> str | None:
    """A clock time like "2:30pm" for an ISO instant, in ``tz``; one style for events and todos."""
    if not iso:
        return None
    t = datetime.fromisoformat(iso)
    t = t.astimezone(tz) if tz else t
    return t.strftime("%-I:%M%p").lower()


def _venue(place: str | None) -> str | None:
    """The first part of an address: "Egan Junior High, 100 W Portola Ave" reads the school."""
    return (place or "").split(",")[0].strip() or None


def _event_row(ev: dict[str, Any], now: datetime, tz: tzinfo | None) -> dict[str, Any]:
    start, end = ev["start"], ev.get("end") or ev["start"]
    return {
        "kind": "event",
        "id": ev.get("event_id") or ev.get("id"),
        "title": ev.get("title") or ev.get("summary") or "(no title)",
        "start": start,
        "end": end,
        "start_label": ev.get("start_label") or _label(start, tz),
        "end_label": ev.get("end_label") or _label(end, tz),
        "detail": _venue(ev.get("detail") or ev.get("location")),
        "live": datetime.fromisoformat(start) <= now < datetime.fromisoformat(end),
    }


#: Ranking weights, highest score first. Tiers are spaced so a bigger tier always wins; tweak here.
W_OVERDUE = 1000  # plus W_PER_DAY_OVERDUE per day late, capped at OVERDUE_CAP_DAYS
W_PER_DAY_OVERDUE = 10
OVERDUE_CAP_DAYS = 30
W_DUE_TODAY = 800
#: Every due-date-driven boost below decays as ``boost * 0.5 ** (days_until_due / HALF_LIFE_DAYS)``,
#: so things due far out matter little. Overdue and due today count as 0 days: full boost.
HALF_LIFE_DAYS = 4.0
W_HIGH_PRIORITY = 400  # the todo's own priority is "high", decayed by its due date
W_HIGH_NO_DUE = 150  # high priority with no due date: moderate, never decays
W_DUE_SOON = 150  # for merely having a due date, decayed
CAP = 10
#: A timed deadline stays visible (marked passed, unboosted) this long after its time, then drops.
DEADLINE_GRACE_HOURS = 24


def _due_day(todo: dict[str, Any], tz: tzinfo | None) -> date | None:
    due = todo.get("due") or {}
    try:
        if due.get("date"):
            return date.fromisoformat(due["date"][:10])
        if due.get("at"):
            at = datetime.fromisoformat(due["at"])
            return (at.astimezone(tz) if tz else at).date()
    except ValueError:
        pass
    return None


def _decay(days: int) -> float:
    return 0.5 ** (max(days, 0) / HALF_LIFE_DAYS)


def _due_at(todo: dict[str, Any], tz: tzinfo | None) -> datetime | None:
    """A todo's deadline time in ``tz``: its timed due, else its reminder (``remind_at``).

    A date-only or missing due with no reminder has none. A timed due wins over a reminder.
    """
    raw = (todo.get("due") or {}).get("at") or todo.get("remind_at")
    try:
        at = datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None
    if at is None:
        return None
    return at.astimezone(tz) if tz else at


def _time_source(todo: dict[str, Any]) -> str | None:
    """Where :func:`_due_at` reads from: "due", "remind", or None."""
    if (todo.get("due") or {}).get("at"):
        return "due"
    return "remind" if todo.get("remind_at") else None


def rank(
    todos: list[dict[str, Any]],
    today: date,
    tz: tzinfo | None = None,
) -> list[dict[str, Any]]:
    """Order todos overdue, due today, then priority and due date; input order breaks ties.

    Reads ``due`` ({date|at}), ``remind_at``, ``high_priority`` and ``passed``. Returns copies
    with ``score`` and ``reasons`` added, best first; a stable sort keeps the input order on
    ties."""
    out: list[dict[str, Any]] = []
    for t in todos:
        score, why = 0.0, []
        day = None if t.get("passed") else _due_day(t, tz)
        at = _due_at(t, tz)
        remind_today = (
            day is None and not t.get("passed") and at is not None and at.date() == today
        )  # a reminder later today counts as due today at that time
        if remind_today:
            day = today
        ahead = (day - today).days if day else None
        if t.get("passed") and at is not None:
            gone = "" if at.date() == today else f"{at:%a} "
            verb = "reminded" if _time_source(t) == "remind" else "was due"
            why.append(f"{verb} {gone}{_label(at.isoformat(), tz)}")
        if day and ahead is not None and ahead < 0:
            score += W_OVERDUE + W_PER_DAY_OVERDUE * min(-ahead, OVERDUE_CAP_DAYS)
            why.append(f"overdue {-ahead}d")
        elif day == today:
            score += W_DUE_TODAY
            stamp = f" {_label(at.isoformat(), tz)}" if at else ""
            why.append(f"reminder{stamp}" if remind_today else f"due today{stamp}")
        if t.get("high_priority"):
            score += W_HIGH_PRIORITY * _decay(ahead) if ahead is not None else W_HIGH_NO_DUE
            why.append("high priority")
        if day and ahead is not None and ahead > 0:
            score += W_DUE_SOON * _decay(ahead)
            why.append("due tomorrow" if ahead == 1 else f"due {day.strftime('%b')} {day.day}")
        out.append({**t, "score": round(score, 1), "reasons": why})
    out.sort(key=lambda t: -float(t["score"]))
    return out


def _todo_row(t: dict[str, Any]) -> dict[str, Any]:
    due = t.get("due") or {}
    return {
        "id": t.get("todo_id"),
        "title": t.get("title"),
        "due": due.get("date") or due.get("at"),
        "remind_at": t.get("remind_at"),
        "source": t.get("source"),
        "high_priority": bool(t.get("high_priority")),
        "score": t["score"],
        "reasons": t["reasons"],
        "passed": bool(t.get("passed")),
        "source_time": _time_source(t),
    }


def _expired(todo: dict[str, Any], now: datetime) -> bool:
    """A timed deadline past its grace period: gone from everything."""
    at = _due_at(todo, now.tzinfo)
    return at is not None and at <= now - timedelta(hours=DEADLINE_GRACE_HOURS)


def _mark_passed(todos: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Copies of ``todos`` with ``passed`` set on those whose due time is behind us."""
    out = []
    for t in todos:
        at = _due_at(t, now.tzinfo)
        out.append({**t, "passed": True} if at is not None and at <= now else t)
    return out


def _deadline_rows(todos: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Todos due at a time later today, as calendar rows (``todos`` here are unexpired)."""
    rows = []
    for t in todos:
        at = _due_at(t, now.tzinfo)
        if at is None or at.date() != now.date():
            continue
        rows.append(
            {
                "kind": "deadline",
                "source_time": _time_source(t),
                "id": t.get("todo_id"),
                "title": t.get("title"),
                "start": at.isoformat(),
                "end": None,
                "start_label": _label(at.isoformat(), now.tzinfo),
                "end_label": None,
                "detail": None,
                "passed": bool(t.get("passed")),
                "live": False,
            }
        )
    return rows


def _due_today_rows(todos: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Todos due today with no time: the "due today:" line."""
    return [
        {"id": t.get("todo_id"), "title": t.get("title")}
        for t in todos
        if _due_at(t, now.tzinfo) is None and _due_day(t, now.tzinfo) == now.date()
    ]


def _shape(
    source: str,
    now: datetime,
    events: list[dict[str, Any]],
    todos: list[dict[str, Any]],
    scope: Scope,
    show_all: bool,
    note: str | None,
) -> dict[str, Any]:
    rows = [_event_row(e, now, now.tzinfo) for e in events]
    rows.sort(key=lambda r: datetime.fromisoformat(r["start"]))
    live = next((r for r in rows if r["live"]), None)
    # A timed deadline expires a grace period after its time; until then it is marked passed.
    todos = _mark_passed([t for t in todos if not _expired(t, now)], now)
    scoped = [t for t in todos if _source_ok(t, scope)]
    deadlines = _deadline_rows(scoped, now)
    due_today = _due_today_rows(scoped, now)
    # The next commitment is the earliest upcoming timed event or deadline.
    upcoming = sorted(
        [r for r in rows if r is not live and datetime.fromisoformat(r["start"]) > now]
        + [r for r in deadlines if datetime.fromisoformat(r["start"]) > now],
        key=lambda r: datetime.fromisoformat(r["start"]),
    )
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
    ranked = rank([t for t in todos if _source_ok(t, scope)], now.date(), now.tzinfo)
    items = [_todo_row(t) for t in ranked]
    return {
        "source": source,
        "note": note,
        "now": now_row,
        "next_commitment": nxt,
        "due_today": due_today,
        "calendar": sorted(
            [r for r in rows if r is not live] + deadlines,
            key=lambda r: datetime.fromisoformat(r["start"]),
        ),
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


def _todo_dict(t: api_client.TodoEntry) -> dict[str, Any]:
    """A todo-list row in the shape :func:`rank` reads."""
    row = dataclasses.asdict(t)
    raw = t.due_date
    day = (raw or "")[:10] or None
    row.update(
        todo_id=t.id,
        high_priority=t.priority == "high",
        due={
            "date": day if t.due_is_date_only else None,
            "at": None if t.due_is_date_only or not raw else raw,
        },
    )
    return row


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
    todos = [_todo_dict(t) for t in api.list_todos(status="open", group_id=scope.room_id)]
    note = None if events is not None else NO_CALENDAR_NOTE
    return {
        "generated_at": now.isoformat(),
        **_shape("todos", now, events or [], todos, scope, show_all, note),
    }
