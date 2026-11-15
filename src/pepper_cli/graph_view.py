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


def _ref(e: object) -> str:
    """``name (kind/subtype)`` for a ``{id, kind, subtype, name}`` entry."""
    if not isinstance(e, dict):
        return "?"
    kind = "/".join(_s(e[k]) for k in ("kind", "subtype") if e.get(k))
    return f"{_s(e.get('name'))} ({kind})" if kind else _s(e.get("name"))


def _edge(e: dict[str, Any], *, history: bool) -> str:
    other = e.get("other")
    if isinstance(other, dict):
        arrow = "<-" if e.get("direction") == "in" else "->"
        who = f"{arrow} {_ref(other)}"
    elif e.get("follow_ref"):
        who = f"follow {_s(e.get('follow_ref'))}"
    else:
        who = "follow"
    bits = [who]
    if e.get("source"):
        bits.append(f"source {_s(e.get('source'))}")
    since = _stamp(e.get("valid_from"))
    if since:
        bits.append(f"since {since}")
    if history and e.get("valid_to"):
        bits.append(f"[red]closed {_stamp(e.get('valid_to'))}[/red]")
    elif history:
        bits.append("open")
    return "  " + "  ·  ".join(bits)


def _edges(edges: list[dict[str, Any]], *, history: bool, indent: str = "") -> None:
    """Edges grouped by type, in the order the server sent them."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for e in edges:
        groups.setdefault(_s(e.get("type")), []).append(e)
    for kind, rows in groups.items():
        _line(f"{indent}[bold]{kind}[/bold]")
        for e in rows:
            _line(f"{indent}{_edge(e, history=history)}")


def _catalog_card(c: dict[str, Any], *, indent: str = "") -> None:
    sensitive = "  [yellow](sensitive)[/yellow]" if c.get("sensitive") else ""
    _line(f"{indent}[bold]{_ref(c)}[/bold]  [dim]{_s(c.get('id'))}[/dim]{sensitive}")
    ids = ", ".join(
        f"{_s(i.get('scheme'))}:{_s(i.get('value'))}" for i in c.get("external_ids") or []
    )
    if ids:
        _line(f"{indent}  external ids: {ids}")
    provs = ", ".join(
        f"{_s(p.get('provider'))}:{_s(p.get('ref'))}" for p in c.get("providers") or []
    )
    if provs:
        _line(f"{indent}  providers: {provs}")
    for n in c.get("neighbours") or []:
        arrow = "<-" if n.get("direction") == "in" else "->"
        _line(f"{indent}  {_s(n.get('type'))} {arrow} {_ref(n.get('entry'))}")


def entity_page(body: dict[str, Any], *, history: bool) -> None:
    """One of the caller's entities: header, catalog card, then its edges by type."""
    page = body.get("entity") or {}
    ent = page.get("entity") or {}
    sens = "  [yellow](sensitive)[/yellow]" if page.get("sensitive") else ""
    _line(f"[bold]{_ref(ent)}[/bold]  [dim]{_s(ent.get('id'))}[/dim]{sens}")
    requested = page.get("requested_id")
    if requested and requested != ent.get("id"):
        _line(f"  [dim]merged: {_s(requested)} is now this entity[/dim]")
    cat = page.get("catalog")
    if isinstance(cat, dict):
        _line(f"  catalog: {_ref(cat)}  [dim]{_s(cat.get('id'))}[/dim]")
        _line(f"  [dim]pepper graph catalog {_s(cat.get('id'))}[/dim]")
    edges = page.get("edges") or []
    if not edges:
        _line("[dim](no edges)[/dim]")
        return
    _edges(edges, history=history)


def catalog_page(body: dict[str, Any], *, history: bool) -> None:
    """A catalog entry, then the caller's own entities that point at it."""
    page = body.get("page") or {}
    _catalog_card(page.get("catalog") or {})
    rows = page.get("entities") or []
    if not rows:
        _line("[dim](none of your entities link here)[/dim]")
        return
    _line("[bold]Your entities[/bold]")
    for row in rows:
        ent = row.get("entity") or {}
        _line(f"  [bold]{_ref(ent)}[/bold]  [dim]{_s(ent.get('id'))}[/dim]")
        _edges(row.get("edges") or [], history=history, indent="    ")
