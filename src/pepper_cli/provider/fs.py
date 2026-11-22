"""Read-only filesystem tools under one or more declared roots.

Three tools:

- ``fs_list`` — the entries directly under a relative directory;
- ``fs_read`` — one file's text, size-capped with a truncation marker;
- ``fs_grep`` — a literal, case-insensitive search **run here**, returning
  bounded snippets rather than whole files (the search happens on the
  device, so what crosses the wire is strictly less than the file).

Paths are relative to a declared root and are re-resolved with the OS's own
``realpath`` before anything is opened, which is what makes the escape matrix
— ``..``, a symlink pointing out, an absolute path, a root-prefix confusion
like ``/notes`` versus ``/notes-private`` — one check rather than four
string rules. The containment test is on *resolved* path components, never on
string prefixes: ``"/notes-private".startswith("/notes")`` is ``True`` and is
precisely the bug that test matrix exists to catch.

Everything refuses loudly. A refusal is an ``is_error`` result the model
reads as a sentence, not a silent empty answer — a tool that returns nothing
when it meant "you may not look there" teaches the model the folder is empty.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

#: Bytes of one file ``fs_read`` will return. Past it the content is cut and
#: a marker says so, because a silently truncated file is one the model will
#: reason about as though it had all of it.
MAX_READ_BYTES = 200_000

#: Entries one ``fs_list`` returns. A directory with 40k files is a listing
#: nobody reads and a prompt nobody can afford.
MAX_LIST_ENTRIES = 500

#: Matches one ``fs_grep`` returns, and the characters of context around
#: each. Both bound the snippets ``fs_grep`` returns.
MAX_GREP_MATCHES = 40
GREP_SNIPPET_CHARS = 240

#: Files ``fs_grep`` will open to read, per search, across every root. The cap
#: counts files actually opened: an ignored name, an external symlink or a
#: binary suffix is turned away without being opened and costs nothing here,
#: so a run of unusable entries cannot hide a readable file behind it. A search
#: that walks a node_modules tree is a search that hangs the turn.
MAX_GREP_FILES = 2_000

#: Directory entries ``fs_grep`` will look at, per search, across every root.
#: The second half of the budget: unusable entries are free against
#: :data:`MAX_GREP_FILES`, so something else has to bound the walk itself.
MAX_GREP_ENTRIES = 50_000

#: Longest ``path`` and ``query`` the provider will act on. The advertised
#: schema says "string" and nothing more, and the provider is the boundary
#: that actually enforces it.
MAX_PATH_CHARS = 4_096
MAX_QUERY_CHARS = 1_000

#: Directory and file names skipped by ``fs_list``'s walk and ``fs_grep``'s.
#: Not a security control — containment is — just the difference between a
#: useful answer and a wall of build artifacts.
IGNORED_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".DS_Store",
    }
)

#: Suffixes treated as binary without reading them. The NUL-byte sniff below
#: is the real test; this is the cheap one that avoids opening a 2 GB video
#: to discover it is a video.
BINARY_SUFFIXES = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff",
        ".pdf", ".zip", ".gz", ".bz2", ".xz", ".tar", ".7z", ".rar",
        ".mp3", ".mp4", ".mov", ".avi", ".mkv", ".wav", ".flac",
        ".so", ".dylib", ".dll", ".exe", ".bin", ".o", ".a",
        ".woff", ".woff2", ".ttf", ".otf", ".eot",
        ".pyc", ".pyo", ".class", ".jar", ".wasm",
        ".sqlite", ".sqlite3", ".db",
    }
)  # fmt: skip

#: Bytes sniffed for a NUL before deciding a file is binary.
_SNIFF_BYTES = 8_000


_OUTSIDE = "That path isn't inside the folder you shared, or doesn't exist."

#: ``errno`` values an ``O_NOFOLLOW`` walk raises when a component that was a
#: plain directory or file at check time is, by open time, a symlink, a
#: different kind of object, or gone: a replacement race, answered with the
#: same refusal as any other out-of-root path.
_RACE_ERRNOS = frozenset({errno.ELOOP, errno.ENOTDIR, errno.ENOENT})

#: Descriptor-relative, no-follow opens need ``O_NOFOLLOW`` and ``dir_fd``
#: support, which Windows lacks; there :func:`_open_file` falls back to
#: opening by path and re-checking the opened object.
_HAS_DIRFD = hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)


def _read_capped_info(fh: IO[bytes]) -> tuple[str, bool]:
    """Read at most :data:`MAX_READ_BYTES` from ``fh``; say whether it was cut.

    Reads from the open file rather than slicing ``read_bytes()``, so the cap
    bounds *memory and I/O* and not merely the returned string. The
    difference is not cosmetic: a valid multi-gigabyte text file inside a
    shared folder would otherwise be loaded in full before being trimmed —
    and ``fs_grep`` would do it again for every one of up to
    :data:`MAX_GREP_FILES` candidates.

    One byte past the cap is read deliberately: it is what distinguishes "the
    file ended here" from "this is where we stopped".
    """
    raw = fh.read(MAX_READ_BYTES + 1)
    truncated = len(raw) > MAX_READ_BYTES
    return raw[:MAX_READ_BYTES].decode("utf-8", errors="replace"), truncated


def _read_capped(fh: IO[bytes]) -> str:
    """:func:`_read_capped_info`'s text, plus a marker if the file was cut.

    Only a cut file earns the truncation marker.
    """
    text, truncated = _read_capped_info(fh)
    if truncated:
        text += f"\n…[truncated at {MAX_READ_BYTES} bytes]"
    return text


def _is_binary(fh: IO[bytes], path: Path) -> bool:
    """Binary by suffix, else by a NUL in the first :data:`_SNIFF_BYTES`.

    Leaves ``fh`` at the start, so the content read that follows sees the
    whole file.
    """
    if path.suffix.lower() in BINARY_SUFFIXES:
        return True
    head = fh.read(_SNIFF_BYTES)
    fh.seek(0)
    return b"\x00" in head


def _open_dir_fd(root: Path, parts: Sequence[str]) -> int:
    """A descriptor for ``root/<parts>``, opened one component at a time.

    Every step is an ``openat`` with ``O_NOFOLLOW`` relative to the previous
    descriptor, so the object reached is the one those names pointed at *as
    each was opened*. ``parts`` come from a path that ``realpath`` already
    resolved, so they hold no symlinks; if one has become a symlink since
    (the replacement race this exists for) the open fails instead of
    following it out of the root.
    """
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | _O_CLOEXEC)
    try:
        for part in parts:
            nxt = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _O_CLOEXEC,
                dir_fd=fd,
            )
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _race_refusal(exc: OSError) -> PathRefused | None:
    return PathRefused(_OUTSIDE) if exc.errno in _RACE_ERRNOS else None


def _open_file(root: Path, real: Path) -> IO[bytes]:
    """``real`` (resolved, inside ``root``) opened for reading, bound to what was opened.

    The file is opened ``O_NOFOLLOW | O_NONBLOCK`` relative to a descriptor for
    its parent, so a swapped-in symlink is refused and a swapped-in fifo cannot
    block; the caller's ``fstat`` check then refuses anything not regular.
    Without ``dir_fd`` support the fallback opens by path and then checks the
    opened object against a fresh resolve: still inside the root, and the same
    file.
    """
    try:
        if not _HAS_DIRFD:
            return _open_file_by_path(root, real)
        parts = real.relative_to(root).parts
        parent = _open_dir_fd(root, parts[:-1])
        try:
            fd = os.open(
                parts[-1] if parts else ".",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _O_CLOEXEC,
                dir_fd=parent,
            )
        finally:
            os.close(parent)
        return _wrap_regular(fd)
    except OSError as exc:
        refusal = _race_refusal(exc)
        if refusal is not None:
            raise refusal from exc
        raise


def _wrap_regular(fd: int) -> IO[bytes]:
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PathRefused("That isn't a readable file.")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _open_file_by_path(root: Path, real: Path) -> IO[bytes]:
    # Opening a directory by path raises PermissionError on Windows, which
    # would read as "permission denied"; say "not a file" on every platform.
    if real.is_dir():
        raise PathRefused("That isn't a readable file.")
    fd = os.open(real, os.O_RDONLY | _O_BINARY | _O_CLOEXEC)
    try:
        again = real.resolve(strict=True)
        if not _is_within(again, root) or not os.path.samestat(os.fstat(fd), again.stat()):
            raise PathRefused(_OUTSIDE)
    except BaseException:
        os.close(fd)
        raise
    return _wrap_regular(fd)


def _lstat_within(root: Path, real: Path) -> os.stat_result:
    """``lstat`` of ``real`` through its parent's descriptor; a symlink is a race."""
    try:
        if not _HAS_DIRFD:
            return real.stat()
        parts = real.relative_to(root).parts
        parent = _open_dir_fd(root, parts[:-1])
        try:
            result = os.stat(parts[-1] if parts else ".", dir_fd=parent, follow_symlinks=False)
        finally:
            os.close(parent)
    except OSError as exc:
        refusal = _race_refusal(exc)
        if refusal is not None:
            raise refusal from exc
        raise
    if stat.S_ISLNK(result.st_mode):
        raise PathRefused(_OUTSIDE)
    return result


