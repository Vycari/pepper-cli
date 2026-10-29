"""Editing a note in ``$EDITOR`` and the helpers around it, shared by ``pepper notes`` and
``pepper tui``.

Pure of any terminal output: a failure is an error string the caller shows its own way (the CLI
through ``render.error``, the TUI as a notice). ``planning/tui-room-todos-notes.md`` §6.8.
"""

from __future__ import annotations

import contextlib
import difflib
import itertools
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from pepper_cli.terminal_text import make_visible


def open_in_editor(initial: str, *, prefix: str = "pepper-") -> tuple[str | None, str | None]:
    """Write ``initial`` to a temp ``.md`` file named ``<prefix>…``, run $VISUAL/$EDITOR (vi
    fallback), and return ``(edited text, None)``, or ``(None, why)`` when the editor can't be
    parsed, can't be launched, or exits nonzero. Nothing is printed: the CLI reports ``why``
    through ``render.error`` and the TUI shows it as a notice. ``shlex.split`` so values like
    ``code --wait`` work."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    try:
        command = shlex.split(editor)
    except ValueError as exc:
        return None, f"couldn't parse $VISUAL/$EDITOR ({editor!r}): {exc}"
    if not command:
        return None, "$VISUAL/$EDITOR is empty — set one to edit skills"
    fd, path = tempfile.mkstemp(suffix=".md", prefix=prefix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(initial)
        try:
            # S603 suppressed: `command` is the user's own $EDITOR, invoked
            # without a shell — they already control what runs in their terminal.
            result = subprocess.run([*command, path], check=False)  # noqa: S603
        except OSError as exc:
            # FileNotFoundError (no such binary) and friends — a
            # misconfigured editor should be a friendly message, not a
            # traceback.
            return None, f"couldn't launch editor {editor!r}: {exc}"
        if result.returncode != 0:
            return None, f"editor exited with status {result.returncode}"
        return Path(path).read_text(encoding="utf-8"), None
    finally:
        os.unlink(path)


def texts_match(original: str, edited: str) -> bool:
    """True when ``edited`` is byte-identical to ``original``, or
    ``original`` with exactly one trailing newline appended.

    The one tolerance anywhere on the write path, and applied to
    ``$EDITOR`` output only (``--file``/stdin compare with ``==``) — every
    other case keeps
    ``render.render_markdown``'s byte-exact contract. vi/vim conventionally
    append a trailing newline on save, so a straight ``==`` would treat
    "opened the editor and changed nothing" as an edit and send a body
    differing from the stored one by whitespace alone, creating a spurious
    version row for a no-op ("send nothing if the text is unchanged").
    """
    return edited == original or edited == original + "\n"


def note_slug(name: str) -> str:
    """Filesystem-safe stem for the conflict-save filename: lowercase,
    every run of non-alphanumerics collapsed to one hyphen, trimmed. Good
    enough for a scratch file whose exact path the CLI prints back to the
    user — not a general-purpose slugify utility."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "note"


def conflict_fallback_dir() -> str:
    r"""Where a conflict copy goes when the working directory won't take it.

    POSIX: the system temp directory; the ``0o600`` mode on the file is what
    keeps it private there. Windows: a ``pepper`` folder under
    ``%LOCALAPPDATA%`` (or ``~\AppData\Local`` when that is unset or not
    absolute), created on demand, never ``%TEMP%``, which can be pointed at a
    shared folder such as ``C:\Temp``. Windows ignores the mode bits, so the
    file's protection is the folder's inherited ACL; the profile's default
    ACL limits access to the user, SYSTEM and Administrators. No ACL is set
    here, so a profile whose ACLs were loosened is not compensated for.
    """
    if sys.platform != "win32":
        return tempfile.gettempdir()
    local = os.environ.get("LOCALAPPDATA", "")
    base = Path(local) if local and Path(local).is_absolute() else Path.home() / "AppData" / "Local"
    directory = base / "pepper"
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory)


def conflict_dir_override() -> str | None:
    """The directory named by ``PEPPER_CONFLICT_DIR``, or ``None`` when it is unset or blank."""
    value = os.environ.get("PEPPER_CONFLICT_DIR", "").strip()
    return value or None


