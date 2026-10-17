"""``pepper artifacts``: kinds, the numbered listing, file names and upload types.

The product calls the whole collection "Artifacts" (the app is titled that in
``agent/apps/registry.py``, the room page has an Artifacts surface, the settings page and the
help topic say it) and names the three kinds Note, Link and File (``KIND_LABELS`` in
``agent/artifacts/applet.py``). The stored discriminators are ``inline_text``, ``external_url``
and ``blob``; the CLI speaks the product words. Images, PDFs,
audio, recipes and research reports are not kinds of their own: an image is a File, a recipe or
a report is a Note.
"""

from __future__ import annotations

import contextlib
import mimetypes
import os
import re
import tempfile
from pathlib import Path, PurePath
from typing import Any

from pepper_cli.numbered_view import NumberedView

VIEW = NumberedView("artifacts-view.json", "artifact", "pepper artifacts list")

#: The CLI word for each kind, and the stored value the list route filters on (``--kind``
#: is restricted to these words by argparse).
KINDS: dict[str, str] = {"note": "inline_text", "link": "external_url", "file": "blob"}
_LABELS = {wire: word.capitalize() for word, wire in KINDS.items()}


def kind_label(wire: str) -> str:
    return _LABELS.get(wire, wire)


#: Types the server's upload gate accepts (``ALLOWED_UPLOAD_MIMES``), by extension, for the
#: ones ``mimetypes`` misses or names differently. The server is the authority; this only
#: picks the type to declare, and a file the server refuses comes back as a 415.
_MIME_BY_EXT = {
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".avif": "image/avif",
    ".webp": "image/webp",
    ".m4a": "audio/m4a",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".text": "text/plain",
}

UPLOAD_TYPES_HINT = "pictures, PDFs, plain text, audio clips and MP4 video"


def guess_mime(filename: str) -> str:
    ext = PurePath(filename).suffix.lower()
    if ext in _MIME_BY_EXT:
        return _MIME_BY_EXT[ext]
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


#: Device names Windows reserves in any folder, with or without an extension.
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def safe_filename(name: str, fallback: str = "artifact") -> str:
    """One path segment made from an artifact's name (which is user text and may hold
    separators, control characters or be empty), valid on Windows as well as POSIX: the
    characters Windows refuses (``<>:"|?*``) become ``_`` and a reserved device name gets one."""
    cleaned = re.sub(r'[\x00-\x1f\x7f/\\<>:"|?*]+', "_", name or "").strip(" .")
    cleaned = cleaned[:200].rstrip(" .")
    if cleaned.split(".")[0].strip().lower() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned or fallback


def write_download(target: Path, content: bytes, *, force: bool) -> None:
    """Write a download to ``target``.

    Without ``force`` the file is created exclusively, so an existing file or link at that
    name (including one that appeared after the caller's check) raises ``FileExistsError``
    and nothing else is touched. With ``force`` the bytes go to a temp file with an
    unpredictable name in the same folder, which then replaces ``target``.
    """
    if not force:
        created = False
        try:
            with target.open("xb") as fh:
                created = True
                fh.write(content)
        except BaseException:
            if created:
                with contextlib.suppress(OSError):
                    target.unlink()
            raise
        return
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        Path(tmp).replace(target)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise


def listing_row(item: dict[str, Any]) -> dict[str, Any]:
    """What a listing remembers about one artifact: enough for ``show``/``delete`` to name it."""
    return {k: item[k] for k in ("id", "name", "kind", "labels", "updated_at") if k in item}


def download_name(name: object, mime: object) -> str:
    """The file name to save under: the artifact's name, with the type's extension added when
    the name has none (a pasted screenshot is often just named "Screenshot")."""
    base = safe_filename(str(name or ""))
    if not PurePath(base).suffix and isinstance(mime, str) and mime:
        ext = next((e for e, m in _MIME_BY_EXT.items() if m == mime), None) or (
            mimetypes.guess_extension(mime) or ""
        )
        base += ext
    return base