def _scan_dir(root: Path, real: Path) -> list[tuple[str, bool]]:
    """``(name, is_dir)`` for ``real`` (resolved, inside ``root``), read through a descriptor.

    ``is_dir`` is read while the descriptor is open: a ``DirEntry`` from a
    descriptor scan cannot be asked after it is closed.
    """
    if not _HAS_DIRFD:
        with os.scandir(real) as it:
            return [(e.name, e.is_dir()) for e in it]
    try:
        fd = _open_dir_fd(root, real.relative_to(root).parts)
    except OSError as exc:
        refusal = _race_refusal(exc)
        if refusal is not None:
            raise refusal from exc
        raise
    try:
        with os.scandir(fd) as it:
            return [(e.name, e.is_dir()) for e in it]
    finally:
        os.close(fd)


class PathRefused(Exception):
    """An argument did not resolve to something inside a declared root.

    Carries a model-facing sentence. It deliberately does **not** name the
    resolved path or the root: a refusal that echoes back where it looked
    turns ``fs_read`` into a filesystem oracle for anyone who can make the
    model call it, which on a prompt-injected turn is whoever wrote the file
    it just read.
    """


@dataclass(frozen=True)
class _ResolvedRoot:
    """One ``--root`` the user declared, as an absolute real path."""

    label: str
    path: Path


