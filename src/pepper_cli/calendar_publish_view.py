"""``pepper calendar publish``: render the publishing status and turn a label into an id.

The CLI only fetches and renders. Which events are published, and the consent rule for adding a
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
        note = ""
        if row.get("titles_switch"):
            shown = "shown" if row.get("titles") else "hidden"
            note = f"  [dim]shared with you · titles {shown}[/dim]"
        _line(f"  {mark} {_s(row.get('label'))}  [dim]{_s(row.get('id'))}[/dim]{note}")


def status(body: dict[str, Any]) -> None:
    """Whether you are publishing, what is published, and how to get a new address (the address is
    never shown again)."""
    on = bool(body.get("enabled"))
    state = "on" if on else "off"
    _line(f"[bold]Publishing:[/bold] {state}  [dim](detail: {_s(body.get('detail'))})[/dim]")
    if on:
        if body.get("link_created_at"):
            _line(f"  address created {_s(_stamp(body['link_created_at']))}")
        _line(
            "  [dim]The address can't be shown again; "
            "'pepper calendar publish rotate' makes a new one.[/dim]"
        )
    else:
        _line(
            "  [dim]'pepper calendar publish on' starts publishing and prints the private "
            "address.[/dim]"
        )
    _line()
    _rows("Calendars", list(body.get("calendars") or []))
    _line()
    _rows("Follows", list(body.get("follows") or []))
    _line()
    _line("[bold]Also published[/bold]")
    for key in ("meals", "workouts", "todos"):
        _line(f"  {key:<9}{'yes' if body.get(key) else 'no'}")


def link(body: dict[str, Any], *, rotated: bool) -> None:
    """The one-time address. Plain text, never folded or marked up, so it pastes cleanly.

    The URL is a credential: it goes to stdout only, and nowhere else."""
    _line(f"[bold]Private address {'replaced' if rotated else 'ready'}.[/bold]")
    out_text(f"url:    {body.get('url')}", soft_wrap=True)
    out_text(f"webcal: {body.get('webcal_url')}", soft_wrap=True)
