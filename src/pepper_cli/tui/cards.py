"""Conversation cards: a topic's top-level messages with their replies nested under them
(``planning/tui-conversation-cards.md``).

Pure: messages in, a stream plan out. Nothing here draws (``rows.py`` does) or talks to the
server. The rules follow the web and the Apple apps, so a topic reads the same everywhere:

- a *card* is a top-level message that is a note comment, or, in a topic that takes cards
  (not a bridged thread), any other top-level message but an activity line, a bridged
  mirror, a broken row or a blocked author's stub;
- a reply nests under the card its chain of parents reaches, a reply to a reply included;
  a deleted reply is not drawn, but replies under it still belong to the card, and a deleted
  root stays a card while it has a live reply;
- a card shows its latest :data:`FOLD_VISIBLE` replies until its fold is opened;
- a card with replies from other people sits where the latest such reply was posted (the
  *bump*); the viewer's own reply never moves it, and a card the viewer is busy with keeps
  the place it had when the hold began.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from pepper_cli.tui.state import parse_ts

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Mapping

    from pepper_cli.api_client import GroupMessage

#: Replies a folded card shows (the maintainer's choice for the terminal, doc §6).
FOLD_VISIBLE = 3
#: A title longer than this is cut with an ellipsis.
TITLE_LIMIT = 100
#: Link chips drawn on a card before "+N more".
CHIPS_ON_CARD = 4
#: How many parents a reply's chain may be followed up, by fetching, to a root older than the
#: loaded page.
MISSING_ROOT_MAX_HOPS = 5
#: The same author within this gap continues a run inside a thread.
_RUN_GAP = timedelta(minutes=15)

_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]*([^#\s].*?)(?:[ \t]+#+)?[ \t]*$")
_WIKILINK = re.compile(r"\[\[([^\]|]*)(?:\|([^\]]*))?\]\]")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_ANGLE_URL = re.compile(r"<(https?://[^>\s]+)>")
_EMPHASIS = re.compile(r"\*\*|__|~~|`")
#: A first line that is not plain text: it is flattened and cut, and stays in the body.
_NOT_PLAIN = re.compile(r"[\[\]`*_<>@~|]|https?://|^\s*(?:[-+>]|\d+[.)])\s")


@dataclass(frozen=True)
class Reply:
    """One reply drawn inside a card."""

    message: GroupMessage
    #: Shows its author line, by the thread's own run rule.
    starts_run: bool
    #: Replies straight to the card, so its quote line is not drawn.
    to_card: bool


@dataclass
class Card:
    """A top-level message and the replies nested under it, oldest first."""

    root: GroupMessage
    replies: list[Reply] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.root.id

    def shown(self, *, expanded: bool) -> tuple[int, list[Reply]]:
        """``(hidden, replies to draw)``: the latest :data:`FOLD_VISIBLE` unless opened."""
        if expanded or len(self.replies) <= FOLD_VISIBLE:
            return 0, list(self.replies)
        hidden = len(self.replies) - FOLD_VISIBLE
        return hidden, self.replies[hidden:]


@dataclass
class Stream:
    """A topic laid out: entries in display order, and which card holds each nested message."""

    entries: list[GroupMessage | Card]
    #: Message id (a nested reply, a deleted one included, or a root) to its card's id.
    owner: dict[str, str]
    #: Card id to the message id it is placed at (its own id when it did not move).
    placement: dict[str, str]

    def card(self, card_id: str) -> Card | None:
        return next((e for e in self.entries if isinstance(e, Card) and e.id == card_id), None)


def is_note_comment(m: GroupMessage) -> bool:
    """A top-level message anchored to a passage of a note: a card in any topic."""
    exact = (m.anchor or {}).get("exact")
    return (
        not m.parent_message_id
        and not m.is_broken
        and isinstance(exact, str)
        and bool(exact.strip())
    )


def could_be_conversation(m: GroupMessage) -> bool:
    """A top-level row of a member or Pepper: not a reply, activity line, mirror or stub."""
    return not (
        m.parent_message_id
        or m.is_activity
        or m.is_bridged
        or m.is_broken
        or m.is_blocked_collapsed
    )


def is_card_root(m: GroupMessage, *, takes_cards: bool) -> bool:
    return is_note_comment(m) or (takes_cards and could_be_conversation(m))


def _starts_run(previous: GroupMessage | None, m: GroupMessage) -> bool:
    if previous is None:
        return True
    if (previous.is_pepper, previous.is_you, previous.display_name) != (
        m.is_pepper,
        m.is_you,
        m.display_name,
    ):
        return True
    before, after = parse_ts(previous.created_at), parse_ts(m.created_at)
    if before is None or after is None:
        return True
    return not (timedelta(0) <= after - before < _RUN_GAP)


def build_stream(
    messages: Iterable[GroupMessage],
    *,
    takes_cards: bool,
    holds: Mapping[str, str] | None = None,
    fetched: Collection[str] = (),
) -> Stream:
    """Lay out ``messages`` (time order) as cards and plain rows.

    ``holds`` maps a card the viewer is busy with to the message id it was placed at when the
    hold began (see :func:`placements`), so another person's reply does not move it from
    under the cursor. ``fetched`` names messages read by id, from before the loaded page, so
    that replies in it can nest (:func:`missing_parents`): one is drawn only as a card with a
    reply on the page, never as a row of its own, and it sits at its latest reply, whoever
    wrote it, since it has no place of its own on screen.
    """
    ordered = list(messages)
    roots = {m.id: m for m in ordered if is_card_root(m, takes_cards=takes_cards)}
    owner: dict[str, str] = {}
    threads: dict[str, list[GroupMessage]] = {}
    flat: list[GroupMessage] = []
    for m in ordered:
        if m.id in roots:
            flat.append(m)
            continue
        parent = m.parent_message_id or ""
        card_id = parent if parent in roots else owner.get(parent)
        if card_id is None:
            # A withdrawn reply never falls out of its conversation as a loose tombstone.
            if takes_cards and parent and m.is_deleted:
                continue
            flat.append(m)
            continue
        owner[m.id] = card_id
        if not m.is_deleted:
            threads.setdefault(card_id, []).append(m)
    cards: dict[str, Card] = {}
    for card_id, root in roots.items():
        replies: list[Reply] = []
        previous: GroupMessage | None = None
        for m in threads.get(card_id, []):
            replies.append(
                Reply(
                    m, starts_run=_starts_run(previous, m), to_card=m.parent_message_id == card_id
                )
            )
            previous = m
        cards[card_id] = Card(root, replies)
    # A deleted root with no live reply is not a card: the tombstone it always was, or,
    # when the replies it had are all withdrawn too, nothing at all.
    for card_id, card in list(cards.items()):
        if card.root.is_deleted and not card.replies:
            del cards[card_id]
            if card_id in owner.values():
                flat = [m for m in flat if m.id != card_id]
    for card_id, card in list(cards.items()):
        if card_id in fetched and all(r.message.id in fetched for r in card.replies):
            del cards[card_id]  # its replies have left the page
    flat = [m for m in flat if m.id not in fetched or m.id in cards]
    for card_id in cards:
        owner[card_id] = card_id
    placement = placements(cards.values(), holds=holds or {}, fetched=fetched)
    entries = _place(flat, cards, placement, ordered)
    return Stream(entries=entries, owner=owner, placement=placement)


def placements(
    cards: Iterable[Card], *, holds: Mapping[str, str], fetched: Collection[str] = ()
) -> dict[str, str]:
    """Where each card sits: at its latest reply from someone else, else at itself.

    A card read by id (``fetched``) sits at its latest reply from anyone. A held card keeps
    the placement it had when the hold began (``holds``), as long as that message is still
    one of its own (an unknown placement falls back to the live rule).
    """
    out: dict[str, str] = {}
    for card in cards:
        live = card.id
        for reply in card.replies:
            if card.id in fetched or not reply.message.is_you:
                live = reply.message.id
        held = holds.get(card.id)
        mine = {card.id, *(r.message.id for r in card.replies)}
        out[card.id] = held if held in mine else live
    return out


def _place(
    flat: list[GroupMessage],
    cards: Mapping[str, Card],
    placement: Mapping[str, str],
    ordered: list[GroupMessage],
) -> list[GroupMessage | Card]:
    """Plain rows in time order, each card in the slot of the message it is placed at."""
    position = {m.id: i for i, m in enumerate(ordered)}
    keyed: list[tuple[int, int, GroupMessage | Card]] = []
    for m in flat:
        if m.id in cards:
            continue
        keyed.append((position[m.id], 0, m))
    for card_id, card in cards.items():
        at = placement.get(card_id, card_id)
        keyed.append((position.get(at, position[card_id]), 1, card))
    keyed.sort(key=lambda item: (item[0], item[1]))
    return [entry for _, _, entry in keyed]


def missing_parents(
    messages: Iterable[GroupMessage],
    *,
    held: Mapping[str, GroupMessage],
    attempted: Collection[str],
) -> list[str]:
    """The ids to read so replies in ``messages`` can nest under a root older than the page.

    Each reply's chain of parents is walked through ``messages`` and ``held`` (read earlier);
    where it reaches an id neither holds, that id is missing, unless it was asked for already
    (``attempted``, found or not) or the chain is past :data:`MISSING_ROOT_MAX_HOPS` reads.
    Display order, no repeats.
    """
    ordered = list(messages)
    known: dict[str, GroupMessage] = {**held, **{m.id: m for m in ordered}}
    out: list[str] = []
    for m in ordered:
        parent, hops, seen = m.parent_message_id, 0, {m.id}
        while parent and parent in known and parent not in seen:
            seen.add(parent)
            if parent in held:
                hops += 1
            parent = known[parent].parent_message_id
        if not parent or parent in seen or hops >= MISSING_ROOT_MAX_HOPS:
            continue
        if parent not in attempted and parent not in out:
            out.append(parent)
    return out


def card_of(stream: Stream, message_id: str | None) -> str | None:
    """The card a message (a root or a nested reply) belongs to."""
    return stream.owner.get(message_id or "")


@dataclass(frozen=True)
class Face:
    """What a conversation card draws above its thread."""

    title: str
    #: The message without the line the title took; the whole message when it took none.
    body: str


def split_title(body: str) -> tuple[str, bool]:
    """A conversation's title from its text, when the server sent none: ``(title, took_line)``.

    A heading line is the title; else a short first line is taken whole; else the first line
    is cut. ``took_line`` says the title is the first line, so the body leaves it out.
    """
    lines = body.strip().split("\n")
    first = lines[0] if lines else ""
    if (match := _HEADING.match(first)) and match.group(1).strip():
        text = _flatten(match.group(1))
        return (_clamp(text), len(text) <= TITLE_LIMIT)
    for index, line in enumerate(lines):
        flat = _flatten(line)
        if not flat:
            continue
        if index == 0 and not _NOT_PLAIN.search(line) and len(flat) <= TITLE_LIMIT:
            return flat, True
        return _clamp(flat), False
    return "Untitled", False


def _flatten(line: str) -> str:
    """``line`` as plain words: links reduced to their text, emphasis marks dropped."""
    out = _WIKILINK.sub(lambda m: m.group(2) or m.group(1), line)
    out = _LINK.sub(lambda m: m.group(1), out)
    out = _ANGLE_URL.sub(lambda m: m.group(1), out)
    out = _EMPHASIS.sub("", out).replace("\\#", "#")
    return " ".join(out.split())


def _clamp(text: str) -> str:
    if len(text) <= TITLE_LIMIT:
        return text
    return text[: TITLE_LIMIT - 1].rstrip() + "…"


def face(root: GroupMessage) -> Face:
    """The card's title and the body under it.

    The server's ``title`` wins when it sent one. Its ``title_source`` says whether the title
    is the heading line (``heading``) or the first line (``first_line``); a first-line title
    left the line in the body only when it had to be cut, which shows as a title that is not
    the line itself.
    """
    text = root.raw_body.strip()
    lines = text.split("\n")
    rest = "\n".join(lines[1:]).strip()
    if root.title:
        if root.title_source == "heading":
            return Face(root.title, rest)
        if " ".join(lines[0].split()) == root.title:
            return Face(root.title, rest)
        return Face(root.title, text)
    title, took_line = split_title(text)
    return Face(title, rest if took_line else text)


def note_comment_face(root: GroupMessage) -> Face:
    """A note comment's title is the passage it is on, on one line."""
    exact = str((root.anchor or {}).get("exact") or "")
    return Face(_clamp(" ".join(exact.split())), root.raw_body.strip())


