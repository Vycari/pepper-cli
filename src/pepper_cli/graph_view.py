"""``pepper graph``: render the entity graph's person cards and meeting briefs.

The CLI only fetches and renders. What a card holds, which people rank, and which meetings come
next are the server's (``/api/v1/graph/*``); this module prints what came back.
"""

from __future__ import annotations

from typing import Any

from pepper_cli.render import local_stamp as _stamp
from pepper_cli.render import one_line as _s
from pepper_cli.render import out_line as _line

#: What a ``status`` other than ``ok`` means, for a person reading the output.
_STATUS_NOTE = {
    "timeout": "the read timed out; this is what it had so far",
    "error": "the read failed; nothing to show",
}


def _tie(tie: object) -> str:
    if not isinstance(tie, dict):
        return "no tie yet"
    band = tie.get("band")
    where = f"top {band}" if band else "past the top 150"
    return f"#{_s(tie.get('rank'))} of {_s(tie.get('total'))} ({where})"


def card(c: dict[str, Any], *, indent: str = "") -> None:
    """One person: name, handles, last contact, tie, and recent meetings."""
    new = "  [dim](new to you)[/dim]" if c.get("is_new") else ""
    _line(f"{indent}[bold]{_s(c.get('name'))}[/bold]{new}  [dim]{_s(c.get('entity_id'))}[/dim]")
    handles = ", ".join(_s(a.get("value")) for a in c.get("aliases") or [])
    if handles:
        _line(f"{indent}  {handles}")
    last = _stamp(c.get("last_contact")) or "never"
    _line(f"{indent}  last contact: {last}  ·  tie: {_tie(c.get('tie'))}")
    meetings = c.get("meetings") or []
    if meetings:
        shown = ", ".join(_stamp(m.get("occurred_at")) for m in meetings)
        _line(f"{indent}  recent meetings: {shown}")


def _note(body: dict[str, Any]) -> None:
    note = _STATUS_NOTE.get(str(body.get("status")))
    if note:
        _line(f"[yellow]{note}[/yellow]")


def people(body: dict[str, Any], *, query: str | None) -> None:
    """A lookup's matches, or the strongest ties."""
    rows = body.get("people") or []
    _note(body)
    if not rows:
        _line(f"[dim](nobody matches {_s(query)!r})[/dim]" if query else "[dim](no people)[/dim]")
        return
    for row in rows:
        card(row)


def brief(body: dict[str, Any]) -> None:
    """The attendees of one calendar event."""
    _note(body)
    if not body.get("found"):
        _line(f"[dim](the graph holds no attendees for event {_s(body.get('event_id'))})[/dim]")
        return
    _line(f"[bold]Event[/bold] {_s(body.get('event_id'))}")
    for row in body.get("attendees") or []:
        card(row, indent="  ")


def upcoming(body: dict[str, Any]) -> None:
    """The next meetings and who is on each."""
    _note(body)
    rows = body.get("meetings") or []
    if not rows:
        _line("[dim](no upcoming meetings in the graph)[/dim]")
        return
    for m in rows:
        _line(
            f"[bold]{_stamp(m.get('starts_at'))}[/bold]  [dim]event {_s(m.get('event_id'))}[/dim]"
        )
        for row in m.get("attendees") or []:
            card(row, indent="  ")
