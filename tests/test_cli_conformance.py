"""Conformance test for the CLI contract (``planning/cli-information-architecture.md`` §8).

The CLI is judged by what it does, not how it is built: commands are discovered by
running ``pepper --help`` / ``pepper <noun> --help`` / ``pepper <noun> <verb> --help``
and reading the text, and output is checked by running ``cli.main([...])`` against a
fake server (an ``httpx`` mock transport) with stdout and stderr captured. Nothing
here reads argparse internals, so it survives a parser-library switch (pepper-cli#21).

Today's tree predates the contract, so most checks start with their violations listed
in a ``*_PENDING`` set. Each set is a ratchet in both directions:

* a leaf that violates a rule and is not listed fails the test (new code must conform);
* a listed leaf that has started to conform fails too, so the row gets deleted in the
  phase that fixed it (§8: "a phase is done when its rows are gone").

``*_EXEMPT`` / ``*_NA`` sets are permanent and carry their reason.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import itertools
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from pepper_cli import api_client, cli, credentials, feed, following_search
from pepper_cli.secret import SecretStr

# ---------------------------------------------------------------------------
# The contract's vocabularies

#: §2 nouns, plus the views/session commands of §3 rule 4.
NOUNS = frozenset(
    {
        "rooms",
        "topics",
        "messages",
        "attachments",
        "members",
        "apps",
        "sharing",
        "trajectories",
        "todos",
        "scheduled-tasks",
        "skills",
        "notes",
        # `research-projects` and `research-items` are the product's words "Research projects"
        # and their ledger (planning/research-projects.md §2).
        "research-projects",
        "research-items",
        # `artifacts` and `preferences` are the product's own words "Artifacts" and "Preferences"
        # (planning/cli-information-architecture.md §2).
        "artifacts",
        "preferences",
        "agents",
        "calendar",
        "next",
        # `brief` is a view (IA §3 rule 4): a daily read, like `next`.
        "brief",
        # `following` is the product app's noun (list/show/search/create/delete); `feed` is a
        # noun too (list/show): the server's ranked stream of what you follow.
        "following",
        "feed",
        "chat",
        "login",
        "logout",
        "whoami",
        "takeout",
        "tui",
    }
)

#: §7: nouns retired by the 0.2.0 rename. They stay registered (as stubs, in the phase
#: that renames each) so the implicit-chat fallback never sends them to Pepper.
RENAMED = frozenset({"groups", "trajectory", "agent-skill"})

#: §3.1 verbs.
VERBS = frozenset(
    {
        "list",
        "show",
        "search",
        "watch",
        "activity",
        "create",
        "edit",
        "delete",
        "invite",
        "remove",
        "share",
        "revoke",
        "install",
        "uninstall",
        "leave",
        "post",
        "react",
        "unreact",
        "archive",
        "unarchive",
        "mute",
        "unmute",
        "pause",
        "resume",
        "renew",
        "done",
        "dismiss",
        "reopen",
        "download",
        "run",
        # `runs OBJECT` is the run history of an object that runs (IA §3.1).
        "runs",
        "guide",
        # Research projects (IA §3.1): `steer` queues guidance for an object's next run;
        # `take` and `reconsider` mark a ledger item.
        "steer",
        "take",
        "reconsider",
    }
)

#: §3.1: these prompt on a TTY and need ``--yes`` otherwise.
DESTRUCTIVE = frozenset({"delete", "remove", "revoke", "uninstall", "leave"})

# ---------------------------------------------------------------------------
# Exemptions (permanent) and pending lists (shrink to empty by phase 3)

#: J1: leaves that do not take ``--json`` at all. ``chat`` is not here: one-shot
#: ``chat MESSAGE`` takes the flag (§5.4); only its REPL path (no message) ignores
#: ``--json`` and ``PEPPER_JSON``, which ``test_the_repl_...`` pins.
#: ``tui`` is a full-screen interactive app with no data output to put in JSON.
JSON_EXEMPT = frozenset({"login", "tui"})

#: §5.4 fill list and every leaf that has no ``--json`` today (empty since phase 2d).
JSON_PENDING: frozenset[str] = frozenset()

#: Leaves where a room is not a meaningful scope (§6 class N), or is the object itself.
ROOM_NA = {
    "rooms list": "a room is the object, not a scope",
    "rooms show": "a room is the object, not a scope",
    "rooms create": "a room is the object, not a scope",
    "rooms edit": "a room is the object, not a scope",
    "rooms archive": "a room is the object, not a scope",
    "rooms unarchive": "a room is the object, not a scope",
    "rooms leave": "a room is the object, not a scope",
    "rooms mute": "a room is the object, not a scope",
    "rooms unmute": "a room is the object, not a scope",
    "following list": "your follows are account-wide; a room's follows live on its own page",
    "following show": "your follows are account-wide; a room's follows live on its own page",
    "following search": "your follows are account-wide; a room's follows live on its own page",
    "following create": "your follows are account-wide; a room's follows live on its own page",
    "following delete": "your follows are account-wide; a room's follows live on its own page",
    "feed list": "the server ranks your own follows; a room's follows are not part of it",
    "feed show": "the server ranks your own follows; a room's follows are not part of it",
    "brief": "your own morning brief; the server exposes no room's brief to a PAT here",
    "artifacts show": "reads any artifact the viewer can see; the id carries the room",
    "artifacts download": "reads any artifact the viewer can see; the id carries the room",
    "preferences show": "your own account's Calendar preferences",
    "preferences edit": "your own account's Calendar preferences",
    "login": "session command",
    "logout": "session command",
    "whoami": "session command",
    "tui": "opens the whole app; rooms are chosen inside it",
    "chat": "talking to Pepper in a room is `messages post --mention`",
    "takeout": "exports the account, not a room",
    "agents guide": "prints the bundled guide; nothing about it is per room",
    "skills create": "a room holds a grant of someone's skill, never a skill",
    "skills edit": "a room holds a grant of someone's skill, never a skill",
    "skills delete": "a room holds a grant of someone's skill, never a skill",
}

#: Leaves that should take ``--room`` and do not yet. §6 is the per-command authority;
#: the ``groups`` leaves take the room as a positional until phase 2a/2b.
ROOM_PENDING: frozenset[str] = frozenset(
    {
        # Personal commands only until the server ships the room twins (research-projects.md
        # phase 3, tracked by #85); each of these takes --room then.
        "research-projects list",
        "research-projects show",
        "research-projects runs",
        "research-projects create",
        "research-projects edit",
        "research-projects run",
        "research-projects steer",
        "research-projects pause",
        "research-projects resume",
        "research-items list",
        "research-items show",
        "research-items take",
        "research-items reconsider",
    }
)

#: Leaves whose verb is not in §3.1 yet (old spellings; phase 2a-2d renames them).
VERB_PENDING = frozenset(
    {
        "scheduled-tasks thread",
    }
)

#: Destructive verbs (``DESTRUCTIVE``) lacking ``--yes`` (empty since phase 2c).
YES_PENDING: frozenset[str] = frozenset()

#: Leaves with a positional ``group`` (rule 2: none survives). Phase 2a/2b.
GROUP_POSITIONAL_PENDING = frozenset({})

#: Leaves whose help still says "group" or "channel" (§2: the CLI says "room").
#: Leaves under a ``RENAMED`` noun are not checked; their whole noun goes away.
WORDING_PENDING = frozenset(
    {
        # `--group` is the Following app's own filter chip (Sports, Shows, ...), not a room.
        "following list",
        "feed list",
        "notes edit",
        "notes list",
        "notes create",
        "notes delete",
        "notes search",
        "notes show",
        "scheduled-tasks create",
        "scheduled-tasks list",
        "scheduled-tasks pause",
        "scheduled-tasks resume",
        "skills list",
        "skills revoke",
        "skills share",
        "skills show",
        "todos create",
        "todos dismiss",
        "todos done",
        "todos edit",
        "todos list",
        "todos reopen",
    }
)

#: ``--to`` must list ``room`` as a choice (empty since phase 2c).
TO_ROOM_PENDING: frozenset[str] = frozenset()

#: §8: list leaves (J3) that do not emit the envelope yet (empty since phase 2d).
ENVELOPE_PENDING: frozenset[str] = frozenset()

#: Renamed nouns that are still real commands, not yet stubs (empty since phases 2a/2c).
RENAMED_PENDING: frozenset[str] = frozenset()

# ---------------------------------------------------------------------------
# Discovery: from --help text only


def _help(*argv: str) -> str:
    """The ``--help`` text for ``argv`` (wide enough that argparse never wraps)."""
    buf = io.StringIO()
    saved = os.environ.get("COLUMNS")
    os.environ["COLUMNS"] = "240"
    try:
        with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
            cli.main([*argv, "--help"])
    finally:
        if saved is None:
            del os.environ["COLUMNS"]
        else:
            os.environ["COLUMNS"] = saved
    return buf.getvalue()


def _children(text: str) -> list[str]:
    """The subcommand names in a usage line: ``{a,b,c} ...``."""
    match = re.search(r"\{([\w,-]+)\} \.\.\.", " ".join(text.split()))
    if match:
        return match.group(1).split(",")
    # No brace list (the root parser sets a metavar): read the command listing instead.
    names: list[str] = []
    section = False
    for line in text.splitlines():
        if line.rstrip().endswith(":") and not line.startswith(" "):
            section = line.startswith("positional arguments")
        elif (
            section and (m := re.match(r"^ {4}(\S+)", line)) and not re.match(r"^ {4}COMMAND", line)
        ):
            names.append(m.group(1))
    return names


def _discover() -> dict[str, str]:
    """Every leaf command (``"next"``, ``"messages list"``) mapped to its help text."""
    leaves: dict[str, str] = {}
    for noun in _children(_help()):
        text = _help(noun)
        if not text.strip():  # a renamed-noun stub: it answers on stderr, with no help
            continue
        verbs = _children(text)
        if not verbs:
            leaves[noun] = text
        for verb in verbs:
            leaves[f"{noun} {verb}"] = _help(noun, verb)
    return leaves


LEAVES = _discover()


def _flags(text: str) -> set[str]:
    """Long option names a help text declares (read from the option lines only)."""
    found: set[str] = set()
    for line in text.splitlines():
        if re.match(r"^ {2}-", line):
            found.update(re.findall(r"--[\w-]+", re.split(r"\s{2,}", line.strip())[0]))
    return found


def _positionals(text: str) -> set[str]:
    """Positional argument names, from the ``positional arguments:`` section."""
    names: set[str] = set()
    section = False
    for line in text.splitlines():
        if line.rstrip().endswith(":") and not line.startswith(" "):
            section = line.startswith("positional arguments")
        elif section and (m := re.match(r"^ {2}(\S+)", line)):
            names.add(m.group(1).strip("{}[]").split(",")[0])
    return names


def _option_help(text: str, flag: str) -> str:
    """The help text of one option: its line plus the continuation lines right after it.

    Collection stops at the first line that is not a continuation (the next option, a
    blank line, a section heading), so a later option's wrapped help is never included.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^ {2}-", line) and flag in re.findall(
            r"--[\w-]+", re.split(r"\s{2,}", line.strip())[0]
        ):
            out = [line]
            out.extend(itertools.takewhile(lambda nxt: nxt.startswith(" " * 8), lines[i + 1 :]))
            return " ".join(out)
    return ""