def save_conflict_copy(name: str, etag: str | None, text: str) -> tuple[str | None, str | None]:
    """Write ``text`` to ``./<note-slug>.<short-etag>.mine.md``
    (or into ``$PEPPER_CONFLICT_DIR``, created if missing, when that is set;
    the returned path is then absolute) and return ``(path, None)``. ``etag``
    is the server's *current* one as of the conflict, not the stale one the
    CLI sent.

    Never overwrites: two attempts against the same server version (a
    retry in this run, or a second ``pepper notes edit`` later) can hold
    different unsaved text, so the file is created exclusively and an
    existing one bumps a counter (``.mine.1.md``, ``.mine.2.md``, …). If
    the working directory can't be written at all, the copy goes to
    :func:`conflict_fallback_dir` (the system temp directory on POSIX, a
    per-user folder under ``%LOCALAPPDATA%`` on Windows) instead — likewise
    if the file opens but the write fails (a full disk). Returns ``(None, why)`` only when neither
    can be written, and prints nothing: the caller reports ``why`` in its
    own output shape (a text line, or folded into the one J8 line under
    ``--json``). The human caller's diff still shows the user's lines on
    stderr in that case.

    Created owner-only (``0o600``) whatever the umask on POSIX, since a
    note — a room's especially — is private text and the temp directory is
    shared with every local user. On Windows the mode only toggles the
    read-only bit and no ACL is set: the copy is as private as the folder
    it lands in. The working directory is the user's own choice and the
    fallback is a per-user folder under the profile (see
    :func:`conflict_fallback_dir`), both of which inherit their ACL from
    the user's profile or the directory; a cwd on a shared folder is
    therefore as readable as that folder. ``O_EXCL`` refuses an existing path,
    symlinks included, so a planted link in the temp directory can't
    redirect the write; ``O_NOFOLLOW`` says so twice where the platform
    has it.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)

    stem = f"{note_slug(name)}.{(etag or 'unknown')[:8]}.mine"
    last_error: OSError | RuntimeError | None = None
    for fallback in (False, True):
        try:
            if fallback:
                directory = Path(conflict_fallback_dir())
            elif (override := conflict_dir_override()) is not None:
                directory = Path(override).expanduser().absolute()
                directory.mkdir(parents=True, exist_ok=True)
            else:
                directory = Path()
        except (OSError, RuntimeError) as exc:
            # RuntimeError: ``expanduser`` on a ``~user`` that doesn't
            # exist. Either way the override is unusable; try the fallback.
            last_error = exc
            continue
        for n in itertools.count():
            path = directory / (f"{stem}.md" if n == 0 else f"{stem}.{n}.md")
            try:
                fd = os.open(path, flags, 0o600)
            except FileExistsError:
                continue
            except OSError as exc:
                last_error = exc
                break
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
            except OSError as exc:
                # Opened but couldn't be filled (a full disk, say): drop the
                # partial file this call just created — nothing else can own
                # it, O_EXCL made it ours — and try the next directory, so
                # the caller still gets its diff and recovery prompt.
                last_error = exc
                with contextlib.suppress(OSError):
                    path.unlink()
                break
            return str(path), None
    return None, f"couldn't save your text to a file: {last_error}"


def note_diff(current_body: str, mine: str) -> str:
    r"""Unified diff from the server's current body to the user's text, as text.

    The diff is taken over the raw bodies, so a change that is only an escape sequence or a
    carriage return still shows up before the user decides whether to overwrite; each diff
    line is then spelled out (``\x1b``) through :func:`make_visible` rather than left to be
    sent to the terminal live. Lines are split on ``\n`` alone so a stray control character
    cannot itself end a line. The CLI writes it straight to stderr rather than through
    ``render.error`` (a note's body can hold literal ``[``/``]`` that Rich's markup parser
    would misread); the TUI shows it in an ``InfoScreen`` as plain ``Text``."""

    def lines(text: str) -> list[str]:
        return re.findall(r"[^\n]*\n|[^\n]+$", text)

    diff = difflib.unified_diff(
        lines(current_body), lines(mine), fromfile="server (current)", tofile="yours"
    )
    return "".join(make_visible(line) for line in diff)