def _is_within(candidate: Path, root: Path) -> bool:
    """Containment by path components, never by string prefix.

    ``Path.is_relative_to`` compares parts, so ``/notes-private`` is not
    inside ``/notes`` — which a ``startswith`` check would get wrong, and
    which is its own row in the escape matrix.

    Both sides are already ``realpath``-resolved by the caller, so a symlink
    inside the root pointing out of it fails here rather than being followed.
    On macOS the comparison is still exact-case: the filesystem is usually
    case-insensitive but ``realpath`` returns the *stored* spelling, so
    ``/Users/x/Notes`` resolving under a root declared as ``/Users/x/notes``
    is caught by the resolve, not by a case-folded compare we would then have
    to get right on three platforms.
    """
    return candidate == root or candidate.is_relative_to(root)


def _text_arg(arguments: dict[str, Any], key: str, limit: int) -> str:
    """``arguments[key]`` as a string, or a refusal; absent means empty.

    The advertised JSON schema says "string", but a caller can send anything,
    and ``str()`` on an array or a number turns a malformed call into a path
    or a search term. Only a missing key is tolerated; ``null`` is not a string.
    """
    value = arguments.get(key, "")
    if not isinstance(value, str):
        raise PathRefused(f"`{key}` must be a string.")
    if len(value) > limit:
        raise PathRefused(f"`{key}` is too long (at most {limit} characters).")
    if "\x00" in value:
        raise PathRefused(f"`{key}` must not contain a NUL character.")
    return value


@dataclass
class _GrepBudget:
    """One search's shared allowance of files opened and entries walked.

    ``exhausted`` is set only when the search wanted more than it was given,
    so a tree that fits exactly in the budget is still a complete search.
    """

    files_left: int = 0
    entries_left: int = 0
    exhausted: bool = False

    def __post_init__(self) -> None:
        self.files_left = MAX_GREP_FILES
        self.entries_left = MAX_GREP_ENTRIES


