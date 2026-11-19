"""Pictures in a note body: finding local references and pointing them at uploads.

Pure text work, no I/O, so ``notes create --image`` / ``notes edit --image`` can read the
files and talk to the server in ``cli.py`` and leave the rewriting here, where it is tested
on its own.

A note shows a picture written as ``![alt](/artifacts/<id>/raw)``. A draft written
elsewhere refers to the same picture by its file: a markdown image whose target is a local
path (``![alt](attachments/chart.svg)``), or an Obsidian embed (``![[chart.svg]]``,
``![[chart.svg|alt]]``, ``![[chart.svg|300]]``). :func:`rewrite` replaces every reference
whose file name matches an uploaded file, and leaves everything else exactly as written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote

#: ``![[target]]`` or ``![[target|label]]``. ``target`` may carry a ``#heading`` part,
#: which never applies to a picture and is dropped with it.
_EMBED = re.compile(r"!\[\[([^\]|\n]+?)(?:\|([^\]\n]*))?\]\]")

#: ``![alt](target)`` or ``![alt](target "title")``, with an optional ``<...>`` around a
#: target holding spaces. Only a target with no scheme and no leading ``/`` is local.
_IMAGE = re.compile(
    r"!\[(?P<alt>(?:\\.|[^\]\\\n])*)\]"
    r"\(\s*(?P<target><[^>\n]+>|(?:[^\s()\\]|\\.|\([^\s()]*\))+)"
    r"(?P<title>\s+\"[^\"\n]*\")?\s*\)"
)

#: Fenced blocks (closed by the same fence, or running to the end) and inline code spans.
#: A reference inside code is an example, so :func:`rewrite` leaves it alone.
_FENCE = re.compile(
    r"^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}\1[`~]*[ \t]*$|\Z)", re.MULTILINE | re.DOTALL
)
_INLINE_CODE = re.compile(r"(`+)(?!`).+?(?<!`)\1(?!`)", re.DOTALL)

#: An Obsidian embed's ``|300`` or ``|300x200`` is a display size, not alt text.
_SIZE = re.compile(r"\d+(?:x\d+)?")

_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


@dataclass(frozen=True)
class Uploaded:
    """One local picture after upload: its file name and the link that shows it."""

    filename: str
    artifact_id: str

    @property
    def link(self) -> str:
        return f"/artifacts/{self.artifact_id}/raw"


@dataclass(frozen=True)
class Rewrite:
    """What :func:`rewrite` did: the new body, and the uploads no reference named."""

    body: str
    replaced: int
    unreferenced: tuple[Uploaded, ...]


def _escape_alt(alt: str) -> str:
    return alt.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _local_name(target: str) -> str | None:
    """The file name a local image target points at, or ``None`` for a URL or a site path."""
    target = target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    if not target or target.startswith(("/", "#")) or _SCHEME.match(target):
        return None
    path = unquote(target.split("#", 1)[0].split("?", 1)[0])
    name = PurePosixPath(path.replace("\\", "/")).name
    return name or None


def _key(name: str) -> str:
    # Obsidian resolves an embed by file name, case-insensitively on the platforms most
    # vaults live on; match the same way so ``![[Chart.SVG]]`` finds ``chart.svg``.
    return name.casefold()


def _code_spans(text: str) -> list[tuple[int, int]]:
    spans = [m.span() for m in _FENCE.finditer(text)]
    rest = _FENCE.sub(lambda m: " " * len(m.group(0)), text)
    spans += [m.span() for m in _INLINE_CODE.finditer(rest)]
    return spans


def _in_code(position: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


def rewrite(body: str, uploads: list[Uploaded]) -> Rewrite:
    """Point every local reference to an uploaded file at its link.

    A markdown image keeps its alt text and title. An embed's label becomes the alt text,
    unless it is a size (``|300``), which is dropped. A reference to a file that was not
    uploaded is left as it is.
    """
    by_name = {_key(u.filename): u for u in uploads}
    code = _code_spans(body)
    used: set[str] = set()
    count = 0

    def embed(match: re.Match[str]) -> str:
        nonlocal count
        if _in_code(match.start(), code):
            return match.group(0)
        target, label = match.group(1), match.group(2)
        name = _local_name(target.split("#", 1)[0])
        upload = by_name.get(_key(name)) if name else None
        if upload is None:
            return match.group(0)
        alt = "" if label is None or _SIZE.fullmatch(label.strip()) else label.strip()
        used.add(_key(upload.filename))
        count += 1
        return f"![{_escape_alt(alt)}]({upload.link})"

    def image(match: re.Match[str]) -> str:
        nonlocal count
        if _in_code(match.start(), code):
            return match.group(0)
        name = _local_name(match.group("target"))
        upload = by_name.get(_key(name)) if name else None
        if upload is None:
            return match.group(0)
        used.add(_key(upload.filename))
        count += 1
        return f"![{match.group('alt')}]({upload.link}{match.group('title') or ''})"

    # Embeds first, then images over the result, each against its own text's code
    # spans: an embed's replacement is a different length, so offsets move.
    out = _EMBED.sub(embed, body)
    code = _code_spans(out)
    out = _IMAGE.sub(image, out)
    missing = tuple(u for u in uploads if _key(u.filename) not in used)
    return Rewrite(body=out, replaced=count, unreferenced=missing)


def append_unreferenced(body: str, uploads: tuple[Uploaded, ...]) -> str:
    """``body`` with a picture line for each upload, each on its own line at the end."""
    if not uploads:
        return body
    lines = "\n\n".join(f"![]({u.link})" for u in uploads)
    stripped = body.rstrip("\n")
    return f"{stripped}\n\n{lines}\n" if stripped else f"{lines}\n"