def _words_ok(text: str) -> bool:
    """No "group" or "channel" in a help text (the wire value ``groups_service`` is allowed)."""
    body = text.replace("groups_service", "").replace("pepper groups", "")
    return re.search(r"group|channel", body, re.IGNORECASE) is None


def _noun(leaf: str) -> str:
    return leaf.split(maxsplit=1)[0]


def _verb(leaf: str) -> str:
    return leaf.rsplit(maxsplit=1)[-1]


def _ratchet(rule: str, failing: set[str], pending: frozenset[str] | set[str]) -> None:
    """Leaves failing ``rule`` must be exactly the pending ones."""
    new = sorted(failing - set(pending))
    stale = sorted(set(pending) - failing)
    assert not new, f"{rule}: leaves break the contract and are not on the pending list: {new}"
    assert not stale, (
        f"{rule}: these leaves now conform; delete them from the pending list: {stale}"
    )


# ---------------------------------------------------------------------------
# Static checks (from --help)


def test_discovery_sees_the_commands() -> None:
    # A discovery that quietly found nothing would make every check below vacuous.
    assert len(LEAVES) > 60
    assert {"next", "messages list", "notes show", "login"} <= set(LEAVES)


def test_every_noun_is_known() -> None:
    nouns = {_noun(leaf) for leaf in LEAVES}
    assert nouns <= NOUNS | RENAMED, sorted(nouns - NOUNS - RENAMED)


def test_every_verb_is_known() -> None:
    failing = {leaf for leaf in LEAVES if " " in leaf and _verb(leaf) not in VERBS}
    _ratchet("verb", failing, VERB_PENDING)


def test_every_leaf_takes_json() -> None:
    failing = {leaf for leaf, text in LEAVES.items() if "--json" not in _flags(text)} - JSON_EXEMPT
    _ratchet("--json", failing, JSON_PENDING)
    assert JSON_EXEMPT.isdisjoint(JSON_PENDING)
    assert all("--json" not in _flags(LEAVES[leaf]) for leaf in JSON_EXEMPT)


