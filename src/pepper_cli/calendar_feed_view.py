"""``pepper calendar feed``: render the feed's status and turn a label into an id.

The CLI only fetches and renders. Which events a feed carries, and the consent rule for adding a
Google calendar, are the server's; this module looks a label up in the status list the server
sent (so the replacement list can be sent back) and prints what came back.
"""

from __future__ import annotations

from typing import Any

from pepper_cli.render import local_stamp as _stamp
from pepper_cli.render import one_line as _s
from pepper_cli.render import out_line as _line
from pepper_cli.render import out_text


class SourceError(Exception):
    """A name that matches no source, or more than one. The message says what to type."""


def resolve(name: str, rows: list[dict[str, Any]], kind: str) -> str:
    """The id of the ``rows`` entry ``name`` means: an exact id, an exact label (any case), or
    a unique label prefix. Raises :class:`SourceError` when none or several match."""
    wanted = name.strip().lower()
    if not wanted:
        raise SourceError(f"--{kind}: give a name or id")
    for match in (
        [r for r in rows if str(r.get("id")) == name.strip()],
        [r for r in rows if str(r.get("label") or "").lower() == wanted],
        [r for r in rows if str(r.get("label") or "").lower().startswith(wanted)],
    ):
        if len(match) == 1:
            return str(match[0]["id"])
        if len(match) > 1:
            labels = ", ".join(str(r.get("label")) for r in match)
            raise SourceError(f"{name!r} matches several {kind}s ({labels}); use the id")
    known = ", ".join(str(r.get("label")) for r in rows) or "none"
    raise SourceError(f"no {kind} named {name!r}; yours are: {known}")


def _rows(title: str, rows: list[dict[str, Any]]) -> None:
    _line(f"[bold]{title}[/bold]")
    if not rows:
        _line("  [dim](none)[/dim]")
    for row in rows:
        mark = "\\[x]" if row.get("included") else "\\[ ]"
        _line(f"  {mark} {_s(row.get('label'))}  [dim]{_s(row.get('id'))}[/dim]")


def status(body: dict[str, Any]) -> None:
    """The feed's state, what it carries, and where to go for the link (it is never shown again)."""
    on = bool(body.get("enabled"))
    state = "on" if on else "off"
    _line(f"[bold]Calendar feed:[/bold] {state}  [dim](detail: {_s(body.get('detail'))})[/dim]")
    if on:
        if body.get("link_created_at"):
            _line(f"  link created {_s(_stamp(body['link_created_at']))}")
        _line(
            "  [dim]The link can't be shown again; "
            "'pepper calendar feed rotate' makes a new one.[/dim]"
        )
    else:
        _line("  [dim]'pepper calendar feed on' turns it on and prints the link.[/dim]")
    _line()
    _rows("Calendars", list(body.get("calendars") or []))
    _line()
    _rows("Follows", list(body.get("follows") or []))
    _line()
    _line("[bold]Also in the feed[/bold]")
    for key in ("meals", "workouts", "todos"):
        _line(f"  {key:<9}{'yes' if body.get(key) else 'no'}")


def link(body: dict[str, Any], *, rotated: bool) -> None:
    """The one-time link. Plain text, never folded or marked up, so it pastes cleanly.

    The URL is a credential: it goes to stdout only, and nowhere else."""
    _line(f"[bold]Calendar feed link {'rotated' if rotated else 'on'}.[/bold]")
    out_text(f"url:    {body.get('url')}")
    out_text(f"webcal: {body.get('webcal_url')}")
