"""Messages as Rich renderables for the thread pane (``planning/terminal-ui.md`` §7, P1 rows).

Everything here returns a renderable and prints nothing: the TUI owns the alternate screen,
and ``render.py`` stays the only module that writes to stdout. Message text is only ever put
into a ``Text`` (never parsed as markup) after ``sanitize_text``, so participant text can
neither style nor link anything. That is an invariant, not a habit: do not feed a body to
``Text.from_markup`` or a ``Static`` that parses markup.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from rich.box import ROUNDED
from rich.console import Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.segment import Segment
from rich.styled import Styled
from rich.table import Table
from rich.text import Text

from pepper_cli import render
from pepper_cli.terminal_text import sanitize_line, sanitize_text
from pepper_cli.tui import cards
from pepper_cli.tui.state import draw_checklist, parse_ts, url_domain

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable

    from rich.console import Console, ConsoleOptions, RenderableType, RenderResult

    from pepper_cli.api_client import ChatHistoryEntry, DraftCard, GroupCard, GroupMessage
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


def new_divider() -> Text:
    """``── new messages ──``: where the person's read cursor was when the topic opened."""
    return Text("── new messages ──", style="bold", justify="center")


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


def _markdown_or_text(clean: str) -> RenderableType:
    """Sanitized ``clean`` as Rich ``Markdown`` when it uses markdown syntax (links as text,
    never terminal hyperlinks), else as plain ``Text``."""
    if looks_like_markdown(clean):
        try:
            return Markdown(clean, hyperlinks=False)
        except (ValueError, TypeError, RecursionError):
            pass
    return Text(clean)


def note_body_renderable(body: str) -> RenderableType:
    """A note's text as the viewer shows it: sanitized, its checklist boxes drawn as ``☐``/``☑``,
    and markdown rendered the way a message's is (links as text, never terminal hyperlinks), so
    a note can't style itself beyond what a message can."""
    return _markdown_or_text(draw_checklist(sanitize_text(body)))


def reply_renderable(text: str) -> RenderableType:
    """Pepper's answer as the reply strip shows it: sanitized, then markdown like a note body
    (but with no checklist boxes drawn: it is a message, not a note)."""
    return _markdown_or_text(sanitize_text(text))


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


def draft_panel(draft: DraftCard) -> RenderableType:
    """A boxed draft Pepper staged: who it goes to, its subject and body, and the key that
    sends it (as :func:`_card_panel` boxes an app card)."""
    body = Text(f"Draft, not sent: to {sanitize_line(draft.to)}", style="bold")
    if draft.sends_from:
        body.append(f"\nFrom: {sanitize_line(draft.sends_from)}")
    body.append(f"\n{sanitize_line(draft.subject)}")
    body.append("\n" + sanitize_text(draft.body))
    body.append("\nsend (s)", style="dim")
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


def _name_line(m: GroupMessage, *, mentions_you: bool) -> RenderableType:
    """``Name (via X) (for Y) @`` with the time right-aligned."""
    name = Text(sanitize_line(m.display_name), style=f"bold {safe_color(m.avatar_tint) or ''}")
    via, agent = render.group_message_tags(m)
    for tag in (via, agent):
        if tag:
            name.append(" " + sanitize_line(tag), style="dim")
    if mentions_you:
        name.append(" @", style=ACCENT)
    return _header(name, m.created_at_display)


def message_renderable(
    m: GroupMessage,
    *,
    you_id: str | None = None,
    show_name: bool | None = None,
    show_quote: bool = True,
    show_body: bool = True,
    show_reactions: bool = True,
) -> RenderableType:
    """One room message: optional name line (only on ``run_start``), quote, body, extras.

    ``show_quote`` is ``False`` for a reply straight to the card it is drawn in, whose quote
    would only repeat the card. ``show_body`` is ``False`` for a card whose title took the
    whole message, so only its extras (previews, cards, files, reactions) are drawn."""
    mentions_you = bool(you_id) and you_id in m.mentions
    parts: list[RenderableType] = []
    if m.run_start if show_name is None else show_name:
        parts.append(_name_line(m, mentions_you=mentions_you))
    if not (m.is_deleted or m.is_broken):
        if show_quote and (line := _quote_line(m.quote)):
            parts.append(line)
        if show_body:
            parts.append(_body(m, mentions_you=mentions_you))
        parts.extend(_preview_lines(m.link_previews))
        parts.extend(_card_panel(c) for c in m.cards)
        parts.extend(_attachment_lines(m.attachments))
        if show_reactions and m.reactions and (line := _reaction_line(m.reactions)):
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


#: Style of a card's frame.
FRAME = "dim"


class _Frame:
    """``inner`` between a card's side rules: ``│ …  │``, two columns of the width each side."""

    def __init__(self, inner: RenderableType) -> None:
        self.inner = inner

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = options.max_width
        inner = options.update_width(max(width - 4, 1))
        for line in console.render_lines(self.inner, inner, pad=True):
            yield Segment("│ ", console.get_style(FRAME))
            yield from line
            yield Segment(" │", console.get_style(FRAME))
            yield Segment.line()