def test_room_is_a_flag_where_it_means_something() -> None:
    failing = {leaf for leaf, text in LEAVES.items() if "--room" not in _flags(text)} - set(ROOM_NA)
    _ratchet("--room", failing, ROOM_PENDING)
    assert all(reason for reason in ROOM_NA.values())


#: Nouns whose leaves each take a required container flag (IA §3 rule 2).
CONTAINER = {"research-items": "--project"}


def test_container_flag_is_required_on_every_leaf() -> None:
    for leaf, text in LEAVES.items():
        flag = CONTAINER.get(_noun(leaf))
        if flag is None:
            continue
        usage = " ".join(text.split("\n\n", 1)[0].split())
        assert flag in _flags(text), leaf
        # Required options print bare in the usage line; optional ones are bracketed.
        assert re.search(rf"(?<!\[){flag}\b", usage), (leaf, usage)


#: IA §3.5: verbs that start asynchronous work and answer a receipt take `--wait` and
#: `--timeout`, and no other leaf has them.
WAITS = frozenset({"research-projects run"})


def test_only_the_asynchronous_verbs_take_wait() -> None:
    have = {leaf for leaf, text in LEAVES.items() if "--wait" in _flags(text)}
    assert have == WAITS
    for leaf in WAITS:
        assert "--timeout" in _flags(LEAVES[leaf]), leaf
    assert not {leaf for leaf, text in LEAVES.items() if "--timeout" in _flags(text)} - WAITS


def test_wait_codes_are_in_the_error_vocabulary() -> None:
    # IA J8: `timeout` and `run_failed` exist for `--wait` only (J9: exit 4 for timeout).
    assert {"timeout", "run_failed"} <= cli._ERROR_CODES


def test_destructive_verbs_take_yes() -> None:
    failing = {
        leaf
        for leaf, text in LEAVES.items()
        if _verb(leaf) in DESTRUCTIVE and "--yes" not in _flags(text)
    }
    _ratchet("--yes", failing, YES_PENDING)


def test_no_positional_group() -> None:
    failing = {leaf for leaf, text in LEAVES.items() if "group" in _positionals(text)}
    _ratchet("positional group", failing, GROUP_POSITIONAL_PENDING)


def test_help_says_room_not_group_or_channel() -> None:
    failing = {
        leaf for leaf, text in LEAVES.items() if _noun(leaf) not in RENAMED and not _words_ok(text)
    }
    _ratchet("wording", failing, WORDING_PENDING)


def test_to_lists_room_as_a_choice() -> None:
    failing = {
        leaf
        for leaf, text in LEAVES.items()
        if "--to" in _flags(text)
        and not re.search(r"Choices:[^.]*\broom\b", _option_help(text, "--to"))
    }
    _ratchet("--to room", failing, TO_ROOM_PENDING)


def test_option_help_stops_at_the_next_option() -> None:
    # A later option's wrapped help must not count as --to's: here only --mode says "room".
    text = (
        "options:\n"
        "  --to {topic,email}    Where to deliver.\n"
        "                        Choices: topic, email.\n"
        "  --mode {a,b}          How to deliver.\n"
        "                        Choices: a, b, room.\n"
        "\n"
        "examples:\n"
        "        pepper x --to room\n"
    )
    to_help = _option_help(text, "--to")
    assert (
        " ".join(to_help.split()) == "--to {topic,email} Where to deliver. Choices: topic, email."
    )
    assert not re.search(r"Choices:[^.]*\broom\b", to_help)
    assert _option_help(text, "--mode").endswith("Choices: a, b, room.")
    assert _option_help(text, "--missing") == ""


def test_pending_lists_name_real_leaves() -> None:
    for pending in (
        JSON_PENDING,
        ROOM_PENDING,
        VERB_PENDING,
        YES_PENDING,
        GROUP_POSITIONAL_PENDING,
        WORDING_PENDING,
        TO_ROOM_PENDING,
        ENVELOPE_PENDING,
        set(ROOM_NA),
    ):
        assert pending <= set(LEAVES), sorted(pending - set(LEAVES))


# ---------------------------------------------------------------------------
# Behaviour checks: run main() against a fake server

_G = "00000000-0000-0000-0000-0000000000a1"
_T = "00000000-0000-0000-0000-0000000000b1"
_M = "00000000-0000-0000-0000-0000000000c1"
_P = "00000000-0000-0000-0000-0000000000d1"
_TRAJ = "00000000-0000-0000-0000-0000000000f1"
_NOTE = "00000000-0000-0000-0000-0000000000e1"
_TASK = "00000000-0000-0000-0000-0000000000a8"
_A = "00000000-0000-0000-0000-0000000000a9"
_RP = "00000000-0000-0000-0000-0000000000d3"
_RUN = "00000000-0000-0000-0000-0000000000d5"
_RI = "00000000-0000-0000-0000-0000000000d4"

_TOPIC = {"topic_id": _T, "name": "General", "last_activity_at": "2026-10-01T12:00:00+00:00"}
_MESSAGE = {
    "id": _M,
    "is_pepper": False,
    "is_you": False,
    "is_deleted": False,
    "is_broken": False,
    "run_start": True,
    "display_name": "Dana",
    "initial": "D",
    "avatar_tint": "t1",
    "raw_body": "hello there",
    "rendered_html": "<p>hello there</p>",
    "created_at": "2026-10-01T12:00:00+00:00",
    "created_at_display": "12:00 PM",
    "day_key": "2026-10-01",
    "day_kicker": "THURSDAY",
    "day_heading": "October 1, 2026",
    "day_label": "Thursday, October 1",
    "quote_excerpt": "",
}
_NOTE_ROW = {
    "id": _NOTE,
    "name": "Runbook",
    "kind": "inline_text",
    "updated_at": "2026-10-01T12:00:00+00:00",
    "etag": "e1",
    "labels": [],
    "summary": None,
}
_GROUP_ROW = {
    "group_id": _G,
    "display_name": "Trip planning",
    "color": "#ffaa00",
    "sub": "active 2h ago",
    "member_count": 3,
    "archived": False,
    "topics": [_TOPIC],
}
_TASK_ROW = {
    "id": _TASK,
    "name": "morning-weather",
    "description": None,
    "cron": "0 8 * * 1-5",
    "timezone": "UTC",
    "destinations": None,
    "active": True,
    "one_shot": False,
    "last_run_at": None,
    "next_run_at": "2026-10-02T08:00:00+00:00",
}

