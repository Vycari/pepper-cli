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
import ipaddress
import os
import re
import stat
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from pepper_cli.secret import SecretStr


class CredentialsError(Exception):
    """Raised when the credentials file is unreadable, missing, or has
    insecure permissions.

    Distinct from ``FileNotFoundError`` etc. so the CLI can surface a
    consistent "log in again" message at the call site without
    sniffing built-in exception types.
    """


class InvalidServerUrlError(CredentialsError):
    """The stored ``server_url`` is not one the token may be sent to (:func:`validate_server_url`).

    A subclass so the CLI can report it as a usage error (exit 2, like an unparseable
    ``--server``) rather than as "log in again".
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


#: Set to ``1`` to let a token travel over plain ``http`` to a host that is not this machine
#: (a self-hosted server on a trusted private network). Loopback needs no opt-in.
ALLOW_INSECURE_HTTP_ENV = "PEPPER_ALLOW_INSECURE_HTTP"


def is_loopback_host(host: str) -> bool:
    """``True`` for ``localhost``, ``*.localhost`` and loopback IP literals (``127.0.0.0/8``,
    ``::1``): the hosts a plain-``http`` token never leaves the machine to reach."""
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def insecure_http_allowed() -> bool:
    """Whether the user opted in to plain ``http`` for a non-loopback server."""
    return os.environ.get(ALLOW_INSECURE_HTTP_ENV, "").strip().lower() in {"1", "true", "yes"}


def validate_server_url(url: str) -> str:
    """Return ``url`` as the canonical API base the bearer token may be sent to.

    The canonical form is ``scheme://host[:port]`` with no trailing slash: every request joins
    ``/api/v1/...`` to it directly. Raises ``ValueError`` (with a message fit for the terminal,
    never echoing userinfo) for anything else:

    * a scheme other than ``http`` / ``https``, or no hostname;
    * userinfo (``user:password@``), a query, a fragment, a path other than ``/``, or a port
      that does not parse;
    * plain ``http`` to a host that is not loopback, unless
      :data:`ALLOW_INSECURE_HTTP_ENV` is set, because the token would cross the network in
      the clear.
    """
    if not url or any(ch.isspace() or ord(ch) < 0x20 or ch == "\x7f" for ch in url):
        raise ValueError(
            "Invalid server URL: it is empty or contains spaces or control characters."
        )
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"Invalid server URL: {exc}.") from exc
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(
            "Invalid server URL: it must start with https:// (or http:// for a local server)."
        )
    host = parts.hostname
    if not host:
        raise ValueError("Invalid server URL: it has no host name.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError("Invalid server URL: it must not carry a user name or password.")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise ValueError("Invalid server URL: it must not have a query (?) or fragment (#).")
    if parts.path not in {"", "/"}:
        raise ValueError(
            "Invalid server URL: give only the server's address, with no path "
            f"(for example {scheme}://{parts.netloc})."
        )
    if scheme == "http" and not is_loopback_host(host) and not insecure_http_allowed():
        raise ValueError(
            f"Refusing to send your token to {host} over plain http. Use https://, or set "
            f"{ALLOW_INSECURE_HTTP_ENV}=1 for a server on a network you trust."
        )
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return f"{scheme}://{netloc}"


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
    except UnicodeDecodeError as exc:
        raise CredentialsError(f"Credentials file {path} is not valid UTF-8: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise CredentialsError(f"Credentials file {path} is not valid TOML: {exc}") from exc
    server_url = data.get("server_url")
    token = data.get("token")
    if not isinstance(server_url, str) or not isinstance(token, str):
        raise CredentialsError(
            f"Credentials file {path} is missing 'server_url' and/or 'token'. "
            f"Run 'pepper login' to recreate it."
        )
    # The file is user-editable, so its URL is checked before the token is ever sent to it.
    try:
        server_url = validate_server_url(server_url)
    except ValueError as exc:
        raise InvalidServerUrlError(
            f"Credentials file {path}: {exc} Run `pepper login --server <url>` to replace it."
        ) from exc
    return Credentials(server_url=server_url, token=SecretStr(token))


def save(server_url: str, token: SecretStr) -> Path:
    """Write the credentials file with ``0600`` permissions, replacing any old one atomically.

    The new content goes to a uniquely named temporary file in the same directory, created by
    ``mkstemp`` with mode ``0600`` (so it is never visible with wider permissions, whatever the
    umask), flushed and fsynced, and only then moved over the destination with ``os.replace``.
    Until that rename the previous file is untouched, so a failed or interrupted write leaves
    the last good login in place; the temporary file is removed on any failure.

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

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".credentials.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(_serialize(server_url, token))
            fh.flush()
            os.fsync(fh.fileno())
        # Defense-in-depth: ``mkstemp`` already creates 0600, independent of the umask.
        if not sys.platform.startswith("win"):
            tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
    except BaseException:
        # Best-effort cleanup; surfacing the original exception is more useful than masking
        # it with a follow-up OSError.
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise
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