class FilesystemProvider:
    """The three tools, bound to the roots one CLI invocation declared."""

    def __init__(self, roots: Sequence[str | os.PathLike[str]]) -> None:
        resolved: list[_ResolvedRoot] = []
        seen: set[Path] = set()
        for raw in roots:
            path = Path(raw).expanduser()
            if not path.is_dir():
                raise PathRefused(f"{raw} is not a directory")
            real = path.resolve(strict=True)
            if real in seen:
                continue
            seen.add(real)
            resolved.append(_ResolvedRoot(label=real.name or str(real), path=real))
        if not resolved:
            raise PathRefused("no readable roots")
        self._roots = tuple(resolved)

    @property
    def roots(self) -> tuple[Path, ...]:
        return tuple(r.path for r in self._roots)

    # -- manifest ------------------------------------------------------

    def manifest(self) -> list[dict[str, Any]]:
        """The ``tools/list``-shaped manifest to declare on a chat request.

        ``readOnlyHint`` is ``True`` on all three and is the truth: nothing
        in this module opens a file for writing. The server would drop a
        tool without it anyway (no write tools are declared), which is the
        conservative default working in the right direction.

        The root *names* appear in the descriptions so the model can say
        "your notes folder" — a label, never a path, and never something the
        server may treat as a guarantee.
        """
        where = ", ".join(r.label for r in self._roots)
        return [
            {
                "name": "fs_list",
                "description": (
                    f"List the files and folders inside {where} on this machine. "
                    "`path` is relative to the folder root; omit it for the top level."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative directory, e.g. 'projects/2026'. Optional.",
                        }
                    },
                },
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "fs_read",
                "description": (
                    f"Read one text file from {where} on this machine. "
                    "`path` is relative to the folder root."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative file path, e.g. 'projects/2026/plan.md'.",
                        }
                    },
                    "required": ["path"],
                },
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "fs_grep",
                "description": (
                    f"Search {where} on this machine for a literal phrase, "
                    "case-insensitively, and return the matching lines with a little "
                    "context. The search runs on the machine — only the matches travel."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Literal text to look for."},
                        "path": {
                            "type": "string",
                            "description": "Relative directory to search under. Optional.",
                        },
                    },
                    "required": ["query"],
                },
                "annotations": {"readOnlyHint": True},
            },
        ]

    # -- dispatch ------------------------------------------------------

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Run one tool. Returns ``(text, is_error)``.

        Never raises for an ordinary refusal or an OS error: both are
        answers, and an exception here would leave the server's call to time
        out twenty seconds later with no reason attached.
        """
        try:
            if name == "fs_list":
                return self._list(_text_arg(arguments, "path", MAX_PATH_CHARS)), False
            if name == "fs_read":
                return self._read(_text_arg(arguments, "path", MAX_PATH_CHARS)), False
            if name == "fs_grep":
                return (
                    self._grep(
                        _text_arg(arguments, "query", MAX_QUERY_CHARS),
                        _text_arg(arguments, "path", MAX_PATH_CHARS),
                    ),
                    False,
                )
        except PathRefused as exc:
            return str(exc), True
        except OSError as exc:
            # Errno text, not the path: the same oracle argument as
            # ``PathRefused``.
            return f"Couldn't read that: {exc.strerror or 'I/O error'}.", True
        return f"No such tool: {name}.", True

    # -- resolution ----------------------------------------------------

    def _resolve(self, relative: str) -> Path:
        """Resolve ``relative`` inside exactly one root, or refuse.

        The order is the point. An absolute path is refused *before*
        resolution rather than joined (``Path("/a") / "/etc/passwd"`` is
        ``/etc/passwd`` — the join silently discards the root), and the
        containment check runs on the ``realpath`` so a symlink is followed
        first and judged second.
        """
        candidate = Path(relative)
        if candidate.is_absolute() or (os.name == "nt" and candidate.drive):
            raise PathRefused(
                "Paths must be relative to the shared folder — absolute paths aren't allowed."
            )
        for root, rest in self._candidates(candidate):
            try:
                real = (root.path / rest).resolve(strict=True)
            except (OSError, RuntimeError):
                # Missing, or a symlink loop. Try the next root; if none
                # matches, the refusal below is deliberately the same one an
                # out-of-root path gets, so "does this file exist" cannot be
                # answered for anything outside the grant.
                continue
            if _is_within(real, root.path):
                return real
        raise PathRefused("That path isn't inside the folder you shared, or doesn't exist.")

    def _candidates(self, relative: Path) -> Iterator[tuple[_ResolvedRoot, Path]]:
        """``(root, path-within-root)`` pairs to try, in order.

        With one root there is one answer. With several, results are labelled
        ``<root-name>/<rest>`` (see :meth:`_label_for`), so a path the model
        read off a previous result starts with a root's own name — try that
        reading first, then fall back to treating the whole thing as relative
        to each root in turn. Without the first reading, ``notes/plan.md``
        would resolve as ``<notes-root>/notes/plan.md`` and a file the model
        had just been shown would come back "not found".
        """
        parts = relative.parts
        if len(self._roots) > 1 and parts:
            for root in self._roots:
                if parts[0] == root.label:
                    yield root, Path(*parts[1:]) if len(parts) > 1 else Path()
        for root in self._roots:
            yield root, relative

    def _contained(self, path: Path) -> Path | None:
        """``path`` re-resolved, if it is still inside a granted root.

        The containment check every *opening* path goes through, not only the
        one a caller named. ``_resolve`` covers an argument; this covers a
        candidate the provider found for itself — which is how ``fs_grep``
        reaches a file, and a walked candidate can be a symlink whose target
        is outside the grant even though the link itself sits inside it
        (``os.walk(followlinks=False)`` stops a symlinked *directory* being
        descended and says nothing about a symlinked file).

        This is a check on a pathname, so it is only half of the guard: what
        it returns is then opened through :func:`_open_file` and friends,
        which re-bind the open to the object that was checked.
        """
        try:
            real = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        return real if any(_is_within(real, r.path) for r in self._roots) else None

    def _root_for(self, real: Path) -> Path:
        for root in self._roots:
            if _is_within(real, root.path):
                return root.path
        raise PathRefused(_OUTSIDE)

    def _label_for(self, path: Path) -> str:
        """``root-name/relative/path`` — how a result names a file.

        Rooted at the folder's own name rather than at an absolute path so a
        result never ships the user's home directory to the server.
        """
        for root in self._roots:
            if _is_within(path, root.path):
                rel = path.relative_to(root.path)
                return f"{root.label}/{rel}" if str(rel) != "." else root.label
        return path.name

    # -- tools ---------------------------------------------------------

    def _list(self, relative: str) -> str:
        if not relative and len(self._roots) > 1:
            # More than one ``--root``: the top level is the set of shared
            # folders, not the first one. Falling through to ``_roots[0]``
            # would make the second folder reachable only by a name the
            # model was never shown.
            names = "\n".join(f"{r.label}/" for r in self._roots)
            return f"Shared folders:\n{names}"
        target = self._resolve(relative) if relative else self._roots[0].path
        if not target.is_dir():
            raise PathRefused("That isn't a folder.")
        root = self._root_for(target)
        rows: list[str] = []
        truncated = False
        entries = sorted(_scan_dir(root, target), key=lambda e: (not e[1], e[0].lower()))
        for name, _is_dir in entries:
            if name in IGNORED_NAMES:
                continue
            if len(rows) >= MAX_LIST_ENTRIES:
                truncated = True
                break
            # A listing names a symlink's target as if it were a file here,
            # and ``stat()`` follows the link — so an outward link would
            # publish an external file's existence and size. Same containment
            # rule as everything else, applied before the name is printed;
            # the size then comes from the object the checked name still
            # points at, not from a second lookup by pathname.
            real = self._contained(target / name)
            if real is None:
                continue
            try:
                info = _lstat_within(self._root_for(real), real)
            except PathRefused:
                # Replaced by a link between the check and the stat: not
                # listed, like any other entry outside the grant.
                continue
            if stat.S_ISDIR(info.st_mode):
                rows.append(f"{name}/")
            elif stat.S_ISREG(info.st_mode):
                rows.append(f"{name} ({info.st_size} bytes)")
            # Anything else (fifo, socket, device) is simply not listed:
            # naming it invites the model to read it, and it cannot.
        if not rows:
            return f"{self._label_for(target)} is empty."
        body = "\n".join(rows)
        if truncated:
            body += f"\n…[truncated: more than {MAX_LIST_ENTRIES} entries; narrow the path]"
        return f"{self._label_for(target)}:\n{body}"

    def _read(self, relative: str) -> str:
        if not relative:
            raise PathRefused("Which file? `path` is required.")
        target = self._resolve(relative)
        with _open_file(self._root_for(target), target) as fh:
            if _is_binary(fh, target):
                raise PathRefused("That file is binary — there's no text to read.")
            text = _read_capped(fh)
        return f"{self._label_for(target)}:\n{text}"

    def _walk(self, start: Path, budget: _GrepBudget) -> Iterator[Path]:
        """Yield candidate files under ``start``, ignoring the usual noise.

        Every yielded entry spends one of ``budget``'s entries. When the
        budget is spent with entries still unvisited, ``budget.exhausted`` is
        set before the walk stops, so the caller can tell a search that ran
        out of walk from one that finished.

        ``followlinks`` stays off: a symlinked directory would otherwise be
        walked, and a cycle would make the search unbounded. That says
        nothing about a symlinked *file*, which this happily yields — the
        caller is responsible for putting every candidate through
        :meth:`_contained` before opening it.
        """
        for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_NAMES)
            for filename in sorted(filenames):
                if filename in IGNORED_NAMES:
                    continue
                if budget.entries_left <= 0:
                    budget.exhausted = True
                    return
                budget.entries_left -= 1
                yield Path(dirpath) / filename

    def _walk_all(self, starts: Sequence[Path], budget: _GrepBudget) -> Iterator[Path]:
        """:meth:`_walk` over several trees, sharing one budget."""
        for start in starts:
            yield from self._walk(start, budget)
            if budget.exhausted:
                return

    def _grep(self, query: str, relative: str) -> str:
        needle = query.strip()
        if not needle:
            raise PathRefused("What should I search for? `query` is required.")
        if relative:
            start = self._resolve(relative)
            if not start.is_dir():
                raise PathRefused("That isn't a folder.")
            starts: tuple[Path, ...] = (start,)
        else:
            # No path given means "everywhere I can see", which with several
            # ``--root``s is all of them — searching only the first would
            # answer "no matches" about a folder it never opened.
            starts = self.roots
        lowered = needle.lower()
        hits: list[str] = []
        budget = _GrepBudget()
        cut_files: list[str] = []
        for candidate in self._walk_all(starts, budget):
            if len(hits) >= MAX_GREP_MATCHES:
                break
            # Containment, before anything is opened. A walked candidate is a
            # path this provider chose, not one the caller named, so it has
            # had no realpath check yet — and a symlink inside a shared root
            # pointing at a readable file outside it is exactly how a search
            # would otherwise return content the user never granted.
            real = self._contained(candidate)
            if real is None or real.suffix.lower() in BINARY_SUFFIXES:
                continue
            if budget.files_left <= 0:
                budget.exhausted = True
                break
            budget.files_left -= 1
            try:
                with _open_file(self._root_for(real), real) as fh:
                    if _is_binary(fh, real):
                        continue
                    content, truncated = _read_capped_info(fh)
            except (OSError, PathRefused):
                # One unreadable, non-regular or just-replaced file does not
                # fail a search over a folder.
                continue
            label = self._label_for(real)
            if truncated:
                cut_files.append(label)
            for lineno, line in enumerate(content.splitlines(), start=1):
                if lowered not in line.lower():
                    continue
                snippet = line.strip()[:GREP_SNIPPET_CHARS]
                hits.append(f"{label}:{lineno}: {snippet}")
                if len(hits) >= MAX_GREP_MATCHES:
                    break
        notes = self._grep_notes(budget, cut_files)
        if not hits:
            if notes:
                return f"No matches for {needle!r} in the part that was searched.\n{notes}"
            return f"No matches for {needle!r}."
        body = "\n".join(hits)
        if len(hits) >= MAX_GREP_MATCHES:
            body += f"\n…[stopped at {MAX_GREP_MATCHES} matches; narrow the search]"
        return f"{body}\n{notes}" if notes else body

    @staticmethod
    def _grep_notes(budget: _GrepBudget, cut_files: list[str]) -> str:
        """The partial-result notices: nothing when the search was complete."""
        notes: list[str] = []
        if budget.exhausted:
            notes.append(
                f"…[search incomplete: stopped after {MAX_GREP_FILES} files opened or "
                f"{MAX_GREP_ENTRIES} entries seen, so later files were not searched; "
                "narrow the path]"
            )
        if cut_files:
            shown = ", ".join(cut_files[:5])
            more = f" and {len(cut_files) - 5} more" if len(cut_files) > 5 else ""
            notes.append(
                f"…[search incomplete: only the first {MAX_READ_BYTES} bytes of {shown}{more} "
                "were searched]"
            )
        return "\n".join(notes)


__all__ = [
    "BINARY_SUFFIXES",
    "GREP_SNIPPET_CHARS",
    "IGNORED_NAMES",
    "MAX_GREP_ENTRIES",
    "MAX_GREP_FILES",
    "MAX_GREP_MATCHES",
    "MAX_LIST_ENTRIES",
    "MAX_PATH_CHARS",
    "MAX_QUERY_CHARS",
    "MAX_READ_BYTES",
    "FilesystemProvider",
    "PathRefused",
]