_BLOB = "00000000-0000-0000-0000-0000000000e2"
_TODO = "00000000-0000-0000-0000-0000000000a7"
_GRANT = "00000000-0000-0000-0000-0000000000a6"
_TODO_ROW = {
    "id": _TODO,
    "title": "Buy milk",
    "status": "open",
    "source": "cli",
    "created_at": "2026-10-01T12:00:00+00:00",
}
_RESEARCH_PROJECT = {
    "id": _RP,
    "name": "robot-vacuums",
    "status": "active",
    "delivery_policy": "digest",
    "brief_snippet": "Robot vacuums under $500",
    "skill": "web-research",
    "scheduled_task_id": None,
    "schedule": None,
    "last_iteration": {
        "id": _RI,
        "status": "succeeded",
        "started_at": "2026-10-19T16:00:00+00:00",
        "finished_at": "2026-10-19T16:03:00+00:00",
    },
    "inflight_run": None,
    "created_at": "2026-10-01T12:00:00+00:00",
}
_RESEARCH_ITEM = {
    "id": _RI,
    "seq": 48213,
    "title": "Roborock Q7 Max+",
    "state": "candidate",
    "state_reason": None,
    "url": "https://roborock.com/q7",
    "summary": "Strong suction",
    "first_seen_at": "2026-10-19T16:00:00+00:00",
    "state_changed_at": "2026-10-19T16:00:00+00:00",
}
_SKILL = {"name": "triage", "description": "Sort the inbox", "builtin": False}
_SKILL_FULL = {**_SKILL, "body": "Do the thing.", "toolsets": []}
_GRANT_ROW = {
    **_SKILL,
    "id": _GRANT,
    "granted_by": "Dana",
    "owner_is_you": True,
    "can_revoke": True,
}
_DAY = {"date": "2026-10-02", "label": "Today", "date_label": "Fri, Oct 2"}
_RESEARCH_DETAIL = {
    **_RESEARCH_PROJECT,
    "brief": "Robot vacuums under $500",
    "source_url": None,
    "filters": [],
    "pending_steering_count": 0,
    "item_counts": {"candidate": 2},
    "report_artifact_id": None,
    "open_questions": [],
    "owner": {"kind": "user", "id": _P},
}

#: path -> body for every GET the leaves below make (query strings are ignored).
ROUTES: dict[str, object] = {
    "/api/v1/whoami": {"user_id": "u", "display_name": "U", "timezone": "UTC"},
    f"/api/v1/trajectories/{_TRAJ}": {
        "trajectory": {
            "id": _TRAJ,
            "user_id": _P,
            "channel": "web",
            "created_at": "2026-10-01T12:00:00+00:00",
        },
        "events": [],
    },
    "/api/v1/calendars/range": {
        "window": [_DAY],
        "spanning": [],
        "days": [{**_DAY, "events": [], "meals": [], "deadlines": [], "travel": []}],
    },
    "/api/v1/calendars/environment": {},
    "/api/v1/calendars/schedule-plan": {
        "now": "2026-10-02T16:00:00+00:00",
        "active": "urgent",
        "next_up": None,
        "plans": [],
        "more": {"count": 0, "items": []},
    },
    "/api/v1/calendars/planning": {"cards": []},
    "/api/v1/trajectories": [],
    "/api/v1/todos": [_TODO_ROW],
    "/api/v1/skills": [_SKILL],
    "/api/v1/skills/triage": _SKILL_FULL,
    f"/api/v1/groups/{_G}/skills": [_GRANT_ROW],
    f"/api/v1/groups/{_G}/skills/{_GRANT}": {**_GRANT_ROW, "skill": _SKILL_FULL},
    "/api/v1/scheduled-tasks": [_TASK_ROW],
    f"/api/v1/scheduled-tasks/{_TASK}": {**_TASK_ROW, "latest_run": None},
    f"/api/v1/scheduled-tasks/{_TASK}/runs": [
        {"id": "r1", "fired_at": "2026-10-01T08:00:00+00:00", "status": "ok"}
    ],
    f"/api/v1/scheduled-tasks/{_TASK}/thread": [
        {
            "id": "m1",
            "role": "assistant",
            "text": "Sunny",
            "channel": "web",
            "created_at": "2026-10-01T08:00:05+00:00",
        }
    ],
    "/api/v1/research-projects": {"items": [_RESEARCH_PROJECT], "next_before": None},
    f"/api/v1/research-projects/{_RP}": _RESEARCH_DETAIL,
    f"/api/v1/research-projects/{_RP}/report": {
        "artifact_id": None,
        "body": "# Report\n",
        "summary": "",
        "current_recommendation": "",
        "open_questions": [],
        "changelog": [],
        "collapsed_changelog": 0,
        "updated_at": None,
    },
    f"/api/v1/research-projects/{_RP}/iterations": {
        "items": [
            {
                "id": _RI,
                "occurrence_key": None,
                "trigger": "scheduled",
                "requested_by": None,
                "run_id": None,
                "status": "succeeded",
                "started_at": "2026-10-19T16:00:00+00:00",
                "finished_at": None,
                "error": None,
                "source_failures": None,
            }
        ],
        "next_before": None,
    },
    f"/api/v1/research-projects/{_RP}/items": {"items": [_RESEARCH_ITEM], "next_before": None},
    f"/api/v1/research-projects/{_RP}/items/{_RI}": {
        **_RESEARCH_ITEM,
        "source_type": "web",
        "creator": None,
        "year": None,
        "published_at": None,
        "citations": [],
        "superseded_by": None,
    },
    "/api/v1/takeout": {"archive": "bytes"},
    "/api/v1/apps": [
        {"slug": "notes", "title": "Notes", "description": "", "owner_scoped_actions": ["delete"]}
    ],
    "/api/v1/apps/notes/data": {"rev": 2, "data": {"k": "v"}},
    "/api/v1/apps/brief/data": {
        "rev": 1,
        "data": {
            "configured": True,
            "briefs": [
                {
                    "id": "b1",
                    "date": "2026-10-16",
                    "dateline": "Friday, October 16",
                    "delivered_at": "6:16 AM",
                    "text": "Early start.",
                    "sections": {"structured": True, "lede": "Early start.", "events": []},
                }
            ],
        },
    },
    "/api/v1/apps/following/data": {
        "rev": 1,
        "data": {
            "items": [
                {
                    "ref": "feeds:feed:11111111-2222-3333-4444-555555555555",
                    "kind": "feed",
                    "group": "Reading",
                    "name": "Example Weekly",
                    "latest": "A post",
                    "latest_at": "2026-10-16T17:00:00Z",
                    "when_label": "Today",
                    "live": False,
                    "state": "ok",
                    "recent": {
                        "label": "Recent posts",
                        "items": [
                            {
                                "title": "A post",
                                "url": "https://example.com/a",
                                "when_label": "Today",
                                "new": True,
                                "upcoming": False,
                            }
                        ],
                    },
                }
            ],
            "counts": {"unread": 1, "live": 0, "by_group": {"Reading": 1}},
            "feed": [
                {
                    "source": "Example Weekly",
                    "ref": "feeds:feed:11111111-2222-3333-4444-555555555555",
                    "group": "Reading",
                    "kind": "feed",
                    "title": "A post",
                    "url": "https://example.com/a",
                    "when_label": "Today",
                    "new": True,
                    "more": [],
                    "fold": None,
                }
            ],
        },
    },
    "/api/v1/groups": {
        "enabled": True,
        "degraded": False,
        "total": 1,
        "archived_total": 0,
        "groups": [_GROUP_ROW],
    },
    f"/api/v1/groups/{_G}/topics": {"topics": [_TOPIC]},
    f"/api/v1/groups/{_G}/roster": {"members": [{"person_id": _P, "display_name": "Dana"}]},
    f"/api/v1/groups/{_G}/attachments/{_A}": {"file": "bytes"},
    f"/api/v1/groups/{_G}/topics/{_T}/messages": {"messages": [_MESSAGE], "next_before": None},
    f"/api/v1/groups/{_G}/apps/launcher": {"tiles": [], "installed_unavailable": False},
    f"/api/v1/groups/{_G}": {
        "apps": {
            "catalog": [{"app_key": "shopping"}],
            "installed": [{"install_id": "i1", "app_key": "notes", "title": "Notes"}],
        }
    },
    f"/api/v1/groups/{_G}/sharing": {
        "available_calendars": [{"calendar_id": "c1", "name": "Home"}],
        "available_levels": ["busy"],
        "calendar": {"calendars": {}},
        "default_levels": {},
    },
    "/api/v1/groups/activity": {
        "topics": [
            {"group_label": "Trip", "topic_label": "General", "author": "Dana", "excerpt": "hi"}
        ]
    },
    "/api/v1/groups/topics/search": {
        "topics": [{"group_name": "Trip", "name": "General", "topic_id": _T}]
    },
    "/api/v1/artifacts": {"truncated": False, "items": [_NOTE_ROW]},
    f"/api/v1/artifacts/{_BLOB}/detail": {
        **_NOTE_ROW,
        "id": _BLOB,
        "name": "Receipt",
        "kind": "blob",
        "shared": False,
        "body": None,
        "mime_type": "image/png",
        "size_label": "2 KB",
    },
    f"/api/v1/artifacts/{_BLOB}/raw": {"png": "bytes"},
    "/api/v1/calendars/preferences": {
        "timezones": ["UTC"],
        "maps": False,
        "timezone": "UTC",
        "location": None,
        "work_location": None,
        "work_days": [0, 1, 2, 3, 4],
        "work_start": "09:00",
        "work_end": "17:00",
        "show_commute": True,
        "show_weather": True,
        "meals": {},
        "workout": {"at": "07:00", "minutes": 45},
    },
    f"/api/v1/artifacts/{_NOTE}/detail": {**_NOTE_ROW, "shared": False, "body": "hello note"},
}

