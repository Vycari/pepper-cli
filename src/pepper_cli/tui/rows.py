"""Messages as Rich renderables for the thread pane (``planning/terminal-ui.md`` §7, P1 rows).

Everything here returns a renderable and prints nothing: the TUI owns the alternate screen,
and ``render.py`` stays the only module that writes to stdout. Message text is only ever put
into a ``Text`` (never parsed as markup) after ``sanitize_text``, so participant text can
neither style nor link anything. That is an invariant, not a habit: do not feed a body to
``Text.from_markup`` or a ``Static`` that parses markup.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from rich.box import ROUNDED
from rich.console import Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.styled import Styled
from rich.table import Table
from rich.text import Text

from pepper_cli import render
from pepper_cli.terminal_text import sanitize_line, sanitize_text
from pepper_cli.tui.state import draw_checklist, parse_ts, url_domain

if TYPE_CHECKING:
    from collections.abc import Iterable

    from rich.console import RenderableType

    from pepper_cli.api_client import ChatHistoryEntry, GroupCard, GroupMessage
    from pepper_cli.tui.state import PendingSend

_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
#: Text that reads as markdown: emphasis, code, a heading, list or quote marker at the start of
#: a line, a link, or a table row. A message without any of these stays plain text, so its line
#: breaks and its @mention highlighting are kept exactly as typed.
_MARKDOWN_HINT = re.compile(
    r"\*\*|__|`|\[[^\]\n]+\]\([^)\n]+\)"
    r"|^[ ]{0,3}(?:#{1,6}|[-*+]|\d{1,9}[.)]|>)[ \t]"
    r"|^[ ]{0,3}\|.*\|[ \t]*$",
    re.MULTILINE,
)
#: Style of a message that mentions the viewer (the web's accent treatment).
ACCENT = "bold yellow"
DELETED = "Message deleted"
BROKEN = "Couldn't show this message"
INDENT = "  "
#: Background of the selected row (the target of ``r``).
SELECTED = "on grey23"
#: What a pending row says after "You", by status.
PENDING_LABEL = {
    "queued": "sending…",
    "sending": "sending…",
    "sent": "sent",
    "failed": "failed, r to retry",
}


def safe_color(value: str | None) -> str | None:
    """A server colour only if it is a plain ``#rrggbb``; anything else is ignored."""
    return value if value and _HEX_COLOR.fullmatch(value) else None


def day_divider(label: str) -> Text:
    """``── Today ──``, centred."""
    return Text(f"── {sanitize_line(label)} ──", style="dim", justify="center")


def _header(name: Text, stamp: str) -> RenderableType:
    grid = Table.grid(expand=True)
    grid.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
    grid.add_column(justify="right", no_wrap=True)
    grid.add_row(name, Text(sanitize_line(stamp), style="dim"))
    return grid


def _human_size(count: Any) -> str:
    if not isinstance(count, int | float) or isinstance(count, bool) or count < 0:
        return ""
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def _attachment_lines(attachments: Iterable[dict[str, Any]]) -> list[Text]:
    lines = []
    for item in attachments:
        name = sanitize_line(str(item.get("filename") or item.get("name") or item.get("id") or ""))
        size = _human_size(item.get("size_bytes", item.get("size")))
        label = f"📎 {name or 'attachment'}" + (f" ({size})" if size else "")
        lines.append(Text(INDENT + label, style="dim"))
    return lines


def _reaction_line(reactions: Iterable[dict[str, Any]]) -> Text | None:
    """``👍 2  ❤️ 1`` — a chip you have added is bold, the server's ``mine`` being the word."""
    line = Text(INDENT)
    count = 0
    for chip in reactions:
        label = sanitize_line(f"{chip.get('emoji', '')} {chip.get('count', '')}".strip())
        if not label:
            continue
        if count:
            line.append("  ")
        line.append(label, style="bold" if chip.get("mine") is True else "")
        count += 1
    return line if count else None


def looks_like_markdown(text: str) -> bool:
    """Whether a (sanitized) body uses markdown syntax worth rendering."""
    return _MARKDOWN_HINT.search(text) is not None


def _markdown_body(text: str) -> RenderableType | None:
    """The body as Rich ``Markdown`` with its links shown as text, never as terminal
    hyperlinks, or ``None`` when it cannot be built (the caller falls back to plain text)."""
    try:
        return Padding(Markdown(text, hyperlinks=False), (0, 0, 0, len(INDENT)))
    except (ValueError, TypeError, RecursionError):
        return None


