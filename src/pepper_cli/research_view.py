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

#: ``edit --clear``: what can be emptied. ``schedule`` turns the schedule off and keeps the
#: run history (``planning/research-projects.md`` §7).
CLEARABLE = ("filters", "schedule")


def listing_row(item: dict[str, Any]) -> dict[str, Any]:
    """What a listing remembers about one item: enough to name it in a later message."""
    return {k: item[k] for k in ("id", "title", "state") if k in item}


#: The line the research engine appends to a stored report: ``<!-- pepper-research-report-
#: metadata:v1 {...} -->``. It is for the server's parser, not for a person reading the report.
REPORT_MARKER_PREFIX = "<!-- pepper-research-report-metadata:"


def strip_report_marker(body: str) -> str:
    """``body`` without its trailing engine marker line; everything else stays byte for byte.

    Only the last non-blank line is looked at, and only when it starts with
    :data:`REPORT_MARKER_PREFIX`; blank lines after it go with it."""
    kept = body.rstrip()
    start = kept.rfind("\n") + 1
    if kept[start:].startswith(REPORT_MARKER_PREFIX):
        return body[:start]
    return body


def list_command(project: str, room: str | None) -> str:
    """The ``research-items list`` command for this project, as it can be pasted."""
    import shlex

    where = f"--room {shlex.quote(room)} " if room else ""
    return f"pepper research-items list {where}--project {shlex.quote(project)}"


def another_ledger(project: str, room: str | None, *, project_differs: bool) -> str:
    """The refusal for a number from a listing of another project or room, with the command
    that lists this one."""
    which = "another project" if project_differs else "another room"
    return (
        f"that number is from the last 'research-items list', which was of {which} — "
        f"run '{list_command(project, room)}' first and use its numbers"
    )
