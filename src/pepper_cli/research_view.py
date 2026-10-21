"""``pepper research-projects`` and ``pepper research-items``: the numbered ledger listing.

The product calls them "research projects" (the help guide's "Research projects" section) and
a project's "ledger" of what Pepper found; ``research-items`` is the ledger's own noun, scoped to
a project by ``--project`` (``planning/research-projects.md`` §3.1). An item is addressed by its
place in the last ``research-items list`` (a number) or by an item id: the server's ``seq`` is
global, not per project, so it is never shown as a number to type (§5.2).
"""

from __future__ import annotations

from typing import Any

from pepper_cli.numbered_view import NumberedView

VIEW = NumberedView(
    "research-items.json", "research item", "pepper research-items list --project PROJECT"
)

#: The ledger states the items route accepts (``ITEM_STATES`` on the server). ``--state`` is
#: restricted to these so a typo is a usage error before a round trip.
ITEM_STATES = (
    "candidate",
    "accepted",
    "rejected",
    "delivered",
    "taken",
    "superseded",
    "retracted",
)

#: ``--status`` on ``research-projects list``: the server filters on one status, and ``all``
#: sends none.
LIST_STATUSES = ("active", "paused", "all")

#: ``create --policy``: fixed here so a typo is a usage error before a round trip. The server
#: needs one, and ``drip`` is the one its own chat tool and ``create`` default to (§3).
POLICIES = ("drip", "digest")
DEFAULT_POLICY = "drip"

#: ``edit --clear``: what can be emptied. ``schedule`` joins it with the schedule routes
#: (``planning/research-projects.md`` §7, a later phase).
CLEARABLE = ("filters",)


def listing_row(item: dict[str, Any]) -> dict[str, Any]:
    """What a listing remembers about one item: enough to name it in a later message."""
    return {k: item[k] for k in ("id", "title", "state") if k in item}


ANOTHER_PROJECT = "the last 'research-items list' was of another project — list this one first"
