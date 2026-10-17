"""``pepper preferences``: the Calendar > Preferences page, as a command.

The product's own page (title "Preferences" in ``web/apps/defaults/calendar.html``, reached by
the gear beside Manage calendars) holds: Locations (home address, work address, **Timezone**,
"the same timezone as in Settings"), Work & commute (work days, start, end, show commute),
Meals, Workouts, and Environment (weather switches). ``GET/POST /api/v1/calendars/preferences``
carries exactly those, so the account timezone is edited here: no separate ``timezone`` noun.

Which calendars count as busy is *not* on this page or route; it is per booking page (Manage
calendars, ``booking`` app ``set_conflicts``) and is not covered here.
"""

from __future__ import annotations

from typing import Any

#: Mon=0 ... Sun=6, as the route's ``work_days`` counts them.
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MEALS = ("breakfast", "lunch", "dinner")
#: ``--show`` / ``--hide`` names and the route field each one switches.
SWITCHES = {
    "weather": "show_weather",
    "commute": "show_commute",
    "precip-amount": "show_precip_amount",
    "humidity": "show_humidity",
    "wind": "show_wind",
    "uv": "show_uv",
    "severe": "show_severe",
}


class PrefsError(ValueError):
    """A flag the CLI cannot turn into a request body (an unknown day or switch name, or half a
    meal with nothing stored to complete it). Values the server validates (times, zones,
    addresses) are sent as given and its 422 is shown."""


def parse_days(value: str) -> list[int]:
    """``mon,wed-fri`` (or ``weekdays`` / ``weekend`` / ``all``) as sorted 0-6 numbers."""
    low = value.strip().lower()
    named = {
        "weekdays": [0, 1, 2, 3, 4],
        "weekend": [5, 6],
        "all": list(range(7)),
        "none": [],
    }
    if low in named:
        return named[low]
    days: set[int] = set()
    for part in low.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if a[:3] not in DAYS or b[:3] not in DAYS:
                raise PrefsError(f"bad day range {part!r}: use names like mon-fri")
            i, j = DAYS.index(a[:3]), DAYS.index(b[:3])
            days.update(range(i, j + 1) if i <= j else [*range(i, 7), *range(j + 1)])
        elif part[:3] in DAYS:
            days.add(DAYS.index(part[:3]))
        else:
            raise PrefsError(f"unknown day {part!r}: use mon, tue, wed, thu, fri, sat, sun")
    return sorted(days)


def parse_meal(value: str) -> tuple[str, str | None, str | int | None]:
    """``lunch=12:30`` / ``lunch=12:30,45`` / ``lunch=,45`` as ``(name, start, minutes)``, the
    text as typed; the server checks it."""
    name, sep, rest = value.partition("=")
    name = name.strip().lower()
    if not sep or name not in MEALS:
        raise PrefsError(f"--meal is NAME=HH:MM[,MINUTES] with NAME one of {', '.join(MEALS)}")
    start_text, _, minutes_text = rest.partition(",")
    start = start_text.strip() or None
    minutes: str | int | None = minutes_text.strip() or None
    if isinstance(minutes, str) and minutes.isdigit():
        minutes = int(minutes)
    if start is None and minutes is None:
        raise PrefsError("--meal needs a start time, minutes, or both")
    return name, start, minutes


def day_names(days: list[int]) -> str:
    if days == [0, 1, 2, 3, 4]:
        return "Mon-Fri"
    return ", ".join(DAYS[d].capitalize() for d in days if 0 <= d <= 6) or "none"


def public(prefs: dict[str, Any]) -> dict[str, Any]:
    """The values without the page helpers (``timezones``, the ~600-name zone menu, and
    ``maps``)."""
    return {k: v for k, v in prefs.items() if k not in ("timezones", "maps")}


def build_changes(
    current: dict[str, Any],
    *,
    timezone: str | None = None,
    home_address: str | None = None,
    work_address: str | None = None,
    work_days: list[int] | None = None,
    work_start: str | None = None,
    work_end: str | None = None,
    show: list[str] | None = None,
    hide: list[str] | None = None,
    meals: list[tuple[str, str | None, str | int | None]] | None = None,
    workout_at: str | None = None,
    workout_minutes: int | None = None,
    run_minutes: int | None = None,
    break_minutes: int | None = None,
) -> dict[str, Any]:
    """The POST body: only what was asked. A meal or the workout needs its start *and*
    minutes on the wire, so the missing half comes from ``current``."""
    body: dict[str, Any] = {}
    if timezone is not None:
        body["timezone"] = timezone
    if home_address is not None:
        body["location"] = home_address
    if work_address is not None:
        body["work_location"] = work_address
    if work_days is not None:
        body["work_days"] = work_days
    if work_start is not None:
        body["work_start"] = work_start
    if work_end is not None:
        body["work_end"] = work_end
    for names, value in ((show, True), (hide, False)):
        for name in names or []:
            body[SWITCHES[name]] = value
    for name, start, minutes in meals or []:
        have = (current.get("meals") or {}).get(name) or {}
        # Only a half that was not typed is completed; a typed 0 goes to the server as is.
        start = start if start is not None else have.get("start")
        minutes = minutes if minutes is not None else have.get("minutes")
        if start is None or minutes is None:
            raise PrefsError(f"--meal {name} has no stored time yet: give HH:MM,MINUTES")
        body.setdefault("meals", {})[name] = {"start": start, "minutes": minutes}
    if workout_at is not None or workout_minutes is not None:
        have = current.get("workout") or {}
        at = workout_at if workout_at is not None else have.get("at")
        minutes = workout_minutes if workout_minutes is not None else have.get("minutes")
        if at is None or minutes is None:
            raise PrefsError("the workout has no stored time yet: give --workout-at and minutes")
        body["workout"] = {"at": at, "minutes": minutes}
    if run_minutes is not None:
        body["run_minutes"] = run_minutes
    if break_minutes is not None:
        body["break_minutes"] = break_minutes
    return body
