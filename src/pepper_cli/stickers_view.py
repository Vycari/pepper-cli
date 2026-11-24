"""``pepper stickers``: render the sticker book the server sends.

The CLI only fetches and renders. Which stickers exist, what earns one, which tier is held and what
a hidden one shows are the server's (``GET /api/v1/achievements``); this module prints them.
"""

from __future__ import annotations

from typing import Any

from pepper_cli.render import one_line as _s
from pepper_cli.render import out_line as _line


def _weeks(n: object) -> str:
    count = n if isinstance(n, int) else 0
    return "1 week" if count == 1 else f"{count} weeks"


def _earned(row: dict[str, Any]) -> None:
    tier = f" ({_s(row.get('tier_name'))})" if row.get("tier_name") else ""
    last = f", last {_s(row.get('last_week'))}" if row.get("last_week") else ""
    _line(f"[bold]{_s(row.get('title'))}[/bold]{tier}  [dim]{_weeks(row.get('times'))}{last}[/dim]")
    if row.get("citation"):
        _line(f"  {_s(row.get('citation'))}")
    tiers = row.get("tiers") or []
    ahead = [t for t in tiers if isinstance(t, dict) and not t.get("earned_week")]
    if ahead:
        nxt = ahead[0]
        _line(f"  [dim]next: {_s(nxt.get('name'))} at {_s(nxt.get('threshold'))}[/dim]")


def _unearned(row: dict[str, Any]) -> None:
    _line(f"[dim]{_s(row.get('title'))}  ·  {_s(row.get('how'))}[/dim]")


def book(body: dict[str, Any], *, show_all: bool) -> None:
    """Earned stickers first, in the server's order, then (with ``--all``) the rest."""
    rows = [r for r in body.get("stickers") or [] if isinstance(r, dict)]
    _line(f"{_s(body.get('earned'))} of {_s(body.get('total'))} stickers earned")
    earned = [r for r in rows if r.get("earned")]
    if not earned:
        _line("[dim](none yet: your Sunday Wrapped awards them)[/dim]")
    for row in earned:
        _earned(row)
    if show_all:
        rest = [r for r in rows if not r.get("earned")]
        if rest:
            _line("")
            _line("Not earned yet")
        for row in rest:
            _unearned(row)
