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

from rich.console import Group
from rich.styled import Styled
from rich.table import Table
from rich.text import Text

from pepper_cli import render
from pepper_cli.terminal_text import sanitize_line, sanitize_text
from pepper_cli.tui.state import parse_ts

if TYPE_CHECKING:
    from collections.abc import Iterable

    from rich.console import RenderableType

    from pepper_cli.api_client import ChatHistoryEntry, GroupMessage
    from pepper_cli.tui.state import PendingSend

_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
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
    chips = [sanitize_line(f"{r.get('emoji', '')} {r.get('count', '')}".strip()) for r in reactions]
    chips = [c for c in chips if c]
    return Text(INDENT + "  ".join(chips)) if chips else None


def _body(m: GroupMessage, *, mentions_you: bool) -> Text:
    if m.is_deleted:
        return Text(INDENT + DELETED, style="dim")
    if m.is_broken:
        return Text(INDENT + BROKEN, style="dim")
    body = Text(sanitize_text(m.raw_body), style=ACCENT if mentions_you else "")
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