def note_body_renderable(body: str) -> RenderableType:
    """A note's text as the viewer shows it: sanitized, its checklist boxes drawn as ``☐``/``☑``,
    and markdown rendered the way a message's is (links as text, never terminal hyperlinks), so
    a note can't style itself beyond what a message can."""
    clean = draw_checklist(sanitize_text(body))
    if looks_like_markdown(clean):
        try:
            return Markdown(clean, hyperlinks=False)
        except (ValueError, TypeError, RecursionError):
            pass
    return Text(clean)


def _body(m: GroupMessage, *, mentions_you: bool) -> RenderableType:
    if m.is_deleted:
        return Text(INDENT + DELETED, style="dim")
    if m.is_broken:
        return Text(INDENT + BROKEN, style="dim")
    clean = sanitize_text(m.raw_body)
    if looks_like_markdown(clean) and (rendered := _markdown_body(clean)) is not None:
        # Markdown loses the @mention highlighting (it is not Text), and "(edited)" cannot
        # share its last line.
        if m.is_edited:
            return Group(rendered, Text(INDENT + "(edited)", style="dim"))
        return rendered
    body = Text(clean, style=ACCENT if mentions_you else "")
    body.highlight_words(
        [f"@{sanitize_line(n)}" for n in m.mention_names if sanitize_line(n)], style="bold"
    )
    indented = Text()
    for i, line in enumerate(body.split("\n", allow_blank=True)):
        if i:
            indented.append("\n")
        indented.append(INDENT)
        indented.append_text(line)
    if m.is_edited:
        indented.append(" (edited)", style="dim")
    return indented


def _preview_lines(previews: Iterable[dict[str, Any]]) -> list[Text]:
    """``↗ title — domain`` per link preview, on one line each. A preview a card already
    covers (``covered_by_card``) is left out, and an empty title falls back to the URL."""
    lines = []
    for preview in previews:
        if preview.get("covered_by_card"):
            continue
        url = str(preview.get("url") or "")
        title = sanitize_line(str(preview.get("title") or "")) or sanitize_line(url)
        if not title:
            continue
        domain = sanitize_line(url_domain(url))
        label = f"↗ {title}" + (f" — {domain}" if domain and domain != title else "")
        lines.append(Text(INDENT + label, style="cyan", no_wrap=True, overflow="ellipsis"))
    return lines


def _card_panel(card: GroupCard) -> RenderableType:
    """A boxed app card: bold title, its summary lines, and the way to open it."""
    body = Text(sanitize_line(card.title) or sanitize_line(card.app_key), style="bold")
    for line in card.summary_lines:
        body.append("\n" + sanitize_line(str(line)))
    body.append("\nopen in web (o)", style="dim")
    return Padding(
        Panel(body, box=ROUNDED, border_style="dim", expand=False, padding=(0, 1)),
        (0, 0, 0, len(INDENT)),
    )


def _quote_line(quote: object) -> Text | None:
    """The quoted parent as ``┃ Author: excerpt``, or ``None`` when the message quotes nothing.

    Reads ``quote`` (the parent this message replies to), never ``quote_excerpt``: the
    server fills that on every message with its *own* text, for when someone else quotes it.
    """
    if not isinstance(quote, dict):
        return None
    if quote.get("deleted"):
        return Text(f"{INDENT}┃ (deleted message)", style="dim")
    author = sanitize_line(str(quote.get("author") or ""))
    excerpt = sanitize_line(str(quote.get("excerpt") or ""))
    if not (author or excerpt):
        return None
    if quote.get("composed_via") or quote.get("is_composed"):
        author = f"{author} (via Pepper)" if author else "(via Pepper)"
    return Text(f"{INDENT}┃ {author + ': ' if author else ''}{excerpt}", style="dim")


def message_renderable(
    m: GroupMessage, *, you_id: str | None = None, show_name: bool | None = None
) -> RenderableType:
    """One room message: optional name line (only on ``run_start``), quote, body, extras."""
    mentions_you = bool(you_id) and you_id in m.mentions
    parts: list[RenderableType] = []
    if m.run_start if show_name is None else show_name:
        name = Text(sanitize_line(m.display_name), style=f"bold {safe_color(m.avatar_tint) or ''}")
        via, agent = render.group_message_tags(m)
        for tag in (via, agent):
            if tag:
                name.append(" " + sanitize_line(tag), style="dim")
        if mentions_you:
            name.append(" @", style=ACCENT)
        parts.append(_header(name, m.created_at_display))
    if not (m.is_deleted or m.is_broken):
        if line := _quote_line(m.quote):
            parts.append(line)
        parts.append(_body(m, mentions_you=mentions_you))
        parts.extend(_preview_lines(m.link_previews))
        parts.extend(_card_panel(c) for c in m.cards)
        parts.extend(_attachment_lines(m.attachments))
        if m.reactions and (line := _reaction_line(m.reactions)):
            parts.append(line)
    else:
        parts.append(_body(m, mentions_you=False))
    return Group(*parts)