#: Non-GET routes (POST, DELETE): the room post answers ``{"message": {...}}``.
POST_ROUTES: dict[str, object] = {
    "/api/v1/research-projects": _RESEARCH_DETAIL,
    f"/api/v1/research-projects/{_RP}": _RESEARCH_DETAIL,
    f"/api/v1/research-projects/{_RP}/filters": _RESEARCH_DETAIL,
    f"/api/v1/research-projects/{_RP}/schedule": _RESEARCH_DETAIL,
    f"/api/v1/research-projects/{_RP}/pause": _RESEARCH_DETAIL,
    f"/api/v1/research-projects/{_RP}/resume": _RESEARCH_DETAIL,
    f"/api/v1/research-projects/{_RP}/runs": {
        "run_id": _RUN,
        "project_id": _RP,
        "status": "queued",
        "requested_at": "2026-10-20T10:00:00+00:00",
    },
    f"/api/v1/research-projects/{_RP}/steering": {
        "id": _RI,
        "text": "skip refurbished",
        "question_id": None,
        "author_user_id": _P,
        "author_name": "Dana",
        "created_at": "2026-10-20T10:00:00+00:00",
        "consumed_at": None,
    },
    f"/api/v1/research-projects/{_RP}/items/{_RI}/take": {**_RESEARCH_ITEM, "state": "taken"},
    f"/api/v1/research-projects/{_RP}/items/{_RI}/reconsider": {
        **_RESEARCH_ITEM,
        "state": "candidate",
    },
    f"/api/v1/groups/{_G}/topics/{_T}/messages": {"message": {**_MESSAGE, "topic_id": _T}},
    f"/api/v1/scheduled-tasks/{_TASK}/renew": _TASK_ROW,
    f"/api/v1/scheduled-tasks/{_TASK}": _TASK_ROW,
    f"/api/v1/scheduled-tasks/{_TASK}/cancel": {**_TASK_ROW, "active": False},
    f"/api/v1/scheduled-tasks/{_TASK}/resume": _TASK_ROW,
    "/api/v1/scheduled-tasks": _TASK_ROW,
    "/api/v1/todos": _TODO_ROW,
    f"/api/v1/todos/{_TODO}": _TODO_ROW,
    f"/api/v1/todos/{_TODO}/done": {**_TODO_ROW, "status": "done"},
    f"/api/v1/todos/{_TODO}/dismiss": {**_TODO_ROW, "status": "dismissed"},
    f"/api/v1/todos/{_TODO}/reopen": _TODO_ROW,
    "/api/v1/chat/send": {"reply": "hi back"},
    "/api/v1/artifacts/new": {"id": _NOTE, "name": "Runbook", "etag": "e1"},
    f"/api/v1/artifacts/{_NOTE}/edit": {
        "artifact": {**_NOTE_ROW, "shared": False, "body": "changed"}
    },
    "/api/v1/apps/notes/actions/delete": {"result": {"name": "Runbook"}},
    "/api/v1/apps/artifacts/actions/delete": {"result": {"name": "Receipt"}},
    "/api/v1/artifacts/upload": {
        "id": _BLOB,
        "name": "pic.png",
        "mime_type": "image/png",
        "byte_size": 4,
    },
    "/api/v1/calendars/preferences": {"timezone": "America/Chicago", "work_days": [0, 1]},
    "/api/v1/apps/brief/actions/run_now": {"result": {"started": True, "message": "Running."}},
    "/api/v1/apps/following/actions/search": {
        "result": {
            "yours": [],
            "new": [
                {
                    "ref": "sports:team:mlb:19",
                    "payload": {},
                    "kind": "team",
                    "name": "Los Angeles Dodgers",
                    "detail": "Team · MLB",
                    "following": False,
                }
            ],
        }
    },
    "/api/v1/apps/following/actions/follow": {
        "result": {"ok": True, "message": "Now following Los Angeles Dodgers.", "item": None}
    },
    "/api/v1/apps/following/actions/unfollow": {"result": {"ok": True, "message": "Unfollowed."}},
    "/api/v1/apps/notes/actions/archive": {"result": {"ok": True}},
    "/api/v1/skills": _SKILL_FULL,
    "/api/v1/skills/triage": _SKILL_FULL,
    f"/api/v1/groups/{_G}/skills": _GRANT_ROW,
    f"/api/v1/groups/{_G}/skills/{_GRANT}": {},
    "/api/v1/groups": {"group_id": _G, "display_name": "Trip planning", "created": True},
    f"/api/v1/groups/{_G}/rename": {"group_id": _G, "display_name": "Renamed"},
    f"/api/v1/groups/{_G}/archive": {"group_id": _G},
    f"/api/v1/groups/{_G}/unarchive": {"group_id": _G},
    f"/api/v1/groups/{_G}/mute": {"group_id": _G},
    f"/api/v1/groups/{_G}/unmute": {"group_id": _G},
    f"/api/v1/groups/{_G}/leave": {"group_id": _G},
    f"/api/v1/groups/{_G}/members": {"invited": True},
    f"/api/v1/groups/{_G}/members/{_P}": {"removed": True},
    f"/api/v1/groups/{_G}/members/{_P}/role": {"person_id": _P, "role": "owner"},
    f"/api/v1/groups/{_G}/apps": {"install_id": "i2", "app_key": "shopping"},
    f"/api/v1/groups/{_G}/apps/i1": {},
    f"/api/v1/groups/{_G}/sharing": {
        "available_calendars": [{"calendar_id": "c1", "name": "Home"}],
        "available_levels": ["busy"],
        "calendar": {"calendars": {"c1": "busy"}},
        "default_levels": {},
    },
    f"/api/v1/groups/{_G}/topics": {"topic": _TOPIC},
    f"/api/v1/groups/{_G}/topics/{_T}/rename": _TOPIC,
    f"/api/v1/groups/{_G}/topics/{_T}/archive": _TOPIC,
    f"/api/v1/groups/{_G}/topics/{_T}/unarchive": _TOPIC,
    f"/api/v1/groups/{_G}/messages/{_M}/edit": {**_MESSAGE, "raw_body": "better"},
    f"/api/v1/groups/{_G}/messages/{_M}": {**_MESSAGE, "is_deleted": True},
    f"/api/v1/groups/{_G}/messages/{_M}/reactions": {"reactions": []},
    f"/api/v1/groups/{_G}/messages/{_M}/reactions/clear": {"reactions": []},
}


