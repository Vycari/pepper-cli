"""Terminal output for the account commands (``artifacts``, ``preferences``).

Plain text on stdout, server text escaped, like the rest of ``render``."""

from __future__ import annotations

from typing import Any

from pepper_cli import artifacts_view
from pepper_cli.render import local_stamp as _stamp
from pepper_cli.render import one_line as _s
from pepper_cli.render import out_line as _line
from pepper_cli.render import out_text

# ---------------------------------------------------------------------------
# artifacts


def artifacts_list(rows: list[dict[str, Any]], *, truncated: bool) -> None:
    """``rows`` are numbered (``n``). One line each: number, kind, name, labels, updated."""
    if not rows:
        _line("(no artifacts)")
        return
    width = len(str(len(rows)))
    for row in rows:
        labels = "[" + ", ".join(row.get("labels") or []) + "]"
        tag = f"  [dim]{_s(labels)}[/dim]" if row.get("labels") else ""
        kind = artifacts_view.kind_label(str(row.get("kind") or ""))
        _line(
            f"[bold]{row['n']:>{width}}[/bold]  {_s(kind):<5} {_s(row.get('name'))}{tag}  "
            f"[dim]{_s(_stamp(row.get('updated_at')))}  {_s(row.get('id'))}[/dim]"
        )
    if truncated:
        _line(
            "[dim](more artifacts than shown: narrow with --kind, --label, --search "
            "or --room)[/dim]"
        )


def artifact_detail(detail: dict[str, Any]) -> None:
    kind = str(detail.get("kind") or "")
    _line(f"[bold]{_s(detail.get('name'))}[/bold]")
    meta = [artifacts_view.kind_label(kind)]
    if detail.get("mime_type"):
        meta.append(str(detail["mime_type"]))
    if detail.get("size_label"):
        meta.append(str(detail["size_label"]))
    if detail.get("shared"):
        meta.append("shared with a room")
    _line(f"  {_s(', '.join(meta))}")
    if detail.get("labels"):
        _line(f"  labels: {_s(', '.join(detail['labels']))}")
    if detail.get("updated_at"):
        _line(f"  updated: {_s(_stamp(detail['updated_at']))}")
    if detail.get("summary"):
        _line(f"  summary: {_s(detail['summary'])}")
    if detail.get("link"):
        _line(f"  link: {_s(detail['link'])}")
    _line(f"[dim]id {_s(detail.get('id'))}[/dim]")
    body = detail.get("body")
    if body is None:
        body = detail.get("text_body")
        if body is not None and detail.get("text_truncated"):
            body += "\n[...preview truncated: 'pepper artifacts download' for the whole file]"
    if body:
        _line()
        out_text(str(body))
    elif kind == "blob":
        _line()
        _line("[dim](a file: 'pepper artifacts download' saves it)[/dim]")


# ---------------------------------------------------------------------------
# preferences


def preferences(prefs: dict[str, Any]) -> None:
    """The page's sections, in its order."""
    from pepper_cli import preferences_view as pv

    def yes(value: object) -> str:
        return "yes" if value else "no"

    def row(label: str, value: object) -> None:
        _line(f"  {label:<22}{_s(value) if value not in (None, '') else '[dim]not set[/dim]'}")

    _line("[bold]Locations[/bold]")
    row("Home address", prefs.get("location"))
    row("Work address", prefs.get("work_location"))
    row("Timezone", prefs.get("timezone"))
    _line()
    _line("[bold]Work & commute[/bold]")
    row("Work days", pv.day_names(list(prefs.get("work_days") or [])))
    row("Starts", prefs.get("work_start"))
    row("Ends", prefs.get("work_end"))
    row("Show commute", yes(prefs.get("show_commute")))
    meals = prefs.get("meals") or {}
    if meals:
        _line()
        _line("[bold]Meals[/bold]")
        for name in pv.MEALS:
            if isinstance(meals.get(name), dict):
                row(
                    name.capitalize(),
                    f"{meals[name].get('start')}, {meals[name].get('minutes')} min",
                )
    workout = prefs.get("workout")
    if isinstance(workout, dict) and workout:
        _line()
        _line("[bold]Workouts[/bold]")
        row("Usually at", workout.get("at"))
        row("Session", f"{workout.get('minutes')} min")
    _line()
    _line("[bold]Environment[/bold]")
    labels = {
        "weather": "Show weather",
        "precip-amount": "Precipitation amount",
        "humidity": "Humidity",
        "wind": "Wind",
        "uv": "UV index",
        "severe": "Severe warnings",
    }
    for name, label in labels.items():
        row(label, yes(prefs.get(pv.SWITCHES[name])))
    if prefs.get("run_minutes") or prefs.get("break_minutes"):
        _line()
        _line("[bold]Schedule[/bold]")
        row("Run / break", f"{prefs.get('run_minutes')} / {prefs.get('break_minutes')} min")
