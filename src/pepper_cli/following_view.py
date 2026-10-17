"""``pepper following``: read-only helpers over the Following app's data document.

The document is ``GET /api/v1/apps/following/data`` (what ``pepper apps show following`` prints);
its ``data`` holds ``items`` (one per followed thing), ``counts`` and ``browse``. Nothing here
writes. Field names are the server's; unknown fields pass through untouched.
"""

from __future__ import annotations

import re
from typing import Any

#: The app's filter chips, in its own order (server ``group`` values).
GROUPS = ("Sports", "Shows", "Reading", "Podcasts", "Events")

_UUID_RE = re.compile(r"^[0-9a-f]{8}(?:-?[0-9a-f]{4}){3}-?[0-9a-f]{12}$", re.IGNORECASE)


class LookupFailure(Exception):
    """A name/id that matched nothing or too much. ``code`` is a J8 error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def data_of(doc: dict[str, Any]) -> dict[str, Any]:
    """The Following document from an ``apps/following/data`` answer (``{"rev", "data"}``)."""
    data = doc.get("data")
    return data if isinstance(data, dict) else {}


def items_of(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [i for i in data.get("items") or [] if isinstance(i, dict)]


def short_id(ref: str) -> str:
    """A handle short enough to type: ``feeds:feed:<uuid>`` gives the uuid's first 8 characters,
    ``sports:team:nfl:21`` gives ``nfl:21``."""
    parts = ref.split(":", 2)
    tail = parts[2] if len(parts) == 3 else ref
    return tail[:8] if _UUID_RE.match(tail) else tail


def resolve_group(data: dict[str, Any], query: str) -> str:
    """The canonical group for a case-insensitive prefix (``--group pod`` is ``Podcasts``)."""
    known = list(dict.fromkeys([*GROUPS, *((data.get("counts") or {}).get("by_group") or {})]))
    q = query.strip().lower()
    hits = [g for g in known if g.lower().startswith(q)] if q else []
    exact = [g for g in known if g.lower() == q]
    hits = exact or hits
    if len(hits) == 1:
        return hits[0]
    raise LookupFailure("usage", f"--group {query!r} is not one of: {', '.join(known)}")


def filter_group(items: list[dict[str, Any]], group: str | None) -> list[dict[str, Any]]:
    return [i for i in items if i.get("group") == group] if group else items


def grouped(items: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Items by group in the app's chip order (unknown groups last); each group keeps the
    server's order, which is live first, then latest activity."""
    order = {g: n for n, g in enumerate(GROUPS)}
    names = sorted(
        {str(i.get("group") or "Other") for i in items}, key=lambda g: (order.get(g, 99), g)
    )
    return [(g, [i for i in items if str(i.get("group") or "Other") == g]) for g in names]


def resolve_item(items: list[dict[str, Any]], query: str) -> dict[str, Any]:
    """One followed thing by full ``ref``, short id, ``ref`` suffix, or name.

    Tried in that order, each stage only when the one before matched nothing: exact ref or
    short id; then a ref ending in the query; then a case-insensitive name prefix; then a
    name substring. More than one match at a stage is ``ambiguous``, never a guess.
    """
    q = query.strip().lower()
    if not q:
        raise LookupFailure("usage", "ITEM is empty")

    def ref(i: dict[str, Any]) -> str:
        return str(i.get("ref") or "")

    def name(i: dict[str, Any]) -> str:
        return str(i.get("name") or "").lower()

    stages = (
        [i for i in items if ref(i).lower() == q or short_id(ref(i)).lower() == q],
        [i for i in items if len(q) >= 4 and ref(i).lower().endswith(q)],
        [i for i in items if name(i).startswith(q)],
        [i for i in items if q in name(i)],
    )
    for hits in stages:
        if len(hits) == 1:
            return hits[0]
        if hits:
            options = "; ".join(f"{i.get('name')} ({short_id(ref(i))})" for i in hits[:6])
            raise LookupFailure("ambiguous", f"{query!r} matches {len(hits)} follows: {options}")
    raise LookupFailure("not_found", f"You don't follow anything matching {query!r}.")