@dataclass
class _Server:
    seen: list[tuple[str, str]] = field(default_factory=list)
    unrouted: list[tuple[str, str]] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        self.seen.append(key)
        table = ROUTES if request.method == "GET" else POST_ROUTES
        if request.url.path in table:
            return httpx.Response(200, json=table[request.url.path])
        self.unrouted.append(key)
        return httpx.Response(404, json={"detail": "unrouted in conformance fake"})


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch) -> _Server:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("PEPPER_JSON", raising=False)
    monkeypatch.chdir(tmp_path)
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    # `feed show N` follows the last `feed list`, `following create N` the last
    # `following search`; seed the one item and candidate ROUTES' documents hold.
    feed.VIEW.save(
        [
            {
                "id": "x",
                "ref": "feeds:feed:11111111-2222-3333-4444-555555555555",
                "url": "https://example.com/a",
                "title": "A post",
            }
        ]
    )
    following_search.save(
        "dodgers",
        [{"n": 1, "ref": "sports:team:mlb:19", "payload": {}, "name": "Los Angeles Dodgers"}],
    )
    server = _Server()
    real = httpx.Client

    def make_client(**kw):
        return real(transport=httpx.MockTransport(server.handle), **kw)

    monkeypatch.setattr(httpx, "Client", make_client)
    return server


def _watch_one_event(monkeypatch) -> None:
    """``rooms watch`` streams until interrupted; script one event, then Ctrl-C."""

    def fake_watch(self, *, since=0, last_event_id=None, **_kw):
        yield api_client.FeedFrame(
            kind="message.created",
            id=7,
            payload={
                "id": 7,
                "kind": "message.created",
                "group_id": _G,
                "person_id": _P,
                "payload": {"topic_id": _T, "message_id": _M},
                "created_at": "2026-10-01T12:00:00+00:00",
            },
        )
        raise KeyboardInterrupt

    monkeypatch.setattr(api_client.ApiClient, "watch_agent_events", fake_watch)


