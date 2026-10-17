"""``pepper feed``: the server's ranked stream, mapped to rows for display.

The server ranks, folds and decides (``data.feed`` of the Following app's document, best first);
the CLI fetches and renders. Nothing in this module scores, orders, folds or reads a time back
from a label. It only picks the server's list out of the document, tidies each entry's shape,
and filters it by a plain field for ``--group``.
"""

from __future__ import annotations

import os
import sys
import webbrowser
from datetime import datetime, tzinfo
from typing import Any

from pepper_cli.numbered_view import NumberedView

VIEW = NumberedView("feed-view.json", "feed item", "pepper feed list")

#: Shown by ``pepper feed list`` when the document has no ``feed`` key (a server that predates
#: pepper#4852). The CLI never falls back to ranking by itself.
NOT_RANKED_NOTE = "this Pepper server doesn't rank the feed yet (needs pepper#4852)"


#: Keys a newer server adds to a Sports entry (``league`` ... ``broadcast``) or a podcast entry
#: (``episode_id``). Copied when present and non-empty, so an older server's rows are unchanged.
EXTRA_KEYS = (
    "league",
    "records",
    "headline",
    "note",
    "status",
    "venue",
    "broadcast",
    "episode_id",
)


def server_feed(data: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The server's ranked entries, in the server's order; ``None`` when it sends none at all."""
    raw = data.get("feed")
    if not isinstance(raw, list):
        return None
    return [row(e) for e in raw if isinstance(e, dict)]


def row(entry: dict[str, Any]) -> dict[str, Any]:
    """One server entry with every display key present and its ``more`` entries tidied the
    same way. Values are the server's, untouched."""
    out = {
        "source": entry.get("source"),
        "ref": entry.get("ref") or "",
        "group": entry.get("group") or "Other",
        "kind": entry.get("kind"),
        "title": str(entry.get("title") or "").strip(),
        "url": entry.get("url") or None,
        "when_label": entry.get("when_label") or "",
        "published_at": entry.get("published_at") or None,
        "starts_at": entry.get("starts_at") or None,
        "summary": entry.get("summary") or None,
        "new": bool(entry.get("new")),
        "upcoming": bool(entry.get("upcoming")),
        "live": bool(entry.get("live")),
        "fold": entry.get("fold") or None,
    } | {k: entry[k] for k in EXTRA_KEYS if entry.get(k)}
    more = entry.get("more")
    out["more"] = (
        [row(m) | {"more": [], "fold": None} for m in more if isinstance(m, dict)]
        if (isinstance(more, list))
        else []
    )
    return out


def only_group(entries: list[dict[str, Any]], group: str | None) -> list[dict[str, Any]]:
    """``--group``: a plain filter on the entries' ``group`` field."""
    return [e for e in entries if e["group"] == group] if group else entries


def identity(entry: dict[str, Any]) -> dict[str, Any]:
    """What ``pepper feed list`` remembers about a numbered row: enough to name it again."""
    return {
        "id": f"{entry['ref']}\n{entry['url'] or ''}\n{entry['title']}",
        "ref": entry["ref"],
        "url": entry["url"],
        "title": entry["title"],
    }


def find(entries: list[dict[str, Any]], remembered: dict[str, Any]) -> dict[str, Any] | None:
    """The entry (top-level or under a ``more``) with a remembered row's ref, url and title."""
    for e in entries:
        for cand in (e, *e["more"]):
            if (cand["ref"], cand["url"], cand["title"]) == (
                remembered.get("ref"),
                remembered.get("url"),
                remembered.get("title"),
            ):
                return cand
    return None


def exact_time(entry: dict[str, Any], zone: tzinfo | None) -> datetime | None:
    """The entry's own exact time (``starts_at`` for an event, else ``published_at``) in
    ``zone``, the account's timezone; ``None`` when the server sent neither or it is unreadable.
    This only displays a timestamp the server gave; it never orders anything."""
    stamp = entry.get("starts_at") or entry.get("published_at")
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(str(stamp)).astimezone(zone)
    except ValueError:
        return None


def open_browser(url: str) -> bool:
    """Open ``url`` in the default browser; False when none could be.

    Nothing is launched in an SSH session (``SSH_CONNECTION`` or ``SSH_TTY`` set), nor on
    Linux and the BSDs with no ``DISPLAY`` or ``WAYLAND_DISPLAY``: there ``webbrowser`` can
    fall back to a text browser such as lynx and take over the terminal, which ``ssh -X``
    (``DISPLAY`` set, no graphical opener on the host) does not prevent. An explicit
    ``BROWSER`` is the user's own choice and is always honoured (an editor's remote session
    sets it to a helper that opens the link on the local machine). Everywhere else
    ``webbrowser.open`` decides.
    """
    env = os.environ
    if not env.get("BROWSER"):
        if env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
            return False
        no_display = not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))
        if no_display and sys.platform.startswith(("linux", "freebsd", "openbsd", "netbsd")):
            return False
    try:
        return bool(webbrowser.open(url))
    except webbrowser.Error:
        return False
