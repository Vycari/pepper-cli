"""``pepper brief``: pick one edition out of the Brief app's document and print it.

The Brief app's document (``GET /api/v1/apps/brief/data``) holds the recent editions, newest
first, each with a ``date`` (``YYYY-MM-DD``) and the sections the web card draws. ``--now``
runs the owner-scoped ``brief.run_now`` action instead, which writes (it queues a real run).
"""

from __future__ import annotations

import html
from typing import Any

from rich.console import Console
from rich.markup import escape

from pepper_cli.terminal_text import sanitize_line, sanitize_text

_console = Console()  # writes to the current sys.stdout; NO_COLOR and pipes are auto-detected


class BriefError(Exception):
    """No such edition. ``code`` is the J8 error code."""

    def __init__(self, message: str, code: str = "not_found") -> None:
        super().__init__(message)
        self.code = code


def _s(value: object) -> str:
    return escape(sanitize_line(html.unescape(str(value))))


def _out(text: str = "") -> None:
    _console.print(text, soft_wrap=True)


def editions_of(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """The editions in an app document, in the order the server sends them (newest first;
    ``list_brief_runs`` orders by ``fired_at`` descending). The CLI never re-sorts."""
    data = doc.get("data")
    rows = data.get("briefs") if isinstance(data, dict) else None
    return [r for r in rows or [] if isinstance(r, dict)]


def pick(doc: dict[str, Any], date: str | None) -> dict[str, Any]:
    """The edition whose ``date`` field is ``date``, or the first one the server sent (its
    latest) when ``date`` is ``None``. A plain lookup; nothing here orders editions."""
    editions = editions_of(doc)
    if date is None:
        if not editions:
            data = doc.get("data")
            configured = isinstance(data, dict) and data.get("configured")
            raise BriefError(
                "No brief has been delivered yet."
                if configured
                else "No brief is set up yet; ask Pepper to start one."
            )
        return editions[0]
    for edition in editions:
        if edition.get("date") == date:
            return edition
    raise BriefError(f"No brief for {date}.")


def render_brief(brief: dict[str, Any]) -> None:
    """One edition, readably: dateline, lede, the day's events, lead-ins, stories, the rest."""
    head = _s(brief.get("dateline") or brief.get("date") or "Brief")
    when = f" [dim]delivered {_s(brief['delivered_at'])}[/dim]" if brief.get("delivered_at") else ""
    _out(f"[bold]{head}[/bold]{when}")
    sections = brief.get("sections")
    if not isinstance(sections, dict) or not sections.get("structured"):
        _out()
        _out(escape(sanitize_text(html.unescape(str(brief.get("text") or "(empty)")))))
        return
    if sections.get("lede"):
        _out()
        _out(_s(sections["lede"]))
    events = [e for e in sections.get("events") or [] if isinstance(e, dict)]
    if events:
        _out()
        for e in events:
            _out(f"  [cyan]{_s(e.get('time', ''))}[/cyan]  {_s(e.get('title', ''))}")
    lead_ins = [li for li in sections.get("lead_ins") or [] if isinstance(li, dict)]
    if lead_ins:
        _out()
        for li in lead_ins:
            _out(f"  [bold]{_s(li.get('name', ''))}:[/bold] {_s(li.get('value', ''))}")
    todos = [t for t in sections.get("todos") or [] if t]
    if todos:
        _out()
        _out("[bold]todos[/bold]")
        for t in todos:
            _out(f"  - {_s((t.get('title') or t.get('text') or '') if isinstance(t, dict) else t)}")
    stories = [s for s in sections.get("stories") or [] if isinstance(s, dict)]
    if stories:
        _out()
        _out("[bold]feeds[/bold]")
        for s in stories:
            _out(f"  - [bold]{_s(s.get('headline', ''))}[/bold]")
            if s.get("context"):
                _out(f"    {_s(s['context'])}")
            if s.get("url"):
                _out(f"    [dim]{_s(s['url'])}[/dim]")
    rest = [r for r in sections.get("rest") or [] if r]
    if rest:
        _out()
        for r in rest:
            _out(f"  {_s(r)}")
