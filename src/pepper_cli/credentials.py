"""Read / write the on-disk credentials file.

Stored at ``$XDG_CONFIG_HOME/pepper/credentials.toml`` (defaulting to
``~/.config/pepper/credentials.toml`` per the XDG Base Directory spec).
Single user-scope file holding the server URL and the bearer token from
``pepper login``.

POSIX permission rule: the file must be ``0600`` (read+write by the
owner only). On load we refuse to read a file with wider permissions
and tell the user how to tighten them — protects against a token leak
if the dotfile accidentally ends up world-readable (e.g. tarred up,
synced to a shared drive, committed to a public dotfiles repo). The
write path always creates the file with ``0600``.

We hand-write the TOML rather than depending on a third-party writer:
the on-disk shape is two scalar keys, easier to keep stable than to
import a library for. ``tomllib`` (stdlib) handles the read.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pepper_cli.secret import SecretStr


class CredentialsError(Exception):
    """Raised when the credentials file is unreadable, missing, or has
    insecure permissions.

    Distinct from ``FileNotFoundError`` etc. so the CLI can surface a
    consistent "log in again" message at the call site without
    sniffing built-in exception types.
    """


@dataclass(frozen=True)
class Credentials:
    """Parsed credentials file. Both fields are required to be useful.

    ``server_url`` is the API base (e.g. ``https://pepper.example.com``)
    without a trailing slash. The CLI joins ``/api/v1/...`` paths to it
    directly, so any trailing slash here would produce ``//api/v1/...``.

    ``token`` is wrapped in :class:`SecretStr` so the dataclass's
    default ``repr`` never prints the plaintext. Callers reach the
    plaintext via ``token.get_secret_value()`` — at the Authorization
    header build site and the TOML serializer, and nowhere else.
    """

    server_url: str
    token: SecretStr


#: Allowed ``--profile`` names. No path separators, so a name can never
#: traverse out of ``profiles/``; ``.`` and ``..`` match the regex and are
#: rejected separately. No leading ``-``, so an option that follows a
#: valueless ``--profile`` (``pepper --profile --help``, ``--profile --prof x``)
#: is never taken as the name.
_PROFILE_NAME = re.compile(r"(?!-)[A-Za-z0-9._-]+")

#: The active profile for this process, set once by :func:`set_profile` from
#: the CLI's leading ``--profile NAME``. ``None`` is the default login at the
#: unchanged path.
_profile: str | None = None


def validate_profile(name: str) -> str:
    """Return ``name`` if it is a legal profile name, else raise ``ValueError``."""
    if name in {".", ".."} or not _PROFILE_NAME.fullmatch(name):
        raise ValueError(
            f"Invalid profile name {name!r}: use only letters, digits, '.', '_' and '-' "
            "(not starting with '-', and not '.' or '..')."
        )
    return name


def set_profile(name: str | None) -> None:
    """Select the profile every later path lookup follows (``None`` = default).

    Raises ``ValueError`` for an illegal name, leaving the previous selection
    in place.
    """
    global _profile  # noqa: PLW0603 — process-wide CLI selection, set once in main()
    _profile = None if name is None else validate_profile(name)


def active_profile() -> str | None:
    """The selected profile name, or ``None`` for the default login."""
    return _profile


def command_prefix() -> str:
    """``pepper``, plus ``--profile NAME`` when one is selected: the start of a command the CLI
    suggests, so following it runs as the same login (an agent's is always a named profile).
    Profile names are validated by :func:`set_profile`, so the name needs no quoting."""
    return f"pepper --profile {_profile}" if _profile else "pepper"


def _config_dir() -> Path:
    """Return the directory the credentials file lives in.

    With a profile selected this is ``<config>/pepper/profiles/<name>``;
    otherwise ``<config>/pepper``, exactly as before profiles existed.

    Honors ``$XDG_CONFIG_HOME`` for users who relocate their config
    tree (mine, for one); falls back to ``~/.config`` per XDG. Uses
    ``Path.home()`` rather than ``$HOME`` so a shell-level ``HOME``
    override (used by tests) is picked up.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    root = base / "pepper"
    return root if _profile is None else root / "profiles" / _profile


def credentials_path() -> Path:
    """Full path to the credentials file. Useful for the CLI's
    ``logout`` output ("removed <path>") and for tests."""
    return _config_dir() / "credentials.toml"


def _check_perms(path: Path) -> None:
    """Raise :class:`CredentialsError` if the file is world- or
    group-readable on POSIX.

    No-op on Windows — NTFS ACLs don't map cleanly to UNIX mode bits,
    and the equivalent check belongs in a Windows-specific code path
    once we have one. The risk profile is the same; we just can't
    enforce it identically.
    """
    if sys.platform.startswith("win"):
        return
    mode = path.stat().st_mode & 0o777
    # Anything with the group / world read or write bits set is too
    # permissive. 0o077 == "group + world rwx mask"; non-zero AND
    # means *some* permission bit outside owner-only is set.
    if mode & 0o077:
        raise CredentialsError(
            f"Refusing to read {path}: permissions are {oct(mode)} but must be 0600. "
            f"Run: chmod 600 {path}"
        )


def load() -> Credentials:
    """Read + parse the credentials file.

    Raises:
        CredentialsError: file missing, malformed, has insecure perms,
            or is missing one of the required keys.

    """
    path = credentials_path()
    if not path.exists():
        raise CredentialsError(f"No credentials at {path}. Run 'pepper login' first.")
    _check_perms(path)
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise CredentialsError(f"Credentials file {path} is not valid TOML: {exc}") from exc
    server_url = data.get("server_url")
    token = data.get("token")
    if not isinstance(server_url, str) or not isinstance(token, str):
        raise CredentialsError(
            f"Credentials file {path} is missing 'server_url' and/or 'token'. "
            f"Run 'pepper login' to recreate it."
        )
    return Credentials(server_url=server_url.rstrip("/"), token=SecretStr(token))


def save(server_url: str, token: SecretStr) -> Path:
    """Write the credentials file with ``0600`` permissions.

    Two-step write — ``open`` with ``O_CREAT|O_EXCL`` would fight an
    existing file; we instead ``unlink`` any old file and re-open with
    a fresh, mode-locked descriptor so the file is *never* visible to
    the filesystem with default (often 0644) permissions. ``os.open``
    + ``mode=0o600`` ensures the file's initial permission bits are
    correct from the first byte — a ``Path.write_text`` + later
    ``chmod`` has a race window where the file is world-readable.

    Returns the path that was written for the caller to surface.
    """
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # ``chmod`` the parent too — leaking the directory listing leaks
    # less than the file contents, but a public ``~/.config/pepper/``
    # still signals "this user has Pepper credentials" to anyone with
    # a directory traversal bug. Keep it private. ``OSError`` swallowed
    # because permission to chmod a parent we don't fully own is rare
    # but not impossible (NFS / setgid dirs); the file's own 0600 is
    # the load-bearing protection so a directory failure isn't fatal.
    with contextlib.suppress(OSError):
        path.parent.chmod(0o700)

    if path.exists():
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(_serialize(server_url, token))
    except Exception:
        # If the write fails partway, leave nothing behind — a half-
        # written file would fail to parse on next load and just
        # confuse the user. Best-effort cleanup; if the unlink itself
        # fails, surfacing the original exception is more useful than
        # masking it with a follow-up OSError.
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        raise

    # Defense-in-depth — make sure the mode is what we think even if
    # the umask was permissive between open() and fdopen().
    if not sys.platform.startswith("win"):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def delete() -> bool:
    """Remove the credentials file. Returns ``True`` if a file existed
    and was removed, ``False`` if there was nothing to delete. Doesn't
    touch the parent directory."""
    path = credentials_path()
    if not path.exists():
        return False
    path.unlink()
    return True


def _serialize(server_url: str, token: SecretStr) -> str:
    """Format the credentials TOML.

    Two scalar string keys, hand-written so we don't pull in a TOML
    writer for one tiny file. The token is unwrapped via
    ``.get_secret_value()`` only here at the I/O boundary — every
    other site holds the :class:`SecretStr`. Escapes backslashes and
    double-quotes per the TOML basic-string spec — covers the
    realistic edge cases (Windows paths in URLs are nonsense; quotes
    in tokens shouldn't happen with our base32 alphabet, but defense
    in depth).
    """
    return (
        "# pepper-cli credentials — do not share. 0600 on disk; "
        "`pepper logout` removes it.\n"
        f'server_url = "{_escape_basic(server_url)}"\n'
        f'token = "{_escape_basic(token.get_secret_value())}"\n'
    )


def _escape_basic(s: str) -> str:
    """Escape a string for a TOML basic string (double-quoted).

    Covers the subset that matters for our two fields: backslash,
    double-quote, control characters. Hand-written rather than
    pulling in ``tomli_w`` because the input space is tiny.
    """
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ord(ch) < 0x20 or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return "".join(out)
