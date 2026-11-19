"""Picture references in a note body that readers won't see.

``notes create`` and ``notes edit`` save whatever they are given, then call
:func:`scan` on the saved text and warn about each reference that will not
show. Three shapes, all easy to write by accident in a draft from elsewhere:

- an Obsidian embed (``![[chart.svg]]``), or a ``[[chart.svg]]`` link to a
  picture file, which a note does not show;
- a markdown image, inline or by reference, whose target is a file on your
  computer (``![](attachments/chart.svg)``);
- a picture link (``![](/artifacts/<id>/raw)``), returned by :func:`scan` for
  the caller to check against the room, since only the server knows where a
  picture is saved.

Text inside code (fenced blocks and inline spans) is left out, so a note that
shows the syntax as an example is not warned about. Pure text work, no I/O.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

#: Fenced blocks (```` ``` ```` or ``~~~``, closed by the same fence or the end of the
#: text), then inline code spans. Both are blanked before the scan.
_FENCE = re.compile(
    r"^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}\1[`~]*[ \t]*$|\Z)", re.MULTILINE | re.DOTALL
)
#: A code span never crosses a blank line (a paragraph break ends it).
_INLINE_CODE = re.compile(r"(`+)(?!`)(?:(?!\n[ \t]*\n).)+?(?<!`)\1(?!`)", re.DOTALL)

_EMBED = re.compile(r"!\[\[[^\]\n]+\]\]")
_WIKILINK = re.compile(r"(?<!!)\[\[([^\]|#\n]+)(?:[#|][^\]\n]*)?\]\]")
_IMAGE = re.compile(r"!\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^)\s]+)[^)\n]*\)")
#: A reference-style image (``![alt][label]``, ``![alt][]`` or ``![alt]``) and the
#: definitions its label points at (``[label]: target``).
_REF_IMAGE = re.compile(r"!\[([^\]\n]*)\](?:\[([^\]\n]*)\])?(?![(\[])")
_REF_DEF = re.compile(r"^ {0,3}\[([^\]\n]+)\]:[ \t]*(<[^>\n]+>|\S+)", re.MULTILINE)
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
#: A Windows drive path (``C:\\Users\\...`` or ``C:/Users/...``), which ``_SCHEME`` would
#: otherwise read as a URL scheme.
_DRIVE = re.compile(r"^[a-zA-Z]:[\\/]")
_PICTURE_LINK = re.compile(
    r"^(?:https?://[^/\s]+)?/artifacts/"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/raw(?:[?#].*)?$"
)

#: File extensions of pictures, for telling a ``[[chart.png]]`` link from a link to a note.
_PICTURE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".svg", ".heic", ".tif", ".tiff"}
)


#: What every text warning tells you to do instead.
_FIX = "Upload the picture with 'pepper artifacts create --file FILE' and paste the line it prints."


@dataclass(frozen=True)
class NoteWarning:
    """One reference a reader won't see: what kind, the text as written, and what to do."""

    kind: str
    reference: str
    message: str


@dataclass(frozen=True)
class Scan:
    """What :func:`scan` found: warnings it can give itself, and picture ids to check."""

    warnings: tuple[NoteWarning, ...]
    picture_ids: tuple[str, ...]


def _escaped(text: str, position: int) -> bool:
    """Whether the character at ``position`` follows an odd run of backslashes."""
    run = 0
    while position - run > 0 and text[position - run - 1] == "\\":
        run += 1
    return run % 2 == 1


def _without_code(body: str) -> str:
    def blank(text: str) -> str:
        return re.sub(r"[^\n]", " ", text)

    text = _FENCE.sub(lambda m: blank(m.group(0)), body)
    # An escaped backtick opens no span; look again one character on.
    out: list[str] = []
    done = position = 0
    while (match := _INLINE_CODE.search(text, position)) is not None:
        if _escaped(text, match.start()):
            position = match.start() + 1
            continue
        out += [text[done : match.start()], blank(match.group(0))]
        done = position = match.end()
    out.append(text[done:])
    return "".join(out)


def _is_picture_file(target: str) -> bool:
    name = target.strip().rsplit("/", 1)[-1].lower()
    return any(name.endswith(ext) for ext in _PICTURE_EXTENSIONS)


def _label(label: str) -> str:
    return " ".join(label.split()).casefold()


def scan(body: str) -> Scan:
    """The references in ``body`` that won't show, in the order they appear."""
    text = _without_code(body)
    found: list[tuple[int, NoteWarning]] = []
    ids: list[str] = []

    for match in _EMBED.finditer(text):
        ref = match.group(0)
        found.append(
            (
                match.start(),
                NoteWarning(
                    kind="obsidian_embed",
                    reference=ref,
                    message=(f"{ref} is an Obsidian embed, which a note doesn't show. {_FIX}"),
                ),
            )
        )
    for match in _WIKILINK.finditer(text):
        if not _is_picture_file(match.group(1)):
            continue
        ref = match.group(0)
        found.append(
            (
                match.start(),
                NoteWarning(
                    kind="obsidian_embed",
                    reference=ref,
                    message=(
                        f"{ref} links to a picture file, which a note treats as a link to a "
                        f"note. {_FIX}"
                    ),
                ),
            )
        )

    def check_target(start: int, ref: str, target: str) -> None:
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        picture = _PICTURE_LINK.match(target)
        if picture is not None:
            ids.append(picture.group(1).lower())
            return
        local = target.lower().startswith("file:") or _DRIVE.match(target) is not None
        if not local and (target.startswith(("/", "#")) or _SCHEME.match(target)):
            return
        found.append(
            (
                start,
                NoteWarning(
                    kind="local_file",
                    reference=ref,
                    message=(
                        f"{ref} points at a file on your computer, which readers can't see. {_FIX}"
                    ),
                ),
            )
        )

    for match in _IMAGE.finditer(text):
        check_target(match.start(), match.group(0), match.group(1))

    # Labels match case-insensitively with runs of whitespace folded; the first wins.
    definitions: dict[str, str] = {}
    for match in _REF_DEF.finditer(text):
        definitions.setdefault(_label(match.group(1)), match.group(2))
    for match in _REF_IMAGE.finditer(text):
        label = match.group(2) or match.group(1)
        target = definitions.get(_label(label))
        if target is not None:
            check_target(match.start(), match.group(0), target)

    found.sort(key=lambda pair: pair[0])
    unique_ids = tuple(dict.fromkeys(ids))
    return Scan(warnings=tuple(w for _, w in found), picture_ids=unique_ids)


def not_in_room(picture_id: str, room: str | None) -> NoteWarning:
    """The warning for a picture link that the room can't show."""
    where = room or "this room"
    flag = shlex.quote(room) if room else "ROOM"
    return NoteWarning(
        kind="not_in_room",
        reference=f"/artifacts/{picture_id}/raw",
        message=(
            f"picture {picture_id} isn't saved in {where}, so other members see a broken "
            f"image. Upload it with 'pepper artifacts create --file FILE --room {flag}' and "
            "use the line it prints."
        ),
    )


def unchecked(picture_id: str) -> NoteWarning:
    """The warning for a picture link that couldn't be checked (the server didn't answer)."""
    return NoteWarning(
        kind="unchecked",
        reference=f"/artifacts/{picture_id}/raw",
        message=f"couldn't check whether picture {picture_id} is saved in this room.",
    )
