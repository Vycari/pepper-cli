"""Picture references in a note body that readers won't see.

``notes create`` and ``notes edit`` save whatever they are given, then call
:func:`scan` on the saved text and warn about each reference that will not
show. Three shapes, all easy to write by accident in a draft from elsewhere:

- an Obsidian embed (``![[chart.svg]]``), or a ``[[chart.svg]]`` link to a
  picture file, which a note does not show;
- a markdown image whose target is a file on your computer
  (``![](attachments/chart.svg)``);
- a picture link (``![](/artifacts/<id>/raw)``), returned by :func:`scan` for
  the caller to check against the room, since only the server knows where a
  picture is saved.

Text inside code (fenced blocks and inline spans) is left out, so a note that
shows the syntax as an example is not warned about. Pure text work, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Fenced blocks (```` ``` ```` or ``~~~``, closed by the same fence or the end of the
#: text), then inline code spans. Both are blanked before the scan.
_FENCE = re.compile(
    r"^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}\1[`~]*[ \t]*$|\Z)", re.MULTILINE | re.DOTALL
)
_INLINE_CODE = re.compile(r"(`+)(?!`).+?(?<!`)\1(?!`)", re.DOTALL)

_EMBED = re.compile(r"!\[\[[^\]\n]+\]\]")
_WIKILINK = re.compile(r"(?<!!)\[\[([^\]|#\n]+)(?:[#|][^\]\n]*)?\]\]")
_IMAGE = re.compile(r"!\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^)\s]+)[^)\n]*\)")
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


def _without_code(body: str) -> str:
    def blank(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    return _INLINE_CODE.sub(blank, _FENCE.sub(blank, body))


def _is_picture_file(target: str) -> bool:
    name = target.strip().rsplit("/", 1)[-1].lower()
    return any(name.endswith(ext) for ext in _PICTURE_EXTENSIONS)


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
    for match in _IMAGE.finditer(text):
        target = match.group(1)
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        picture = _PICTURE_LINK.match(target)
        if picture is not None:
            ids.append(picture.group(1).lower())
            continue
        local = target.lower().startswith("file:") or _DRIVE.match(target) is not None
        if not local and (target.startswith(("/", "#")) or _SCHEME.match(target)):
            continue
        ref = match.group(0)
        found.append(
            (
                match.start(),
                NoteWarning(
                    kind="local_file",
                    reference=ref,
                    message=(
                        f"{ref} points at a file on your computer, which readers can't see. {_FIX}"
                    ),
                ),
            )
        )

    found.sort(key=lambda pair: pair[0])
    unique_ids = tuple(dict.fromkeys(ids))
    return Scan(warnings=tuple(w for _, w in found), picture_ids=unique_ids)


def not_in_room(picture_id: str, room: str) -> NoteWarning:
    """The warning for a picture link that the room can't show."""
    return NoteWarning(
        kind="not_in_room",
        reference=f"/artifacts/{picture_id}/raw",
        message=(
            f"picture {picture_id} isn't saved in {room}, so other members see a broken "
            f"image. Upload it with 'pepper artifacts create --file FILE --room {room}' and use "
            "the line it prints."
        ),
    )


def unchecked(picture_id: str) -> NoteWarning:
    """The warning for a picture link that couldn't be checked (the server didn't answer)."""
    return NoteWarning(
        kind="unchecked",
        reference=f"/artifacts/{picture_id}/raw",
        message=f"couldn't check whether picture {picture_id} is saved in this room.",
    )