def chips(root: GroupMessage) -> tuple[list[tuple[str, str]], int]:
    """``([(type, label), …], more)``: the server's links for a card, a removed one left out,
    at most :data:`CHIPS_ON_CARD` and how many more there are."""
    out: list[tuple[str, str]] = []
    for link in root.links:
        if not isinstance(link, dict) or link.get("removed") or link.get("is_removed"):
            continue
        kind, label = str(link.get("type") or ""), str(link.get("label") or "")
        if label:
            out.append((kind, label))
    return out[:CHIPS_ON_CARD], max(0, len(out) - CHIPS_ON_CARD)


def hold_ids(stream: Stream, *, selected: str | None, replying_to: str | None) -> set[str]:
    """The cards the viewer is busy with: the one the selection is in, the one a reply goes to."""
    out: set[str] = set()
    for message_id in (selected, replying_to):
        if (card_id := card_of(stream, message_id)) is not None:
            out.add(card_id)
    return out


def settle_holds(
    holds: Mapping[str, str], busy: Collection[str], live: Mapping[str, str]
) -> dict[str, str]:
    """The holds after a render: a card still busy keeps its placement, a newly busy one
    takes its live placement, and one no longer busy is let go."""
    return {card_id: holds.get(card_id, live.get(card_id, card_id)) for card_id in busy}
