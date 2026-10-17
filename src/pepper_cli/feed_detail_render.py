"""Terminal output for ``pepper feed N``: the server's entry, then the optional
transcript and article blocks. Plain lines; every server or web field goes through ``_s`` /
``_text`` (control characters out, markup escaped). ``--json`` never comes through here.
"""

from __future__ import annotations

import html
import textwrap
from datetime import datetime
from typing import Any

from rich.markup import escape

from pepper_cli.following_render import _out, _s
from pepper_cli.terminal_text import sanitize_text

_WRAP = 100


def _clock(iso: str, zone: str | None) -> str:
    """``Thu Oct 15, 5:00 PM PDT`` (no platform-specific strftime flags)."""
    dt = datetime.fromisoformat(iso)
    hour = dt.hour % 12 or 12
    tail = f" {zone}" if zone else ""
    return f"{dt:%a %b} {dt.day}, {hour}:{dt:%M} {'PM' if dt.hour >= 12 else 'AM'}{tail}"


def _when(d: dict[str, Any]) -> str:
    label = d.get("when_label") or ""
    if d.get("when"):
        return f"{label} ({_clock(d['when'], d.get('when_zone'))})".strip()
    return label or "unknown"


def _text(value: object) -> str:
    return sanitize_text(str(value))


def _flags(d: dict[str, Any]) -> str:
    marks = []
    if d.get("live"):
        marks.append("[bold red]LIVE[/bold red]")
    if d.get("new"):
        marks.append("[green]new[/green]")
    if d.get("upcoming"):
        marks.append("[cyan]upcoming[/cyan]")
    return " ".join(marks)


def render_detail(d: dict[str, Any]) -> None:
    n = f"{d['n']}. " if d.get("n") else ""
    _out(f"[bold]{n}{_s(d.get('title') or '?')}[/bold]  {_flags(d)}".rstrip())
    kind = " · ".join(str(x) for x in (d.get("group"), d.get("kind")) if x)
    _out(f"  [dim]from[/dim]      {_s(d.get('source') or '?')}  [dim]{_s(kind)}[/dim]")
    _out(f"  [dim]when[/dim]      {_s(_when(d))}")
    if d.get("url"):
        _out(f"  [dim]url[/dim]       {_s(d['url'])}")
    if d.get("fold"):
        _out(f"  [dim]folded[/dim]    {_s(d['fold'])}")
    if d.get("summary"):
        _summary(d["summary"])
    _game(d)
    if d.get("more"):
        _more(d)
    if d.get("transcript") is not None:
        _transcript(d["transcript"])
    if d.get("article") is not None:
        _article(d["article"])


#: ``(key, label)`` in the order the Game block prints them. ``venue`` and ``broadcast`` are
#: not sent by today's server; they print if a later one sends them.
_GAME_FIELDS = (
    ("league", "league"),
    ("status", "status"),
    ("records", "records"),
    ("headline", "headline"),
    ("note", "note"),
    ("venue", "venue"),
    ("broadcast", "broadcast"),
)


def _game(d: dict[str, Any]) -> None:
    rows = [(label, d[key]) for key, label in _GAME_FIELDS if d.get(key)]
    if not rows:
        return
    _out("\n[bold]Game[/bold]")
    for label, value in rows:
        first, *rest = textwrap.wrap(_text(value), _WRAP - 13) or [""]
        _out(f"  [dim]{label:<10}[/dim] {_s(first)}")
        for ln in rest:
            _out(f"  {'':<10} {_s(ln)}")


def _summary(text: str) -> None:
    _out("\n[bold]Summary[/bold]")
    body = " ".join(_text(html.unescape(text)).split())
    for ln in textwrap.wrap(body, _WRAP):
        _out(f"  {escape(ln)}")


def _more(d: dict[str, Any]) -> None:
    source = _s(d.get("source") or "this source")
    _out(f"\n[bold]Also from {source}[/bold] [dim]({len(d['more'])})[/dim]")
    for m in d["more"]:
        when = _s(m.get("when_label") or "")
        _out(f"  [dim]{when:<16}[/dim] {_s(m.get('title') or '')}  {_flags(m)}".rstrip())
        if m.get("url"):
            _out(f"  [dim]{'':<16} {_s(m['url'])}[/dim]")


def _transcript(t: dict[str, Any]) -> None:
    if t.get("error"):
        _out(f"\n[yellow]{_s(t['error'])}[/yellow]")
        return
    ready = t.get("kind") == "transcript"
    _out(
        "\n[bold]Transcript[/bold]"
        if ready
        else "\n[bold]Show notes[/bold] [dim](no transcript yet)[/dim]"
    )
    if t.get("note"):
        _out(f"[yellow]{_s(t['note'])}[/yellow]")
    text = _text(t.get("text") or "").strip()
    if not text:
        _out("[dim](empty)[/dim]")
        return
    for para in text.split("\n"):
        for ln in textwrap.wrap(para, _WRAP) or [""]:
            _out(_s(ln))


def _article(a: dict[str, Any]) -> None:
    if a.get("error") and not a.get("paragraphs") and not a.get("description"):
        _out(f"\n[yellow]Article: {_s(a['error'])}[/yellow]")
        return
    _out("\n[bold]Article[/bold]")
    if a.get("title"):
        _out(f"  [bold]{_s(a['title'])}[/bold]")
    meta = " · ".join(str(x) for x in (a.get("author"), a.get("published"), a.get("site")) if x)
    if meta:
        _out(f"  [dim]{_s(meta)}[/dim]")
    if a.get("final_url") and a["final_url"] != a.get("url"):
        _out(f"  [dim]{_s(a['final_url'])}[/dim]")
    if a.get("description"):
        for ln in textwrap.wrap(_text(a["description"]).replace("\n", " "), _WRAP):
            _out(f"  [italic]{_s(ln)}[/italic]")
    for para in a.get("paragraphs") or []:
        _out("")
        for ln in textwrap.wrap(_text(para).replace("\n", " "), _WRAP):
            _out(f"  {_s(ln)}")
    if a.get("error"):
        _out(f"\n[yellow]{_s(a['error'])}[/yellow]")
    elif hidden := int(a.get("paragraph_count") or 0) - len(a.get("paragraphs") or []):
        _out(f"\n[dim]{hidden} more paragraphs; --full prints them all.[/dim]")
