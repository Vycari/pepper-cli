"""``pepper next``: assemble existing Pepper reads into Now / Do next / Due soon / Later.

No ranking happens here. Order comes from the server's Schedule plan
(``GET /api/v1/calendars/schedule-plan``); when that door is missing (404) the todos list stands in,
in the server's own order, and the result says so. Nothing here calls ``schedule-now``, which
writes.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from pepper_cli import api_client

#: Shown for the fallback so nobody mistakes todos-list order for Pepper's ranking.
UNRANKED_NOTE = "unranked: Pepper's Schedule ranking isn't available on this server yet"
_WINDOW = timedelta(hours=48)
_LATER_SHOWN = 5
_FALLBACK_DO_NEXT = 5


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
    """What ``--room`` and ``--source`` narrow ``next`` to; empty (falsy) keeps everything.

    ``room_id`` is a room's id, already resolved strictly by the caller (an item belongs
    to it when its ``principal`` is that id). ``source`` is a case-insensitive prefix of
    the item's source label, room name or source.
    """

    room_id: str | None = None
    source: str | None = None

    def __bool__(self) -> bool:
        return bool(self.room_id or self.source)


def _same_id(a: object, b: str) -> bool:
    return isinstance(a, str) and a.lower() == b.lower()


def _room_ok(item: dict[str, Any], scope: Scope) -> bool:
    if scope.room_id and not _same_id(item.get("principal"), scope.room_id):
        return False
    if scope.source:
        names = [item.get("source_label"), item.get("room"), item.get("source")]
        prefix = scope.source.lower()
        return any(isinstance(n, str) and n.lower().startswith(prefix) for n in names)
    return True


def _in_window(day: str | None, at: str | None, now: datetime) -> bool:
    """A due moment inside [today, now + 48h]; a bare day counts through its end."""
    # 48 elapsed hours: add in UTC, since aware-datetime arithmetic is wall-clock across DST.
    end = (now.astimezone(UTC) + _WINDOW).astimezone(now.tzinfo)
    try:
        if at:
            return now <= datetime.fromisoformat(at) <= end
        if day:
            return now.date().isoformat() <= day <= end.date().isoformat()
    except ValueError:
        pass
    return False


def _due_soon(
    todos: list[dict[str, Any]], cards: list[dict[str, Any]], now: datetime, scope: Scope
) -> list[dict[str, Any]]:
    """Todos (already minus Do next) due in the window plus email-deadline cards, by title."""
    items = [
        t
        for t in todos
        if _in_window((t.get("due") or {}).get("date"), (t.get("due") or {}).get("at"), now)
    ]
    items += [
        {**c, "kind": "email_deadline", "reason": c.get("rationale")}
        for c in cards
        if c.get("kind") == "email_deadline" and _in_window(None, c.get("when"), now)
    ]
    seen: set[str] = set()
    out = []
    for it in items:
        key = (it.get("title") or "").strip().lower()
        if key and key not in seen and _room_ok(it, scope):
            seen.add(key)
            out.append(it)
    return out


def _cards(api: Any, now: datetime) -> list[dict[str, Any]]:
    body = _tolerate(lambda: api.calendar_planning(start=now.date().isoformat(), days=2))
    return list((body or {}).get("cards") or [])


def _later(rest: list[dict[str, Any]], count: int | None, scope: Scope, show_all: bool) -> dict:
    """``count`` is the server's total when it is known and unfiltered, else the items' own."""
    items = [t for t in rest if _room_ok(t, scope)]
    return {
        "count": count if count is not None and not scope else len(items),
        "items": items if show_all else items[:_LATER_SHOWN],
    }


def _plan_for(plan: dict[str, Any], key: str | None) -> dict[str, Any]:
    """The plan listing ``key``, following ``same_as`` to the plan that carries the blocks."""
    plans = plan.get("plans") or []
    found = next((p for p in plans if key in p["philosophies"]), {})
    if found.get("same_as"):
        found = next((p for p in plans if found["same_as"] in p["philosophies"]), found)
    return found


def _ahead(chosen: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    return [
        b
        for b in chosen.get("blocks") or []
        if b.get("type") == "todo" and not b.get("done") and datetime.fromisoformat(b["end"]) > now
    ]


def _plan_info(plan: dict[str, Any], first: str, now: datetime) -> dict[str, Any]:
    """Which plan Do next shows, and how the other two differ; names and subtitles are Pepper's."""
    meta = {p["key"]: p for p in plan.get("philosophies") or []}

    def facts(key: str) -> tuple[str | None, int]:
        ahead = _ahead(_plan_for(plan, key), now)
        return (ahead[0].get("title") if ahead else None), len(ahead)

    others = []
    for key, entry in meta.items():
        if key == first:
            continue
        listed = next((p for p in plan.get("plans") or [] if key in p["philosophies"]), {})
        title, count = facts(key)
        others.append(
            {
                "first": key,
                "name": entry.get("name"),
                "same_as": listed.get("same_as"),
                "summary": listed.get("summary"),
                "first_block": title,
                "blocks": count,
                "differs": (title, count) != facts(first),
            }
        )
    return {
        "first": first,
        "name": (meta.get(first) or {}).get("name"),
        "subtitle": (meta.get(first) or {}).get("subtitle"),
        "chosen_by_pepper": first == plan.get("active"),
        "agree_note": _plan_for(plan, first).get("agree_note"),
        "others": others,
    }


