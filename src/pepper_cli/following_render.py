"""Terminal output for ``pepper following`` and ``pepper feed``.

Plain lines, no tables, so the output stays grep-able. Every server field goes through
``_s`` (control characters out, markup escaped); ``--json`` never comes through here.
"""

from __future__ import annotations

import html
from typing import Any
from urllib.parse import urlsplit

from rich.console import Console
from rich.markup import escape
from rich.style import Style
from rich.text import Text

from pepper_cli import following_view
from pepper_cli.terminal_text import sanitize_line

_LATEST_CHARS = 64
_console = Console()  # writes to the current sys.stdout; NO_COLOR and pipes are auto-detected


def _s(value: object) -> str:
    """A one-line server field for a markup string: control characters out, markup escaped."""
    return escape(sanitize_line(html.unescape(str(value))))  # feeds ship "&#8217;" in titles


def web_link(url: object) -> str | None:
    """The server's ``url``, unchanged, when it is a usable ``http(s)`` link; else None.

    Only ``http`` or ``https`` with a host is a link (``file:``, ``javascript:`` are refused).
    A URL that does not parse, or that carries whitespace, a control character, an escape
    sequence or an invisible format character, is refused rather than repaired, so whatever
    comes back is exactly what the server sent: nothing is HTML-decoded or percent-encoded,
    and IPv6 brackets stay as they are. Markup safety is not this function's job: ``linked``
    attaches the URL as a style, never as markup text.
    """
    if not isinstance(url, str):
        return None
    text = url.strip()
    if not text or text != sanitize_line(text) or any(c.isspace() for c in text):
        return None
    try:
        parts = urlsplit(text)
        host = parts.hostname
    except ValueError:  # "https://[broken": one bad link must not stop the feed rendering
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    return text


def linked(label: str, url: object, *, style: str = "") -> Text:
    """``label`` (markup already escaped) in ``style``, as a terminal hyperlink to ``url``.

    The link is a ``Style(link=...)`` on the text, not a ``[link=...]`` markup tag, so no
    character in the URL can end a tag. Only when stdout is a terminal and ``url`` is an
    ``http(s)`` link; otherwise there is no link, so pipes and ``--json`` never see an
    escape sequence.
    """
    target = web_link(url) if _console.is_terminal else None
    return Text.from_markup(label, style=Style.parse(style) + Style(link=target))


def _out(text: str | Text = "") -> None:
    _console.print(text, soft_wrap=not _console.is_terminal)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


_SUMMARY_INDENT = 13
_SUMMARY_CHARS = 140


def _summary_line(summary: str) -> str:
    """One dim line for a feed row: entities unescaped, whitespace collapsed, clipped to the
    terminal's width (about 140 characters when output is not a terminal)."""
    text = " ".join(sanitize_line(html.unescape(summary)).split())
    width = _console.width - _SUMMARY_INDENT if _console.is_terminal else _SUMMARY_CHARS
    return _clip(text, max(min(width, _SUMMARY_CHARS * 2), 20))


def _new_count(item: dict[str, Any]) -> int:
    rows = (item.get("recent") or {}).get("items") or []
    return sum(1 for r in rows if isinstance(r, dict) and r.get("new"))


def render_following_list(items: list[dict[str, Any]], counts: dict[str, Any]) -> None:
    """What you follow, by group in the app's order; live first within a group (server order)."""
    if not items:
        _out("(you don't follow anything yet)")
        return
    for group, rows in following_view.grouped(items, counts):
        _out(f"[bold]{_s(group)}[/bold] [dim]({len(rows)})[/dim]")
        for it in rows:
            marks = []
            if it.get("live"):
                marks.append("[bold red]LIVE[/bold red]")
            if (n := _new_count(it)) and not it.get("live"):
                marks.append(f"[green]{n} new[/green]")
            state = it.get("state")
            if state and state != "ok":
                marks.append(f"[yellow]{_s(state)}[/yellow]")
            latest = it.get("latest")
            line = _clip(str(latest), _LATEST_CHARS) if latest else "nothing yet"
            when = f" · {it['when_label']}" if it.get("when_label") else ""
            tail = "  " + " ".join(marks) if marks else ""
            _out(
                f"  {_s(it.get('name') or '?')}{tail}\n"
                f"    [dim]{_s(line + when)}[/dim]  "
                f"[dim cyan]{_s(following_view.short_id(str(it.get('ref') or '')))}[/dim cyan]"
            )
    live, unread = counts.get("live", 0), counts.get("unread", 0)
    _out(f"\n[dim]{len(items)} followed · {live} live · {unread} unread[/dim]")