#: The leaves that take ``--json`` today, with an argv that runs each against ROUTES.
JSON_CASES: dict[str, list[str]] = {
    "trajectories show": ["trajectories", "show", _TRAJ],
    "trajectories list": ["trajectories", "list"],
    "brief": ["brief"],
    "agents guide": ["agents", "guide"],
    "scheduled-tasks renew": ["scheduled-tasks", "renew", _TASK],
    "calendar": ["calendar"],
    "next": ["next"],
    "following list": ["following", "list"],
    "following show": ["following", "show", "example"],
    "following search": ["following", "search", "dodgers"],
    "following create": ["following", "create", "1"],
    "following delete": ["following", "delete", "example", "--yes"],
    "feed list": ["feed", "list"],
    "feed show": ["feed", "show", "1"],
    "todos list": ["todos", "list"],
    "rooms list": ["rooms", "list"],
    "rooms show": ["rooms", "show", _G],
    "rooms create": ["rooms", "create", "--name", "Plans", "dana@example.com"],
    "rooms edit": ["rooms", "edit", _G, "--name", "Renamed"],
    "rooms archive": ["rooms", "archive", _G],
    "rooms unarchive": ["rooms", "unarchive", _G],
    "rooms leave": ["rooms", "leave", _G, "--yes"],
    "rooms mute": ["rooms", "mute", _G],
    "rooms unmute": ["rooms", "unmute", _G],
    "rooms activity": ["rooms", "activity"],
    "rooms watch": ["rooms", "watch"],
    "topics list": ["topics", "list", "--room", _G],
    "topics create": ["topics", "create", "--room", _G, "Plans"],
    "topics edit": ["topics", "edit", "--room", _G, _T, "--name", "Renamed"],
    "topics archive": ["topics", "archive", "--room", _G, _T],
    "topics unarchive": ["topics", "unarchive", "--room", _G, _T],
    "topics search": ["topics", "search", "q"],
    "messages list": ["messages", "list", "--room", _G, "--topic", _T],
    "messages post": ["messages", "post", "--room", _G, "--topic", _T, "hi"],
    "messages edit": ["messages", "edit", "--room", _G, _M, "better"],
    "messages delete": ["messages", "delete", "--room", _G, _M, "--yes"],
    "messages react": ["messages", "react", "--room", _G, _M, "+1"],
    "messages unreact": ["messages", "unreact", "--room", _G, _M, "+1"],
    "attachments download": ["attachments", "download", "--room", _G, _A, "--out", "got.bin"],
    "members list": ["members", "list", "--room", _G],
    "members invite": ["members", "invite", "--room", _G, "dana@example.com"],
    "members remove": ["members", "remove", "--room", _G, _P, "--yes"],
    "members edit": ["members", "edit", "--room", _G, _P, "--role", "owner", "--yes"],
    "sharing show": ["sharing", "show", "--room", _G],
    "sharing edit": ["sharing", "edit", "--room", _G, "--set", "Home=busy"],
    "apps list": ["apps", "list", "--room", _G],
    "apps install": ["apps", "install", "--room", _G, "shopping"],
    "apps uninstall": ["apps", "uninstall", "--room", _G, "notes", "--yes"],
    "notes list": ["notes", "list"],
    "notes search": ["notes", "search", "run"],
    "notes show": ["notes", "show", _NOTE],
    "notes create": ["notes", "create", "Runbook", "--file", "body.md"],
    "notes edit": ["notes", "edit", _NOTE, "--file", "body.md"],
    "notes delete": ["notes", "delete", _NOTE, "--yes"],
    "artifacts list": ["artifacts", "list", "--kind", "file"],
    "artifacts show": ["artifacts", "show", _BLOB],
    "artifacts download": ["artifacts", "download", _BLOB, "--out", "got.png", "--force"],
    "artifacts create": ["artifacts", "create", "--file", "pic.png"],
    "artifacts delete": ["artifacts", "delete", _BLOB, "--yes"],
    "preferences show": ["preferences", "show"],
    "preferences edit": ["preferences", "edit", "--timezone", "America/Chicago"],
    "todos create": ["todos", "create", "Buy milk"],
    "todos done": ["todos", "done", _TODO],
    "todos edit": ["todos", "edit", _TODO, "--title", "Buy oat milk"],
    "todos dismiss": ["todos", "dismiss", _TODO],
    "todos reopen": ["todos", "reopen", _TODO],
    "scheduled-tasks list": ["scheduled-tasks", "list"],
    "scheduled-tasks show": ["scheduled-tasks", "show", _TASK],
    "scheduled-tasks runs": ["scheduled-tasks", "runs", _TASK],
    "scheduled-tasks thread": ["scheduled-tasks", "thread", _TASK],
    "scheduled-tasks create": [
        "scheduled-tasks",
        "create",
        "Brief",
        "--prompt",
        "p",
        "--cron",
        "0 8 * * *",
    ],
    "scheduled-tasks edit": ["scheduled-tasks", "edit", _TASK, "--prompt", "q"],
    "scheduled-tasks delete": ["scheduled-tasks", "delete", _TASK, "--yes"],
    "scheduled-tasks pause": ["scheduled-tasks", "pause", _TASK],
    "scheduled-tasks resume": ["scheduled-tasks", "resume", _TASK],
    "research-projects list": ["research-projects", "list"],
    "research-projects show": ["research-projects", "show", _RP],
    "research-projects runs": ["research-projects", "runs", _RP],
    "research-items list": ["research-items", "list", "--project", _RP],
    "research-items show": ["research-items", "show", "--project", _RP, _RI],
    "research-projects create": [
        "research-projects",
        "create",
        "robot-vacuums",
        "--file",
        "body.md",
    ],
    "research-projects edit": ["research-projects", "edit", _RP, "--file", "body.md"],
    "research-projects run": ["research-projects", "run", _RP],
    "research-projects steer": ["research-projects", "steer", _RP, "skip", "refurbished"],
    "research-projects pause": ["research-projects", "pause", _RP],
    "research-projects resume": ["research-projects", "resume", _RP],
    "research-items take": ["research-items", "take", "--project", _RP, _RI],
    "research-items reconsider": [
        "research-items",
        "reconsider",
        "--project",
        _RP,
        _RI,
        "--reason",
        "price dropped",
    ],
    "skills list": ["skills", "list"],
    "skills show": ["skills", "show", "triage"],
    "skills create": ["skills", "create", "triage", "--file", "triage.md"],
    "skills edit": ["skills", "edit", "triage", "--file", "triage.md"],
    "skills delete": ["skills", "delete", "triage", "--yes"],
    "skills share": ["skills", "share", "--room", _G, "triage"],
    "skills revoke": ["skills", "revoke", "--room", _G, _GRANT, "--yes"],
    "apps show": ["apps", "show", "notes"],
    "apps run": ["apps", "run", "notes", "archive"],
    "chat": ["chat", "hello"],
    "whoami": ["whoami"],
    "logout": ["logout"],
    "takeout": ["takeout", "--out", "t.zip"],
}


def _run(argv: list[str], capsys) -> tuple[int, str, str]:
    rc = cli.main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


#: Leaves that prompt only in some cases (IA §3.1, ``planning/research-projects.md`` §7): the
#: leaf takes ``--yes``, and each case here exits 2 with a J8 ``usage`` line without a terminal
#: or ``--yes``, having written nothing. Room cases join in the rooms phase.
CONFIRMS = {
    "research-projects edit": ["research-projects", "edit", _RP, "--cron", "0 9 * * 0"],
}

#: The same leaves' cases that do not prompt: each succeeds without a terminal or ``--yes``
#: and sends its write.
NO_PROMPT = {
    "research-projects edit --clear schedule": (
        ["research-projects", "edit", _RP, "--clear", "schedule"],
        "DELETE",
    ),
    "research-projects edit --clear filters": (
        ["research-projects", "edit", _RP, "--clear", "filters"],
        "PUT",
    ),
}


def test_confirming_leaves_take_yes() -> None:
    assert all("--yes" in _flags(LEAVES[leaf]) for leaf in CONFIRMS)