class _Edge:
    """A card's top or bottom rule, with ``label`` near its left end: ``╭─ label ────╮``."""

    def __init__(self, left: str, right: str, label: Text | None = None) -> None:
        self.left, self.right, self.label = left, right, label

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = max(options.max_width, 4)
        line = Text(self.left + "─", style=FRAME)
        if self.label is not None and self.label.plain:
            label = self.label.copy()
            label.truncate(max(width - 6, 1), overflow="ellipsis")
            line.append(" ")
            line.append_text(label)
            line.append(" ")
        line.append("─" * max(width - line.cell_len - 1, 0), style=FRAME)
        line.append(self.right, style=FRAME)
        line.truncate(width)
        yield line


def _chip_line(root: GroupMessage) -> Text | None:
    """``Linked: Note · Trip plan   Source · airbnb.com/…  +1``, or ``None`` with no links."""
    shown, more = cards.chips(root)
    if not shown:
        return None
    line = Text(INDENT + "Linked: ", style="dim")
    for i, (kind, label) in enumerate(shown):
        if i:
            line.append("   ")
        if kind:
            line.append(f"{sanitize_line(kind)} · ", style="dim")
        line.append(sanitize_line(label), style="cyan")
    if more:
        line.append(f"  +{more}", style="dim")
    line.no_wrap, line.overflow = True, "ellipsis"
    return line


class _CardTop:
    """A card's top rule and face. When the title took the message's first line but is too
    wide for the rule, the face draws the whole message instead, so no text is lost."""

    def __init__(self, title: Text, body: RenderableType, full: RenderableType | None) -> None:
        self.title, self.body, self.full = title, body, full

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        fits = self.title.cell_len <= max(options.max_width, 4) - 6
        yield _Edge("╭", "╮", self.title)
        yield _Frame(self.body if fits or self.full is None else self.full)


def _card_face(card: cards.Card, *, you_id: str | None) -> _CardTop:
    """A card's title (for the top rule) and what the frame holds above the thread."""
    root = card.root
    if root.is_deleted:
        return _CardTop(
            Text("Message deleted", style="dim italic"), Text(INDENT + DELETED, style="dim"), None
        )
    comment = cards.is_note_comment(root)
    face = cards.note_comment_face(root) if comment else cards.face(root)
    title = Text(sanitize_line(f"“{face.title}”" if comment else face.title), style="bold")
    if root.is_edited and not face.body.strip():
        title.append(" (edited)", style="dim")  # no body line to carry it
    extras: list[RenderableType] = []
    if line := _chip_line(root):
        extras.append(line)
    if root.reactions and (line := _reaction_line(root.reactions)):
        extras.append(line)

    def body(text: str) -> RenderableType:
        return Group(
            message_renderable(
                replace(root, raw_body=text),
                you_id=you_id,
                show_name=False,
                show_body=bool(text.strip()),
                show_reactions=False,
            ),
            *extras,
        )

    whole = root.raw_body.strip()
    full = body(whole) if not comment and face.body != whole else None
    return _CardTop(title, body(face.body), full)


def _card_items(
    card: cards.Card,
    *,
    you_id: str | None,
    selected: str | None,
    expanded: bool,
    pending: list[PendingSend],
    full_view: bool = False,
) -> list[tuple[str | None, RenderableType]]:
    """A card's rows: the head (author line, top rule, face), the fold, the replies, this
    session's replies still sending into it, and the foot. In the full view (``full_view``)
    every reply is shown and there is no fold."""
    root = card.root
    key = f"msg:{root.id}"
    top = _card_face(card, you_id=you_id)
    mentions_you = bool(you_id) and you_id in root.mentions
    head_parts: list[RenderableType] = []
    if not root.is_deleted:
        head_parts.append(_name_line(root, mentions_you=mentions_you))
    head_parts.append(top)
    head: RenderableType = Group(*head_parts)
    items: list[tuple[str | None, RenderableType]] = [
        (key, Styled(head, SELECTED) if key == selected else head)
    ]
    hidden, shown = card.shown(expanded=expanded or full_view)
    if len(card.replies) > cards.FOLD_VISIBLE and not full_view:
        fold_key = f"fold:{root.id}"
        label = (
            f"⋯ {hidden} earlier {'reply' if hidden == 1 else 'replies'} (Enter to show)"
            if hidden
            else "⋯ Show fewer replies (Enter)"
        )
        fold = _Frame(Text(label, style="dim italic"))
        items.append((fold_key, Styled(fold, SELECTED) if fold_key == selected else fold))
    for i, reply in enumerate(shown):
        m = reply.message
        rkey = f"msg:{m.id}"
        show_name = reply.starts_run or i == 0 or rkey == selected
        row = _Frame(
            message_renderable(m, you_id=you_id, show_name=show_name, show_quote=not reply.to_card)
        )
        items.append((rkey, Styled(row, SELECTED) if rkey == selected else row))
    for p in pending:
        pkey = f"pending:{p.key}"
        quoted = p.parent_id != root.id
        row = _Frame(pending_renderable(p if quoted else replace(p, quote_text="")))
        items.append((pkey, Styled(row, SELECTED) if pkey == selected else row))
    hints = "r reply · Esc back" if full_view else "r reply · Enter open"
    foot_label = Text(hints, style="dim italic")
    if card.replies:
        count = len(card.replies)
        foot_label = Text(
            f"{count} {'reply' if count == 1 else 'replies'} · {hints}", style="dim italic"
        )
    # The foot belongs to the card's last row, so that row counts as seen only once the
    # whole card is on screen, and the selection never stops on a rule.
    key_last, last = items[-1]
    items[-1] = (key_last, Group(last, _Edge("╰", "╯", foot_label)))
    return items