def render_following_show(item: dict[str, Any]) -> None:
    """One followed thing: header, latest line, then its recent items."""
    name = _s(item.get("name") or "?")
    kind = " · ".join(str(x) for x in (item.get("group"), item.get("kind"), item.get("sub")) if x)
    _out(f"[bold]{name}[/bold]  [dim]{_s(kind)}[/dim]")
    sid = following_view.short_id(str(item.get("ref") or ""))
    _out(f"[dim]id {_s(sid)}  ({_s(item.get('ref'))})[/dim]")
    state = item.get("state")
    if item.get("live"):
        _out("[bold red]LIVE[/bold red]")
    if state and state != "ok":
        _out(f"[yellow]{_s(state)}[/yellow]")
    if item.get("latest"):
        when = f"  [dim]{_s(item['when_label'])}[/dim]" if item.get("when_label") else ""
        _out(f"{_s(item['latest'])}{when}")
    recent = item.get("recent") or {}
    if recent.get("note"):
        _out(f"[yellow]{_s(recent['note'])}[/yellow]")
    rows = [r for r in recent.get("items") or [] if isinstance(r, dict)]
    _out(f"\n[bold]{_s(recent.get('label') or 'Recent')}[/bold]")
    if not rows:
        _out(f"  [dim]{_s(recent.get('empty') or 'Nothing fetched from this one yet.')}[/dim]")
    for r in rows:
        flags = []
        if r.get("new"):
            flags.append("[green]new[/green]")
        if r.get("upcoming"):
            flags.append("[cyan]upcoming[/cyan]")
        if r.get("duration_label"):
            flags.append(f"[dim]{_s(r['duration_label'])}[/dim]")
        _out(
            f"  [dim]{_s(r.get('when_label') or ''):<14}[/dim] {_s(r.get('title') or '')} "
            + " ".join(flags)
        )
        if r.get("url"):
            _out(f"  [dim]{'':<14} {_s(r['url'])}[/dim]")
    if recent.get("meta"):
        _out(f"\n[dim]{_s(recent['meta'])}[/dim]")
    if recent.get("link"):
        _out(f"[dim]{_s(recent['link'])}[/dim]")


# The feed mixes kinds; a coloured tag per group lets the eye pick out sports, podcasts and
# articles. Unknown groups fall back to a plain tag.
_GROUP_TAG = {
    "Sports": ("sports", "yellow"),
    "Podcasts": ("podcast", "magenta"),
    "Reading": ("article", "blue"),
    "Shows": ("show", "bright_red"),
    "Events": ("event", "bright_cyan"),
}


def group_tag(group: str) -> str:
    """``[sports]``-style marker for a feed row, coloured by group."""
    label, colour = _GROUP_TAG.get(group, (group.lower() or "item", "white"))
    return f"[bold {colour}]{_s(label):<7}[/bold {colour}]"


def _flags(e: dict[str, Any]) -> list[str]:
    flags = []
    if e.get("live"):
        flags.append("[bold red]LIVE[/bold red]")
    if e.get("new"):
        flags.append("[green]new[/green]")
    if e.get("upcoming"):
        flags.append("[cyan]upcoming[/cyan]")
    return flags


def render_feed(entries: list[dict[str, Any]], *, note: str | None = None) -> None:
    """The server's ranked list, in the server's order, numbered."""
    if note:
        _out(f"[yellow]{_s(note)}[/yellow]")
        return
    if not entries:
        _out("(nothing in your feed yet; follow something with `pepper following create`)")
        return
    for n, e in enumerate(entries, 1):
        prefix = Text.from_markup(f"[dim]{n:>2}.[/dim] {group_tag(e['group'])} ")
        _out(prefix + linked(_s(e["title"]), e.get("url"), style="bold"))
        source = str(e.get("source") or "")
        meta = f"{source} · {e['when_label']}" if e["when_label"] else source
        _out(f"             [dim]{_s(meta)}[/dim]  " + " ".join(_flags(e)))
        if e.get("summary") and (line := _summary_line(e["summary"])):
            _out(f"             [dim]{escape(line)}[/dim]")
        if e.get("url"):
            _out(f"             [dim]{_s(e['url'])}[/dim]")
        if e.get("fold"):
            _out(f"             [dim]{_s(e['fold'])}[/dim]")


def render_candidates(candidates: list[dict[str, Any]]) -> None:
    """The numbered search list: name, kind and detail, and whether you already follow it."""
    if not candidates:
        _out("(nothing found)")
        return
    for c in candidates:
        kind = str(c.get("kind") or "")
        detail = str(c.get("detail") or " · ".join(x for x in (kind, c.get("sub")) if x))
        mark = " [green]following[/green]" if c.get("following") else ""
        _out(f"[dim]{c['n']:>2}.[/dim] [bold]{_s(c.get('name') or '?')}[/bold]{mark}")
        _out(f"    [dim]{_s(detail)}[/dim]")