def _indented(text: str, *, style: str = "") -> Text:
    out = Text(style=style)
    for i, line in enumerate(sanitize_text(text).split("\n")):
        if i:
            out.append("\n")
        out.append(INDENT + line)
    return out


def pending_renderable(p: PendingSend) -> RenderableType:
    """An own message not yet returned by a poll: dimmed, with its state after "You"."""
    failed = p.status == "failed"
    label = PENDING_LABEL.get(p.status, "")
    if failed and p.error:
        label = f"{label} ({sanitize_line(p.error)})"
    name = Text("You", style="bold dim")
    stamp = Text(" " + label, style="bold red" if failed else "dim italic")
    parts: list[RenderableType] = [name + stamp]
    if p.quote_text:
        parts.append(Text(f"{INDENT}┃ {sanitize_line(p.quote_text)}", style="dim"))
    parts.append(_indented(p.body, style="dim"))
    return Group(*parts)


def thread_items(
    messages: Iterable[GroupMessage],
    *,
    you_id: str | None,
    pending: Iterable[PendingSend] = (),
    selected: str | None = None,
) -> list[tuple[str | None, RenderableType]]:
    """A topic's rows with their selection keys (``msg:<id>``, ``pending:<key>``; ``None``
    for a day divider), in display order: server rows, then this session's pending sends.

    A message with an empty ``day_key`` or ``day_label`` (the server's broken-message
    placeholder) neither opens a divider nor resets the day, as on the web. A message is
    shown with its name line when it is selected, so the selection always says whose it is.
    """
    items: list[tuple[str | None, RenderableType]] = []
    last_day: str | None = None
    for m in messages:
        if m.day_key and m.day_label and m.day_key != last_day:
            items.append((None, day_divider(m.day_label)))
            last_day = m.day_key
        key = f"msg:{m.id}"
        if key == selected:
            row = message_renderable(m, you_id=you_id, show_name=True)
            items.append((key, Styled(row, SELECTED)))
        else:
            items.append((key, message_renderable(m, you_id=you_id)))
    for p in pending:
        key = f"pending:{p.key}"
        row = pending_renderable(p)
        items.append((key, Styled(row, SELECTED) if key == selected else row))
    return items


def thread_renderable(
    messages: Iterable[GroupMessage],
    *,
    you_id: str | None,
    pending: Iterable[PendingSend] = (),
    selected: str | None = None,
) -> RenderableType:
    """The topic pane's body: :func:`thread_items` as one renderable."""
    items = thread_items(messages, you_id=you_id, pending=pending, selected=selected)
    if not items:
        return Text("(no messages)", style="dim")
    return Group(*(r for _, r in items))


def dm_renderable(
    entries: Iterable[ChatHistoryEntry],
    *,
    hour24: bool = False,
    provisional: Iterable[tuple[str, str, str]] = (),
) -> RenderableType:
    """Today's Pepper thread. ``You`` and ``Pepper`` take a name line on each change of speaker.

    ``provisional`` rows are this session's turns not yet in history, as ``(role, text,
    label)``: an own message still sending (dimmed, ``label`` saying so), or a reply the
    stream delivered before the history read caught up with it.
    """
    parts: list[RenderableType] = [day_divider("Today")]
    previous_role: str | None = None
    for entry in entries:
        mine = entry.role == "user"
        if entry.role != previous_role:
            moment = parse_ts(entry.created_at)
            stamp = render.local_clock(moment, hour24=hour24) if moment else ""
            who = Text("You" if mine else "Pepper", style="bold")
            parts.append(_header(who, stamp))
            previous_role = entry.role
        body = Text()
        for i, line in enumerate(sanitize_text(entry.text).split("\n")):
            if i:
                body.append("\n")
            body.append(INDENT + line)
        parts.append(body)
        parts.extend(_attachment_lines(entry.attachments))
        if entry.reactions and (line := _reaction_line(entry.reactions)):
            parts.append(line)
    for role, text, label in provisional:
        mine = role == "user"
        if role != previous_role or label:
            who = Text("You" if mine else "Pepper", style="bold dim" if mine else "bold")
            if label:
                who.append(" " + sanitize_line(label), style="dim italic")
            parts.append(who)
            previous_role = role
        parts.append(_indented(text, style="dim" if mine else ""))
    if len(parts) == 1:
        parts.append(Text("(no messages yet today)", style="dim"))
    return Group(*parts)