def _entry_day(entry: GroupMessage | cards.Card, stream: cards.Stream | None) -> GroupMessage:
    """The message whose day an entry is shown under: a card takes its placement's day."""
    if isinstance(entry, cards.Card):
        at = stream.placement.get(entry.id, entry.id) if stream is not None else entry.id
        if at != entry.id:
            for reply in entry.replies:
                if reply.message.id == at:
                    return reply.message
        return entry.root
    return entry


def thread_items(
    messages: Iterable[GroupMessage],
    *,
    you_id: str | None,
    pending: Iterable[PendingSend] = (),
    selected: str | None = None,
    new_before: str | None = None,
    stream: cards.Stream | None = None,
    expanded: Collection[str] = (),
    full_view: bool = False,
) -> list[tuple[str | None, RenderableType]]:
    """A topic's rows with their selection keys (``msg:<id>``, ``pending:<key>``,
    ``fold:<card id>``; ``None`` for a divider), in display order: server
    rows, then this session's pending sends.

    With ``stream`` (:func:`cards.build_stream` over the same ``messages``), top-level
    messages are drawn as cards with their replies inside, and a pending reply into a card is
    drawn in it; ``expanded`` holds the cards whose fold is open. Without it the topic is the
    flat list it always was. ``full_view`` draws each card with every reply and no fold, for
    one conversation opened on its own.

    A message with an empty ``day_key`` or ``day_label`` (the server's broken-message
    placeholder) neither opens a divider nor resets the day, as on the web. A message is
    shown with its name line when it is selected, so the selection always says whose it is.
    ``new_before`` is the id of the first message after the read cursor: a ``new messages``
    divider (also keyless) goes right above the row (or card) holding it, below its day.
    """
    entries: list[GroupMessage | cards.Card] = (
        stream.entries if stream is not None else list(messages)
    )
    pending = list(pending)
    into: dict[str, list[PendingSend]] = {}
    loose: list[PendingSend] = []
    for p in pending:
        card_id = cards.card_of(stream, p.parent_id) if stream is not None else None
        if card_id is not None and stream is not None and stream.card(card_id) is not None:
            into.setdefault(card_id, []).append(p)
        else:
            loose.append(p)
    new_entry = cards.card_of(stream, new_before) if stream is not None else None
    items: list[tuple[str | None, RenderableType]] = []
    last_day: str | None = None
    for entry in entries:
        day = _entry_day(entry, stream)
        if day.day_key and day.day_label and day.day_key != last_day:
            items.append((None, day_divider(day.day_label)))
            last_day = day.day_key
        if isinstance(entry, cards.Card):
            if new_before is not None and new_entry == entry.id:
                items.append((None, new_divider()))
            items.extend(
                _card_items(
                    entry,
                    you_id=you_id,
                    selected=selected,
                    expanded=entry.id in expanded,
                    pending=into.get(entry.id, []),
                    full_view=full_view,
                )
            )
            continue
        m = entry
        key = f"msg:{m.id}"
        if m.id == new_before:
            items.append((None, new_divider()))
        if key == selected:
            row = message_renderable(m, you_id=you_id, show_name=True)
            items.append((key, Styled(row, SELECTED)))
        else:
            items.append((key, message_renderable(m, you_id=you_id)))
    for p in loose:
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
    sent_drafts: Collection[str] = (),
) -> RenderableType:
    """Today's Pepper thread. ``You`` and ``Pepper`` take a name line on each change of speaker.

    ``provisional`` rows are this session's turns not yet in history, as ``(role, text,
    label)``: an own message still sending (dimmed, ``label`` saying so), or a reply the
    stream delivered before the history read caught up with it. A draft the turn staged is
    boxed under its reply until it is sent (``sent_drafts`` holds ids sent this session).
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
        parts.extend(
            draft_panel(d) for d in entry.drafts if not d.sent and d.draft_id not in sent_drafts
        )
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
