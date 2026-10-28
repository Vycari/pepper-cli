"""``pepper todos``: the numbered listing ``todos list`` and ``next`` both save.

``todos list`` numbers its rows in the order the server returned them, and ``next`` numbers the
todos it shows; either one remembers them (``todos-view.json`` next to the credentials) so
``todos done 3`` means "the third todo I just saw". The listing records which room it came from
(none is your own todos): a number from one room's list never acts on another's. Presentation
only; the order is the server's.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from pepper_cli.numbered_view import NumberedView

VIEW = NumberedView("todos-view.json", "todo", "pepper todos list")


def listing_rows(rows: list[Any]) -> list[dict[str, Any]]:
    """What the listing keeps of each todo: its id and title, from ``TodoEntry`` objects or the
    row dicts ``next`` shapes."""
    items = [dataclasses.asdict(r) if dataclasses.is_dataclass(r) else r for r in rows]
    return [{"id": r["id"], "title": r.get("title")} for r in items]


def meta(room_id: str | None) -> dict[str, Any]:
    """How the listing was scoped: the room's id, or ``None`` for your own todos."""
    return {"room_id": room_id}


def same_room(saved: object, current: str | None) -> bool:
    """Whether a listing saved for room ``saved`` is the one ``current`` (a room id, or
    ``None``) acts on. Ids are compared case-insensitively."""
    if not isinstance(saved, str) or current is None:
        return saved is None and current is None
    return saved.lower() == current.lower()