def _from_plan(
    api: Any, plan: dict[str, Any], scope: Scope, show_all: bool, first: str | None = None
) -> dict:
    now = datetime.fromisoformat(plan["now"])
    key = first if first and _plan_for(plan, first) else plan.get("active")
    chosen = _plan_for(plan, key)
    ahead = _ahead(chosen, now)
    left_out = chosen.get("left_out") or []
    more = (plan.get("more") or {}).get("items") or []
    note = None if ahead else chosen.get("summary")
    blocks = [t for t in (ahead or left_out) if _room_ok(t, scope)]
    claimed = {b.get("todo_id") for b in blocks}
    pool = [t for t in left_out + more if t.get("todo_id") not in claimed]
    due = _due_soon(pool, _cards(api, now), now, scope)
    due_ids = {t.get("todo_id") for t in due}
    # An unplaced left_out todo that isn't due soon goes to Later, ahead of ``more``.
    rest = [t for t in pool if t.get("todo_id") not in due_ids]
    next_up = plan.get("next_up")
    return {
        "source": "schedule-plan",
        "plan": _plan_info(plan, key, now) if key else None,
        "now": [next_up] if next_up else [],
        "do_next": {"note": note, "items": blocks},
        "due_soon": due,
        "later": _later(
            rest,
            (plan.get("more") or {}).get("count", len(more)) - len(more) + len(rest),
            scope,
            show_all,
        ),
    }


def _next_event(api: Any, now: datetime) -> list[dict[str, Any]]:
    payload = _tolerate(lambda: api.calendar_range(start=now.date().isoformat(), days=1)) or {}
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
    return timed[:1]


def _fallback(api: Any, now: datetime, scope: Scope, show_all: bool) -> dict:
    todos = [dataclasses.asdict(t) for t in api.list_todos(status="open")]
    for t in todos:
        t["todo_id"] = t["id"]
        t["principal"] = None
        t["due"] = {"date": (t["due_date"] or "")[:10] or None, "at": None, "label": None}
        if t["due_date"] and not t["due_is_date_only"]:
            t["due"]["at"] = t["due_date"]
    todos = [t for t in todos if _room_ok(t, scope)]
    head = todos[:_FALLBACK_DO_NEXT]
    tail = todos[len(head) :]
    due = _due_soon(tail, _cards(api, now), now, scope)
    ids = {t["id"] for t in due}
    return {
        "source": "fallback",
        "plan": None,
        "now": _next_event(api, now),
        "do_next": {"note": UNRANKED_NOTE, "items": head},
        "due_soon": due,
        "later": _later([t for t in tail if t["id"] not in ids], None, scope, show_all),
    }


def assemble(
    api: Any, *, scope: Scope | None = None, show_all: bool = False, first: str | None = None
) -> dict[str, Any]:
    """Build the ``pepper next`` result; the documented ``--json`` shape (see the README)."""
    scope = scope or Scope()
    try:
        plan = api.calendar_schedule_plan()
    except api_client.ApiError as exc:
        if exc.status != 404:
            raise
        now = user_now(api)
        result = _fallback(api, now, scope, show_all)
    else:
        now = datetime.fromisoformat(plan["now"])
        result = _from_plan(api, plan, scope, show_all, first)
    return {"generated_at": now.isoformat(), **result}