@pytest.mark.parametrize("leaf", sorted(CONFIRMS))
@pytest.mark.parametrize("as_json", [False, True])
def test_a_prompting_case_without_a_terminal_or_yes_is_a_usage_error(
    leaf, as_json, _env, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    rc, out, err = _run([*CONFIRMS[leaf], *(["--json"] if as_json else [])], capsys)
    assert (rc, out) == (2, "")
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1, err
    assert "--yes" in lines[0]
    if as_json:
        assert json.loads(lines[0])["error"]["code"] == "usage"
    assert [m for m, _ in _env.seen if m != "GET"] == []
    assert not _env.unrouted, _env.unrouted


@pytest.mark.parametrize("case", sorted(NO_PROMPT))
def test_a_non_prompting_case_succeeds_without_a_terminal_or_yes(
    case, _env, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    argv, method = NO_PROMPT[case]
    rc, _out, err = _run(argv, capsys)
    assert rc == 0, err
    assert [m for m, _ in _env.seen if m != "GET"] == [method]


def _is_json(out: str) -> bool:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    try:
        json.loads(out)
    except ValueError:
        pass
    else:
        return True
    # JSON lines (a stream): every line parses.
    try:
        return bool(lines) and all(isinstance(json.loads(ln), dict) for ln in lines)
    except ValueError:
        return False


#: Command lines a retired noun must answer the same way (§8 "Renamed" row): bare, with a
#: verb, and with option-looking tokens that a real parser would act on or reject.
_RENAMED_ARGVS: tuple[tuple[str, ...], ...] = ((), ("list",), ("list", "--json"), ("--help",))


def _is_stub(noun: str, server: _Server, capsys) -> bool:
    """Every ``_RENAMED_ARGVS`` line exits 2 with one stderr line and touches no API."""
    for rest in _RENAMED_ARGVS:
        server.seen.clear()
        try:
            rc = cli.main([noun, *rest])
        except SystemExit as exc:  # argparse errors and --help exit instead of returning
            rc = exc.code
        captured = capsys.readouterr()
        err_lines = captured.err.splitlines()
        if not (
            rc == 2
            and captured.out == ""
            and len(err_lines) == 1
            and f"'{noun}' was renamed" in err_lines[0]
            and not server.seen
        ):
            return False
    return True


def test_renamed_nouns_are_stubs(_env, monkeypatch, capsys) -> None:
    # A chat fall-through would reach the fake server (and so fail the check); an empty
    # stdin keeps any interactive path from waiting on the terminal.
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert {_noun(leaf) for leaf in LEAVES} >= RENAMED_PENDING
    failing = {noun for noun in RENAMED if not _is_stub(noun, _env, capsys)}
    _ratchet("renamed stub", failing, RENAMED_PENDING)


def test_stub_check_accepts_a_wired_stub(_env, monkeypatch, capsys) -> None:
    # Control for the check above: once a phase wires the stub, the noun passes. This
    # one test reaches into the parser to wire it, the way phase 2 will.
    real_build = cli._build_parser

    def build():
        parser, names = real_build()
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        cli._add_renamed_stub(sub, "trajectory", "see 'pepper trajectories --help'")
        return parser, names

    monkeypatch.setattr(cli, "_build_parser", build)
    assert _is_stub("trajectory", _env, capsys)


def test_json_cases_cover_every_leaf_that_takes_json() -> None:
    with_json = {leaf for leaf, text in LEAVES.items() if "--json" in _flags(text)}
    assert with_json == set(JSON_CASES)


def _run_fresh(argv: list[str], capsys) -> tuple[int, str, str]:
    """``_run`` with the credentials restored first (``logout`` deletes them)."""
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    return _run(argv, capsys)


@pytest.mark.parametrize("leaf", sorted(JSON_CASES))
def test_pepper_json_switches_json_mode(leaf, _env, monkeypatch, capsys) -> None:
    argv = JSON_CASES[leaf]
    Path("body.md").write_text("hello\n")
    Path("pic.png").write_bytes(b"\x89PNG")
    Path("triage.md").write_text(
        "---\nname: triage\ndescription: Sort the inbox\n---\nDo the thing.\n"
    )
    if leaf == "rooms watch":
        _watch_one_event(monkeypatch)

    # Unset, "0", empty and any other value than exactly "1": the human output.
    for value in (None, "0", "", "true", "yes", " 1", "11"):
        if value is None:
            monkeypatch.delenv("PEPPER_JSON", raising=False)
        else:
            monkeypatch.setenv("PEPPER_JSON", value)
        rc, out, err = _run_fresh(argv, capsys)
        assert rc == 0, (leaf, value, err)
        assert (out + err).strip(), (leaf, value)
        assert not _is_json(out), (leaf, value, out)

    # PEPPER_JSON=1 with no flag: JSON.
    monkeypatch.setenv("PEPPER_JSON", "1")
    rc, out, err = _run_fresh(argv, capsys)
    assert rc == 0, (leaf, err)
    assert _is_json(out), (leaf, out)

    # --json wins over any value of the variable.
    for value in ("0", "", "1"):
        monkeypatch.setenv("PEPPER_JSON", value)
        rc, out_flag, err = _run_fresh([*argv, "--json"], capsys)
        assert rc == 0, (leaf, err)
        assert _is_json(out_flag), (leaf, value, out_flag)
    assert not _env.unrouted, _env.unrouted


def test_login_ignores_pepper_json(monkeypatch, capsys) -> None:
    monkeypatch.setenv("PEPPER_JSON", "1")
    rc, out, _err = _run(
        ["login", "--server", "http://example.com", "--token", "pep_pat_new"], capsys
    )
    assert rc == 0
    assert not _is_json(out)
    assert "pep_pat_new" not in out


def test_the_repl_ignores_pepper_json(monkeypatch, capsys) -> None:
    calls: list[object] = []
    monkeypatch.setattr(cli, "_run_repl", lambda creds, *, provider: calls.append(creds) or 0)
    monkeypatch.setenv("PEPPER_JSON", "1")
    assert cli.main(["chat"]) == 0
    assert len(calls) == 1
    assert capsys.readouterr().out == ""


def test_list_leaves_emit_the_envelope(monkeypatch, capsys) -> None:
    """J3: ``list``/``search``/``activity`` and ``scheduled-tasks runs/thread`` leaves."""
    is_list = {
        leaf
        for leaf in LEAVES
        if (" " in leaf and _verb(leaf) in {"list", "search", "activity"})
        or leaf in {"scheduled-tasks runs", "scheduled-tasks thread", "research-projects runs"}
    }
    failing: set[str] = set()
    for leaf in is_list:
        argv = JSON_CASES.get(leaf)
        if argv is None:  # no --json yet, so it cannot emit the envelope
            failing.add(leaf)
            continue
        if leaf == "rooms watch":
            continue
        rc, out, _err = _run([*argv, "--json"], capsys)
        body = json.loads(out) if rc == 0 and _is_json(out) else None
        if not (
            isinstance(body, dict) and isinstance(body.get("items"), list) and "next_before" in body
        ):
            failing.add(leaf)
    _ratchet("envelope", failing, ENVELOPE_PENDING)
