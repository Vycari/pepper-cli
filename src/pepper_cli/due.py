"""Parsing a typed wall-clock due time, shared by the CLI and the terminal UI."""

from __future__ import annotations

import datetime as dt


def parse_due(raw: str, *, flag: str = "--due") -> tuple[str | None, str | None]:
    """Parse a wall-clock flag value into a timezone-aware ISO-8601 string.

    Accepts ``YYYY-MM-DD`` or ``YYYY-MM-DD HH:MM`` (and anything else
    :func:`datetime.fromisoformat` understands), interpreted in the
    machine's **local** timezone — the right zone for a CLI on the
    user's own machine, and what they mean when they type a time. The
    server stores it as UTC.

    ``flag`` only names the option in the error message. Shared by
    ``todos create --due`` and ``scheduled-tasks create/edit --at``: both take a
    local wall-clock time and both must send an *aware* value, since the
    server has no way to guess the client's zone.

    Returns ``(iso, None)`` on success or ``(None, error_message)`` so
    the caller can print + exit 2 without raising.
    """
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None, f"couldn't parse {flag} {raw!r} (try 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM')"
    # Naive input → interpret in local zone (astimezone on a naive dt
    # assumes local), giving an aware datetime the server can store.
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.isoformat(), None
