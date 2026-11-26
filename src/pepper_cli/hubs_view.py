"""``pepper hubs``: render the hubs the server sends.

The CLI only fetches and renders. What a hub holds, which of its rooms and items a person may
see, and who it is offered to are the server's (``/api/v1/hubs``); this module prints them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pepper_cli.render import one_line as _s
from pepper_cli.render import out_line as _line

if TYPE_CHECKING:
    from pepper_cli.api_client import HubDetail, Hubs

#: A hub's share level, in words. A level this client does not know is printed as sent.
SHARE_WORDS = {"view": "can view", "edit": "can edit"}

#: A room's notification level, in words.
NOTIFY_WORDS = {"all": "all messages", "mentions": "@mentions only", "muted": "muted"}

_INCOMPLETE = "[dim](Pepper couldn't check all of your rooms just now, so this may be short.)[/dim]"


def rows(result: Hubs) -> list[dict[str, Any]]:
    """Every hub in ``result`` as one flat list: the caller's own, then taken, then offered.

    ``standing`` is ``owner``, ``taken`` or ``offered``.
    """
    own = [
        {
            "id": hub.id,
            "name": hub.name,
            "standing": "owner",
            "owner_name": None,
            "pinned": hub.pinned,
            "shared_level": hub.shared_level,
        }
        for hub in result.hubs
    ]
    others = [
        {
            "id": hub.id,
            "name": hub.name,
            "standing": standing,
            "owner_name": hub.owner_name,
            "pinned": False,
            "shared_level": None,
        }
        for standing, hubs in (("taken", result.shared), ("offered", result.offers))
        for hub in hubs
    ]
    return own + others


def listing(result: Hubs) -> None:
    """The caller's hubs, then the ones they took, then the ones on offer."""
    if not (result.hubs or result.shared or result.offers):
        _line("[dim](no hubs yet: make one with 'pepper hubs create NAME')[/dim]")
    for hub in result.hubs:
        notes = [
            *(["pinned"] if hub.pinned else []),
            *([f"shared: people {_share(hub.shared_level)}"] if hub.shared_level else []),
        ]
        tail = f"  [dim]{', '.join(notes)}[/dim]" if notes else ""
        _line(f"[bold]{_s(hub.name)}[/bold]{tail}  [dim]{_s(hub.id)}[/dim]")
    for title, hubs in (("Shared with you", result.shared), ("Offered to you", result.offers)):
        if not hubs:
            continue
        _line("")
        _line(title)
        for hub in hubs:
            _line(
                f"[bold]{_s(hub.name)}[/bold]  from {_s(hub.owner_name)}  [dim]{_s(hub.id)}[/dim]"
            )
    if result.offers:
        _line("[dim]Take one with 'pepper hubs take HUB'.[/dim]")
    if not result.rooms_complete:
        _line(_INCOMPLETE)


def _share(level: str) -> str:
    return _s(SHARE_WORDS.get(level, level))


def detail(hub: HubDetail) -> None:
    """One hub: whose it is, what the caller may do, its rooms and the caller's items."""
    _line(f"[bold]{_s(hub.name)}[/bold]  [dim]{_s(hub.id)}[/dim]")
    if hub.role == "owner":
        if hub.shared_level:
            _line(f"Yours. Shared with the people in its rooms: they {_share(hub.shared_level)}.")
        else:
            _line("Yours. Not shared.")
        if hub.pinned:
            _line("Pinned: your chat with Pepper is focused on this hub.")
    else:
        whose = f"{_s(hub.owner_name)}'s" if hub.owner_name else "Someone else's"
        # A role this client does not know is shown as the least it could mean.
        _line(f"{whose}. You {_share(hub.role if hub.role in SHARE_WORDS else 'view')}.")
    _line("")
    _line("Rooms")
    if not hub.rooms:
        _line("  [dim](none)[/dim]")
    for room in hub.rooms:
        level = room.notification_level
        tail = f"  [dim]{_s(NOTIFY_WORDS.get(level, level))}[/dim]" if level else ""
        _line(f"  {_s(room.name)}{tail}")
    _line("")
    _line("Your notes and files")
    if not hub.items:
        _line("  [dim](none)[/dim]")
    for item in hub.items:
        kind = f"  [dim]{_s(item.kind)}[/dim]" if item.kind else ""
        _line(f"  {_s(item.name)}{kind}  [dim]{_s(item.id)}[/dim]")
    if not hub.rooms_complete:
        _line(_INCOMPLETE)
