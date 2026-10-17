"""Command-line entry point for ``pepper``.

Subcommands:

- ``pepper login [--server URL] [--token TOKEN] [--manual] [--no-browser]``
  — store credentials. By default this drives the browser-based
  device-authorization flow: start a device login, print a human code
  + verification URL, open the browser (unless ``--no-browser``), poll
  until approved, then validate the minted token via ``GET
  /api/v1/whoami`` before writing it. ``--token`` (scripting) or
  ``--manual`` (force the prompt) fall back to pasting a PAT minted in
  the web UI — the same manual path this command used to be the only
  way to do. A pasted agent token (``pep_agt_``) is validated against
  ``GET /api/v1/groups`` instead, since it is not a Pepper account.
- ``pepper whoami`` — print the user behind the current credentials.
- ``pepper chat MESSAGE`` — one-shot turn against the server, render
  the reply. ``pepper chat`` with no message (or bare ``pepper``) opens
  the interactive REPL, which streams each turn over SSE.
- ``pepper trajectories show <id> | list`` — inspect saved turns; ``--room`` scopes
  to one room's.
- ``pepper calendar [today|week|schedule] [--planning]`` — read-only agenda.
- ``pepper next [--room R] [--source PREFIX] [--all] [--json]`` — Now / today's calendar /
  todos, prioritized.
- ``pepper todos list | create | done | dismiss | reopen`` — manage tracked
  action items; ``--room`` acts on a room's todos instead of yours.
- ``pepper scheduled-tasks list | show | runs | thread | create | edit |
  pause | resume | renew | delete`` — manage recurring and one-off
  scheduled prompts; ``--room`` acts on a room's
  tasks instead of yours — any member reads, only a room owner changes.
  ``edit --to/--default/--silent`` sets where a task announces its results.
- ``pepper skills list | show | create | edit | delete`` — author skills as
  SKILL.md documents in ``$EDITOR`` or ``--file``. ``delete`` asks before deleting
  unless ``--yes``. ``pepper skills share | revoke NAME --room ROOM`` puts one
  of your skills in a room or takes it out, and ``list``/``show`` take
  ``--room`` to read what a room holds — any member reads and shares, the
  skill's owner or a room owner revokes. A room never owns a skill, so
  ``create``/``edit``/``delete`` refuse ``--room``.
- ``pepper rooms list | show | create | edit | archive | unarchive | leave | mute |
  unmute | activity | watch``, ``pepper topics list | create | edit | archive |
  unarchive | search``, ``pepper messages list | post | edit | delete | react |
  unreact`` and ``pepper attachments download`` — read and post in your rooms and
  their topics. A room that is the container is ``--room ROOM`` (``rooms show ROOM``
  takes it as the object); a topic is ``--topic``. ``pepper rooms watch [--room ROOM
  [--topic TOPIC]] [--since CURSOR] [--cursor-file PATH] [--json] [--kinds K,...]
  [--ignore-kinds K,...]`` is the agent-member counterpart of polling
  ``messages list``: it consumes the agent's event stream (SSE), one line per
  event, and reconnects from its cursor; ``--room``/``--topic`` have the server
  narrow it to one room or topic (exit 3 when the server can no longer replay
  from the cursor asked for). The destructive ones confirm unless ``--yes``.
  Reference client for the groups API — this is the surface a native port
  reproduces, so a shape change here mirrors the server contract, not the
  other way round. ``groups`` is a retired noun (a stub that exits 2).
- ``pepper notes list | search | show | create | edit | delete`` — your text
  artifacts: your own, or a room's shared ones with ``--room``. Reference
  client for the artifacts API — ``show`` writes a note's raw
  markdown body to stdout, byte-exact when piped, so ``show > f.md``
  round-trips through ``notes edit --file f.md``. ``create``/``edit`` take the
  body from ``$EDITOR``, ``--file PATH``, or ``--file -`` for stdin;
  ``edit`` sends nothing when the text is unchanged and runs the
  conflict flow (save + diff + re-open/overwrite/quit) on a stale
  ``If-Match``. ``delete`` asks before deleting unless ``--yes``.
- ``pepper brief [--date YYYY-MM-DD | --now]`` — your morning brief, read from the server's
  Brief document in the order it sends; ``--now`` runs a fresh one.
- ``pepper feed list | show`` and ``pepper following list | show | search | create | delete`` — the
  server's ranked stream of what you follow (``feed show N`` shows one item in full), and
  managing the follows; ``following create N`` follows a number from the last
  ``following search``.
- ``pepper artifacts list | show | download | create | delete`` — everything you saved: notes,
  links and files. ``create`` uploads a file; to edit a note's text use ``notes``.
- ``pepper preferences show | edit`` — the Calendar > Preferences values.
- ``pepper takeout [--out FILE]`` — download a zip archive of everything
  Pepper holds about your account.
- ``pepper logout`` — delete the credentials file.
- ``pepper agents guide [--write DIR [--force]]`` — print (or write) the
  SKILL.md shipped in this package that teaches an agent member to use the
  CLI in rooms. Reads no credentials and makes no network call.

Each command returns an integer exit code so shells / scripts can
detect failure without parsing stderr.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import getpass
import json
import mimetypes
import os
import re
import socket
import sys
import time
import uuid
import webbrowser
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from pepper_cli import (
    __version__,
    account_render,
    agent_skill,
    api_client,
    article_reader,
    artifacts_view,
    brief_view,
    credentials,
    feed,
    feed_detail_render,
    following_render,
    following_search,
    following_view,
    next_view,
    numbered_view,
    preferences_view,
    proctitle,
    render,
    skill_doc,
    turn,
    window_title,
)
from pepper_cli.provider import fs as provider_fs
from pepper_cli.secret import SecretStr
from pepper_cli.terminal_text import make_visible, sanitize_line

#: Default server URL used by ``pepper login`` when ``--server`` is
#: not provided and the user accepts the prompt default. Production
#: instance — local-dev users pass ``--server http://localhost:8000``
#: explicitly. (No env-var override yet; add one if running against
#: multiple servers becomes routine.)
_DEFAULT_SERVER = "https://pepper.vycari.ai"


def _repl_title(server_url: str) -> str:
    """Terminal window title for the REPL.

    Bare ``Pepper`` against the default production server; ``Pepper
    (host)`` otherwise, so a REPL pointed at a local dev server or a
    self-hosted instance never looks indistinguishable from one talking
    to production in your window/tab list.

    Only the hostname (plus a port, when one is given) goes into the title,
    never ``netloc``: a ``--server`` URL may carry ``user:password@``
    userinfo, and a window title is visible to anyone looking at the screen
    and to other processes that can read it. A URL with no parseable host
    falls back to bare ``Pepper`` rather than echoing the raw string.
    """
    if server_url == _DEFAULT_SERVER:
        return "Pepper"
    parsed = urlparse(server_url)
    host = parsed.hostname
    if not host:
        return "Pepper"
    if ":" in host:  # IPv6 literal: re-bracket so a port suffix stays readable.
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:  # a malformed port; the host alone still identifies it
        port = None
    if port is not None:
        host = f"{host}:{port}"
    return f"Pepper ({host})"


#: Destinations a user may pin a scheduled task to. Mirrors
#: ``pepper.agent.scheduled_tasks.destinations.OFFERED`` — the server
#: rejects anything outside it, so listing them as argparse ``choices``
#: turns a typo into a local usage error instead of a round-trip 422.
#: ``client_api`` is storable server-side but isn't offered in any
#: picker, so it isn't here either.
_DESTINATION_CHOICES = ("main_chat", "resend_email", "twilio_sms", "whatsapp")

#: A room's task speaks a different dialect — the room's topic and every
#: member's inbox, never one person's phone. Mirrors
#: ``pepper.agent.scheduled_tasks.destinations.GROUP_OFFERED``.
_ROOM_DESTINATION_CHOICES = ("groups_service", "resend_email")

#: The CLI's spelling of a room destination is ``room`` (the product says "Delivery",
#: not the service name); the server's enum value is unchanged, so ``room`` is mapped
#: to it just before a request is built. ``groups_service`` stays an accepted, unlisted
#: spelling of the same thing.
_ROOM_DESTINATION_WIRE = {"room": "groups_service"}
_ROOM_DESTINATION_SHOWN = ("room", "resend_email")

#: What ``--to`` accepts at parse time: both dialects, since argparse cannot
#: see ``--room``. :func:`_destination_dialect_error` narrows it afterwards.
_ANY_DESTINATION_CHOICES = (
    *_DESTINATION_CHOICES,
    *(d for d in _ROOM_DESTINATION_CHOICES if d not in _DESTINATION_CHOICES),
    *_ROOM_DESTINATION_WIRE,
)


#: ``--to`` help: every choice before the first full stop (the conformance test reads it so),
#: then which ones a room's task takes.
_TO_CHOICES_TEXT = (
    f"{', '.join(_DESTINATION_CHOICES)}, room (room tasks take only "
    f"{' and '.join(_ROOM_DESTINATION_SHOWN)}, with --room)"
)


def _wire_destinations(to: list[str] | None) -> list[str]:
    """``--to`` values as the server spells them (``room`` -> ``groups_service``)."""
    return [_ROOM_DESTINATION_WIRE.get(d, d) for d in to or []]


#: The one sentence a non-owner gets for any refused room-task write.
_ROOM_TASK_OWNER_ONLY = "only a room owner can change its scheduled tasks."

#: The one sentence anyone else gets for a refused ``skills revoke --room``.
_ROOM_SKILL_REVOKE_FORBIDDEN = "only the skill's owner or a room owner can revoke it."
#: An email or text thread (wire ``kind: "sealed"``) takes no shared skill, by design.
_ROOM_SKILL_SEALED = (
    "Sharing a skill into this room isn't allowed. A skill can only be shared into a named channel."
)

#: ``rooms create`` without a name, refused before any request is sent.
_ROOM_CREATE_NAME_REQUIRED = (
    "a room needs a name: pass --name NAME. "
    "Email and text threads are created by the system, not by you."
)
#: ``skills create|edit|delete --room``: there is no such thing to do.
_ROOM_OWNS_NO_SKILL = (
    "a room never owns a skill, so there is nothing to {verb} there — {verb} your own "
    "skill (without --room) and it updates everywhere it is shared. "
    "'pepper skills share NAME --room ROOM' and 'pepper skills revoke NAME --room ROOM' "
    "put it in a room or take it out."
)
_ROOM_SKILL_ARG_HELP = (
    "Skill name (from ``skills list --room ROOM``) or, when two shared skills "
    "have the same name, the grant id that listing prints."
)

#: Every task-addressing subcommand takes the same argument. The API is
#: keyed by id, but a name is what the user (and the agent) actually calls
#: a task — see :func:`_resolve_task_id`.
_TASK_ARG_HELP = "Task name (from ``scheduled-tasks list``) or its UUID."

#: Every group-addressing subcommand takes the same argument shape: a name
#: (case-insensitive, unique prefix OK — resolved client-side over
#: ``list_groups``/``list_group_topics``, same idiom as ``_resolve_task_id``)
#: or the group's/topic's own UUID.
_GROUP_ARG_HELP = "Room/channel name (case-insensitive, unique prefix OK) or its UUID."
_TOPIC_ARG_HELP = "Topic name (case-insensitive, unique prefix OK) or its UUID."
_NOTE_ARG_HELP = "Note name (case-insensitive, unique prefix OK) or its UUID."

# MIRROR: src/pepper/agent/artifacts/service.py::INLINE_TEXT — the kind
# value ``pepper notes`` restricts every list/search/show call to. This
# package doesn't depend on pepper-server (see api_client.py's module
# docstring), so the string is hardcoded rather than imported; a rename on
# the server side MUST be matched here.
_NOTES_KIND = "inline_text"

#: Cap passed to the server when resolving a ``<note>`` name to an id.
#: The server's own cap (``list_artifacts_route``: ``limit`` caps at 200),
#: applied to listings already narrowed server-side (exact ``name=``, then
#: ``name_prefix=``); the route has no offset or cursor, so a still-truncated
#: answer is refused rather than resolved inside a page.
_NOTE_RESOLVE_LIMIT = 200


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code.

    ``argv`` defaults to ``sys.argv[1:]`` when not provided — tests
    pass a list explicitly to exercise commands without poking
    ``sys.argv``.

    Implicit-chat dispatch: if the first arg isn't a known subcommand
    or a top-level flag, the whole arg list is treated as a single
    chat message — ``pepper what's on my calendar`` shorthand for
    ``pepper chat "what's on my calendar"``. Bare ``pepper`` (no args)
    opens the REPL. The explicit ``pepper chat ...`` form keeps
    working unchanged.
    """
    # First thing, every subcommand: a console-script's argv[0] is the
    # interpreter, not ``pepper``, so tab-naming tools would otherwise show
    # ``python3``. See ``proctitle`` for why this is best-effort.
    proctitle.set_process_title("pepper")
    argv = list(sys.argv[1:]) if argv is None else list(argv)

    # Build the parser first so the set of known subcommand names
    # comes from the same place that registers them. If we hard-coded
    # the names here, a future ``trajectory`` (or whatever) subparser
    # would silently fall into the implicit-chat path until someone
    # remembered to bump the constant.
    parser, subcommand_names = _build_parser()

    # A leading ``--profile NAME`` is consumed before the implicit-chat
    # decision below, so ``pepper --profile x groups …`` reaches ``groups``
    # and ``pepper --profile x hello`` is still implicit chat. Set on every
    # call (``None`` when absent) so one process never inherits a profile.
    try:
        profile, argv = _consume_profile(argv)
        credentials.set_profile(profile)
    except ValueError as exc:
        print(f"pepper: {exc}", file=sys.stderr)
        return 2

    if not argv:
        # Bare ``pepper`` — drop straight into the REPL via the chat
        # subcommand with no message. Most CLIs print help here, but
        # interactive tools (psql, python, node) open a REPL and that's
        # the natural shape for this one.
        argv = ["chat"]
    elif argv[0] not in subcommand_names and not argv[0].startswith("-"):
        # Implicit chat: anything that isn't a subcommand name and
        # doesn't look like a flag becomes a chat message. We let the
        # chat subparser collect the words via ``nargs="*"`` rather
        # than joining here, so explicit (``pepper chat hi there``)
        # and implicit (``pepper hi there``) forms route through the
        # same path.
        if (rc := _reject_command_typo(argv[0], subcommand_names)) is not None:
            return rc
        argv = ["chat", *argv]
    # Anything starting with ``-`` (``--help``, ``--version``, unknown
    # flags) flows straight to the top-level argparse so flag-style
    # errors and help text stay sensible.

    args = parser.parse_args(argv)
    if hasattr(args, "json"):
        # Resolve J10 once, here: every leaf that declares ``--json`` reads the
        # result from ``args.json`` below, so ``PEPPER_JSON=1`` reaches all of
        # them. ``chat`` and ``login`` declare no ``--json``, so they never see it.
        args.json = _json_mode(args)
    handler = args.handler
    try:
        return handler(args)
    except api_client.UpgradeRequiredError as exc:
        # A 426 from any request: the server no longer supports this client
        # version. Raised past every per-command ``except ApiError``, so one
        # handler covers all commands (and the REPL) with no traceback.
        if hasattr(args, "json") and args.json:
            _emit_error(args, "upgrade_required", exc.message, status=426)
        else:
            print(exc.message, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # Ctrl-C inside an interactive prompt or a long-running turn.
        # Print a newline so the next shell prompt isn't pasted onto
        # the same line as the partial output.
        render.info("")
        return 130


def _consume_profile(argv: list[str]) -> tuple[str | None, list[str]]:
    """Strip a leading ``--profile NAME`` / ``--profile=NAME`` from ``argv``.

    Only the leading position counts, so the command text always starts
    ``pepper --profile NAME …`` (what a permission rule matches on). Raises
    ``ValueError`` for a missing or illegal name.
    """
    if not argv:
        return None, argv
    first = argv[0]
    if first == "--profile":
        if len(argv) < 2:
            raise ValueError("--profile requires a profile name.")
        name, rest = argv[1], argv[2:]
    elif first.startswith("--profile="):
        name, rest = first.removeprefix("--profile="), argv[1:]
    else:
        return None, argv
    return credentials.validate_profile(name), rest


class _RejectUnappliedProfile(argparse.Action):
    """Fail closed on any profile option that reaches argparse.

    :func:`_consume_profile` has already taken the single leading
    ``--profile NAME`` by the time the parser runs, so anything argparse
    matches to this option — a prefix abbreviation (``--prof``), a second
    ``--profile``, one placed after another top-level flag — would be accepted
    and then dropped, running the command as the wrong login. Exit 2 instead.
    """

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        del namespace, values
        parser.error(
            f"{option_string or '--profile'} was not applied: give --profile NAME "
            "exactly once, spelled in full, as the first argument "
            "(pepper --profile NAME <command> ...)."
        )


def _reject_command_typo(first: str, subcommand_names: frozenset[str]) -> int | None:
    """Guard implicit chat: return exit code 2 (after a stderr hint) when ``first``
    looks like a mistyped command, or when ``PEPPER_NO_IMPLICIT_CHAT`` is set.

    zsh doesn't word-split ``$var``, so ``pepper "todos create"`` arrives as one
    word; its first token is checked too. Returns ``None`` to let chat proceed.
    """
    # Truthy-looking values only: "0"/"false"/"" leave implicit chat on.
    disabled = os.environ.get("PEPPER_NO_IMPLICIT_CHAT", "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
    )
    tokens = first.split()
    head = tokens[0] if tokens else first
    guess: str | None = None
    if len(tokens) > 1 and head in subcommand_names:
        guess = head
    elif close := difflib.get_close_matches(head.lower(), sorted(subcommand_names), 1, 0.8):
        guess = close[0]
    if guess is None and not disabled:
        return None
    suggestion = f" Did you mean '{guess}'?" if guess else ""
    print(
        f"pepper: unknown command '{first}'.{suggestion} "
        "To send it to Pepper as a message, use: pepper chat …",
        file=sys.stderr,
    )
    return 2


def _register_account_commands(sub: Any) -> None:
    """``artifacts`` and ``preferences``."""
    # -- artifacts ----------------------------------------------------------
    p_art = sub.add_parser(
        "artifacts",
        help=(
            "Everything you've asked Pepper to keep: notes, links and files (pictures, PDFs, "
            "recordings). ``list``/``show``/``download``/``create`` (uploads a file)/``delete``; "
            "to edit a note's text use ``notes``."
        ),
    )
    sub_art = p_art.add_subparsers(dest="artifacts_action", required=True)

    p = sub_art.add_parser(
        "list",
        help="List your artifacts, numbered (or a room's, with --room).",
        parents=[_json_parent, _room_parent(omitted="Omit for your own artifacts.")],
    )
    p.add_argument("--kind", choices=sorted(artifacts_view.KINDS), help="Only this kind.")
    p.add_argument("--label", help="Only artifacts carrying this label.")
    p.add_argument(
        "--search",
        metavar="TEXT",
        help="Case-insensitive substring match over names and note bodies.",
    )
    p.add_argument(
        "--limit", type=int, metavar="N", help="At most N (1-200; the server's default is 50)."
    )
    p.set_defaults(handler=cmd_artifacts_list)

    p = sub_art.add_parser(
        "show",
        help="One artifact: its details, and its text if it has any (--json: the full detail).",
        parents=[_json_parent],
    )
    p.add_argument("artifact", help=_ARTIFACT_REF_HELP)
    p.set_defaults(handler=cmd_artifacts_show)

    p = sub_art.add_parser(
        "download",
        help="Save a file artifact's bytes (--out PATH, a directory, or - for stdout).",
        parents=[_json_parent],
    )
    p.add_argument("artifact", help=_ARTIFACT_REF_HELP)
    p.add_argument(
        "--out",
        metavar="PATH",
        help="File or directory to write; '-' for stdout. Default: the artifact's name here.",
    )
    p.add_argument("--force", action="store_true", help="Overwrite an existing file.")
    p.set_defaults(handler=cmd_artifacts_download)

    p = sub_art.add_parser(
        "create",
        help=(
            "Upload a file as a new artifact (pictures, PDFs, plain text, audio, MP4 video; "
            "the server refuses other types and large files)."
        ),
        parents=[_json_parent, _room_parent(omitted="Omit to save into your own artifacts.")],
    )
    p.add_argument("--file", required=True, metavar="PATH", help="The file to upload.")
    p.add_argument("--name", help="Name for the artifact. Default: the file's name.")
    p.add_argument("--summary", help="A one-line summary.")
    p.add_argument(
        "--label", action="append", metavar="LABEL", help="A label to file it under; repeatable."
    )
    p.set_defaults(handler=cmd_artifacts_create)

    p = sub_art.add_parser(
        "delete",
        help="Delete an artifact of any kind (kept for a while by the server, then gone).",
        parents=[_json_parent, _yes_parent, _room_parent(omitted="Omit for your own.")],
    )
    p.add_argument("artifact", help=_ARTIFACT_REF_HELP)
    p.set_defaults(handler=cmd_artifacts_delete)

    # -- preferences --------------------------------------------------------
    p_prefs = sub.add_parser(
        "preferences",
        help=(
            "Calendar > Preferences: addresses, the account timezone, work days and hours, "
            "meals, workouts and weather switches. ``show`` / ``edit``."
        ),
    )
    sub_prefs = p_prefs.add_subparsers(dest="preferences_action", required=True)
    p = sub_prefs.add_parser("show", help="Show your preferences.", parents=[_json_parent])
    p.set_defaults(handler=cmd_preferences_show)
    p = sub_prefs.add_parser(
        "edit",
        help="Change only the preferences given.",
        parents=[_json_parent],
        epilog=(
            "Changing the timezone also moves your scheduled tasks that follow the account's "
            "zone. An address the server cannot place is refused."
        ),
    )
    p.add_argument("--timezone", metavar="ZONE", help="An IANA zone name, e.g. America/Chicago.")
    p.add_argument("--home-address", metavar="TEXT", help="Home address ('' clears it).")
    p.add_argument("--work-address", metavar="TEXT", help="Work address ('' clears it).")
    p.add_argument(
        "--work-days",
        metavar="DAYS",
        help="Work days: names (mon,wed-fri) or weekdays, weekend, all; none clears them.",
    )
    p.add_argument("--work-start", metavar="HH:MM", help="Work day starts (24-hour).")
    p.add_argument("--work-end", metavar="HH:MM", help="Work day ends (24-hour).")
    switches = ", ".join(preferences_view.SWITCHES)
    p.add_argument("--show", metavar="NAMES", help=f"Switch on (comma-separated): {switches}.")
    p.add_argument("--hide", metavar="NAMES", help="Switch off (same names as --show).")
    p.add_argument(
        "--meal",
        action="append",
        metavar="NAME=HH:MM[,MIN]",
        help="Set a meal's start and/or length, e.g. lunch=12:30,45; repeatable.",
    )
    p.add_argument("--workout-at", metavar="HH:MM", help="When you usually train.")
    p.add_argument("--workout-minutes", type=int, metavar="MIN", help="Length of a session.")
    p.add_argument("--run-minutes", type=int, metavar="MIN", help="Schedule: minutes of work run.")
    p.add_argument("--break-minutes", type=int, metavar="MIN", help="Schedule: minutes of break.")
    p.set_defaults(handler=cmd_preferences_edit)


def _build_parser() -> tuple[argparse.ArgumentParser, frozenset[str]]:
    """Construct the argparse parser and return it alongside the
    frozen set of subcommand names registered on it.

    Returning the set here (rather than maintaining a separate
    ``_SUBCOMMANDS`` constant) keeps the implicit-chat dispatch in
    :func:`main` in sync with whatever subparsers we declare — new
    commands added to this function are automatically excluded from
    the chat fallback without a second edit.
    """
    parser = argparse.ArgumentParser(
        prog="pepper",
        description=(
            "Pepper CLI — talk to your agent and read what it holds for you. "
            "Bare `pepper` opens the REPL; `pepper <message>` is shorthand for "
            "`pepper chat <message>`. Use a subcommand for everything else."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    # :func:`main` consumes the one leading ``--profile`` before parsing (it
    # must precede implicit-chat dispatch). It is declared here so ``--help``
    # documents it, with an action that refuses: whatever argparse still
    # resolves to this option was not applied, and must not be ignored.
    parser.add_argument(
        "--profile",
        action=_RejectUnappliedProfile,
        default=argparse.SUPPRESS,
        metavar="NAME",
        help=(
            "Use a separate login stored under <config>/pepper/profiles/NAME/ "
            "(e.g. an agent token). Must come before the subcommand."
        ),
    )
    # ``metavar`` keeps the retired-noun stubs (no ``help=``) out of the usage brace list.
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p_login = sub.add_parser("login", help="Store credentials for a Pepper server.")
    p_login.add_argument(
        "--server",
        help=f"Server base URL (default: {_DEFAULT_SERVER}). Stored in the credentials file.",
    )
    p_login.add_argument(
        "--token",
        help=(
            "Personal access token — skips the browser device-login flow entirely "
            "(scripting / CI). Mint one at <server>/settings#tokens."
        ),
    )
    p_login.add_argument(
        "--manual",
        action="store_true",
        help=(
            "Force the interactive manual-token prompt instead of the browser "
            "device-login flow (reads without echoing)."
        ),
    )
    p_login.add_argument(
        "--no-browser",
        action="store_true",
        help=(
            "Device-login flow only: don't automatically open the verification URL "
            "in a browser (it's still printed)."
        ),
    )
    p_login.set_defaults(handler=cmd_login)

    p_whoami = sub.add_parser(
        "whoami", help="Print the user behind the current credentials.", parents=[_json_parent]
    )
    p_whoami.set_defaults(handler=cmd_whoami)

    p_tui = sub.add_parser(
        "tui",
        help="Open the full-screen terminal UI for your rooms and Pepper chat (needs a terminal).",
    )
    # The same grant as ``pepper chat --root``, for the Pepper DM turns sent from the UI:
    # read-only, enforced here, for the length of this session.
    p_tui.add_argument(
        "--root",
        "--tools-root",
        action="append",
        dest="root",
        metavar="PATH",
        help=(
            "Let Pepper read files under PATH on this machine during DM turns (repeatable). "
            "Read-only; the search runs here and only matches are sent."
        ),
    )
    p_tui.set_defaults(handler=cmd_tui)

    p_chat = sub.add_parser(
        "chat",
        help="Send one message (one-shot) or open the interactive REPL when no message is given.",
    )
    # ``nargs="*"`` so ``pepper chat hi there`` (unquoted) joins
    # cleanly into a single message — same shape the implicit-chat
    # dispatch in :func:`main` produces. An empty list (``pepper`` or
    # ``pepper chat``) drops into the REPL.
    p_chat.add_argument(
        "message",
        nargs="*",
        help="Message to send. Multi-word OK without quotes. Omit to open the interactive REPL.",
    )
    # Client-provided tools. Repeatable:
    # ``--root ~/notes --root ~/work``. Each one grants read-only access to
    # that folder **for the length of this invocation**, enforced here rather
    # than on the server — the server never learns what a root is, and a
    # tool takes no root argument, so the grant cannot be widened from the
    # other end.
    #
    # ``--tools-root`` is an accepted alias for ``--root``: both work rather
    # than picking one and stranding whoever read the other.
    p_chat.add_argument(
        "--root",
        "--tools-root",
        action="append",
        dest="root",
        metavar="PATH",
        help=(
            "Let this turn read files under PATH on this machine (repeatable). "
            "Read-only; the search runs here and only matches are sent."
        ),
    )
    p_chat.set_defaults(handler=cmd_chat)
    p_chat.add_argument(
        "--json",
        action="store_true",
        help="One-shot only: emit one JSON line per streamed event on stdout, the reply last "
        "(also on when PEPPER_JSON=1). The REPL ignores it.",
    )

    p_traj = sub.add_parser(
        "trajectories",
        help=(
            "Inspect saved trajectories you own (or share via a room). "
            "Use ``show <id>`` for one turn, ``list`` for recent turns."
        ),
    )
    # Nested subparser: ``pepper trajectories show ...`` / ``... list``.
    # ``required=True`` so ``pepper trajectories`` alone prints help rather
    # than silently doing nothing.
    sub_traj = p_traj.add_subparsers(dest="trajectory_action", required=True)
    p_traj_show = sub_traj.add_parser(
        "show",
        help="Print one trajectory's events. With --room, only if it is that room's.",
        parents=[
            _room_parent(
                omitted="Given, the trajectory must be one of that room's turns "
                "(a room-shared turn is readable without it)."
            ),
            _json_parent,
        ],
    )
    p_traj_show.add_argument("trajectory_id", help="Trajectory UUID.")
    p_traj_show.set_defaults(handler=cmd_trajectory_show)
    p_traj_list = sub_traj.add_parser(
        "list",
        help="List your recent trajectories, or one room's with --room.",
        parents=[
            _room_parent(omitted="Omit for your own, plus the rooms you share."),
            _json_parent,
        ],
    )
    p_traj_list.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Number of rows (default 20, max 200).",
    )
    p_traj_list.add_argument(
        "--before",
        metavar="CURSOR",
        help="Older than this cursor (a next_before value an earlier call printed).",
    )
    p_traj_list.set_defaults(handler=cmd_trajectory_list)
    _add_renamed_stub(sub, "trajectory", "see 'pepper trajectories --help'")

    p_brief = sub.add_parser(
        "brief",
        help="Your morning brief: the latest, or an older day. --now runs a fresh one.",
        description=(
            "Print the latest edition of your brief readably (dateline, lede, the day's events, "
            "feeds). --date YYYY-MM-DD reads an older one (the app keeps about a month). "
            "--now asks the server to run and deliver a fresh brief, which takes about a minute "
            "and arrives where your brief is delivered; it prints the server's answer and does "
            "not wait."
        ),
        parents=[_json_parent],
    )
    p_brief.add_argument("--date", metavar="YYYY-MM-DD", help="An older edition.")
    p_brief.add_argument(
        "--now",
        action="store_true",
        help="Run and deliver a fresh brief now (a write; not with --date).",
    )
    p_brief.set_defaults(handler=cmd_brief)

    p_cal = sub.add_parser(
        "calendar",
        help="Read-only agenda: today, a week, or any window.",
        description=(
            "Show your calendar as a per-day agenda with weather, daylight and commute. "
            "today (the default), week and schedule are shortcuts; --start/--days pick any "
            "window (1-31 days). --room reads a room's calendar instead."
        ),
        parents=[_room_parent(omitted="Omit for your own calendar."), _json_parent],
    )
    p_cal.add_argument(
        "window",
        nargs="?",
        choices=["today", "week", "schedule"],
        help="today (default), week, or schedule (events plus timed todo deadlines).",
    )
    p_cal.add_argument("--start", help="First day, YYYY-MM-DD (default: today).")
    p_cal.add_argument("--days", type=int, help="Number of days, 1-31 (default: 1; week = 7).")
    p_cal.add_argument(
        "--planning",
        action="store_true",
        help="Add Planning: followed events, email suggestions, meal windows, suggestion cards "
        "(max 7 days).",
    )
    p_cal.set_defaults(handler=cmd_calendar)

    p_next = sub.add_parser(
        "next",
        help="What is on now, today's calendar, your open todos in Pepper's order (read-only).",
        description=(
            "Now (the live event and the next upcoming event), today's calendar, and your open "
            "todos in the order Pepper returns them. Reads only; the CLI does no ranking."
        ),
    )
    p_next.add_argument(
        "--room",
        metavar="ROOM",
        help="Only this room's todos: its UUID, or a case-insensitive unique name prefix.",
    )
    p_next.add_argument(
        "--source",
        metavar="PREFIX",
        help="Only todos whose source (user, or a producer name) starts with this "
        "(case-insensitive).",
    )
    p_next.add_argument("--all", action="store_true", help="List every todo, not just the top 10.")
    p_next.add_argument("--json", action="store_true", help="Emit the assembled result as JSON.")
    p_next.set_defaults(handler=cmd_next)

    p_following = sub.add_parser(
        "following",
        help="Teams, shows, podcasts and websites you follow: search, follow, unfollow.",
        description=(
            "Following is two steps: 'following search QUERY' lists numbered matches, then "
            "'following create N' follows number N. 'create' only takes a number; it never "
            "picks a match for typed text. For a website, search its address, then create "
            "from the result (e.g. 'following search https://simonwillison.net', then "
            "'following create 1'); for a team, search its name (e.g. 'following search utah "
            "utes') and pick the sport. 'following delete' stops following."
        ),
    )
    sub_following = p_following.add_subparsers(dest="following_action", required=True)
    p = sub_following.add_parser(
        "list",
        parents=[_json_parent],
        help="List what you follow, by group, with each one's latest line.",
    )
    p.add_argument(
        "--group",
        metavar="GROUP",
        help="Only this group (a name or prefix, as `pepper following list` shows it).",
    )
    p.set_defaults(handler=cmd_following_list)
    p = sub_following.add_parser(
        "show", parents=[_json_parent], help="One followed thing and its recent items."
    )
    p.add_argument("item", metavar="ITEM", help="Its id from `following list`, or a name prefix.")
    p.set_defaults(handler=cmd_following_show)

    p = sub_following.add_parser(
        "search",
        parents=[_json_parent],
        help="Find something to follow: a team, show, podcast, or a website's address. "
        "Prints numbered matches for 'following create N'.",
    )
    p.add_argument(
        "query",
        nargs="+",
        metavar="QUERY",
        help="A name (e.g. 'utah utes', 'the moth') or a web address.",
    )
    p.set_defaults(handler=cmd_following_search)
    p = sub_following.add_parser(
        "create",
        parents=[_json_parent],
        help="Follow number N from your last 'following search' (search first, even for a "
        "web address).",
    )
    p.add_argument(
        "number",
        metavar="N",
        help="A number from the last 'following search'.",
    )
    p.set_defaults(handler=cmd_following_create)
    p = sub_following.add_parser(
        "delete",
        parents=[_json_parent, _yes_parent],
        help="Stop following something (the app keeps it, with Follow again). Asks first.",
    )
    p.add_argument("item", metavar="ITEM", help="Its id from `following list`, or a name prefix.")
    p.set_defaults(handler=cmd_following_delete)

    p_feed = sub.add_parser(
        "feed",
        help="What's new in everything you follow, as the server ranks it: list, show.",
        description=(
            "The Pepper server's ranked list of the recent items of everything you follow "
            "(best first). 'feed list' prints it numbered; 'feed show N' shows item N of the "
            "last list in full. The CLI does no ranking of its own. Read-only; manage what you "
            "follow with 'following'."
        ),
    )
    sub_feed = p_feed.add_subparsers(dest="feed_action", required=True)
    p = sub_feed.add_parser(
        "list",
        parents=[_json_parent],
        help="The server's ranked list, numbered (for 'feed show N').",
    )
    p.add_argument("--limit", type=int, default=20, metavar="N", help="At most N items (20).")
    p.add_argument(
        "--group",
        metavar="GROUP",
        help="Only this group (a name or prefix, as `pepper following list` shows it).",
    )
    p.set_defaults(handler=cmd_feed_list)
    p = sub_feed.add_parser(
        "show", parents=[_json_parent], help="Item N of the last 'feed list' in full."
    )
    p.add_argument("item", metavar="N", help="A number from the last 'feed list' (up to 4 digits).")
    p.add_argument(
        "--transcript",
        action="store_true",
        help="A podcast episode's transcript (its show notes until one exists).",
    )
    p.add_argument(
        "--read",
        action="store_true",
        help="Fetch the article's page from its own site and print its readable text.",
    )
    p.add_argument(
        "--full", action="store_true", help="With --read: every paragraph, not the first twelve."
    )
    p.add_argument(
        "--open",
        action="store_true",
        help="Open the item's link in your default browser (the link is printed too).",
    )
    p.set_defaults(handler=cmd_feed_show)

    p_todos = sub.add_parser(
        "todos",
        help=(
            "Your tracked action items. ``list`` to see them, ``create`` to add one, "
            "``done``/``dismiss`` to close one."
        ),
    )
    # Nested subparser: ``pepper todos list`` / ``add`` / ``done`` /
    # ``dismiss``. ``required=True`` so bare ``pepper todos`` prints help.
    sub_todos = p_todos.add_subparsers(dest="todos_action", required=True)

    p_todos_list = sub_todos.add_parser("list", help="List your todos (open by default).")
    p_todos_list.add_argument(
        "--status",
        choices=("open", "done", "dismissed", "all"),
        default="open",
        help="Which todos to show (default: open).",
    )
    p_todos_list.add_argument("--json", action="store_true", help="Emit the raw JSON response.")
    p_todos_list.set_defaults(handler=cmd_todo_list)

    p_todos_add = sub_todos.add_parser("create", help="Create a todo.", parents=[_json_parent])
    # ``nargs="+"`` so ``pepper todos create buy milk`` joins unquoted words
    # into one title, mirroring ``pepper chat``'s message handling.
    p_todos_add.add_argument("title", nargs="+", help="The todo title. Multi-word OK.")
    p_todos_add.add_argument("--details", help="Optional longer description.")
    p_todos_add.add_argument(
        "--due",
        help="Soft deadline: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' (your local time; "
        "with --room, 'YYYY-MM-DD' only).",
    )
    p_todos_add.add_argument("--priority", help="Free-form priority label, e.g. high.")
    p_todos_add.set_defaults(handler=cmd_todo_create)

    for name, handler, text in (
        ("done", cmd_todo_done, "Mark a todo done, as ticking it on Schedule does."),
        ("dismiss", cmd_todo_dismiss, "Dismiss a todo (close without completing)."),
        ("reopen", cmd_todo_reopen, "Put a done or dismissed todo back on the list."),
    ):
        p_todo = sub_todos.add_parser(name, help=text, parents=[_json_parent])
        p_todo.add_argument("todo_id", help="Todo UUID (from ``todos list`` or ``next --json``).")
        p_todo.set_defaults(handler=handler)
    for name in ("list", "create", "done", "dismiss", "reopen"):
        sub_todos.choices[name].add_argument(
            "--room", help=_GROUP_ARG_HELP + " Acts on the room's todos instead of yours."
        )

    p_tasks = sub.add_parser(
        "scheduled-tasks",
        help=(
            "Your scheduled prompts. ``list``/``show``/``runs``/``thread`` to "
            "inspect, ``create``/``edit`` to change one, ``pause``/``resume`` to "
            "take it off and put it back on the schedule, ``renew`` to keep "
            "one going, ``delete`` to remove one of yours for good."
        ),
    )
    sub_tasks = p_tasks.add_subparsers(dest="scheduled_tasks_action", required=True)

    p_tasks_list = sub_tasks.add_parser(
        "list", help="List your scheduled tasks (active only).", parents=[_json_parent]
    )
    p_tasks_list.add_argument(
        "--all",
        action="store_true",
        help="Include paused tasks and spent one-offs.",
    )
    p_tasks_list.set_defaults(handler=cmd_scheduled_task_list)

    p_tasks_show = sub_tasks.add_parser(
        "show", help="Show one task's config and its last run.", parents=[_json_parent]
    )
    p_tasks_show.add_argument("task", help=_TASK_ARG_HELP)
    p_tasks_show.set_defaults(handler=cmd_scheduled_task_show)

    p_tasks_runs = sub_tasks.add_parser(
        "runs",
        help="A task's fire history, newest first.",
        parents=[_json_parent, _before_parent],
    )
    p_tasks_runs.add_argument("task", help=_TASK_ARG_HELP)
    p_tasks_runs.add_argument(
        "--limit", type=int, default=20, help="Number of runs (default 20, max 100)."
    )
    p_tasks_runs.set_defaults(handler=cmd_scheduled_task_runs)

    p_tasks_thread = sub_tasks.add_parser(
        "thread",
        help="A task's own conversation (its results and your replies).",
        parents=[_json_parent, _before_parent],
    )
    p_tasks_thread.add_argument("task", help=_TASK_ARG_HELP)
    p_tasks_thread.add_argument(
        "--limit", type=int, default=20, help="Number of messages (default 20, max 100)."
    )
    p_tasks_thread.set_defaults(handler=cmd_scheduled_task_thread)

    p_tasks_add = sub_tasks.add_parser(
        "create",
        help="Schedule a new task. Pass exactly one of --cron or --at, and --prompt.",
        parents=[_json_parent],
    )
    p_tasks_add.add_argument(
        "name", help="Slug for the task (lowercase letters, digits, hyphens; 1-64 chars)."
    )
    p_tasks_add.add_argument(
        "--prompt",
        help=(
            "What the agent should do when it fires, written as if you were "
            "asking at that moment. Don't include delivery instructions — "
            "that's --to's job. Required, except with --room and --skill."
        ),
    )
    p_tasks_add.add_argument(
        "--skill",
        metavar="NAME",
        help=(
            "With --room: run a skill shared with that room when it fires "
            "(alone, or alongside --prompt). Not for personal tasks."
        ),
    )
    p_tasks_add.add_argument("--cron", help="Five-field cron for a recurring task ('0 8 * * 1-5').")
    p_tasks_add.add_argument(
        "--at",
        help=(
            "One-off fire time: 'YYYY-MM-DD HH:MM' in your local time. "
            "Mutually exclusive with --cron."
        ),
    )
    p_tasks_add.add_argument(
        "--timezone",
        help="IANA zone the cron fires in, pinned (default: your profile zone). Cron tasks only.",
    )
    p_tasks_add.add_argument("--description", help="Short label shown in listings.")
    p_tasks_add.add_argument(
        "--to",
        action="append",
        choices=_ANY_DESTINATION_CHOICES,
        metavar="DEST",
        help=(
            "Where to announce the result; repeatable. "
            f"Choices: {_TO_CHOICES_TEXT}. Omit to use your default "
            "notification channel (with --room, to let the server pick the room's delivery)."
        ),
    )
    p_tasks_add.add_argument(
        "--silent",
        action="store_true",
        help="Announce nowhere — the result still lands in the task's own thread.",
    )
    p_tasks_add.set_defaults(handler=cmd_scheduled_task_create)

    p_tasks_edit = sub_tasks.add_parser(
        "edit",
        help="Change an existing task. Only the flags you pass are written.",
        parents=[_json_parent],
    )
    p_tasks_edit.add_argument("task", help=_TASK_ARG_HELP)
    p_tasks_edit.add_argument("--prompt", help="Replace what the agent does when it fires.")
    p_tasks_edit.add_argument("--cron", help="New cron (recurring tasks only).")
    p_tasks_edit.add_argument("--at", help="New fire time, local (one-off tasks only).")
    p_tasks_edit.add_argument("--timezone", help="New pinned zone (recurring tasks only).")
    p_tasks_edit.add_argument("--description", help="Replace the label.")
    p_tasks_edit.add_argument(
        "--clear-description",
        action="store_true",
        help="Remove the label entirely (distinct from leaving it alone).",
    )
    p_tasks_edit.add_argument(
        "--to",
        action="append",
        choices=_ANY_DESTINATION_CHOICES,
        metavar="DEST",
        help=(
            "Where to announce the result, replacing the whole set; repeatable. "
            f"Choices: {_TO_CHOICES_TEXT}."
        ),
    )
    p_tasks_edit.add_argument(
        "--default",
        action="store_true",
        dest="use_default",
        help="Follow your notification preference instead of a pinned set. Not with --room.",
    )
    p_tasks_edit.add_argument(
        "--silent",
        action="store_true",
        help="Announce nowhere. Distinct from --default: a later change to your "
        "notification preference won't un-silence it.",
    )
    p_tasks_edit.add_argument(
        "--deliver-topic",
        metavar="NAME",
        help="With --room and --to room: the name of the room topic the result is posted into.",
    )
    p_tasks_edit.set_defaults(handler=cmd_scheduled_task_edit)

    p_tasks_delete = sub_tasks.add_parser(
        "delete",
        help="Delete a task for good, with its history. Pause keeps it.",
        parents=[_json_parent, _yes_parent],
    )
    p_tasks_delete.add_argument("task", help=_TASK_ARG_HELP)
    p_tasks_delete.set_defaults(handler=cmd_scheduled_task_delete)

    p_tasks_pause = sub_tasks.add_parser(
        "pause",
        help="Take a task off the schedule. Its history is kept and it can be resumed.",
        parents=[_json_parent],
    )
    p_tasks_pause.add_argument("task", help=_TASK_ARG_HELP)
    p_tasks_pause.set_defaults(handler=cmd_scheduled_task_pause)

    p_tasks_resume = sub_tasks.add_parser(
        "resume",
        help="Put a paused task back on the schedule, next fire re-derived.",
        parents=[_json_parent],
    )
    p_tasks_resume.add_argument("task", help=_TASK_ARG_HELP)
    p_tasks_resume.set_defaults(handler=cmd_scheduled_task_resume)

    p_tasks_renew = sub_tasks.add_parser(
        "renew",
        help="Keep a task going past its renewal warning. Prints the server's reason if refused.",
        parents=[_json_parent],
    )
    p_tasks_renew.add_argument("task", help=_TASK_ARG_HELP)
    p_tasks_renew.set_defaults(handler=cmd_scheduled_task_renew)

    for name in ("show", "runs", "thread", "renew", "delete"):
        sub_tasks.choices[name].add_argument(
            "--room",
            metavar="ROOM",
            help=(
                _ROOM_ARG_HELP
                + (
                    " Any member can read a room's tasks."
                    if name not in ("renew", "delete")
                    else f" Only a room owner can {name} one."
                )
            ),
        )
    for name in ("list", "create", "pause", "resume"):
        sub_tasks.choices[name].add_argument(
            "--room",
            help=(
                _GROUP_ARG_HELP + " Acts on the room's scheduled tasks instead of yours. "
                "Any member can list them; only a room owner can change them."
            ),
        )
    sub_tasks.choices["edit"].add_argument(
        "--room",
        metavar="ROOM",
        help=(
            f"{_ROOM_ARG_HELP} Edits the room's task instead of yours: prompt, schedule, "
            "label and delivery in one atomic request. Only a room owner can. "
            "To pause or resume it use those commands."
        ),
    )
    sub_tasks.choices["create"].add_argument(
        "--deliver-topic",
        metavar="NAME",
        help="With --room and --to room: the name of the room topic the result is posted into.",
    )

    p_skills = sub.add_parser(
        "skills",
        help=(
            "Your skill library. ``list``/``show`` to inspect, ``create``/``edit`` "
            "to author in $EDITOR or from --file, ``delete`` to remove one of yours. "
            "``share``/``revoke`` with --room put one in a room or take it out."
        ),
    )
    sub_skills = p_skills.add_subparsers(dest="skills_action", required=True)

    p_skills_list = sub_skills.add_parser(
        "list", help="List your skills and Pepper's built-ins.", parents=[_json_parent]
    )
    p_skills_list.set_defaults(handler=cmd_skills_list)

    p_skills_show = sub_skills.add_parser(
        "show", help="Print one skill as SKILL.md.", parents=[_json_parent]
    )
    p_skills_show.add_argument("name", help="Skill name (from ``skills list``).")
    p_skills_show.set_defaults(handler=cmd_skills_show)

    p_skills_new = sub_skills.add_parser(
        "create",
        help="Author a new skill in $EDITOR (or from --file) and save it.",
        parents=[_json_parent],
    )
    p_skills_new.add_argument("name", help="Slug name for the new skill.")
    p_skills_new.add_argument(
        "--file",
        metavar="PATH",
        help="Read the SKILL.md document from this path, or '-' for stdin, instead of $EDITOR.",
    )
    p_skills_new.set_defaults(handler=cmd_skills_create)

    p_skills_edit = sub_skills.add_parser(
        "edit",
        help=(
            "Edit a skill in $EDITOR. Editing a built-in creates a shadowing "
            "override on your account."
        ),
        parents=[_json_parent],
    )
    p_skills_edit.add_argument("name", help="Skill name (from ``skills list``).")
    p_skills_edit.add_argument(
        "--file",
        metavar="PATH",
        help="Read the new SKILL.md document from this path, or '-' for stdin, instead of $EDITOR.",
    )
    p_skills_edit.set_defaults(handler=cmd_skills_edit)

    p_skills_delete = sub_skills.add_parser(
        "delete",
        help="Delete one of YOUR skills (built-ins can't be deleted).",
        parents=[_yes_parent, _json_parent],
    )
    p_skills_delete.add_argument("name", help="Skill name (from ``skills list``).")
    p_skills_delete.set_defaults(handler=cmd_skills_delete)

    p_skills_share = sub_skills.add_parser(
        "share",
        help=(
            "Share one of YOUR skills with a room. The room gets a live reference, "
            "not a copy: your later edits show there too."
        ),
        parents=[_json_parent],
    )
    p_skills_share.add_argument("name", help="Name of one of your own skills.")
    p_skills_share.add_argument("--room", required=True, help=_GROUP_ARG_HELP)
    p_skills_share.set_defaults(handler=cmd_skills_share)

    p_skills_revoke = sub_skills.add_parser(
        "revoke",
        help=(
            "Stop sharing a skill with a room. The skill itself is untouched. "
            "Its owner can always do this; a room owner can in a channel."
        ),
        parents=[_yes_parent, _json_parent],
    )
    p_skills_revoke.add_argument("name", help=_ROOM_SKILL_ARG_HELP)
    p_skills_revoke.add_argument("--room", required=True, help=_GROUP_ARG_HELP)
    p_skills_revoke.set_defaults(handler=cmd_skills_revoke)

    for name in ("list", "show"):
        sub_skills.choices[name].add_argument(
            "--room",
            help=(
                _GROUP_ARG_HELP + " Reads the skills shared with the room instead of "
                "your own library. Any member can."
            ),
        )
    for name in ("create", "edit", "delete"):
        # Accepted so the refusal can explain itself (exit 2) instead of
        # argparse's "unrecognized arguments".
        sub_skills.choices[name].add_argument(
            "--room",
            help="Not available: a room never owns a skill. See ``skills share``.",
        )

    # --- rooms, topics, messages, attachments (§4) ---------------------------------------
    # ``groups`` is a retired noun (§7); its leaves live under the nouns below and
    # ``members``, ``sharing`` and ``apps``.
    _add_renamed_stub(
        sub,
        "groups",
        "use 'pepper rooms', 'topics', 'messages', 'attachments', 'members', 'sharing' "
        "or 'apps' (run 'pepper --help')",
    )

    p_rooms = sub.add_parser(
        "rooms",
        help=(
            "Your rooms. ``list``/``show`` to inspect, ``create``/``edit`` to change, "
            "``watch`` to follow events."
        ),
    )
    sub_rooms = p_rooms.add_subparsers(dest="rooms_action", required=True)

    p = sub_rooms.add_parser(
        "list", parents=[_json_parent], help="List your rooms (unarchived by default)."
    )
    p.add_argument(
        "--archived", action="store_true", help="List only archived rooms, instead of the default."
    )
    p.set_defaults(handler=cmd_rooms_list)

    p = sub_rooms.add_parser(
        "show",
        parents=[_json_parent],
        help="One room's info: kind, members + roles, topics, installed apps.",
    )
    p.add_argument("room", help=_ROOM_ARG_HELP)
    p.set_defaults(handler=cmd_rooms_show)

    p = sub_rooms.add_parser(
        "create",
        parents=[_json_parent],
        help="Create a named room with these people (--name is required).",
    )
    p.add_argument(
        "--name",
        help="Room name. Required: email and text threads are created by the system, not here.",
    )
    p.add_argument(
        "people", nargs="+", help="Email, phone, or a contact's name (looked up in your Contacts)."
    )
    p.set_defaults(handler=cmd_rooms_create)

    p = sub_rooms.add_parser("edit", parents=[_json_parent], help="Change a room (its name).")
    p.add_argument("room", help=_ROOM_ARG_HELP)
    p.add_argument("--name", required=True, help="The new room name.")
    p.set_defaults(handler=cmd_rooms_edit)

    p = sub_rooms.add_parser(
        "archive", parents=[_json_parent], help="Archive a room (your view only)."
    )
    p.add_argument("room", help=_ROOM_ARG_HELP)
    p.add_argument(
        "--yes", action="store_true", help="Accepted and ignored: archiving asks nothing."
    )
    p.set_defaults(handler=cmd_rooms_archive)
    p = sub_rooms.add_parser("unarchive", parents=[_json_parent], help="Unarchive a room.")
    p.add_argument("room", help=_ROOM_ARG_HELP)
    p.set_defaults(handler=cmd_rooms_unarchive)
    p = sub_rooms.add_parser(
        "leave", parents=[_json_parent, _yes_parent], help="Leave an open room."
    )
    p.add_argument("room", help=_ROOM_ARG_HELP)
    p.set_defaults(handler=cmd_rooms_leave)
    p = sub_rooms.add_parser(
        "mute",
        parents=[_json_parent],
        help="Stop push notifications from a room (your devices only).",
    )
    p.add_argument("room", help=_ROOM_ARG_HELP)
    p.set_defaults(handler=cmd_rooms_mute)
    p = sub_rooms.add_parser(
        "unmute", parents=[_json_parent], help="Resume push notifications from a room."
    )
    p.add_argument("room", help=_ROOM_ARG_HELP)
    p.set_defaults(handler=cmd_rooms_unmute)

    p = sub_rooms.add_parser(
        "activity",
        parents=[_room_parent(omitted="Omit for all your rooms."), _json_parent],
        help="Newest messages from others in your rooms' topics.",
    )
    p.add_argument(
        "--since",
        metavar="TIME",
        help="ISO-8601 time (the route takes a time, not a cursor). Default: 24 hours ago.",
    )
    p.set_defaults(handler=cmd_rooms_activity)

    p_rooms_watch = sub_rooms.add_parser(
        "watch",
        help="Stream your rooms' events as an agent member (one line per event).",
        description=(
            "Follow the event stream of the agent whose token you are signed in with "
            "(a pep_agt_ token). Prints one line per event, reconnecting from the last "
            "cursor with backoff when the stream drops, and exits non-zero if the "
            "token is revoked. Events carry ids, not message bodies: read a body "
            "with 'messages list'. --room (and --topic) narrow the stream on the server "
            "to one room, or one topic in it; a server that cannot do that is an "
            "error, never a wider stream. Under --room, a mention of you in another "
            "room is not shown; under --topic, room-wide events (membership, room "
            "renames) are not shown either."
        ),
    )
    p_rooms_watch.add_argument(
        "--room",
        default=None,
        metavar="ROOM",
        help="Only this room's events (an id, or a name or unique name prefix). "
        "Exit 2 if it does not resolve or you are not a member.",
    )
    p_rooms_watch.add_argument(
        "--topic",
        default=None,
        metavar="TOPIC",
        help="Only this topic's events, within --room (required). Without it, every "
        "topic in the room.",
    )
    p_rooms_watch.add_argument(
        "--since",
        type=int,
        default=None,
        metavar="CURSOR",
        help="Resume after this cursor (an event id, or a cursor frame's value). "
        "Default: the cursor file's, else the beginning.",
    )
    p_rooms_watch.add_argument(
        "--cursor-file",
        default=None,
        metavar="PATH",
        help="Persist the cursor here (atomically, after each event is printed) and "
        "start from it when --since is not given. Use one file per scope: a scoped "
        "watch's file records its room and topic, and a file from another scope is "
        "refused (exit 2).",
    )
    p_rooms_watch.add_argument(
        "--json",
        action="store_true",
        help='One JSON object per line: the event\'s fields plus "cursor"; a cursor '
        'frame is {"kind": "cursor", "cursor": N}.',
    )
    p_rooms_watch.add_argument(
        "--kinds",
        type=_kind_list,
        default=None,
        metavar="KIND[,KIND...]",
        help="Only print events of these kinds (comma-separated, e.g. "
        "message.created,mention.created); every other event is skipped but the "
        "cursor still moves past it. Default: all kinds.",
    )
    p_rooms_watch.add_argument(
        "--ignore-kinds",
        type=_kind_list,
        default=None,
        metavar="KIND[,KIND...]",
        help="Skip events of these kinds (comma-separated, e.g. "
        "message.link_cards_updated); the cursor still moves past them. Kinds not "
        "listed, including ones added later, still print. Combines with --kinds.",
    )
    p_rooms_watch.set_defaults(handler=cmd_rooms_watch)

    p_topics = sub.add_parser(
        "topics",
        help="A room's topics. ``list``/``search`` to find one, ``create``/``edit`` to change.",
    )
    sub_topics = p_topics.add_subparsers(dest="topics_action", required=True)
    _topic_arg = "Topic: its UUID, or a case-insensitive unique name prefix."

    p = sub_topics.add_parser(
        "list", parents=[_room_parent(required=True), _json_parent], help="List a room's topics."
    )
    p.add_argument(
        "--archived", action="store_true", help="Include archived topics (folded by default)."
    )
    p.set_defaults(handler=cmd_topics_list)
    p = sub_topics.add_parser(
        "create",
        parents=[_room_parent(required=True), _json_parent],
        help="Start a topic in a room.",
    )
    p.add_argument("name", nargs="+", help="The topic name. Multi-word OK without quotes.")
    p.set_defaults(handler=cmd_topics_create)
    p = sub_topics.add_parser(
        "edit",
        parents=[_room_parent(required=True), _json_parent],
        help="Change a topic (its name).",
    )
    p.add_argument("topic", help=_topic_arg)
    p.add_argument("--name", required=True, help="The new topic name.")
    p.set_defaults(handler=cmd_topics_edit)
    p = sub_topics.add_parser(
        "archive", parents=[_room_parent(required=True), _json_parent], help="Archive a topic."
    )
    p.add_argument("topic", help=_topic_arg)
    p.set_defaults(handler=cmd_topics_archive)
    p = sub_topics.add_parser(
        "unarchive", parents=[_room_parent(required=True), _json_parent], help="Unarchive a topic."
    )
    p.add_argument("topic", help=_topic_arg)
    p.set_defaults(handler=cmd_topics_unarchive)
    p = sub_topics.add_parser(
        "search",
        parents=[_room_parent(omitted="Omit to search all your rooms."), _json_parent],
        help="Find topics by name (topic names only) across your unarchived rooms.",
    )
    p.add_argument("query", nargs="+", help="Search text. Multi-word OK without quotes.")
    p.set_defaults(handler=cmd_topics_search)

    p_messages = sub.add_parser(
        "messages",
        help="A topic's messages. ``list`` to read, ``post`` to send, ``edit``/``react``.",
    )
    sub_messages = p_messages.add_subparsers(dest="messages_action", required=True)
    _mid = "Message id (from 'messages list --json')."
    _room_req = _room_parent(required=True)

    p = sub_messages.add_parser(
        "list",
        parents=[_room_req, _topic_parent(), _page_parent, _json_parent],
        help="Read a topic's messages, oldest to newest.",
    )
    p.add_argument(
        "--mark", action="store_true", help="Then mark the topic read through its newest message."
    )
    p.set_defaults(handler=cmd_messages_list)

    p = sub_messages.add_parser(
        "post",
        parents=[_room_req, _topic_parent(required=True), _json_parent],
        help="Post a message into a topic, as yourself.",
    )
    p.add_argument(
        "message",
        nargs="*",
        help=(
            "Message text. Multi-word OK without quotes. "
            "Pass '-' alone to read the body from stdin."
        ),
    )
    p.add_argument(
        "--attach",
        action="append",
        metavar="PATH",
        help="Attach a file; repeatable. Uploaded before the message is posted.",
    )
    p.add_argument(
        "--reply-to",
        metavar="MESSAGE_ID",
        help="Thread the post onto this message (its id, from 'messages list --json').",
    )
    p.add_argument(
        "--mention",
        action="append",
        metavar="PERSON_ID",
        help=(
            "Structurally mention this person id (from 'members list'); repeatable. "
            "Keep their '@Display Name' in the message text."
        ),
    )
    p.add_argument(
        "--on-behalf-of",
        metavar="PERSON_ID",
        help=(
            "Agent tokens only: post as answering this person id, so the room shows "
            "the reply 'for' them. Needs --reply-to naming their message."
        ),
    )
    p.add_argument(
        "--idempotency-key",
        metavar="KEY",
        help=(
            "Send this Idempotency-Key instead of a fresh one, so re-running the same "
            "command cannot post twice (e.g. the id of the message being answered)."
        ),
    )
    p.set_defaults(handler=cmd_messages_post)

    p = sub_messages.add_parser(
        "edit", parents=[_room_req, _json_parent], help="Edit one of your messages."
    )
    p.add_argument("message", help=_mid)
    p.add_argument(
        "text",
        nargs="*",
        help="The new message text. Multi-word OK without quotes. Pass '-' alone to read stdin.",
    )
    p.set_defaults(handler=cmd_messages_edit)
    p = sub_messages.add_parser(
        "delete",
        parents=[_room_req, _json_parent, _yes_parent],
        help="Delete one of your messages.",
    )
    p.add_argument("message", help=_mid)
    p.set_defaults(handler=cmd_messages_delete)
    p = sub_messages.add_parser(
        "react", parents=[_room_req, _json_parent], help="React to a message."
    )
    p.add_argument("message", help=_mid)
    p.add_argument("emoji", help="The emoji.")
    p.set_defaults(handler=cmd_messages_react)
    p = sub_messages.add_parser(
        "unreact", parents=[_room_req, _json_parent], help="Take your reaction back."
    )
    p.add_argument("message", help=_mid)
    p.add_argument("emoji", help="The emoji.")
    p.set_defaults(handler=cmd_messages_unreact)

    p_attachments = sub.add_parser("attachments", help="A message's attachments. ``download`` one.")
    sub_attachments = p_attachments.add_subparsers(dest="attachments_action", required=True)
    p = sub_attachments.add_parser(
        "download",
        parents=[_room_parent(required=True), _json_parent],
        help="Download a message attachment.",
    )
    p.add_argument("attachment", help="Attachment id (from 'messages list --json').")
    p.add_argument(
        "--out",
        required=True,
        metavar="PATH",
        help="Where to write it; '-' for stdout (not under --json).",
    )
    p.set_defaults(handler=cmd_attachments_download)

    # --- members, sharing, apps (§4) -------------------------------------------------------
    p_members = sub.add_parser(
        "members",
        help="A room's people. ``list`` who is in it, ``invite``/``remove`` them, ``edit --role``.",
    )
    sub_members = p_members.add_subparsers(dest="members_action", required=True)
    _person_arg = "A member's name (unique prefix OK) or person id (from 'members list')."

    p = sub_members.add_parser(
        "list",
        parents=[_room_parent(required=True), _json_parent],
        help="List a room's members, Pepper first, with the person id a --mention takes.",
    )
    p.set_defaults(handler=cmd_members_list)
    p = sub_members.add_parser(
        "invite",
        parents=[_room_parent(required=True), _json_parent],
        help="Invite someone to an open room.",
    )
    p.add_argument("handle", help="Email or phone.")
    p.set_defaults(handler=cmd_members_invite)
    p = sub_members.add_parser(
        "remove",
        parents=[_room_parent(required=True), _json_parent, _yes_parent],
        help="Remove a member from a room.",
    )
    p.add_argument("person", help=_person_arg)
    p.set_defaults(handler=cmd_members_remove)
    p = sub_members.add_parser(
        "edit",
        parents=[_room_parent(required=True), _json_parent, _yes_parent],
        help="Change a member (their role: owner or plain member).",
    )
    p.add_argument("person", help=_person_arg)
    p.add_argument("--role", required=True, choices=("owner", "member"), help="The new role.")
    p.set_defaults(handler=cmd_members_edit)

    p_sharing = sub.add_parser(
        "sharing",
        help="Which of your calendars a room sees. ``show`` the levels, ``edit`` them.",
    )
    sub_sharing = p_sharing.add_subparsers(dest="sharing_action", required=True)
    p = sub_sharing.add_parser(
        "show",
        parents=[_room_parent(required=True), _json_parent],
        help="Which of your calendars this room sees, and at what level.",
    )
    p.set_defaults(handler=cmd_sharing_show)
    p = sub_sharing.add_parser(
        "edit",
        parents=[_room_parent(required=True), _json_parent],
        help="Set the level at which this room sees a calendar of yours.",
    )
    p.add_argument(
        "--set",
        action="append",
        required=True,
        metavar="CALENDAR=LEVEL",
        help="Set a calendar (name or id) to a level, or 'default'; repeatable. "
        "Calendars not named keep theirs.",
    )
    p.set_defaults(handler=cmd_sharing_edit)

    p_apps = sub.add_parser(
        "apps",
        help=(
            "A room's apps: ``list`` them and the installable ones, ``install``/``uninstall``. "
            "``show`` an app's data, ``run`` one of its actions."
        ),
    )
    sub_apps = p_apps.add_subparsers(dest="apps_action", required=True)
    p = sub_apps.add_parser(
        "list",
        parents=[_room_parent(required=True), _json_parent],
        help="A room's installed apps (launcher tiles), and the ones you can install.",
    )
    p.set_defaults(handler=cmd_apps_list)
    p = sub_apps.add_parser(
        "install",
        parents=[_room_parent(required=True), _json_parent],
        help="Install an app in a room.",
    )
    p.add_argument("app_key", help="App key (see 'apps list').")
    p.set_defaults(handler=cmd_apps_install)
    p = sub_apps.add_parser(
        "uninstall",
        parents=[_room_parent(required=True), _json_parent, _yes_parent],
        help="Uninstall an app from a room.",
    )
    p.add_argument("app", help="Installed app's key, title or install id.")
    p.set_defaults(handler=cmd_apps_uninstall)
    p = sub_apps.add_parser(
        "show",
        parents=[_room_parent(omitted="Omit for your own data."), _json_parent],
        help="An app's data document, yours or a room's.",
    )
    p.add_argument("slug", help="App slug (for example 'notes').")
    p.set_defaults(handler=cmd_apps_show)
    p = sub_apps.add_parser(
        "run",
        parents=[_room_parent(omitted="Omit to act on your own data."), _json_parent],
        help="Run an app action. With no ACTION, list the owner-scoped actions the server names.",
    )
    p.add_argument("slug", help="App slug (for example 'notes').")
    p.add_argument("action", nargs="?", help="Action name. Omit to list the ones the server names.")
    p.add_argument(
        "--param",
        action="append",
        metavar="KEY=VALUE",
        help="An action parameter, sent as a string; repeatable.",
    )
    p.set_defaults(handler=cmd_apps_run)

    p_notes = sub.add_parser(
        "notes",
        help=(
            "Your text artifacts. ``list``/``search`` to find one, ``show`` "
            "to print it, ``create``/``edit``/``delete`` to write it. ``--room`` "
            "scopes to a room's shared notes instead of your own."
        ),
    )
    sub_notes = p_notes.add_subparsers(dest="notes_action", required=True)

    p_notes_list = sub_notes.add_parser(
        "list", help="List your notes (or a room's, with --room).", parents=[_json_parent]
    )
    p_notes_list.add_argument("--room", help=_GROUP_ARG_HELP + " Omit for your own notes.")
    p_notes_list.add_argument("--label", help="Only notes carrying this label.")
    p_notes_list.set_defaults(handler=cmd_notes_list)

    p_notes_search = sub_notes.add_parser(
        "search",
        help="Case-insensitive substring search over note names and bodies.",
        parents=[_json_parent],
    )
    p_notes_search.add_argument(
        "query", nargs="+", help="Search text. Multi-word OK without quotes."
    )
    p_notes_search.add_argument("--room", help=_GROUP_ARG_HELP + " Omit for your own notes.")
    p_notes_search.add_argument("--label", help="Only notes carrying this label.")
    p_notes_search.set_defaults(handler=cmd_notes_search)

    p_notes_show = sub_notes.add_parser(
        "show",
        help="Print one note's markdown body to stdout (--json: the full detail).",
        parents=[_json_parent],
    )
    p_notes_show.add_argument("note", help=_NOTE_ARG_HELP)
    p_notes_show.add_argument("--room", help=_GROUP_ARG_HELP + " Omit for your own notes.")
    p_notes_show.set_defaults(handler=cmd_notes_show)

    p_notes_new = sub_notes.add_parser(
        "create",
        help="Create a note. Body from $EDITOR, --file PATH, or --file - for stdin.",
        parents=[_json_parent],
    )
    p_notes_new.add_argument("name", help="The new note's name.")
    p_notes_new.add_argument(
        "--room", help=_GROUP_ARG_HELP + " Save into a room's shared notes instead of your own."
    )
    p_notes_new.add_argument(
        "--file", help="Read the body from this path, or '-' to read stdin, instead of $EDITOR."
    )
    p_notes_new.set_defaults(handler=cmd_notes_create)

    p_notes_edit = sub_notes.add_parser(
        "edit",
        help="Edit a note. Body from $EDITOR, --file PATH, or --file - for stdin.",
        parents=[_json_parent],
    )
    p_notes_edit.add_argument("note", help=_NOTE_ARG_HELP)
    p_notes_edit.add_argument("--room", help=_GROUP_ARG_HELP + " Omit for your own notes.")
    p_notes_edit.add_argument(
        "--file",
        help="Read the new body from this path, or '-' to read stdin, instead of $EDITOR.",
    )
    p_notes_edit.add_argument("--name", metavar="NAME", help="Rename the note to NAME.")
    p_notes_edit_precondition = p_notes_edit.add_mutually_exclusive_group()
    p_notes_edit_precondition.add_argument(
        "--if-match",
        metavar="ETAG",
        help="Send this etag as If-Match instead of one freshly read from the server.",
    )
    p_notes_edit_precondition.add_argument(
        "--force", action="store_true", help="Send no If-Match — last write wins."
    )
    p_notes_edit.set_defaults(handler=cmd_notes_edit)

    p_notes_rm = sub_notes.add_parser(
        "delete", help="Delete a note.", parents=[_yes_parent, _json_parent]
    )
    p_notes_rm.add_argument("note", help=_NOTE_ARG_HELP)
    p_notes_rm.add_argument("--room", help=_GROUP_ARG_HELP + " Omit for your own notes.")
    p_notes_rm.set_defaults(handler=cmd_notes_delete)

    p_takeout = sub.add_parser(
        "takeout",
        help="Download a zip archive of everything Pepper holds about your account.",
        parents=[_json_parent],
    )
    p_takeout.add_argument(
        "--out",
        help=(
            "Output file path (default: the server-suggested filename, "
            "e.g. pepper-takeout-<you>-<timestamp>.zip)."
        ),
    )
    p_takeout.set_defaults(handler=cmd_takeout)

    p_logout = sub.add_parser("logout", help="Delete the credentials file.", parents=[_json_parent])
    p_logout.set_defaults(handler=cmd_logout)

    p_agents = sub.add_parser(
        "agents",
        help="Agent members: ``guide`` prints the usage guide for taking part in rooms.",
    )
    sub_agents = p_agents.add_subparsers(dest="agents_action", required=True)
    p_agents_guide = sub_agents.add_parser(
        "guide",
        help="Print the agent-usage guide (SKILL.md) for taking part in rooms as an agent member.",
        description=(
            "Print the SKILL.md that tells an agent member how to use this CLI in Pepper "
            "rooms. It is shipped inside this binary, so it describes this version. Needs no "
            "login and makes no network call. (Not 'pepper skills', which manages your "
            "account's own skills.)"
        ),
        parents=[_json_parent],
    )
    p_agents_guide.add_argument(
        "--write",
        metavar="DIR",
        help="Write DIR/SKILL.md (creating DIR) instead of printing; refuses to overwrite.",
    )
    p_agents_guide.add_argument(
        "--force", action="store_true", help="With --write: overwrite an existing SKILL.md."
    )
    p_agents_guide.set_defaults(handler=cmd_agents_guide)
    _register_account_commands(sub)
    _add_renamed_stub(sub, "agent-skill", "see 'pepper agents guide'")

    # ``sub.choices`` is the public mapping of subparser name → parser
    # that argparse exposes (``argparse.SubParsersAction.choices``).
    # Freeze the keys so callers can't mutate the dispatch table by
    # accident.
    return parser, frozenset(sub.choices)


# ---------------------------------------------------------------------------
# Command handlers
# Each handler accepts an ``argparse.Namespace`` and returns an exit code.


def cmd_login(args: argparse.Namespace) -> int:
    """Resolve the server URL, then dispatch to the device-login flow
    (the default) or the manual paste-a-PAT flow (``--token`` /
    ``--manual``)."""
    server_url = args.server or _prompt(f"Server URL [{_DEFAULT_SERVER}]: ") or _DEFAULT_SERVER
    server_url = server_url.rstrip("/")

    if args.token or args.manual:
        return _login_manual(server_url, args.token)
    return _login_device(server_url, open_browser=not args.no_browser)


def _login_manual(server_url: str, raw_token: str | None) -> int:
    """Manual paste-a-PAT login: prompt if not given, validate, save.

    This is the whole of ``cmd_login`` before the device-login flow
    existed — kept verbatim as the ``--token`` (scripting) / ``--manual``
    (force-prompt) fallback. A typo'd token fails loudly here rather
    than silently breaking the next ``chat``.
    """
    raw_token = raw_token or _prompt_secret("Token (pep_pat_...): ")
    if not raw_token:
        render.error("No token provided. Mint one at <server>/settings#tokens and try again.")
        return 2
    # Wrap at the boundary: the raw string lives only inside this
    # local until it's into the SecretStr, after which the unwrap
    # path is grep-able via ``.get_secret_value()`` call sites.
    token = SecretStr(raw_token)
    is_agent = raw_token.startswith(AGENT_TOKEN_PREFIX)
    del raw_token
    if is_agent and credentials.active_profile() is None:
        # Saving it would overwrite the person's own login in the default file.
        render.error(
            "Agent tokens (pep_agt_) must not replace your own login. "
            "Re-run with a profile: pepper --profile <name> login ..."
        )
        return 2
    if is_agent:
        return _login_agent(server_url, token)

    # Validate before saving — a typo'd token should fail loudly here,
    # not silently break the next ``chat``. We close the client right
    # away (no reuse: ``login`` is one-shot).
    try:
        with api_client.ApiClient(server_url=server_url, token=token) as api:
            who = api.whoami()
    except api_client.ApiError as exc:
        if exc.status == 401:
            render.error("Token rejected (401). Double-check what you pasted.")
        else:
            render.error(f"Server rejected the token: {exc}")
        return 1
    except httpx.RequestError as exc:
        render.error(f"Couldn't reach {server_url}: {exc}")
        return 1

    path = credentials.save(server_url=server_url, token=token)
    _forget_listings()
    render.info(
        render.styled(
            "Logged in as ",
            (sanitize_line(who.display_name), "bold"),
            f" ({sanitize_line(who.email or 'no email')}).",
        )
    )
    render.info(f"Credentials written to {path}.")
    return 0


#: An agent member's token. It authenticates as the agent, not a Pepper account, so it reaches
#: only the room routes — ``whoami`` refuses it by design.
AGENT_TOKEN_PREFIX = "pep_agt_"


def _login_agent(server_url: str, token: SecretStr) -> int:
    """Validate and save an agent token.

    ``whoami`` answers only a Pepper account, so an agent token is checked
    against ``GET /api/v1/groups`` instead — the one read every agent may
    make — and saved the same way a PAT is. ``groups`` commands then work
    as the agent; everything else answers 401.
    """
    try:
        with api_client.ApiClient(server_url=server_url, token=token) as api:
            rooms = api.list_groups()
    except api_client.ApiError as exc:
        if exc.status == 401:
            render.error("Agent token rejected (401). It may be revoked or expired.")
        else:
            render.error(f"Server rejected the token: {exc}")
        return 1
    except httpx.RequestError as exc:
        render.error(f"Couldn't reach {server_url}: {exc}")
        return 1
    # A 200 can still say the rooms surface is off or unreadable for this
    # person; saving the token then would report a login that cannot work.
    if not rooms.enabled:
        render.error("Groups is not enabled on this server for that agent.")
        return 1
    if rooms.degraded:
        render.error("The groups service could not read this agent's rooms. Try again shortly.")
        return 1

    path = credentials.save(server_url=server_url, token=token)
    _forget_listings()
    render.info(f"Logged in as an agent member of {rooms.total} room(s).")
    render.info(f"Credentials written to {path}.")
    return 0


def _login_device(server_url: str, *, open_browser: bool) -> int:
    """Browser-based device-authorization login (the default path).

    Starts a device-authorization grant, prints the human code and
    verification URL, opens the browser (unless suppressed), then polls
    until the human approves/denies on the web page or the code expires.
    Ctrl-C during the wait propagates as ``KeyboardInterrupt`` — caught
    by :func:`main`, which prints a newline and returns 130, same as
    everywhere else in the CLI.

    On approval the server has already minted a PAT bound to this
    device; it's validated via ``whoami()`` exactly like
    :func:`_login_manual` before being saved, so a hiccup between mint
    and poll-return is still caught here rather than landing silently
    in the credentials file.
    """
    device_name = f"{getpass.getuser()}@{socket.gethostname()}"[:80]
    try:
        start = api_client.device_start(server_url, device_name)
    except api_client.ApiError as exc:
        render.error(f"Server rejected the device-login request: {exc}")
        return 1
    except httpx.RequestError as exc:
        render.error(f"Couldn't reach {server_url}: {exc}")
        return 1

    render.info(
        render.styled(
            "Confirm this code in your browser: ", (sanitize_line(start.user_code), "bold")
        )
    )
    render.info(start.verification_url)
    if open_browser:
        # Best-effort only — a headless box / no default browser must
        # never make the login fail; the URL is already printed above
        # for the human to open by hand.
        with contextlib.suppress(Exception):
            webbrowser.open(start.verification_url)

    interval = max(start.interval, 1)
    deadline = time.monotonic() + start.expires_in

    while True:
        if time.monotonic() >= deadline:
            render.error("Login expired before it was approved. Run 'pepper login' to try again.")
            return 1
        time.sleep(interval)
        try:
            poll = api_client.device_poll(server_url, start.device_code)
        except api_client.ApiError as exc:
            render.error(f"Server error while polling for login: {exc}")
            return 1
        except httpx.RequestError as exc:
            render.error(f"Couldn't reach {server_url}: {exc}")
            return 1

        if poll.status == "pending":
            continue
        if poll.status == "slow_down":
            # Back off: grow the CLI's own poll interval rather than
            # trusting the server's suggested one blindly — a
            # monotonically increasing local interval can't oscillate
            # even if a buggy/mocked server sends inconsistent values.
            interval += 5
            continue
        if poll.status == "denied":
            render.error("Login was denied.")
            return 1
        if poll.status == "expired":
            render.error("Login expired before it was approved. Run 'pepper login' to try again.")
            return 1
        if poll.status == "approved":
            break
        render.error(f"Unexpected response from server (status={poll.status!r}).")
        return 1

    assert poll.token is not None  # "approved" always carries a token
    token = poll.token

    try:
        with api_client.ApiClient(server_url=server_url, token=token) as api:
            who = api.whoami()
    except api_client.ApiError as exc:
        if exc.status == 401:
            render.error("Token rejected (401) right after approval — run 'pepper login' again.")
        else:
            render.error(f"Server rejected the token: {exc}")
        return 1
    except httpx.RequestError as exc:
        render.error(f"Couldn't reach {server_url}: {exc}")
        return 1

    path = credentials.save(server_url=server_url, token=token)
    _forget_listings()
    render.info(
        render.styled(
            "Logged in as ",
            (sanitize_line(who.display_name), "bold"),
            f" ({sanitize_line(who.email or 'no email')}).",
        )
    )
    render.info(f"Credentials written to {path}.")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    """``pepper whoami [--json]``."""

    def run(api: api_client.ApiClient) -> int:
        who = api.whoami()

        def human() -> None:
            # Plain print to stdout — scriptable. Each value is server text on one
            # line, so it is cleaned of terminal controls and line breaks.
            print(f"user_id: {sanitize_line(who.user_id)}")
            print(f"display_name: {sanitize_line(who.display_name)}")
            print(f"email: {sanitize_line(who.email or '')}")
            print(f"timezone: {sanitize_line(who.timezone or '')}")

        _emit(who, args, human)
        return 0

    return _run_personal(args, run)


def cmd_tui(args: argparse.Namespace) -> int:
    """``pepper tui``: the full-screen terminal UI.

    Exit codes (``planning/terminal-ui.md`` §10): 0 on a normal quit, 1 for an auth or
    unexpected error, 2 when it was not started on a terminal or a ``--root`` is refused.
    The terminal check comes first, before credentials or Textual are touched, so a script
    gets one line and a 2.
    """
    if not (sys.stdout.isatty() and _stdin_is_tty()):
        _emit_error(None, "usage", "pepper tui needs an interactive terminal (stdin and stdout)")
        return 2
    creds = _load_or_error(None)
    if creds is None:
        return 1
    try:
        provider = _build_provider(getattr(args, "root", None))
    except provider_fs.PathRefused as exc:
        _emit_error(None, "usage", f"--root: {exc}")
        return 2
    # Imported here so no other command pays for Textual (a test pins that).
    from pepper_cli import tui

    try:
        return tui.run(server_url=creds.server_url, token=creds.token, provider=provider)
    except httpx.RequestError as exc:
        _emit_error(None, "network", f"Couldn't reach {creds.server_url}: {exc}")
        return 1


def cmd_chat(args: argparse.Namespace) -> int:
    """Dispatch between one-shot ``chat MESSAGE`` and the REPL.

    ``args.message`` is a list (``nargs="*"``) so both the explicit
    (``pepper chat hi there``) and implicit (``pepper hi there``,
    via :func:`main`'s preprocessing) forms route here identically.
    An empty list opens the REPL; anything else joins to a single
    message string for one-shot dispatch.
    """
    message_parts = args.message or []
    # The REPL is exempt from JSON mode (J1): it never emits JSON, errors included.
    # Clearing ``args.json`` keeps ``main``'s 426 handler on text; the startup error
    # paths get ``None`` because ``_emit_error`` re-reads ``PEPPER_JSON`` from ``args``.
    error_args: argparse.Namespace | None = args
    if not message_parts:
        args.json = False
        error_args = None
    creds = _load_or_error(error_args)
    if creds is None:
        return 1
    try:
        provider = _build_provider(getattr(args, "root", None))
    except provider_fs.PathRefused as exc:
        _emit_error(error_args, "usage", f"--root: {exc}")
        return 2
    if not message_parts:
        return _run_repl(creds, provider=provider)
    message = " ".join(message_parts).strip()
    if not message:
        _emit_error(args, "usage", "Empty message — nothing to send.")
        return 2
    try:
        with api_client.ApiClient(server_url=creds.server_url, token=creds.token) as api:
            # Stream one-shot turns too: a long turn can exceed the unary
            # request timeout. trace=False keeps stdout to the final reply;
            # provider=None sends no filesystem manifest or local tools.
            ok = _stream_one_turn(
                api,
                message,
                provider=provider,
                trace=False,
                json_args=args if _json_mode(args) else None,
            )
            return 0 if ok else 1
    except api_client.ApiError as exc:
        return _report_api_error(exc, args)
    except httpx.ReadTimeout:
        _emit_error(
            args,
            "network",
            "Timed out waiting for Pepper's reply. The turn may still be running "
            "or saved; check the web chat before sending the message again.",
        )
        return 1
    except httpx.RequestError as exc:
        _emit_error(
            args,
            "network",
            f"Chat connection to {creds.server_url} failed: {exc}. "
            "The turn may have been saved; check the web chat before retrying.",
        )
        return 1


def _build_provider(roots: list[str] | None) -> provider_fs.FilesystemProvider | None:
    """The filesystem provider for this invocation, or ``None``.

    ``None`` is the whole no-``--root`` contract: no manifest is sent, the
    request body is byte-identical to what it was before this feature, and
    ``pepper chat`` behaves exactly as it always has.
    """
    if not roots:
        return None
    return provider_fs.FilesystemProvider(roots)


#: Static slash commands the REPL recognizes regardless of skill
#: state. Kept as a module constant so the completer and the
#: dispatch loop can't drift.
_STATIC_SLASH_COMMANDS: tuple[str, ...] = ("/quit", "/exit", "/clear", "/help")


class _SlashCompleter:
    """prompt_toolkit completer that fires only on ``/`` prefixes.

    Behavior:

    - Buffer starts with ``/`` (or is exactly ``/``): emit static
      commands and one entry per skill (``/<name>``), prefix-matched
      against whatever the user has typed after the slash.
    - Otherwise: no completions. Free-form chat input should never
      pop a completion menu, and we don't want completion to fight
      with the user's typing speed.

    The skill display includes the description as ``meta`` text so
    the completion menu shows what each skill does without the user
    having to commit to a guess.

    Implemented as a class rather than reusing ``WordCompleter`` so
    we can scope completions to the slash-prefix case and inject the
    per-skill meta text in one place.
    """

    def __init__(self, skills: list[api_client.SkillSummary]) -> None:
        # Resolve the full word list once at construction. The skill
        # list is fetched once at REPL startup (see ``_run_repl``)
        # so there's no advantage to lazy evaluation here.
        self._entries: list[tuple[str, str]] = [
            (cmd, "built-in command") for cmd in _STATIC_SLASH_COMMANDS
        ]
        for skill in skills:
            # Surface the description (truncated) as completion-menu
            # meta. Long descriptions get clipped so the menu stays
            # navigable — the full text is in the skill body which
            # the model loads anyway.
            meta = skill.description[:60] + ("…" if len(skill.description) > 60 else "")
            self._entries.append((f"/{skill.name}", meta))

    def get_completions(self, document, complete_event):
        """Yield prefix-matched ``Completion`` objects.

        Imported lazily so the type isn't pulled at module import
        time when the REPL isn't run.
        """
        from prompt_toolkit.completion import Completion

        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        # ``start_position`` of ``-len(text)`` tells prompt_toolkit to
        # replace the whole slash-prefix on commit, not just append —
        # so typing ``/m<Tab>`` and selecting ``/morning-brief``
        # replaces the buffer rather than producing ``/m/morning-brief``.
        for word, meta in self._entries:
            if word.startswith(text):
                yield Completion(word, start_position=-len(text), display_meta=meta)

    async def get_completions_async(self, document, complete_event):
        """Async surface required by prompt_toolkit's buffer.

        ``prompt_toolkit.buffer.Buffer.async_completer`` calls
        ``get_completions_async`` directly via ``async for``. The
        Completer base class supplies a default that wraps the sync
        ``get_completions``; duck-typed completers (like this one,
        kept lightweight to avoid an eager ``prompt_toolkit`` import
        at module load) must implement the async surface themselves
        or the buffer raises ``AttributeError`` at first Tab press.
        """
        for completion in self.get_completions(document, complete_event):
            yield completion


def _run_repl(
    creds: credentials.Credentials,
    *,
    provider: provider_fs.FilesystemProvider | None = None,
) -> int:
    """Interactive chat REPL.

    Reads one line per prompt via prompt_toolkit (history persists in
    ``history`` beside the credentials file), dispatches each non-empty
    line to ``chat_send_stream`` so the user sees model thoughts /
    tool calls as they happen, then prints the final reply via
    :func:`render.render_reply`.

    Slash commands:

    - ``/quit`` or ``/exit`` — clean exit (rc=0). Ctrl-D does the same.
    - ``/clear`` — clear the terminal.
    - ``/help`` — list these commands.
    - ``/<skill-name>`` — submit ``"Run the <skill-name> skill."`` to
      the agent, which loads the skill via its contextual mechanism
      and acts on it. Tab-completion lists all of the calling user's
      skills (fetched once at REPL startup).

    The REPL is intentionally minimal — Pepper is "one persistent
    conversation per user" on the server side, so there's no
    ``/new-thread`` / ``/branch`` / etc. Keeping the command surface
    small mirrors that.
    """
    # Import lazily so tests that don't exercise the REPL aren't
    # paying for prompt_toolkit's import overhead. Also keeps
    # ``pepper --help`` snappy.
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory

    history_path = credentials.credentials_path().parent / "history"
    history_path.parent.mkdir(parents=True, exist_ok=True)

    with (
        window_title.window_title(_repl_title(creds.server_url)),
        api_client.ApiClient(server_url=creds.server_url, token=creds.token) as api,
    ):
        # Fetch the skill list once at startup. A 401 here means the
        # token is bad and every subsequent turn would also 401, so
        # bail loudly. Anything else (network blip, 5xx) degrades to
        # static-only completion — a REPL with no skill autocomplete
        # is still useful, a REPL that refuses to open is not.
        skills: list[api_client.SkillSummary] = []
        try:
            skills = api.list_skills()
        except api_client.ApiError as exc:
            if exc.status == 401:
                return _report_api_error(exc)
            render.info(
                render.styled(
                    ("warning:", "yellow"),
                    f" couldn't load skills (HTTP {exc.status}); "
                    "completion will only cover built-in commands.",
                )
            )
        except httpx.RequestError as exc:
            render.info(
                render.styled(
                    ("warning:", "yellow"),
                    f" couldn't load skills ({exc}); completion will only cover built-in commands.",
                )
            )

        completer = _SlashCompleter(skills)
        repl_session: PromptSession = PromptSession(
            history=FileHistory(str(history_path)),
            completer=completer,  # ty:ignore[invalid-argument-type]
            complete_while_typing=False,
        )

        render.info(
            render.styled(
                "Pepper REPL — connected to ",
                (creds.server_url, "bold"),
                ". Type / + Tab for commands, Ctrl-D to exit. Run ",
                ("pepper --help", "bold"),
                " for non-chat subcommands (login / whoami / logout).",
            )
        )
        skill_names = {s.name for s in skills}

        while True:
            try:
                line = repl_session.prompt("pepper> ")
            except EOFError:
                # Ctrl-D — clean exit. Print a newline so the next
                # shell prompt doesn't paste onto the prompt arrow.
                render.info("")
                return 0
            except KeyboardInterrupt:
                # Ctrl-C inside the prompt cancels the current line,
                # not the REPL. Loop and re-prompt.
                continue

            line = line.strip()
            if not line:
                continue
            if line in {"/quit", "/exit"}:
                return 0
            if line == "/clear":
                # Stick with raw ANSI rather than dragging in
                # ``shutil.get_terminal_size`` or click — the escape
                # is widely supported and we already require a TTY
                # for the REPL.
                sys.stdout.write("\x1b[2J\x1b[H")
                sys.stdout.flush()
                continue
            if line == "/help":
                render.info(
                    "Commands:\n  /quit, /exit – leave\n  /clear – clear screen\n"
                    "  /help – this\n  /<skill> – ask the agent to run a skill "
                    "(Tab-complete)"
                )
                continue
            if line.startswith("/"):
                # ``/<skill-name>`` — translate into a hint message
                # that the agent's skill-loading mechanism picks up.
                # Keeping the phrasing stable matters: the model uses
                # it to decide which skill to load.
                skill_name = line[1:]
                if skill_name in skill_names:
                    message = f"Run the {skill_name} skill."
                    try:
                        _stream_one_turn(api, message, provider=provider)
                    except api_client.ApiError as exc:
                        if exc.status == 401:
                            _report_api_error(exc)
                            return 1
                        _report_api_error(exc)
                    except httpx.RequestError as exc:
                        render.error(f"Couldn't reach {creds.server_url}: {exc}")
                    continue
                render.error(f"Unknown command: {line}. Type /help for the list.")
                continue

            try:
                _stream_one_turn(api, line, provider=provider)
            except api_client.ApiError as exc:
                # Don't return — the user can keep chatting after a
                # one-off API error (e.g. transient 502). 401 is the
                # exception: a revoked / invalidated token means
                # every subsequent turn will also fail, so bail out
                # of the REPL and tell the user to log in again.
                if exc.status == 401:
                    _report_api_error(exc)
                    return 1
                _report_api_error(exc)
            except httpx.RequestError as exc:
                render.error(f"Couldn't reach {creds.server_url}: {exc}")


def _stream_one_turn(
    api: api_client.ApiClient,
    message: str,
    *,
    provider: provider_fs.FilesystemProvider | None = None,
    trace: bool = True,
    json_args: argparse.Namespace | None = None,
) -> bool:
    """Drive one streamed turn: dispatch each SSE event to a renderer.

    Pulled into a helper so the REPL loop reads as a flat dispatcher
    and so tests can drive a single turn without spinning up the
    whole prompt_toolkit machinery.

    ``provider`` is the local filesystem toolset ``--root`` declared, if
    any. Its manifest rides on the request; its ``client_tool_call`` frames
    are served inline, in this loop, **synchronously** — the answer is
    posted before the next frame is read. That is not a limitation worth
    engineering around: the server runs one client call at a time (each
    costs the turn a tool round), and doing the work between two reads of
    the same stream keeps the whole feature free of threads.

    ``trace`` off suppresses the thought/tool chatter so the one-shot
    ``pepper chat --root … "…"`` prints exactly what it printed before —
    the reply. Tool *calls* are still served either way; only the drawing
    of them changes.

    Return whether the server sent a terminal reply. An error frame or a
    closed stream without a terminal frame must not count as a successful
    one-shot command, and neither path resubmits the message.

    ``json_args`` (the one-shot ``chat MESSAGE --json`` call) switches the output to
    JSON lines (J6): one ``{"kind", "payload"}`` object per streamed event as it
    arrives, the ``reply`` event last; nothing is drawn, and a failure is the J8 line
    on stderr.
    """
    sink: turn.TurnSink
    if json_args is not None:
        sink = _JsonTurnSink(api, provider=provider, json_args=json_args)
    else:
        sink = _ReplTurnSink(api, provider=provider, trace=trace)
    outcome = turn.run_turn(api, message, sink, provider=provider)
    if outcome == "reply":
        return True
    if outcome == "ended":
        _emit_error(
            json_args,
            "server",
            "Chat stream ended without a reply. Check the web chat before sending again.",
        )
    return False


class _ReplTurnSink(turn.TurnSink):
    """Draws a turn in the REPL: thoughts as paragraphs, tools as one line each.

    Thought fragments stream onto one line and the block ends it, so a thought reads as a
    paragraph on every backend rather than one word per line on the token-level ones.
    """

    def __init__(
        self,
        api: api_client.ApiClient,
        *,
        provider: provider_fs.FilesystemProvider | None,
        trace: bool,
    ) -> None:
        self.api = api
        self.provider = provider
        self.trace = trace

    def thought_delta(self, text: str, *, first: bool) -> None:
        if self.trace:
            render.render_thought_delta(text, first=first)

    def thought_block(self, text: str, *, streamed: str) -> None:
        if self.trace:
            render.end_thought(text, streamed=streamed)

    def retry(self, *, streamed: str) -> None:
        # The abandoned attempt's fragments stay on screen; end their line so the retry's
        # start fresh. The common retry lands before any fragment, and then there is no
        # line to end.
        if self.trace and streamed:
            render.discard_thought()

    def tool_call(self, payload: dict) -> None:
        if self.trace:
            render.render_tool_call(payload)

    def tool_result(self, payload: dict) -> None:
        if self.trace:
            render.render_tool_result(payload)

    def client_tools(self, payload: dict) -> None:
        if self.trace:
            _render_client_tools_ack(payload)

    def client_tool_call(self, payload: dict) -> None:
        _serve_client_tool_call(self.api, payload, provider=self.provider, trace=self.trace)

    def reply(self, payload: dict) -> None:
        render.render_reply(payload.get("reply") or "")

    def error(self, payload: dict) -> None:
        render.error(payload.get("message") or "model service unavailable")


class _JsonTurnSink(turn.TurnSink):
    """``chat MESSAGE --json`` (J6): one ``{"kind", "payload"}`` line per frame, nothing drawn."""

    def __init__(
        self,
        api: api_client.ApiClient,
        *,
        provider: provider_fs.FilesystemProvider | None,
        json_args: argparse.Namespace,
    ) -> None:
        self.api = api
        self.provider = provider
        self.json_args = json_args

    def frame(self, event: api_client.StreamEvent) -> None:
        sys.stdout.write(
            json.dumps({"kind": event.kind, "payload": event.payload}, default=str) + "\n"
        )
        sys.stdout.flush()

    def client_tool_call(self, payload: dict) -> None:
        _serve_client_tool_call(
            self.api, payload, provider=self.provider, trace=False, json_args=self.json_args
        )

    def error(self, payload: dict) -> None:
        _emit_error(
            self.json_args,
            "server",
            str(payload.get("message") or "model service unavailable"),
        )


def _render_client_tools_ack(payload: dict) -> None:
    """Say which declared tools the server refused, and why.

    Silent on the happy path. A dropped tool is worth one line because the
    alternative is a model that never calls a tool the user thinks they
    shared, with nothing anywhere saying why.
    """
    reason = payload.get("manifest_reason")
    if reason:
        render.error(f"The server rejected the whole tool manifest: {reason}.")
        return
    for entry in payload.get("rejected") or []:
        render.info(
            render.styled(
                ("note:", "yellow"),
                f" the server dropped {entry.get('name')!r} "
                f"({sanitize_line(str(entry.get('reason')))}).",
            )
        )


def _serve_client_tool_call(
    api: api_client.ApiClient,
    payload: dict,
    *,
    provider: provider_fs.FilesystemProvider | None,
    trace: bool,
    json_args: argparse.Namespace | None = None,
) -> None:
    """Run one tool locally and post the answer back.

    Answers even when there is no provider: a call for a tool this process
    never declared still has a ``call_id`` the server is waiting on, and
    saying so immediately is strictly better than letting it burn the turn's
    whole twenty-second budget on a question already answered.

    Every failure to *post* is swallowed with a note, deliberately. The
    result route's 404 means the call is already gone — timed out, or the
    turn moved on — and there is nothing to recover; raising here would
    abort a stream whose reply is still coming. With ``json_args`` (a JSON-mode
    ``chat``) the note is a J8 line on stderr instead of text, so stderr stays
    parseable; the turn's exit code still says whether the command succeeded.
    """
    answer = turn.answer_client_tool_call(api, payload, provider=provider)
    if answer is None:
        return
    name = answer.name
    if trace:
        render.render_tool_call({"name": name, "args": payload.get("arguments") or {}})
    exc = answer.post_error
    if exc is not None:
        if json_args is not None:
            if isinstance(exc, api_client.ApiError):
                code, status = _error_code_for_status(exc.status), exc.status
            else:
                code, status = "network", None
            _emit_error(
                json_args, code, f"couldn't return {name!r} to the server ({exc}).", status=status
            )
            return
        render.info(
            render.styled(
                ("note:", "yellow"),
                f" couldn't return {name!r} to the server ({exc}).",
            )
        )


def cmd_trajectory_show(args: argparse.Namespace) -> int:
    """``pepper trajectories show <id> [--room ROOM] [--json]``.

    Default mode prints a header and then one line per event (see
    :func:`render.render_trajectory` for the exact shape, which is kept
    stable for tools that parse it). ``--json`` dumps the raw body to
    stdout for ``jq``-able re-querying.

    Exit codes:
      0 — success
      1 — auth failure (revoked / missing token), other API errors
      2 — invalid trajectory id format
    """
    # Validate the UUID shape locally before splatting it into a URL —
    # gives a friendlier error than the server's 422.
    if not _valid_uuid(args.trajectory_id):
        _emit_error(args, "usage", f"trajectory id must be a UUID (got: {args.trajectory_id})")
        return 2

    def run(api: api_client.ApiClient) -> int:
        group_id = _opt_group(api, args.room, args=args)
        if args.json:
            # The raw wire body, passed through unchanged (J7).
            body = api.trajectory_get_raw(args.trajectory_id)
            if group_id and not _raw_trajectory_in_room(body, group_id):
                return _not_in_room(args)
            _print_json(body)
        else:
            detail = api.trajectory_get(args.trajectory_id)
            if group_id and detail.trajectory.user_id != group_id:
                return _not_in_room(args)
            render.render_trajectory(detail)
        return 0

    return _with_api(run, _report_api_error, args)


def _raw_trajectory_in_room(body: Any, group_id: str) -> bool:
    """Whether a raw ``/trajectories/{id}`` body is this room's turn (its ``user_id``)."""
    header = body.get("trajectory") if isinstance(body, dict) else None
    return isinstance(header, dict) and header.get("user_id") == group_id


def _not_in_room(args: argparse.Namespace) -> int:
    """``trajectories show --room`` on a trajectory that is not that room's: exit 2."""
    _emit_error(args, "not_found", "that trajectory isn't one of this room's turns")
    return 2


def _offset_cursor(value: str | None) -> int | None:
    """Decode a ``trajectories list --before`` cursor (the next offset); ``None`` if malformed.

    The server pages by offset only, so the cursor is that offset as text. It is
    opaque to callers: they pass back whatever ``next_before`` printed.
    """
    if value is None:
        return 0
    return int(value) if value.isdecimal() else None


def cmd_trajectory_list(args: argparse.Namespace) -> int:
    """``pepper trajectories list [--room ROOM] [--limit N] [--before CURSOR] [--json]``.

    Renders newest-first. Each row shows seq + channel + created time
    + a snippet of the first user message — same shape as the web
    list page, condensed for terminal output. ``--json`` is the J3
    envelope; ``next_before`` is set when a full page came back (there may be more).
    """
    offset = _offset_cursor(args.before)
    if offset is None:
        _emit_error(
            args, "usage", f"--before expects a cursor from a previous call (got: {args.before})"
        )
        return 2
    if args.limit < 1:
        _emit_error(args, "usage", "--limit must be at least 1")
        return 2

    def run(api: api_client.ApiClient) -> int:
        group_id = _opt_group(api, args.room, args=args)
        rows = api.trajectory_list(limit=args.limit, offset=offset, principal=group_id)
        more = str(offset + len(rows)) if len(rows) >= args.limit else None

        def human() -> None:
            render.render_trajectory_list(rows)
            if more is not None:
                # The resolved id, not the typed name: a multi-word name would
                # print unquoted and break the copy-pasted next-page command.
                room = f" --room {group_id}" if group_id else ""
                render.info(
                    f"More: pepper trajectories list{room} --limit {args.limit} --before {more}",
                    soft_wrap=True,
                )

        _emit_list(rows, args, human, next_before=more)
        return 0

    return _with_api(run, _report_room_error if args.room else _report_api_error, args)


def _add_calendar_extras(api: Any, payload: dict[str, Any], *, schedule: bool) -> None:
    """Fold the optional read-only layers into the range payload, in place.

    ``environment`` (weather, daylight), drive ``facts`` on each commute leg in
    ``days[].travel`` and, for ``schedule``, ``schedule_plan``. Each one is best-effort: a
    server that lacks it (404) or fails leaves the plain agenda unchanged.
    """

    def tolerate(call: Any) -> Any:
        try:
            return call()
        except (api_client.ApiError, httpx.RequestError):
            return None

    dates = [w["date"] for w in payload.get("window") or []]
    if dates and len(dates) <= 14:
        payload["environment"] = tolerate(
            lambda: api.calendar_environment(dates=dates, focus=dates[0])
        )
    legs = [leg for day in payload.get("days") or [] for leg in day.get("travel") or []]
    for leg in legs[:_MAX_COMMUTE_LEGS]:
        leg["facts"] = tolerate(lambda leg=leg: api.calendar_travel(leg["token"]))
    if schedule:
        payload["schedule_plan"] = tolerate(api.calendar_schedule_plan)


#: Each leg costs the server a (cached) Maps route lookup, so a month view stays bounded.
_MAX_COMMUTE_LEGS = 12


def cmd_calendar(args: argparse.Namespace) -> int:
    """``pepper calendar [today|week|schedule] [--start D] [--days N] [--planning] [--room R]
    [--json]``.

    ``--room`` reads the room's calendar view (``principal=<room id>`` on the range and
    planning routes). The weather, drive-time and schedule-plan layers are the caller's
    own, so a room view carries none of them.
    """
    import datetime as dt

    days = args.days if args.days is not None else (7 if args.window == "week" else 1)
    if not 1 <= days <= 31:
        _emit_error(args, "usage", "--days must be between 1 and 31")
        return 2
    try:
        if args.start:
            dt.date.fromisoformat(args.start)
    except ValueError:
        _emit_error(args, "usage", "--start must be YYYY-MM-DD")
        return 2
    if args.planning and (days > 7 or args.window == "schedule"):
        _emit_error(
            args, "usage", "--planning covers at most 7 days and replaces the schedule view"
        )
        return 2
    if args.room and args.window == "schedule":
        _emit_error(args, "usage", "the schedule view is your own; --room does not apply to it")
        return 2

    def run(api: api_client.ApiClient) -> int:
        principal = None
        if args.room:
            room = _resolve_room_strict(api, args, args.room)
            if isinstance(room, int):
                return room
            principal = room
        # "Today" is the account's date, not the host's: the server reads start in user's zone.
        start = args.start or next_view.user_now(api).date().isoformat()
        payload = api.calendar_range(start=start, days=days, principal=principal)
        planning = (
            api.calendar_planning(start=start, days=days, principal=principal)
            if args.planning
            else None
        )
        if principal is None:
            _add_calendar_extras(api, payload, schedule=args.window == "schedule")
        mode = (
            "planning" if args.planning else "schedule" if args.window == "schedule" else "calendar"
        )
        _emit(
            {"range": payload, "planning": planning} if args.planning else payload,
            args,
            lambda: render.render_calendar(
                payload, mode=mode, cards=planning["cards"] if planning else None
            ),
        )
        return 0

    return _run_personal(args, run)


def _resolve_room_strict(
    api: api_client.ApiClient, args: argparse.Namespace, value: str
) -> str | int:
    """Resolve ``--room`` to a room id, else report why (J8 under ``--json``) and return
    the exit code.

    A UUID must be the id of one of your rooms; a name matches exactly, else as a unique
    prefix, on one of their names (archived rooms too). An unknown or ambiguous room is
    exit 2, never a silent empty result: ``next`` filters locally, so an id nobody
    checked would read as an empty room. A rooms list the server could not produce
    (groups off, or degraded) is exit 1 and says so, rather than "no such room" — the
    input may be fine.
    """
    for archived in (False, True):
        gl = api.list_groups(archived=archived)
        if not gl.enabled:
            # Worded and coded as the rooms leaves report it (``_with_room_api``).
            _emit_error(args, "forbidden", "Rooms are not enabled for your account.")
            return 1
        if gl.degraded:
            _emit_error(
                args, "server", "The rooms service could not read your rooms. Try again shortly."
            )
            return 1
        if _valid_uuid(value):
            hit = next((g for g in gl.groups if g.group_id.lower() == value.lower()), None)
            if hit is not None:
                return hit.group_id
            continue
        match, candidates = _match_by_name(value, gl.groups, name_of=lambda g: g.display_name)
        if match is not None:
            return match.group_id
        if candidates:
            listing = ", ".join(f"{g.display_name!r} ({g.group_id})" for g in candidates)
            _emit_error(args, "ambiguous", f"{value!r} matches more than one room: {listing}")
            return 2
    what = "id" if _valid_uuid(value) else "named"
    _emit_error(args, "not_found", f"no room {what} {value!r} — run 'pepper rooms list'")
    return 2


def cmd_next(args: argparse.Namespace) -> int:
    """``pepper next [--room R] [--source PREFIX] [--all] [--json]``."""

    def run(api: api_client.ApiClient) -> int:
        room_id = None
        if args.room:
            room = _resolve_room_strict(api, args, args.room)
            if isinstance(room, int):
                return room
            room_id = room
        scope = next_view.Scope(room_id=room_id, source=args.source)
        result = next_view.assemble(api, scope=scope, show_all=args.all)
        if args.json:
            _print_json(result)
        else:
            render.render_next(result)
        return 0

    return _run_personal(args, run)


def cmd_todo_list(args: argparse.Namespace) -> int:
    """``pepper todos list [--status open|done|dismissed|all] [--room ROOM] [--json]``."""

    def run(api: api_client.ApiClient) -> int:
        rows = api.list_todos(status=args.status, group_id=_opt_group(api, args.room, args=args))
        _emit_list(rows, args, lambda: render.render_todo_list(rows))
        return 0

    return _with_api(run, _report_api_error, args)


def cmd_todo_create(args: argparse.Namespace) -> int:
    """``pepper todos create <title...> [--details D] [--due DATE] [--priority P] [--room ROOM]``.

    A room's todo takes the same fields, but its ``--due`` is a ``YYYY-MM-DD`` day only.
    Exit codes: 0 ok · 1 auth/API/transport · 2 bad input (blank title / unparseable
    ``--due``).
    """
    title = " ".join(args.title).strip()
    if not title:
        _emit_error(args, "usage", "todo title cannot be empty")
        return 2
    due_iso: str | None = None
    due_day: str | None = None
    if args.due and args.room:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.due):
            _emit_error(args, "usage", "with --room, --due must be 'YYYY-MM-DD'")
            return 2
        due_day = args.due
    elif args.due:
        due_iso, err = _parse_due(args.due)
        if err is not None:
            _emit_error(args, "usage", err)
            return 2

    def run(api: api_client.ApiClient) -> int:
        todo = api.create_todo(
            title=title,
            details=args.details,
            priority=args.priority,
            due_date=due_iso,
            due=due_day,
            group_id=_opt_group(api, args.room, args=args),
        )

        def human() -> None:
            render.info(f"Added todo {todo.id}")
            if not args.room:
                render.render_todo_list([todo])

        _emit(todo, args, human)
        return 0

    return _with_api(run, _report_api_error, args)


def cmd_todo_done(args: argparse.Namespace) -> int:
    """``pepper todos done <id> [--room ROOM]``."""
    return _tick_todo(args, reopen=False)


def cmd_todo_reopen(args: argparse.Namespace) -> int:
    """``pepper todos reopen <id> [--room ROOM]``."""
    return _tick_todo(args, reopen=True)


def _tick_todo(args: argparse.Namespace, *, reopen: bool) -> int:
    """``done`` / ``reopen``. A tick also releases the rest of the todo's held time, as
    ticking it on Schedule does. A wrong id or room is the server's one uniform answer;
    there is no fall-through to other rooms."""
    if not _valid_uuid(args.todo_id):
        _emit_error(args, "usage", f"todo id must be a UUID (got: {args.todo_id})")
        return 2

    def run(api: api_client.ApiClient) -> int:
        change = api.reopen_todo if reopen else api.complete_todo
        todo = change(args.todo_id, group_id=_opt_group(api, args.room, args=args))
        _emit(
            todo,
            args,
            lambda: render.info(f"{'Reopened' if reopen else 'Marked done'}: {args.todo_id}"),
        )
        return 0

    return _with_api(run, _report_api_error, args)


def cmd_todo_dismiss(args: argparse.Namespace) -> int:
    """``pepper todos dismiss <id> [--room ROOM]``."""
    if not _valid_uuid(args.todo_id):
        _emit_error(args, "usage", f"todo id must be a UUID (got: {args.todo_id})")
        return 2

    def run(api: api_client.ApiClient) -> int:
        todo = api.dismiss_todo(args.todo_id, group_id=_opt_group(api, args.room, args=args))
        _emit(todo, args, lambda: render.info(f"Dismissed: {todo.title}"))
        return 0

    return _with_api(run, _report_api_error, args)


def _resolve_task_id(
    api: api_client.ApiClient,
    task: str,
    *,
    group_id: str | None = None,
    args: argparse.Namespace | None = None,
) -> str | None:
    """Turn a task name into its id, passing a UUID straight through.

    The API is keyed by id, but a name is the handle every other surface
    uses — the agent cancels by name, the listing prints names — so making
    the CLI id-only would be the one place a user has to paste a UUID to
    do what they can say in a sentence.

    Paused tasks are included in the lookup: ``resume`` is the command
    most likely to be reached by name, and it only ever targets one.
    Returns ``None`` (after printing the error) when no task matches.

    With ``group_id`` the name is looked up among that room's tasks — a
    room's names are its own namespace, so yours never answer for it. A
    UUID still passes straight through: the server refuses one that is not
    this room's task.

    With ``args`` the miss is reported through :func:`_emit_error` (J8 under
    ``--json``) instead of plain text.
    """

    def say(message: str) -> None:
        if args is None:
            render.error(message)
        else:
            _emit_error(args, "not_found", message)

    if _valid_uuid(task):
        return task
    if group_id:
        rows = api.list_scheduled_tasks(include_inactive=True, group_id=group_id)
    else:
        rows = api.list_scheduled_tasks(include_inactive=True)
    for row in rows:
        if row.name == task:
            return row.id
    if group_id:
        say(
            f"no scheduled task named {task!r} in that room — "
            "run 'pepper scheduled-tasks list --all --room ROOM'"
        )
    else:
        say(f"no scheduled task named {task!r} — run 'pepper scheduled-tasks list --all'")
    return None


def _report_room_task_error(
    exc: api_client.ApiError, args: argparse.Namespace | None = None
) -> int:
    """A room-task command's errors: 403 is the owner gate, said in one fixed
    sentence; everything else carries the server's own ``detail`` (no such
    room, no such task, a taken name, a full budget), which is written to be
    read."""
    if exc.status == 403:
        _emit_error(args, "forbidden", _ROOM_TASK_OWNER_ONLY, status=403)
        return 1
    return _report_api_error(exc, args)


def _destination_dialect_error(
    to: list[str] | None, *, room: str | None, topic: str | None
) -> str | None:
    """Why this ``--to`` / ``--topic`` combination cannot be sent, or ``None``.

    Argparse accepts both dialects; this is where ``--room`` decides which
    one applies, so a wrong pairing is a usage error (exit 2) rather than a
    round-trip 422.
    """
    chosen = _wire_destinations(to)
    if room:
        wrong = [d for d in to or [] if _wire_destinations([d])[0] not in _ROOM_DESTINATION_CHOICES]
        if wrong:
            return (
                f"--to {wrong[0]} isn't available with --room — a room's task "
                f"announces to: {', '.join(_ROOM_DESTINATION_SHOWN)}"
            )
        if topic is not None and "groups_service" not in chosen:
            return "--deliver-topic picks the topic for --to room; pass that too"
        return None
    if topic is not None:
        return "--deliver-topic is only available with --room"
    wrong = [d for d in to or [] if d not in _DESTINATION_CHOICES]
    if wrong:
        return f"--to {wrong[0]} is only available with --room"
    return None


def _task_report(args: argparse.Namespace) -> Callable[[api_client.ApiError, Any], int]:
    """The error reporter for a scheduled-tasks leaf: the owner gate under ``--room``."""
    return _report_room_task_error if getattr(args, "room", None) else _report_api_error


def cmd_scheduled_task_list(args: argparse.Namespace) -> int:
    """``pepper scheduled-tasks list [--all] [--room ROOM] [--json]``."""

    def run(api: api_client.ApiClient) -> int:
        if args.room:
            group_id = _need_group(api, args.room, args=args)
            rows = api.list_scheduled_tasks(include_inactive=args.all, group_id=group_id)
        else:
            rows = api.list_scheduled_tasks(include_inactive=args.all)
        _emit_list(rows, args, lambda: render.render_scheduled_task_list(rows))
        return 0

    return _with_api(run, _task_report(args), args)


def cmd_scheduled_task_show(args: argparse.Namespace) -> int:
    """``pepper scheduled-tasks show <task> [--room ROOM] [--json]``."""

    def run(api: api_client.ApiClient) -> int:
        group_id = _opt_group(api, args.room, args=args)
        task_id = _resolve_task_id(api, args.task, group_id=group_id, args=args)
        if task_id is None:
            return 2
        detail = api.get_scheduled_task(task_id, group_id=group_id)
        _emit(detail, args, lambda: render.render_scheduled_task(detail))
        return 0

    return _with_api(run, _task_report(args), args)


#: Separates the timestamp from the row id inside a ``runs``/``thread`` cursor.
_KEYSET_SEP = "|"


def _keyset_cursor(timestamp: str, row_id: str) -> str:
    """The ``next_before`` of a keyset page: the boundary row's time and id, as one string."""
    return f"{timestamp}{_KEYSET_SEP}{row_id}"


def _keyset_parts(cursor: str | None) -> tuple[str | None, str | None]:
    """Split a ``--before`` cursor into the server's ``(before, before_id)``.

    A bare timestamp (no id) is accepted too: it pages by time alone.
    """
    if not cursor:
        return None, None
    before, sep, before_id = cursor.partition(_KEYSET_SEP)
    return before, (before_id if sep and before_id else None)


def _page_limit_ok(args: argparse.Namespace) -> bool:
    """A usage error (reported) when ``--limit`` is outside the server's 1..100."""
    if args.limit is not None and not 1 <= args.limit <= 100:
        _emit_error(args, "usage", "--limit must be between 1 and 100")
        return False
    return True


def cmd_scheduled_task_runs(args: argparse.Namespace) -> int:
    """``pepper scheduled-tasks runs <task> [--room ROOM] [--limit N] [--before C] [--json]``."""
    if not _page_limit_ok(args):
        return 2
    before, before_id = _keyset_parts(args.before)

    def run(api: api_client.ApiClient) -> int:
        group_id = _opt_group(api, args.room, args=args)
        task_id = _resolve_task_id(api, args.task, group_id=group_id, args=args)
        if task_id is None:
            return 2
        rows = api.list_scheduled_task_runs(
            task_id, limit=args.limit, before=before, before_id=before_id, group_id=group_id
        )
        # Newest first: the last row bounds the next (older) page. A short page is the end.
        more = bool(rows) and len(rows) >= args.limit
        cursor = _keyset_cursor(rows[-1].fired_at, rows[-1].id) if more else None
        _emit_list(rows, args, lambda: render.render_scheduled_task_runs(rows), cursor)
        return 0

    return _with_api(run, _task_report(args), args)


def cmd_scheduled_task_thread(args: argparse.Namespace) -> int:
    """``pepper scheduled-tasks thread <task> [--room ROOM] [--limit N] [--before C] [--json]``.

    Read-only. To reply into the thread, chat normally — the server
    routes a reply by conversation, not through this command.
    """
    if not _page_limit_ok(args):
        return 2
    before, before_id = _keyset_parts(args.before)

    def run(api: api_client.ApiClient) -> int:
        group_id = _opt_group(api, args.room, args=args)
        task_id = _resolve_task_id(api, args.task, group_id=group_id, args=args)
        if task_id is None:
            return 2
        rows = api.get_scheduled_task_thread(
            task_id, limit=args.limit, before=before, before_id=before_id, group_id=group_id
        )
        # Oldest first within the page: the first row bounds the next (older) page.
        more = bool(rows) and len(rows) >= args.limit
        cursor = _keyset_cursor(rows[0].created_at, rows[0].id) if more else None
        _emit_list(rows, args, lambda: render.render_scheduled_task_thread(rows), cursor)
        return 0

    return _with_api(run, _task_report(args), args)


def cmd_scheduled_task_create(args: argparse.Namespace) -> int:
    """``pepper scheduled-tasks create <name> --prompt P (--cron C | --at T) ...``.

    Exit codes: 0 ok · 1 auth/API/transport · 2 bad input (neither or both
    of --cron/--at, no --prompt, --to alongside --silent, unparseable --at).

    With ``--room`` the task is the room's: ``--cron`` or ``--at``, a
    ``--prompt`` and/or a ``--skill`` the room has been granted, no
    ``--description``, and ``--to`` in the room's dialect. A flag the room
    form lacks is exit 2; a non-owner is refused by the server (exit 1).
    """
    dialect_err = _destination_dialect_error(args.to, room=args.room, topic=args.deliver_topic)
    if dialect_err is not None:
        _emit_error(args, "usage", dialect_err)
        return 2
    if args.room:
        return _create_room_scheduled_task(args)
    if args.skill is not None:
        _emit_error(args, "usage", "--skill is only available with --room")
        return 2
    if args.prompt is None:
        _emit_error(args, "usage", "--prompt is required")
        return 2
    if (args.cron is None) == (args.at is None):
        _emit_error(args, "usage", "pass exactly one of --cron (recurring) or --at (one-off)")
        return 2
    if args.to and args.silent:
        _emit_error(
            args, "usage", "--silent means announce nowhere; it can't be combined with --to"
        )
        return 2
    run_at: str | None = None
    if args.at:
        run_at, err = _parse_due(args.at, flag="--at")
        if err is not None:
            _emit_error(args, "usage", err)
            return 2
    # Three states on the wire, and only one of them is "don't send the
    # key": omitting inherits the user's notification preference, ``[]``
    # is the silent task, a list pins those.
    destinations: list[str] | None = None
    if args.silent:
        destinations = []
    elif args.to:
        destinations = _wire_destinations(args.to)

    def run(api: api_client.ApiClient) -> int:
        task = api.create_scheduled_task(
            name=args.name,
            prompt=args.prompt,
            cron=args.cron,
            run_at=run_at,
            timezone=args.timezone,
            description=args.description,
            destinations=destinations,
        )

        def human() -> None:
            render.info(f"Scheduled {task.name!r}.")
            render.render_scheduled_task_list([task])

        _emit(task, args, human)
        return 0

    return _with_api(run, _report_api_error, args)


def _create_room_scheduled_task(args: argparse.Namespace) -> int:
    """``scheduled-tasks create --room``. The dialect is already checked."""
    if args.description is not None:
        _emit_error(args, "usage", "--description isn't available with --room")
        return 2
    if args.prompt is None and args.skill is None:
        _emit_error(args, "usage", "with --room, pass --prompt, --skill, or both")
        return 2
    if (args.cron is None) == (args.at is None):
        _emit_error(args, "usage", "pass exactly one of --cron (recurring) or --at (one-off)")
        return 2
    run_at: str | None = None
    if args.at:
        run_at, err = _parse_due(args.at, flag="--at")
        if err is not None:
            _emit_error(args, "usage", err)
            return 2
    if args.to and args.silent:
        _emit_error(
            args, "usage", "--silent means announce nowhere; it can't be combined with --to"
        )
        return 2
    # Omitted leaves the delivery to the server (what it picks depends on
    # the deployment); ``[]`` is silent, a list pins those.
    destinations: list[str] | None = None
    if args.silent:
        destinations = []
    elif args.to:
        destinations = _wire_destinations(args.to)

    def run(api: api_client.ApiClient) -> int:
        task = api.create_scheduled_task(
            name=args.name,
            prompt=args.prompt,
            skill=args.skill,
            cron=args.cron,
            run_at=run_at,
            timezone=args.timezone,
            destinations=destinations,
            group_id=_need_group(api, args.room, args=args),
            topic=args.deliver_topic,
        )

        def human() -> None:
            render.info(f"Scheduled {task.name!r} on the room.")
            render.render_scheduled_task_list([task])

        _emit(task, args, human)
        return 0

    return _with_api(run, _report_room_task_error, args)


#: ``scheduled-tasks edit``'s field keys as the flags that set them, for messages.
_EDIT_FLAG_NAMES = {
    "prompt": "--prompt",
    "cron": "--cron",
    "run_at": "--at",
    "timezone": "--timezone",
    "description": "--description",
}


def _saved_flags(edits: dict[str, object]) -> str:
    """The flags of the field edits that landed, for the partial-save message."""
    return ", ".join(
        "--clear-description" if k == "description" and v is None else _EDIT_FLAG_NAMES.get(k, k)
        for k, v in edits.items()
    )


def cmd_scheduled_task_edit(args: argparse.Namespace) -> int:
    """``pepper scheduled-tasks edit <task> [field flags] [--to D... | --default | --silent]``.

    Only the flags actually passed are sent, so the server can tell "leave
    this alone" from "set it to this" — which is why the edits dict is
    built key by key rather than from a namespace dump.

    The delivery flags replace the whole set, so exactly one of ``--to``
    (repeatable), ``--default`` and ``--silent`` may be given: a command with
    no choice on it would have to guess between inherit and silent, which are
    opposite answers. Field flags and delivery are two requests (the server
    takes them separately), so every check that can run locally runs before
    either is sent; field changes go first, and if the delivery request then
    fails the error says the field changes were saved.

    With ``--room`` the room's task is edited, fields and delivery together in
    ONE request (the server applies it atomically, so there is no partial
    save): ``--to`` in the room's dialect or ``--silent``, no ``--default`` (a
    room has no notification preference of its own to follow). Pausing and
    resuming stay ``pause`` / ``resume``: the server takes ``active`` alone.
    """
    if args.description is not None and args.clear_description:
        _emit_error(args, "usage", "pass either --description or --clear-description, not both")
        return 2
    edits: dict[str, object] = {}
    if args.prompt is not None:
        edits["prompt"] = args.prompt
    if args.cron is not None:
        edits["cron"] = args.cron
    if args.at is not None:
        run_at, err = _parse_due(args.at, flag="--at")
        if err is not None:
            _emit_error(args, "usage", err)
            return 2
        edits["run_at"] = run_at
    if args.timezone is not None:
        edits["timezone"] = args.timezone
    if args.description is not None:
        edits["description"] = args.description
    elif args.clear_description:
        edits["description"] = None
    if sum([bool(args.to), args.use_default, args.silent]) > 1:
        _emit_error(args, "usage", "pass only one of --to (repeatable), --default, or --silent")
        return 2
    has_delivery = bool(args.to) or args.use_default or args.silent
    if not edits and not has_delivery and args.deliver_topic is None:
        _emit_error(args, "usage", "nothing to change — pass at least one field to edit")
        return 2
    if args.room and args.use_default:
        _emit_error(args, "usage", "--default isn't available with --room — pass --to or --silent")
        return 2
    if args.room and not edits and not has_delivery:
        _emit_error(args, "usage", "nothing to change — pass a field to edit, --to or --silent")
        return 2
    dialect_err = _destination_dialect_error(args.to, room=args.room, topic=args.deliver_topic)
    if dialect_err is not None:
        _emit_error(args, "usage", dialect_err)
        return 2
    destinations: list[str] | None = None if args.use_default else _wire_destinations(args.to)

    def run(api: api_client.ApiClient) -> int:
        group_id = _need_group(api, args.room, args=args) if args.room else None
        task_id = _resolve_task_id(api, args.task, group_id=group_id, args=args)
        if task_id is None:
            return 2
        if group_id:
            room_task = api.update_room_scheduled_task(
                group_id,
                task_id,
                edits=edits,
                destinations=destinations if has_delivery else None,
                topic=args.deliver_topic,
            )
            _emit(room_task, args, lambda: _edited_human(room_task))
            return 0
        task = api.update_scheduled_task(task_id, edits=edits) if edits else None
        if has_delivery or task is None:  # no field edits means delivery was asked for
            saved = task
            try:
                task = api.set_scheduled_task_destinations(
                    task_id, destinations=destinations, group_id=group_id, topic=args.deliver_topic
                )
            except (api_client.ApiError, httpx.RequestError) as exc:
                if saved is not None:
                    # The field PATCH already landed; say so with the cause.
                    note = (
                        f"saved {_saved_flags(edits)} on {saved.name!r}, "
                        "but changing its delivery failed"
                    )
                    if _json_mode(args):
                        status = exc.status if isinstance(exc, api_client.ApiError) else None
                        cause = (
                            (exc.detail or f"Server returned {exc.status}.")
                            if isinstance(exc, api_client.ApiError)
                            else str(exc)
                        )
                        _emit_error(
                            args, _error_code_for_status(status), f"{note}: {cause}", status=status
                        )
                        raise _UnavailableError from exc
                    render.error(f"{note}:", soft_wrap=True)
                raise
        _emit(task, args, lambda: _edited_human(task))
        return 0

    return _with_api(run, _task_report(args), args)


def _edited_human(task: api_client.ScheduledTaskSummary) -> None:
    render.info(f"Updated {task.name!r}.")
    render.render_scheduled_task_list([task])


def cmd_scheduled_task_renew(args: argparse.Namespace) -> int:
    """``pepper scheduled-tasks renew <task>`` — keep a task going past its renewal warning.

    A refusal (422) prints the server's own reason as is. With ``--room`` the room's
    task is renewed (a room owner's write).
    """

    def run(api: api_client.ApiClient) -> int:
        group_id = _opt_group(api, args.room, args=args)
        task_id = _resolve_task_id(api, args.task, group_id=group_id, args=args)
        if task_id is None:
            return 2
        task = api.renew_scheduled_task(task_id, group_id=group_id)

        def human() -> None:
            render.info(f"Renewed {task.name!r}.")
            render.render_scheduled_task_list([task])

        _emit(task, args, human)
        return 0

    return _with_api(run, _task_report(args), args)


def cmd_scheduled_task_delete(args: argparse.Namespace) -> int:
    """``pepper scheduled-tasks delete <task> [--room ROOM] [--yes]`` — remove a task for good.

    Confirms before deleting unless ``--yes`` (required under ``--json``). The
    task goes with its run history; ``pause`` is the reversible version. With
    ``--room`` it is the room's task (a room owner's write; 403 otherwise).
    """
    whose = "the room's" if args.room else "your"
    if (
        code := _confirm_destructive(
            args,
            f"Delete {whose} scheduled task {args.task!r} and its history? This cannot be undone.",
            yes=args.yes,
        )
    ) is not None:
        return code

    def run(api: api_client.ApiClient) -> int:
        group_id = _opt_group(api, args.room, args=args)
        task_id = _resolve_task_id(api, args.task, group_id=group_id, args=args)
        if task_id is None:
            return 2
        if group_id:
            api.delete_scheduled_task(task_id, group_id=group_id)
        else:
            api.delete_scheduled_task(task_id)
        _emit(
            _mutation_result(None, task_id),
            args,
            lambda: render.info(f"Deleted scheduled task {args.task!r}."),
        )
        return 0

    return _with_api(run, _task_report(args), args)


def cmd_scheduled_task_pause(args: argparse.Namespace) -> int:
    """``pepper scheduled-tasks pause <task> [--room ROOM]`` — soft delete, resumable."""
    return _flip_scheduled_task(args, resume=False)


def cmd_scheduled_task_resume(args: argparse.Namespace) -> int:
    """``pepper scheduled-tasks resume <task> [--room ROOM]``."""
    return _flip_scheduled_task(args, resume=True)


def _flip_scheduled_task(args: argparse.Namespace, *, resume: bool) -> int:
    """Shared body for ``pause`` / ``resume``.

    A 422 on resume is the server saying the row exists but can't go back
    on the schedule (a one-off whose moment has passed) — its ``detail``
    is written for the user, so :func:`_report_api_error` prints it as is.

    With ``--room`` the task is looked up among, and flipped on, that room.
    """

    def run(api: api_client.ApiClient) -> int:
        group_id = _need_group(api, args.room, args=args) if args.room else None
        task_id = _resolve_task_id(api, args.task, group_id=group_id, args=args)
        if task_id is None:
            return 2
        flip = api.resume_scheduled_task if resume else api.cancel_scheduled_task
        row = flip(task_id, group_id=group_id) if group_id else flip(task_id)

        def human() -> None:
            render.info(f"{'Resumed' if resume else 'Paused'}: {row.name}")
            render.render_scheduled_task_list([row])

        _emit(row, args, human)
        return 0

    return _with_api(run, _task_report(args), args)


def _room_skill_shared_by(row: api_client.RoomSkill) -> str:
    """Who shared it, as the listing says it."""
    if row.owner_is_you:
        return "you"
    return row.granted_by or "someone no longer here"


def _room_skill_state_note(row: api_client.RoomSkill) -> str | None:
    """Why a room turn cannot run this one, or ``None`` when it can."""
    if row.state == "inactive":
        toolsets = ", ".join(row.ungrantable_toolsets) or "one of its toolsets"
        return f"inactive: a room can't run {toolsets}"
    if row.state == "shadowed":
        return f"shadowed: an earlier {row.name!r} shared here wins"
    if row.state != "active":
        return row.state
    return None


def _room_skill_line(row: api_client.RoomSkill) -> str:
    notes = [f"shared by {_room_skill_shared_by(row)}"]
    state = _room_skill_state_note(row)
    if state:
        notes.append(state)
    marker = "*" if row.can_revoke else " "
    # Written straight to stdout, not through rich: one line of server text.
    return sanitize_line(
        f"{marker} {row.name} — {row.description}  [{' · '.join(notes)}]  {row.id}"
    )


def _resolve_room_skill(
    api: api_client.ApiClient,
    value: str,
    group_id: str,
    args: argparse.Namespace | None = None,
) -> str:
    """Turn a shared skill's name into its **grant id**, or exit 2.

    The API is keyed by grant id because a name does not pick a row: two
    members can each share a skill called the same thing into one room. A
    UUID passes straight through (the server 404s one that is not this
    room's). One match is the answer; none or several is reported and
    raises :class:`_BadInputError` — never a guess at which was meant.
    """
    if _valid_uuid(value):
        return value
    matches = [row for row in api.list_room_skills(group_id) if row.name == value]
    if len(matches) == 1:
        return matches[0].id
    if not matches:
        _emit_error(
            args,
            "not_found",
            f"no skill named {value!r} is shared with that room — "
            "run 'pepper skills list --room ROOM'",
        )
        raise _BadInputError
    lines = [
        sanitize_line(f"{row.id}  shared by {_room_skill_shared_by(row)} ({row.state})")
        for row in matches
    ]
    head = (
        f"{len(matches)} skills named {value!r} are shared with that room — "
        "pass the grant id in place of the name:"
    )
    if args is not None and _json_mode(args):
        _emit_error(args, "ambiguous", f"{head} {'; '.join(lines)}")
    else:
        render.error(head, soft_wrap=True)
        for line in lines:
            sys.stderr.write(f"  {line}\n")
    raise _BadInputError


def _report_room_skill_share_error(
    exc: api_client.ApiError, args: argparse.Namespace | None = None
) -> int:
    """``skills share --room``: 409 is the email/text-thread refusal, said in one
    fixed sentence; everything else carries the server's own ``detail`` (no
    such skill of yours, a toolset a room can't run, the room unavailable),
    which is written to be read."""
    if exc.status == 409:
        _emit_error(args, "conflict", _ROOM_SKILL_SEALED, status=409)
        return 1
    return _report_api_error(exc, args)


def _report_room_skill_revoke_error(
    exc: api_client.ApiError, args: argparse.Namespace | None = None
) -> int:
    """``skills revoke --room``: 403 is the who-may-revoke gate, said in one
    fixed sentence; everything else carries the server's ``detail``."""
    if exc.status == 403:
        _emit_error(args, "forbidden", _ROOM_SKILL_REVOKE_FORBIDDEN, status=403)
        return 1
    return _report_api_error(exc, args)


def _refuse_room_for_owned_skill(args: argparse.Namespace, verb: str) -> bool:
    """True (after saying why) when ``--room`` was passed to a command that
    only ever acts on the caller's own library."""
    if getattr(args, "room", None):
        _emit_error(args, "usage", _ROOM_OWNS_NO_SKILL.format(verb=verb), soft_wrap=True)
        return True
    return False


def _cmd_room_skills_list(args: argparse.Namespace) -> int:
    def run(api: api_client.ApiClient) -> int:
        rows = api.list_room_skills(_need_group(api, args.room, args=args))

        def human() -> None:
            # Primary output → stdout, plain and grep-able like the own-library
            # listing; the grant id ends every line because it is what tells two
            # same-named rows apart.
            if not rows:
                sys.stdout.write(
                    "(no skills shared with this room — "
                    "try `pepper skills share NAME --room ROOM`)\n"
                )
                return
            for row in rows:
                sys.stdout.write(_room_skill_line(row) + "\n")
            if any(row.can_revoke for row in rows):
                render.info("* you can revoke it: pepper skills revoke NAME --room ROOM")

        _emit_list(rows, args, human)
        return 0

    return _with_api(run, _report_api_error, args)


def _cmd_room_skills_show(args: argparse.Namespace) -> int:
    def run(api: api_client.ApiClient) -> int:
        group_id = _need_group(api, args.room, args=args)
        detail = api.get_room_skill(group_id, _resolve_room_skill(api, args.name, group_id, args))

        def human() -> None:
            note = f"(shared with this room by {_room_skill_shared_by(detail.row)}"
            state = _room_skill_state_note(detail.row)
            render.info(f"{note} — {state})" if state else f"{note})")
            render.write_document(skill_doc.compose_skill_md(_fields_from_detail(detail.skill)))

        _emit(detail, args, human)
        return 0

    return _with_api(run, _report_api_error, args)


def cmd_skills_share(args: argparse.Namespace) -> int:
    """``pepper skills share NAME --room ROOM`` — share one of YOUR skills.

    Sharing one the room already holds changes nothing and says the same.
    Exit codes: 0 ok · 1 auth/API/transport, or the room refused · 2 no such room.
    """

    def run(api: api_client.ApiClient) -> int:
        row = api.share_room_skill(_need_group(api, args.room, args=args), args.name)

        def human() -> None:
            render.info(f"Shared {row.name!r} with the room.")
            state = _room_skill_state_note(row)
            if state:
                render.info(f"It is {state}.")

        _emit(row, args, human)
        return 0

    return _with_api(run, _report_room_skill_share_error, args)


def cmd_skills_revoke(args: argparse.Namespace) -> int:
    """``pepper skills revoke NAME --room ROOM`` — stop sharing it there.

    ``NAME`` is resolved among the room's shared skills; a grant id is taken
    as is. Removes the room's reference only — the skill stays its owner's.
    Exit codes: 0 ok · 1 auth/API/transport, or not yours to revoke ·
    2 no such room, no such shared skill, or an ambiguous name.
    """
    if (
        code := _confirm_destructive(
            args, f"Stop sharing {args.name!r} with that room?", yes=args.yes
        )
    ) is not None:
        return code

    def run(api: api_client.ApiClient) -> int:
        group_id = _need_group(api, args.room, args=args)
        grant_id = _resolve_room_skill(api, args.name, group_id, args)
        api.revoke_room_skill(group_id, grant_id)
        what = "that skill" if _valid_uuid(args.name) else repr(args.name)
        _emit(
            _mutation_result(None, grant_id),
            args,
            lambda: render.info(
                f"Stopped sharing {what} with the room. The skill itself is untouched."
            ),
        )
        return 0

    return _with_api(run, _report_room_skill_revoke_error, args)


def cmd_skills_list(args: argparse.Namespace) -> int:
    """``pepper skills list [--room ROOM] [--json]`` — yours first, then Pepper's
    built-ins; with ``--room``, the skills shared with that room."""
    if getattr(args, "room", None):
        return _cmd_room_skills_list(args)

    def run(api: api_client.ApiClient) -> int:
        rows = api.list_skills()

        def human() -> None:
            yours = [r for r in rows if not r.builtin]
            builtin = [r for r in rows if r.builtin]
            # The listing is primary output → stdout (pipeable); render.info is
            # stderr chrome, reserved for status lines around commands.
            if yours:
                sys.stdout.write("Yours:\n")
                for row in yours:
                    sys.stdout.write(sanitize_line(f"  {row.name} — {row.description}") + "\n")
            else:
                sys.stdout.write("Yours: (none yet — try `pepper skills create <name>`)\n")
            if builtin:
                sys.stdout.write("Built-in (edit creates an override on your account):\n")
                for row in builtin:
                    sys.stdout.write(sanitize_line(f"  {row.name} — {row.description}") + "\n")

        _emit_list(rows, args, human)
        return 0

    return _run_personal(args, run)


def cmd_skills_show(args: argparse.Namespace) -> int:
    """``pepper skills show <name> [--room ROOM] [--json]`` — print the skill as SKILL.md.

    With ``--room``, one of the skills shared with that room (a grant id is
    accepted in place of the name)."""
    if getattr(args, "room", None):
        return _cmd_room_skills_show(args)

    def run(api: api_client.ApiClient) -> int:
        detail = api.get_skill(args.name)

        def human() -> None:
            if detail.builtin:
                render.info(
                    "(built-in — ``pepper skills edit`` creates an override on your account)"
                )
            render.write_document(skill_doc.compose_skill_md(_fields_from_detail(detail)))

        _emit(detail, args, human)
        return 0

    return _run_personal(args, run)


def _skill_document_from_file(
    path: str, args: argparse.Namespace | None = None
) -> skill_doc.SkillFields | None:
    """Parse a SKILL.md read from ``path`` (``-`` = stdin); report why and return ``None``
    when it can't be read or parsed."""
    text = _read_file_or_stdin(path, args)
    if text is None:
        return None
    fields, err = skill_doc.parse_skill_md(text)
    if fields is None:
        _emit_error(args, "usage", f"couldn't parse the document: {err}")
    return fields


def _skill_editor_refused(args: argparse.Namespace) -> bool:
    """True (after saying why) in JSON mode without ``--file``: JSON mode never opens an
    editor or prompts (J2), so the document has to come from ``--file``."""
    if _json_mode(args) and not args.file:
        _emit_error(args, "usage", "JSON mode does not open an editor; pass --file PATH (or -)")
        return True
    return False


def cmd_skills_create(args: argparse.Namespace) -> int:
    """``pepper skills create <name> [--file PATH|-]`` — template → $EDITOR (or file) → POST.

    Exit codes: 0 ok · 1 auth/API/transport · 2 bad document / aborted / ``--room``.
    """
    if _refuse_room_for_owned_skill(args, "write") or _skill_editor_refused(args):
        return 2
    template = skill_doc.SkillFields(
        name=args.name,
        description="One line: when should Pepper activate this skill?",
        body=(
            "Write the playbook here — concrete steps, exact tool names and\n"
            "arguments, the output format, and the don'ts.\n"
        ),
    )
    if args.file:
        fields = _skill_document_from_file(args.file, args)
    else:
        fields = _edit_skill_document(skill_doc.compose_skill_md(template))
    if fields is None:
        return 2
    if fields.name != args.name:
        _emit_error(
            args,
            "usage",
            f"the document renamed the skill ({args.name!r} → {fields.name!r}) — "
            "keep the name; it's the identifier you asked to create",
        )
        return 2

    def run(api: api_client.ApiClient) -> int:
        created = api.create_skill(
            name=fields.name,
            description=fields.description,
            body=fields.body,
            model=fields.model,
            toolsets=list(fields.toolsets),
            max_tool_rounds=fields.max_tool_rounds,
        )
        _emit(created, args, lambda: render.info(f"Created skill {fields.name!r}."))
        return 0

    return _run_personal(args, run)


def cmd_skills_edit(args: argparse.Namespace) -> int:
    """``pepper skills edit <name> [--file PATH|-]`` — fetch → $EDITOR (or file) → PUT (or
    POST override).

    Editing a built-in never touches the shared row: after confirmation
    the edited document is saved as YOUR same-named skill, which shadows
    the built-in on your account only.
    """
    if _refuse_room_for_owned_skill(args, "edit") or _skill_editor_refused(args):
        return 2

    fetched: list[api_client.SkillDetail] = []

    def fetch(api: api_client.ApiClient) -> int:
        fetched.append(api.get_skill(args.name))
        return 0

    if (code := _run_personal(args, fetch)) != 0:
        return code
    detail = fetched[0]

    if detail.builtin and not args.file:
        answer = _prompt(
            f"{args.name!r} is a built-in skill. Editing creates an override on "
            "your account (the shared version is untouched). Continue? [y/N] "
        )
        if answer.lower() not in ("y", "yes"):
            render.info("Aborted — built-in left as is.")
            return 0

    if args.file:
        fields = _skill_document_from_file(args.file, args)
    else:
        fields = _edit_skill_document(skill_doc.compose_skill_md(_fields_from_detail(detail)))
    if fields is None:
        return 2
    if fields.name != detail.name:
        _emit_error(
            args,
            "usage",
            f"the document renamed the skill ({detail.name!r} → {fields.name!r}) — "
            "names are immutable (rename = delete + create)",
        )
        return 2

    def save(api: api_client.ApiClient) -> int:
        if detail.builtin:
            saved = api.create_skill(
                name=fields.name,
                description=fields.description,
                body=fields.body,
                model=fields.model,
                toolsets=list(fields.toolsets),
                max_tool_rounds=fields.max_tool_rounds,
            )
            said = (
                f"Created your override of {fields.name!r} — it shadows the "
                "built-in on your account."
            )
        else:
            saved = api.update_skill(
                name=fields.name,
                description=fields.description,
                body=fields.body,
                model=fields.model,
                toolsets=list(fields.toolsets),
                max_tool_rounds=fields.max_tool_rounds,
            )
            said = f"Updated skill {fields.name!r}."
        _emit(saved, args, lambda: render.info(said))
        return 0

    return _run_personal(args, save)


def cmd_skills_delete(args: argparse.Namespace) -> int:
    """``pepper skills delete <name> [--yes]`` — confirm, then delete YOUR row.

    Confirms before deleting unless ``--yes``.
    """
    if _refuse_room_for_owned_skill(args, "delete"):
        return 2
    if (
        code := _confirm_destructive(
            args, f"Delete your skill {args.name!r}? This cannot be undone.", yes=args.yes
        )
    ) is not None:
        return code

    def run(api: api_client.ApiClient) -> int:
        api.delete_skill(args.name)
        _emit(
            _mutation_result(None, args.name),
            args,
            lambda: render.info(f"Deleted skill {args.name!r}."),
        )
        return 0

    return _run_personal(args, run)


def _fields_from_detail(detail: api_client.SkillDetail) -> skill_doc.SkillFields:
    return skill_doc.SkillFields(
        name=detail.name,
        description=detail.description,
        body=detail.body,
        model=detail.model,
        toolsets=detail.toolsets,
        max_tool_rounds=detail.max_tool_rounds,
    )


def _edit_skill_document(initial: str) -> skill_doc.SkillFields | None:
    """Open ``initial`` in $EDITOR and parse the result, reprompting on
    shape errors so a typo doesn't throw the edit away.

    Returns ``None`` when the user aborts (editor failed, no changes on
    a reprompt decline). Value-level validation is the server's job.
    """
    text = initial
    while True:
        edited = _open_in_editor(text)
        if edited is None:
            # _open_in_editor already reported the specific failure
            # (bad $EDITOR, launch error, or nonzero exit).
            render.error("aborting without saving")
            return None
        fields, err = skill_doc.parse_skill_md(edited)
        if fields is not None:
            return fields
        render.error(f"couldn't parse the document: {err}")
        answer = _prompt("Re-open the editor to fix it? [Y/n] ")
        if answer.lower() in ("n", "no"):
            return None
        text = edited


def _open_in_editor(initial: str) -> str | None:
    """Write ``initial`` to a temp .md file, run $VISUAL/$EDITOR (vi
    fallback), and return the edited text — ``None`` if the editor
    can't be parsed, can't be launched, or exits nonzero.
    ``shlex.split`` so values like ``code --wait`` work."""
    import os
    import shlex
    import subprocess
    import tempfile
    from pathlib import Path

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    try:
        command = shlex.split(editor)
    except ValueError as exc:
        render.error(f"couldn't parse $VISUAL/$EDITOR ({editor!r}): {exc}")
        return None
    if not command:
        render.error("$VISUAL/$EDITOR is empty — set one to edit skills")
        return None
    fd, path = tempfile.mkstemp(suffix=".md", prefix="pepper-skill-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(initial)
        try:
            # S603 suppressed: `command` is the user's own $EDITOR, invoked
            # without a shell — they already control what runs in their terminal.
            result = subprocess.run([*command, path], check=False)  # noqa: S603
        except OSError as exc:
            # FileNotFoundError (no such binary) and friends — a
            # misconfigured editor should be a friendly message, not a
            # traceback.
            render.error(f"couldn't launch editor {editor!r}: {exc}")
            return None
        if result.returncode != 0:
            render.error(f"editor exited with status {result.returncode}")
            return None
        return Path(path).read_text(encoding="utf-8")
    finally:
        os.unlink(path)


def _to_plain(value: object) -> object:
    """A dataclass instance as a plain dict (recursively); anything else unchanged."""
    import dataclasses

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return value


def _write_json(data: object) -> None:
    """The one place JSON documents reach stdout: indented, newline-terminated."""
    sys.stdout.write(json.dumps(data, indent=2, default=str))
    sys.stdout.write("\n")


def _print_json(obj: object) -> None:
    """Serialize a dataclass (or a plain list/dict of them) to stdout as JSON.

    ``dataclasses.asdict`` recurses through nested dataclasses (e.g.
    ``GroupSummary.topics``), which covers every ``--json`` call site here
    except one shape it doesn't reach on its own: a *top-level* plain
    ``list`` of dataclass instances (``topics list --json`` — the
    endpoint has no wrapping dataclass of its own), so that case is
    converted element-wise. ``default=str`` is defensive, not load-bearing
    — every field on these wire shapes is already JSON-safe.
    """
    _write_json([_to_plain(v) for v in obj] if isinstance(obj, list) else _to_plain(obj))


# ---------------------------------------------------------------------------
# Shared flags, JSON mode, output and error helpers
#
# The building blocks of the CLI contract in ``planning/cli-information-
# architecture.md`` (§5). Phase 1 only builds them; the commands that adopt
# them move over in the phases that rename those commands, because attaching a
# parent parser changes a leaf's ``--help`` and a list/error shape is a
# breaking change (invariant 4).
# ---------------------------------------------------------------------------

#: ``PEPPER_JSON`` switches JSON mode on only when it is exactly this (J10).
_PEPPER_JSON_ON = "1"


def _json_mode(args: argparse.Namespace) -> bool:
    """Is this invocation in JSON mode (J10)?

    ``--json`` on the command line always wins; otherwise ``PEPPER_JSON`` set to
    exactly ``1``. Any other value, or unset, is off — there is no ``--no-json``;
    ``PEPPER_JSON= pepper …`` switches it off for one command. Only leaves that
    accept ``--json`` call this (the REPL and ``login`` never do, which is the J1
    exemption), so a leaf without the flag never reaches the ``getattr`` default.
    """
    return bool(getattr(args, "json", False)) or os.environ.get("PEPPER_JSON") == _PEPPER_JSON_ON


def _build_json_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON on stdout instead of text (also on when PEPPER_JSON=1).",
    )
    return parent


def _topic_parent(*, required: bool = False) -> argparse.ArgumentParser:
    """``--topic TOPIC`` parent: required where the verb acts on one topic's messages."""
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--topic",
        metavar="TOPIC",
        required=required,
        help="Topic: its UUID, or a case-insensitive unique name prefix (needs --room)."
        + (
            ""
            if required
            else " Omit for the most recently active unarchived topic,"
            " or the most recent topic if all are archived."
        ),
    )
    return parent


def _build_yes_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt (required when there is no terminal).",
    )
    return parent


def _build_page_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--limit", type=int, metavar="N", help="At most N items.")
    parent.add_argument(
        "--before",
        metavar="CURSOR",
        help="Older than this cursor (a next_before value an earlier call printed).",
    )
    parent.add_argument("--since", metavar="CURSOR", help="Newer than this cursor.")
    return parent


#: Built once, attached with ``parents=[...]`` (each has ``add_help=False``).
_json_parent = _build_json_parent()
_yes_parent = _build_yes_parent()
_page_parent = _build_page_parent()


def _build_before_parent() -> argparse.ArgumentParser:
    """``--before CURSOR`` alone, for keyset lists that page backwards only (§5.3)."""
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--before",
        metavar="CURSOR",
        help="Older than this cursor (a next_before value an earlier call printed).",
    )
    return parent


_before_parent = _build_before_parent()


def _room_parent(
    *, required: bool = False, omitted: str = "Omit to act on your own."
) -> argparse.ArgumentParser:
    """``--room ROOM`` parent: required where the verb acts inside a room (§6)."""
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--room",
        metavar="ROOM",
        required=required,
        help="Room: its UUID, or a case-insensitive unique name prefix."
        + ("" if required else f" {omitted}"),
    )
    return parent


def _emit(obj: object, args: argparse.Namespace, human: Callable[[], None]) -> None:
    """Write ``obj`` as one JSON document in JSON mode, else call ``human()``.

    ``human`` is the existing ``render.py`` formatter, closed over its inputs, so
    JSON mode never runs it and no colour, prose or progress reaches stdout (J2).
    """
    if _json_mode(args):
        _print_json(obj)
    else:
        human()


def _list_envelope(
    items: Sequence[object], next_before: str | None = None, **meta: object
) -> dict[str, object]:
    """The J3 list envelope: ``{"items": [...], "next_before": str|null, **meta}``.

    ``next_before`` is always present (``None`` on the last page and on unpaged
    lists). Server list-level metadata rides beside the two keys as ``meta``.
    """
    return {"items": [_to_plain(item) for item in items], "next_before": next_before, **meta}


def _emit_list(
    items: Sequence[object],
    args: argparse.Namespace,
    human: Callable[[], None],
    next_before: str | None = None,
    **meta: object,
) -> None:
    """Emit a list: the J3 envelope in JSON mode, else ``human()``."""
    if _json_mode(args):
        _write_json(_list_envelope(items, next_before, **meta))
    else:
        human()


#: The J8 ``error.code`` vocabulary.
_ERROR_CODES = frozenset(
    {
        "auth",
        "forbidden",
        "not_found",
        "conflict",
        "validation",
        "ambiguous",
        "usage",
        "upgrade_required",
        "server",
        "network",
        "gap",
    }
)

_ERROR_CODE_BY_STATUS = {
    401: "auth",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    412: "conflict",
    426: "upgrade_required",
}


def _error_code_for_status(status: int | None) -> str:
    """Map an HTTP status (``None`` = no response) to a J8 ``code``."""
    if status is None:
        return "network"
    if status in _ERROR_CODE_BY_STATUS:
        return _ERROR_CODE_BY_STATUS[status]
    return "server" if status >= 500 else "validation"


def _emit_error(
    args: argparse.Namespace | None,
    code: str,
    message: str,
    *,
    status: int | None = None,
    soft_wrap: bool = False,
) -> None:
    """Report an error: one J8 JSON line on stderr in JSON mode, else the usual text line.

    stdout is left empty either way. The caller still returns the exit code (J9).
    ``message`` must never carry a token. ``args=None`` is a caller that has no
    ``--json`` (another command family sharing a helper): always the text line.
    """
    if code not in _ERROR_CODES:
        raise ValueError(f"unknown error code {code!r}")
    if args is not None and _json_mode(args):
        line = json.dumps({"error": {"code": code, "status": status, "message": message}})
        sys.stderr.write(line + "\n")
    else:
        render.error(message, soft_wrap=soft_wrap)


def _creds_or_fail(args: argparse.Namespace) -> credentials.Credentials | None:
    """:func:`_load_or_error` with the J8 error shape in JSON mode."""
    try:
        return credentials.load()
    except credentials.CredentialsError as exc:
        _emit_error(args, "auth", str(exc))
        return None


def _fail_api(args: argparse.Namespace, exc: api_client.ApiError) -> int:
    """:func:`_report_api_error` with the J8 error shape in JSON mode (exit 1)."""
    if exc.status == 401:
        message = "Authentication failed (401). Run 'pepper login' to refresh your token."
    elif exc.detail:
        message = f"Server returned {exc.status}: {exc.detail}"
    else:
        message = f"Server returned {exc.status}."
    _emit_error(args, _error_code_for_status(exc.status), message, status=exc.status)
    return 1


def _run_personal(
    args: argparse.Namespace,
    run: Callable[[api_client.ApiClient], int],
    report: Callable[[argparse.Namespace, api_client.ApiError], int] = _fail_api,
) -> int:
    """Open a client for ``run`` and map the failures every personal command shares.

    Errors follow J8 under ``--json`` (one JSON line on stderr), else the usual text.
    """
    creds = _creds_or_fail(args)
    if creds is None:
        return 1
    try:
        with api_client.ApiClient(server_url=creds.server_url, token=creds.token) as api:
            return run(api)
    except api_client.ApiError as exc:
        return report(args, exc)
    except httpx.RequestError as exc:
        _emit_error(args, "network", f"Couldn't reach {creds.server_url}: {exc}")
        return 1


def _confirm_destructive(args: argparse.Namespace, prompt: str, *, yes: bool) -> int | None:
    """Gate a destructive personal command: ``None`` to go ahead, else the exit code.

    ``--yes`` skips it; a terminal gets a y/N question; with no terminal (or under
    JSON mode, which never prompts) it refuses with exit 2, since a script must say
    ``--yes`` out loud.
    """
    if yes:
        return None
    if _json_mode(args) or not _stdin_is_tty():
        _emit_error(args, "usage", "this deletes for good; pass --yes to run it without a prompt")
        return 2
    if _prompt(f"{prompt} [y/N] ").lower() not in ("y", "yes"):
        render.info("Aborted.")
        return 0
    return None


class _RenamedStubParser(argparse.ArgumentParser):
    """Parser for a retired noun: accepts any argument tail without looking at it.

    ``REMAINDER`` alone is not enough, because an option-looking first token
    (``pepper groups --json``, ``pepper groups -h``) would still be rejected as
    unrecognized before the handler ran.
    """

    def __init__(self, prog: str, handler: Callable[[argparse.Namespace], int]) -> None:
        super().__init__(prog=prog, add_help=False)
        self._stub_handler = handler

    def parse_known_args(
        self, args: Iterable[str] | None = None, namespace: Any = None
    ) -> tuple[Any, list[str]]:
        del args
        out = namespace if namespace is not None else argparse.Namespace()
        out.handler = self._stub_handler
        return out, []


def _renamed_stub_handler(old: str, hint: str) -> Callable[[argparse.Namespace], int]:
    def run(args: argparse.Namespace) -> int:
        del args
        print(f"pepper: '{old}' was renamed in 0.2.0 — {hint}", file=sys.stderr)
        return 2

    return run


def _add_renamed_stub(
    sub: Any,  # the object ``ArgumentParser.add_subparsers`` returns
    old: str,
    hint: str,
) -> argparse.ArgumentParser:
    """Register ``old`` as a retired noun (§7): it names its replacement and exits 2.

    The name must stay registered so the implicit-chat fallback never sends
    ``pepper <old> …`` to Pepper as a message. The stub runs nothing (no
    credentials, no network) and swallows whatever follows, options included, so
    the exit is the same whatever the old command line was. It is left out of the
    subcommand help listing (no ``help=``).
    """
    stub = _RenamedStubParser(f"pepper {old}", _renamed_stub_handler(old, hint))
    sub.choices[old] = stub
    return stub


def _match_by_name[T](
    value: str, items: Sequence[T], *, name_of: Callable[[T], str]
) -> tuple[T | None, list[T]]:
    """Case-insensitive name resolution shared by group/topic lookups.

    An exact (case-insensitive) name match wins outright even if it is also
    a prefix of other names. Failing that, a *unique* case-insensitive
    prefix match wins. Returns ``(None, candidates)`` on no match (empty
    ``candidates``) or an ambiguous one (``len(candidates) > 1``) so the
    caller can print a "which one?" error without a second pass over
    ``items``.
    """
    lowered = value.lower()
    exact = [it for it in items if name_of(it).lower() == lowered]
    if len(exact) == 1:
        return exact[0], exact
    if len(exact) > 1:
        return None, exact
    prefix = [it for it in items if name_of(it).lower().startswith(lowered)]
    if len(prefix) == 1:
        return prefix[0], prefix
    return None, prefix


class _GroupsDisabledError(Exception):
    """Raised when the groups capability signal says the surface is off.

    Kept distinct from "not found" / "ambiguous" (which return ``None``
    from the resolvers below and exit 2, a bad-input code) because "not
    enabled for your account" is an availability fact, not something the
    user typed wrong — every ``groups`` command handler catches this
    alongside :class:`api_client.ApiError` and exits 1, the same code
    ``_report_api_error`` uses for a 401.
    """


def _group_list_or_error(
    api: api_client.ApiClient, *, archived: bool = False
) -> api_client.GroupList:
    """Fetch the groups list, checking the capability signal first.

    ``enabled=False`` means this deployment has no groups service
    configured — there is no per-user flag, so this is the one place a
    client can tell (``GroupList``'s docstring calls it out as exactly
    that: the capability signal for the surface). Every ``groups``
    subcommand that needs a name→id lookup routes through here so the
    "not enabled" message is worded the same way everywhere. Raises
    :class:`_GroupsDisabledError` rather than returning ``None`` — see its
    docstring for why that exit code has to be distinct.
    """
    gl = api.list_groups(archived=archived)
    if not gl.enabled:
        raise _GroupsDisabledError
    return gl


def _resolve_group(
    api: api_client.ApiClient,
    value: str,
    *,
    rooms: api_client.GroupList | None = None,
    args: argparse.Namespace | None = None,
) -> str | None:
    """Resolve ``<group>`` to its id.

    A UUID passes straight through untouched — same contract as
    ``_resolve_task_id``: the server 404s a wrong one, which is cheaper
    than a client-side existence check nobody asked for. Otherwise matches
    by name against the caller's unarchived rooms first, falling back to
    the archived ones so an archived room's name still resolves without
    ``--archived`` on the enclosing command (only ``groups list``/``topics``
    have a reason to distinguish the two folds). ``rooms`` is the
    unarchived list when the caller has already fetched it.
    """
    if _valid_uuid(value):
        return value
    gl = rooms if rooms is not None else _group_list_or_error(api)
    match, candidates = _match_by_name(value, gl.groups, name_of=lambda g: g.display_name)
    if match is None and not candidates:
        archived = api.list_groups(archived=True)
        match, candidates = _match_by_name(value, archived.groups, name_of=lambda g: g.display_name)
    if match is not None:
        return match.group_id
    if candidates:
        listing = ", ".join(f"{g.display_name!r} ({g.group_id})" for g in candidates)
        _emit_error(
            args, "ambiguous", f"{value!r} matches more than one room: {listing}", soft_wrap=True
        )
    else:
        _emit_error(args, "not_found", f"no room named {value!r} — run 'pepper rooms list'")
    return None


def _default_topic_id(
    api: api_client.ApiClient, group_id: str, *, args: argparse.Namespace | None = None
) -> str | None:
    """The room's default topic, mirroring the web door's own rule
    (``pepper.features.groups.room``: open a room to its most-recently-active
    non-archived topic, or ``topics[0]`` if every topic is archived).

    ``list_group_topics`` already answers most-recently-active first
    (``pepper.features.groups.contract.topics_payload``), so "first
    non-archived" here is exactly that rule applied client-side — there is
    no need to read the wire's ``is_default`` instead: that marks the room's
    *service-side* default topic (where an unprompted post lands), a different
    question from which topic a person's view opens on.
    """
    topics = api.list_group_topics(group_id)
    if not topics:
        _emit_error(args, "not_found", "this room has no topics")
        return None
    default = next((t for t in topics if t.archived_at is None), topics[0])
    return default.topic_id


def _resolve_topic(
    api: api_client.ApiClient,
    group_id: str,
    value: str | None,
    *,
    args: argparse.Namespace | None = None,
) -> str | None:
    """Resolve ``<topic>`` to its id, or the room's default when omitted.

    Same name-or-UUID contract as :func:`_resolve_group`, scoped to one
    room's topics (:meth:`api_client.ApiClient.list_group_topics` — archived
    included, so a topic name still resolves without ``--archived`` on the
    caller's own listing).
    """
    if value is None:
        return _default_topic_id(api, group_id, args=args)
    if _valid_uuid(value):
        return value
    topics = api.list_group_topics(group_id)
    match, candidates = _match_by_name(value, topics, name_of=lambda t: t.name)
    if match is not None:
        return match.topic_id
    if candidates:
        listing = ", ".join(f"{t.name!r} ({t.topic_id})" for t in candidates)
        _emit_error(
            args, "ambiguous", f"{value!r} matches more than one topic: {listing}", soft_wrap=True
        )
    else:
        choices = ", ".join(t.name for t in topics) or "(no topics)"
        _emit_error(
            args, "not_found", f"no topic named {value!r} in this room — choices: {choices}"
        )
    return None


class _BadInputError(Exception):
    """A name that didn't resolve (already reported); the command exits 2."""


class _UnavailableError(Exception):
    """The server said it couldn't ask (already reported); the command exits 1."""


def _need_group(
    api: api_client.ApiClient, value: str, *, args: argparse.Namespace | None = None
) -> str:
    group_id = _resolve_group(api, value, args=args)
    if group_id is None:
        raise _BadInputError
    return group_id


def _opt_group(
    api: api_client.ApiClient, value: str | None, *, args: argparse.Namespace | None = None
) -> str | None:
    return _need_group(api, value, args=args) if value else None


def _need_topic(
    api: api_client.ApiClient,
    group_id: str,
    value: str | None,
    *,
    args: argparse.Namespace | None = None,
) -> str:
    topic_id = _resolve_topic(api, group_id, value, args=args)
    if topic_id is None:
        raise _BadInputError
    return topic_id


def _report_room_error(exc: api_client.ApiError, args: argparse.Namespace | None = None) -> int:
    """One line for the statuses room commands meet: 404 is the server's uniform
    "no such room/topic/message/todo, or you are not a member"; 403 is not allowed."""
    if exc.status == 404:
        _emit_error(
            args, "not_found", "not found, or you are not a member of that room", status=404
        )
        return 2
    if exc.status == 403:
        _emit_error(
            args,
            "forbidden",
            f"not allowed: {exc.detail or 'the room does not permit this'}",
            status=403,
        )
        return 1
    return _report_api_error(exc, args)


def _with_api(
    run: Callable[[api_client.ApiClient], int],
    report: Callable[[api_client.ApiError, argparse.Namespace | None], int] = _report_room_error,
    args: argparse.Namespace | None = None,
) -> int:
    """Load credentials, open a client and map the errors every room command shares.

    With ``args`` (a leaf that takes ``--json``) every failure goes through
    :func:`_emit_error`, so JSON mode gets the J8 line; ``report`` is called as
    ``report(exc, args)``.
    """
    creds = _load_or_error(args)
    if creds is None:
        return 1
    try:
        with api_client.ApiClient(server_url=creds.server_url, token=creds.token) as api:
            return run(api)
    except _BadInputError:
        return 2
    except _UnavailableError:
        return 1
    except _GroupsDisabledError:
        _emit_error(args, "forbidden", "Rooms are not enabled for your account.")
        return 1
    except api_client.ApiError as exc:
        return report(exc, args)
    except httpx.RequestError as exc:
        _emit_error(args, "network", f"Couldn't reach {creds.server_url}: {exc}")
        return 1


def _with_room_api(args: argparse.Namespace, run: Callable[[api_client.ApiClient], int]) -> int:
    """:func:`_with_api` for the ``rooms``/``topics``/``messages``/``attachments`` leaves:
    every failure goes through :func:`_emit_error`, so ``--json`` gets the J8 line."""
    creds = _load_or_error(args)
    if creds is None:
        return 1
    try:
        with api_client.ApiClient(server_url=creds.server_url, token=creds.token) as api:
            return run(api)
    except _BadInputError:
        return 2
    except _UnavailableError:
        return 1
    except _GroupsDisabledError:
        _emit_error(args, "forbidden", "Rooms are not enabled for your account.")
        return 1
    except api_client.ApiError as exc:
        return _report_room_error(exc, args)
    except httpx.RequestError as exc:
        _emit_error(args, "network", f"Couldn't reach {creds.server_url}: {exc}")
        return 1


def _confirm(prompt: str, *, yes: bool, args: argparse.Namespace | None = None) -> int | None:
    """Gate a destructive command: ``None`` to go ahead, else the exit code to return.

    ``--yes`` skips it; a terminal gets a y/N question; anything else refuses,
    since a script must say ``--yes`` out loud. Under JSON mode a prompt never
    happens (J2), so it refuses too."""
    if yes:
        return None
    if (args is not None and _json_mode(args)) or not _stdin_is_tty():
        _emit_error(
            args,
            "usage",
            "this changes the room for everyone; pass --yes to run it without a terminal",
        )
        return 2
    if _prompt(f"{prompt} [y/N] ").lower() not in ("y", "yes"):
        render.info("Aborted.")
        return 0
    return None


#: Help for a leaf whose positional is the room it acts on (``rooms show ROOM``).
_ROOM_ARG_HELP = "Room: its UUID, or a case-insensitive unique name prefix."


def _mutation_result(result: object, object_id: str) -> object:
    """J5: the object the server returned, else ``{"ok": true, "id": ...}`` for a 204 route.

    A reaction route answers the list of reactions, which rides under ``reactions``.
    """
    if isinstance(result, dict) and result:
        return result
    if isinstance(result, list):
        return {"reactions": result}
    return {"ok": True, "id": object_id}


def _room_act(
    args: argparse.Namespace,
    call: Callable[[api_client.ApiClient, str], tuple[object, str]],
    done: str,
    *,
    confirm: str | None = None,
) -> int:
    """Confirm if the act is destructive, resolve ``args.room``, run ``call``, say ``done``.

    ``call`` returns the server's answer and the id of the object it acted on. Human
    mode prints ``done`` on stderr; JSON mode prints the J5 mutation object.
    """
    if confirm is not None and (code := _confirm(confirm, yes=args.yes, args=args)) is not None:
        return code

    def run(api: api_client.ApiClient) -> int:
        result, object_id = call(api, _need_group(api, args.room, args=args))
        _emit(_mutation_result(result, object_id), args, lambda: render.info(done))
        return 0

    return _with_room_api(args, run)


def cmd_rooms_list(args: argparse.Namespace) -> int:
    """``pepper rooms list [--archived] [--json]``."""

    def run(api: api_client.ApiClient) -> int:
        result = _group_list_or_error(api, archived=args.archived)
        _emit_list(
            result.groups,
            args,
            lambda: render.render_group_list(result),
            enabled=result.enabled,
            degraded=result.degraded,
            total=result.total,
            archived_total=result.archived_total,
            features=list(result.features),
        )
        return 0

    return _with_room_api(args, run)


def cmd_rooms_show(args: argparse.Namespace) -> int:
    """``pepper rooms show ROOM [--json]``."""

    def run(api: api_client.ApiClient) -> int:
        detail = api.get_group(_need_group(api, args.room, args=args))
        _emit(detail, args, lambda: render.render_group_detail(detail))
        return 0

    return _with_room_api(args, run)


def cmd_rooms_create(args: argparse.Namespace) -> int:
    """``pepper rooms create [--name N] PERSON... [--json]``; prints the room's id.

    A name is required. The check is local, so an unnamed create never reaches the service
    (which answers 422 for it).
    """
    if not (args.name or "").strip():
        _emit_error(args, "usage", _ROOM_CREATE_NAME_REQUIRED)
        return 2

    def run(api: api_client.ApiClient) -> int:
        made = api.create_group(
            people=[_handle_of(api, p, args=args) for p in args.people], name=args.name
        )

        def human() -> None:
            if not made.get("created", True):
                render.info("That conversation already existed.")
            print(sanitize_line(str(made["group_id"])))

        _emit(made, args, human)
        return 0

    return _with_room_api(args, run)


def cmd_rooms_edit(args: argparse.Namespace) -> int:
    """``pepper rooms edit ROOM --name NAME``."""
    return _room_act(
        args,
        lambda api, g: (api.rename_group(g, name=args.name), g),
        f"Renamed to {args.name!r}.",
    )


def cmd_rooms_archive(args: argparse.Namespace) -> int:
    """``pepper rooms archive ROOM``: hides the room from your list only (not destructive)."""
    return _room_act(
        args, lambda api, g: (api.set_group_archived(g, archived=True), g), "Archived."
    )


def cmd_rooms_unarchive(args: argparse.Namespace) -> int:
    """``pepper rooms unarchive ROOM``."""
    return _room_act(
        args, lambda api, g: (api.set_group_archived(g, archived=False), g), "Unarchived."
    )


def cmd_rooms_leave(args: argparse.Namespace) -> int:
    """``pepper rooms leave ROOM [--yes]`` (open rooms only)."""
    return _room_act(
        args,
        lambda api, g: (api.leave_group(g), g),
        "Left the room.",
        confirm=f"Leave {args.room!r}? You'll need an invite to come back.",
    )


def cmd_rooms_mute(args: argparse.Namespace) -> int:
    """``pepper rooms mute ROOM``."""
    return _room_act(args, lambda api, g: (api.set_group_muted(g, muted=True), g), "Muted.")


def cmd_rooms_unmute(args: argparse.Namespace) -> int:
    """``pepper rooms unmute ROOM``."""
    return _room_act(args, lambda api, g: (api.set_group_muted(g, muted=False), g), "Unmuted.")


def cmd_rooms_activity(args: argparse.Namespace) -> int:
    """``pepper rooms activity [--room R] [--since TIME] [--json]``.

    The feed needs its candidate topics named, so this names the 50 most
    recently active unarchived topics of the room (or of all your rooms).
    """
    from datetime import UTC, datetime, timedelta

    since = args.since or (datetime.now(UTC) - timedelta(hours=24)).isoformat()

    def run(api: api_client.ApiClient) -> int:
        if args.room:
            room_ids = [_need_group(api, args.room, args=args)]
        else:
            room_ids = [r.group_id for r in _group_list_or_error(api).groups]
        topics = sorted(
            (t for gid in room_ids for t in api.list_group_topics(gid) if t.archived_at is None),
            key=lambda t: t.last_activity_at,
            reverse=True,
        )
        rows = api.group_activity(since=since, topic_ids=[t.topic_id for t in topics[:50]])

        def human() -> None:
            for r in rows:
                line = f"{r['group_label']} / {r['topic_label']}  {r['author']}: {r['excerpt']}"
                render.info(sanitize_line(line))

        _emit_list(rows, args, human)
        return 0

    return _with_room_api(args, run)


def cmd_topics_list(args: argparse.Namespace) -> int:
    """``pepper topics list --room R [--archived] [--json]``.

    ``list_group_topics`` always answers every topic, archived included
    (the contract's own fold: "hiding them is the client's fold over one
    full answer"). Default view here is the folded one; ``--archived``
    unfolds it.
    """

    def run(api: api_client.ApiClient) -> int:
        topics = api.list_group_topics(_need_group(api, args.room, args=args))
        if not args.archived:
            # The room's default topic stays even when archived, so ``--json``
            # always lets a caller identify it (``is_default``); the archived tag
            # still shows in the text view.
            topics = [t for t in topics if t.archived_at is None or t.is_default]
        _emit_list(topics, args, lambda: render.render_group_topics(topics))
        return 0

    return _with_room_api(args, run)


def cmd_topics_create(args: argparse.Namespace) -> int:
    """``pepper topics create --room R NAME...``; prints the new topic's id."""

    def run(api: api_client.ApiClient) -> int:
        topic = api.create_group_topic(
            _need_group(api, args.room, args=args), name=" ".join(args.name)
        )
        _emit(
            topic, args, lambda: print(sanitize_line(str(topic.get("topic_id") or topic.get("id"))))
        )
        return 0

    return _with_room_api(args, run)


def cmd_topics_edit(args: argparse.Namespace) -> int:
    """``pepper topics edit --room R TOPIC --name NAME``."""

    def call(api: api_client.ApiClient, g: str) -> tuple[object, str]:
        topic_id = _need_topic(api, g, args.topic, args=args)
        return api.rename_group_topic(g, topic_id, name=args.name), topic_id

    return _room_act(args, call, f"Renamed to {args.name!r}.")


def _topic_archive(args: argparse.Namespace, *, archived: bool) -> int:
    def call(api: api_client.ApiClient, g: str) -> tuple[object, str]:
        topic_id = _need_topic(api, g, args.topic, args=args)
        return api.set_group_topic_archived(g, topic_id, archived=archived), topic_id

    return _room_act(args, call, "Archived." if archived else "Unarchived.")


def cmd_topics_archive(args: argparse.Namespace) -> int:
    """``pepper topics archive --room R TOPIC``."""
    return _topic_archive(args, archived=True)


def cmd_topics_unarchive(args: argparse.Namespace) -> int:
    """``pepper topics unarchive --room R TOPIC``."""
    return _topic_archive(args, archived=False)


def cmd_topics_search(args: argparse.Namespace) -> int:
    """``pepper topics search [--room R] QUERY...``: topics by name across your rooms.

    The server route has no room filter and its rows name the room by display name
    only, so ``--room`` filters client-side on that name (two rooms sharing a name
    both match).
    """

    def run(api: api_client.ApiClient) -> int:
        rows = api.search_group_topics(" ".join(args.query))
        if args.room:
            room_id = _need_group(api, args.room, args=args)
            names = {
                g.display_name
                for g in _group_list_or_error(api).groups
                if _same_id(g.group_id, room_id)
            }
            rows = [r for r in rows if r.get("group_name") in names]

        def human() -> None:
            for r in rows:
                print(sanitize_line(f"{r['group_name']} / {r['name']}  {r['topic_id']}"))

        _emit_list(rows, args, human)
        return 0

    return _with_room_api(args, run)


def cmd_messages_list(args: argparse.Namespace) -> int:
    """``pepper messages list --room R [--topic T] [--limit N] [--before C | --since S]
    [--mark] [--json]``."""
    if args.before is not None and args.since is not None:
        _emit_error(args, "usage", "--before and --since cannot be used together")
        return 2

    def run(api: api_client.ApiClient) -> int:
        group_id = _need_group(api, args.room, args=args)
        topic_id = _need_topic(api, group_id, args.topic, args=args)
        page = api.list_group_messages(
            group_id, topic_id, since=args.since, before=args.before, limit=args.limit
        )
        # Mark before printing: a failed mark exits non-zero with nothing on stdout.
        if args.mark and page.messages:
            newest = max(m.created_at for m in page.messages)
            api.set_group_read_mark(group_id, topic_id, read_through=newest)
        _emit_list(
            page.messages,
            args,
            lambda: render.render_group_messages(page),
            next_before=page.next_before,
        )
        return 0

    return _with_room_api(args, run)


def _read_attachment(
    path: str, args: argparse.Namespace | None = None
) -> tuple[bytes, str, str] | None:
    """Read a local file for ``messages post --attach``.

    Returns ``(content, filename, content_type)``, or ``None`` after
    printing the error (missing file, permissions) so the caller can bail
    with exit code 2 before any network call — same shape as the other
    local-validation-before-any-call checks in this module.
    """
    from pathlib import Path

    p = Path(path)
    try:
        content = p.read_bytes()
    except OSError as exc:
        _emit_error(args, "usage", f"couldn't read attachment {path!r}: {exc}")
        return None
    content_type, _ = mimetypes.guess_type(p.name)
    return content, p.name, content_type or "application/octet-stream"


def cmd_messages_post(args: argparse.Namespace) -> int:
    """``pepper messages post --room R --topic T [MESSAGE | -] [--attach PATH ...]
    [--reply-to MESSAGE_ID] [--mention PERSON_ID ...] [--on-behalf-of PERSON_ID]
    [--idempotency-key KEY] [--json]``.

    ``--json`` prints the created message as one JSON object instead of
    ``Posted.`` and the bare id: the same fields ``messages list --json`` gives
    a message, plus ``topic_id``, so an agent gets the id and ``created_at``
    of its own post without a second read.

    Attachments upload first (``upload_group_attachment``), in the order
    given, and their ids ride the message create — so a message never posts
    without the attachments it referenced. A fresh idempotency key is
    minted per invocation (not per retry): a retried command is a new
    process, so this alone doesn't prevent a double-post from re-running
    the command by hand, but it does mean the server's own retry/transport
    layer can't double it — see ``post_group_message``'s ``Idempotency-Key``.
    ``--idempotency-key`` replaces the fresh key with the caller's own, for a
    script that must not double-post when it re-runs the same command after
    a crash (the agent-member watcher keys it on the message it answers).

    ``--reply-to`` and ``--mention`` pass straight through as the post's
    ``parent_message_id`` and ``mentions`` (bare person ids, from
    ``members list``). Neither is validated here: the service owns both
    checks, and a bad id is its 4xx, reported like any other.
    ``--on-behalf-of`` likewise passes through as ``on_behalf_of_person_id``;
    the server accepts it only from an agent token.

    Exit codes: 0 ok · 1 auth/API/transport · 2 bad input (empty message,
    ambiguous/unknown room or topic, unreadable attachment).
    """
    # A flag given with an empty value is a caller bug (typically an unset
    # shell variable), not a request for the default. For --idempotency-key
    # the default is a fresh key, which would silently drop the caller's
    # retry safety — so refuse rather than fall through. Checked before the
    # body, so a bad flag never blocks on reading a '-' stdin body.
    for flag, value in (
        ("--idempotency-key", args.idempotency_key),
        ("--reply-to", args.reply_to),
        ("--on-behalf-of", args.on_behalf_of),
        *(("--mention", m) for m in args.mention or []),
    ):
        if value is not None and not value.strip():
            _emit_error(args, "usage", f"{flag} cannot be empty")
            return 2
    # The service accepts a requester only on a reply whose parent the
    # requester wrote, so a bare --on-behalf-of is a
    # post it must reject — refuse it here, before any upload.
    if args.on_behalf_of is not None and args.reply_to is None:
        _emit_error(args, "usage", "--on-behalf-of needs --reply-to (the requester's message)")
        return 2

    message_parts = args.message or []
    text = sys.stdin.read().strip() if message_parts == ["-"] else " ".join(message_parts).strip()
    if not text:
        _emit_error(
            args,
            "usage",
            "message body cannot be empty (pass text, or '-' to read it from stdin)",
        )
        return 2

    # Read every --attach path up front, before any upload starts. Reading
    # them one at a time interleaved with uploads would let an unreadable
    # *later* path abort the command after earlier attachments had already
    # landed in the groups service with nothing ever referencing them —
    # orphaned uploads the command's own failure gave no sign of.
    attachments: list[tuple[bytes, str, str]] = []
    for path in args.attach or []:
        read = _read_attachment(path, args)
        if read is None:
            return 2
        attachments.append(read)

    def run(api: api_client.ApiClient) -> int:
        group_id = _need_group(api, args.room, args=args)
        topic_id = _need_topic(api, group_id, args.topic, args=args)
        attachment_ids: list[str] = []
        for content, filename, content_type in attachments:
            uploaded = api.upload_group_attachment(
                group_id, filename=filename, content=content, content_type=content_type
            )
            attachment_ids.append(uploaded["id"])
        created = api.post_group_message(
            group_id,
            topic_id,
            message=text,
            attachment_ids=attachment_ids or None,
            mentions=args.mention or None,
            parent_message_id=args.reply_to,
            idempotency_key=args.idempotency_key or str(uuid.uuid4()),
            on_behalf_of_person_id=args.on_behalf_of,
        )

        def human() -> None:
            render.info("Posted.")
            print(sanitize_line(str(created["id"])))

        if _json_mode(args):
            import dataclasses

            message = api_client.group_message_from_dict(created)
            _print_json({**dataclasses.asdict(message), "topic_id": topic_id})
        else:
            human()
        return 0

    return _with_room_api(args, run)


def cmd_messages_edit(args: argparse.Namespace) -> int:
    """``pepper messages edit --room R MESSAGE [TEXT... | -]`` (your own messages only).

    The text is required for now: there is no single-message read to open in
    ``$EDITOR`` (design §3.3), so a bare ``messages edit`` is a usage error.
    """
    parts = args.text or []
    body = sys.stdin.read().strip() if parts == ["-"] else " ".join(parts).strip()
    if not body:
        _emit_error(
            args,
            "usage",
            "the new message text cannot be empty (pass text, or '-' to read it from stdin)",
        )
        return 2
    return _room_act(
        args,
        lambda api, g: (api.edit_group_message(g, args.message, body=body), args.message),
        "Edited.",
    )


def cmd_messages_delete(args: argparse.Namespace) -> int:
    """``pepper messages delete --room R MESSAGE [--yes]``."""
    return _room_act(
        args,
        lambda api, g: (api.delete_group_message(g, args.message), args.message),
        "Deleted.",
        confirm=f"Delete message {args.message} for everyone in the room?",
    )


def cmd_messages_react(args: argparse.Namespace) -> int:
    """``pepper messages react --room R MESSAGE EMOJI``."""
    return _room_act(
        args,
        lambda api, g: (
            api.react_to_group_message(g, args.message, emoji=args.emoji),
            args.message,
        ),
        "Reacted.",
    )


def cmd_messages_unreact(args: argparse.Namespace) -> int:
    """``pepper messages unreact --room R MESSAGE EMOJI``."""
    return _room_act(
        args,
        lambda api, g: (
            api.clear_group_reaction(g, args.message, emoji=args.emoji),
            args.message,
        ),
        "Done.",
    )


def _need_person(
    api: api_client.ApiClient, group_id: str, value: str, *, args: argparse.Namespace
) -> str:
    """A member's person id from a name (roster match) or the id itself."""
    if _valid_uuid(value):
        return value
    roster = api.group_roster(group_id)
    match, found = _match_by_name(value, roster, name_of=lambda m: m.display_name)
    if match is not None:
        return match.person_id
    names = ", ".join(f"{m.display_name!r} ({m.person_id})" for m in found)
    _emit_error(
        args,
        "ambiguous" if found else "not_found",
        f"{value!r} matches {names or 'no member'} — run 'pepper members list'",
        soft_wrap=True,
    )
    raise _BadInputError


def cmd_members_list(args: argparse.Namespace) -> int:
    """``pepper members list --room R [--json]``: the ``@``-mention roster, Pepper first."""

    def run(api: api_client.ApiClient) -> int:
        roster = api.group_roster(_need_group(api, args.room, args=args))
        _emit_list(roster, args, lambda: render.render_group_roster(roster))
        return 0

    return _with_room_api(args, run)


def cmd_members_invite(args: argparse.Namespace) -> int:
    """``pepper members invite --room R HANDLE`` (open rooms only)."""

    def call(api: api_client.ApiClient, g: str) -> tuple[object, str]:
        return api.invite_to_group(g, handle=args.handle), g

    return _room_act(args, call, "Invited.")


def cmd_members_remove(args: argparse.Namespace) -> int:
    """``pepper members remove --room R PERSON [--yes]``."""

    def call(api: api_client.ApiClient, g: str) -> tuple[object, str]:
        person_id = _need_person(api, g, args.person, args=args)
        return api.remove_group_member(g, person_id), person_id

    return _room_act(args, call, "Removed.", confirm=f"Remove {args.person!r} from {args.room!r}?")


def cmd_members_edit(args: argparse.Namespace) -> int:
    """``pepper members edit --room R PERSON --role owner|member [--yes]``."""

    def call(api: api_client.ApiClient, g: str) -> tuple[object, str]:
        person_id = _need_person(api, g, args.person, args=args)
        return api.set_group_member_role(g, person_id, role=args.role), person_id

    return _room_act(
        args,
        call,
        f"Now {args.role}.",
        confirm=f"Make {args.person!r} {args.role} of {args.room!r}?",
    )


def _cal_name(calendar: dict[str, Any]) -> str:
    return calendar.get("summary") or calendar.get("name") or calendar["calendar_id"]


def _render_sharing(info: dict[str, Any]) -> None:
    """One line per calendar: its name and the level the room sees it at."""
    chosen = (info.get("calendar") or {}).get("calendars") or {}
    defaults = info.get("default_levels") or {}
    for c in info.get("available_calendars") or []:
        level = (
            chosen.get(c["calendar_id"]) or f"{defaults.get(c['calendar_id'], 'none')} (default)"
        )
        print(sanitize_line(f"{_cal_name(c)}  {level}"))


def cmd_sharing_show(args: argparse.Namespace) -> int:
    """``pepper sharing show --room R [--json]``."""

    def run(api: api_client.ApiClient) -> int:
        info = api.get_group_sharing(_need_group(api, args.room, args=args))
        _emit(info, args, lambda: _render_sharing(info))
        return 0

    return _with_room_api(args, run)


def cmd_sharing_edit(args: argparse.Namespace) -> int:
    """``pepper sharing edit --room R --set CALENDAR=LEVEL ... [--json]``.

    Changes the named calendars and re-sends the rest as they are, because the
    server replaces the whole record. ``LEVEL`` ``default`` drops the explicit choice.
    Each ``--set`` must name a calendar before its ``=``: an empty name would be a
    prefix of every calendar, so ``--set busy`` with one calendar would change it.
    """
    for item in args.set:
        name, sep, level = item.rpartition("=")
        if not sep or not name.strip() or not level:
            _emit_error(args, "usage", f"--set takes CALENDAR=LEVEL, got {item!r}")
            return 2

    def run(api: api_client.ApiClient) -> int:
        gid = _need_group(api, args.room, args=args)
        info = api.get_group_sharing(gid)
        cals = info.get("available_calendars") or []
        levels = info.get("available_levels") or []
        chosen = dict((info.get("calendar") or {}).get("calendars") or {})
        for item in args.set:
            name, _, level = item.rpartition("=")
            exact = [c for c in cals if c["calendar_id"] == name]
            match = exact[0] if exact else _match_by_name(name, cals, name_of=_cal_name)[0]
            if match is not None and level == "default":
                chosen.pop(match["calendar_id"], None)
                continue
            if match is None or level not in levels:
                _emit_error(
                    args,
                    "usage",
                    f"can't set {item!r}: levels are default, {', '.join(levels)}; "
                    f"calendars are {', '.join(_cal_name(c) for c in cals)}",
                    soft_wrap=True,
                )
                raise _BadInputError
            chosen[match["calendar_id"]] = level
        info = api.set_group_sharing(gid, calendars=chosen)
        _emit(info, args, lambda: _render_sharing(info))
        return 0

    return _with_room_api(args, run)


def cmd_apps_list(args: argparse.Namespace) -> int:
    """``pepper apps list --room R [--json]``: the launcher tiles, and what is installable."""

    def run(api: api_client.ApiClient) -> int:
        gid = _need_group(api, args.room, args=args)
        tiles, unavailable = api.group_app_launcher(gid)
        apps = api.get_group(gid).get("apps") or {}
        catalog = apps.get("catalog") or []
        catalog_unavailable = bool(apps.get("catalog_unavailable"))

        def human() -> None:
            for t in tiles:
                print(sanitize_line(f"{t.title}  {t.app_key}"))
            if unavailable:
                render.info("(installed apps unavailable right now)")
            elif not tiles:
                print("(no apps installed)")
            if catalog_unavailable:
                render.info("(installable apps unavailable right now)")
            elif catalog:
                keys = ", ".join(sanitize_line(a["app_key"]) for a in catalog)
                render.info("installable: " + keys)

        _emit_list(
            tiles,
            args,
            human,
            installed_unavailable=unavailable,
            catalog=catalog,
            catalog_unavailable=catalog_unavailable,
        )
        return 0

    return _with_room_api(args, run)


def _room_principal(api: api_client.ApiClient, args: argparse.Namespace) -> str | int | None:
    """``--room`` resolved strictly to a room id, ``None`` when not given, else the exit code."""
    if not args.room:
        return None
    return _resolve_room_strict(api, args, args.room)


def _print_pretty_json(obj: object) -> None:
    """Human view of an app's document or action result: indented JSON.

    ``json.dumps`` escapes control characters, so server text cannot reach the terminal raw.
    """
    print(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True))


def cmd_apps_show(args: argparse.Namespace) -> int:
    """``pepper apps show SLUG [--room R] [--json]``: the app's data document
    (``GET /apps/{slug}/data``), yours or the room's. ``--json`` answers ``{"rev", "data"}``."""

    def run(api: api_client.ApiClient) -> int:
        principal = _room_principal(api, args)
        if isinstance(principal, int):
            return principal
        doc = api.get_app_data(args.slug, principal=principal)

        def human() -> None:
            print(f"App {args.slug!r} data (rev {doc.get('rev')}):")
            _print_pretty_json(doc.get("data"))

        _emit(doc, args, human)
        return 0

    return _run_personal(args, run)


def _parse_params(args: argparse.Namespace) -> dict[str, str] | None:
    """``--param k=v`` pairs as a dict of strings, else a usage error (``None``).

    Values are always sent as strings: the CLI cannot know an action's parameter types, and
    guessing (``"007"`` to an integer) would corrupt ids and codes.
    """
    params: dict[str, str] = {}
    for raw in args.param or []:
        key, sep, value = raw.partition("=")
        if not sep or not key:
            _emit_error(args, "usage", f"--param expects KEY=VALUE, got {raw!r}")
            return None
        if key in params:
            _emit_error(args, "usage", f"--param {key!r} given twice")
            return None
        params[key] = value
    return params


def cmd_apps_run(args: argparse.Namespace) -> int:
    """``pepper apps run SLUG [ACTION] [--room R] [--param k=v]... [--json]``.

    With ACTION: ``POST /apps/{slug}/actions/{action}``, answering the server's
    ``{"result": ...}``. With ``--room`` the action writes to the room's data, which a human
    run says on stderr before sending.

    Without ACTION: the actions the server will name for the app. ``GET /apps`` lists only an
    app's ``owner_scoped_actions``, so the answer is partial and says so.
    """
    params = _parse_params(args)
    if params is None:
        return 2

    def run(api: api_client.ApiClient) -> int:
        if args.action is None:
            summary = next((a for a in api.list_apps() if a.get("slug") == args.slug), None)
            if summary is None:
                _emit_error(args, "not_found", f"no app {args.slug!r}")
                return 2
            names = [str(n) for n in summary.get("owner_scoped_actions") or []]

            def human() -> None:
                for name in names:
                    print(sanitize_line(name))
                render.info(
                    "(owner-scoped actions only: the server does not list the rest; "
                    "name an action to run it)"
                )

            _emit({"slug": args.slug, "owner_scoped_actions": names}, args, human)
            return 0
        principal = _room_principal(api, args)
        if isinstance(principal, int):
            return principal
        if principal is not None and not _json_mode(args):
            render.info(f"Running {args.action!r} writes to the room's {args.slug!r} data.")
        result = api.run_app_action(args.slug, args.action, params, principal=principal)

        def human() -> None:
            print(f"Ran {args.action!r} on {args.slug!r}.")
            _print_pretty_json(result.get("result", result))

        _emit(result, args, human)
        return 0

    return _run_personal(args, run)


def cmd_apps_install(args: argparse.Namespace) -> int:
    """``pepper apps install --room R APP_KEY``."""

    def call(api: api_client.ApiClient, g: str) -> tuple[object, str]:
        return api.install_group_app(g, app_key=args.app_key), args.app_key

    return _room_act(args, call, "Installed.")


def cmd_apps_uninstall(args: argparse.Namespace) -> int:
    """``pepper apps uninstall --room R APP [--yes]`` (``APP``: key, title or install id)."""

    def call(api: api_client.ApiClient, g: str) -> tuple[object, str]:
        apps = api.get_group(g).get("apps") or {}
        if apps.get("unavailable"):
            # Couldn't ask is not "not installed": a retry may find it.
            _emit_error(args, "server", "installed apps unavailable right now, try again")
            raise _UnavailableError
        installed = apps.get("installed") or []
        want = args.app.lower()
        hits = [a for a in installed if want in (a["install_id"], a["app_key"], a["title"].lower())]
        if len(hits) != 1:
            _emit_error(
                args,
                "ambiguous" if hits else "not_found",
                f"{args.app!r} isn't one installed app here — run 'pepper apps list'",
            )
            raise _BadInputError
        install_id = hits[0]["install_id"]
        return api.uninstall_group_app(g, install_id), install_id

    return _room_act(
        args, call, "Uninstalled.", confirm=f"Uninstall {args.app!r} from {args.room!r}?"
    )


def cmd_attachments_download(args: argparse.Namespace) -> int:
    """``pepper attachments download --room R ATTACHMENT --out PATH`` (``-`` for stdout).

    ``--json`` emits ``{"path", "bytes"}``; it cannot share stdout with the file bytes,
    so ``--out -`` is a usage error under it.
    """
    from pathlib import Path

    if args.out == "-" and _json_mode(args):
        _emit_error(args, "usage", "--out - writes the file to stdout; use a path with --json")
        return 2

    def run(api: api_client.ApiClient) -> int:
        content = api.get_group_attachment(_need_group(api, args.room, args=args), args.attachment)
        if args.out == "-":
            sys.stdout.buffer.write(content)
            render.info("Done.")
            return 0
        try:
            Path(args.out).write_bytes(content)
        except OSError as exc:
            _emit_error(args, "usage", f"couldn't write {args.out!r}: {exc.strerror or exc}")
            return 2
        _emit(
            {"path": args.out, "bytes": len(content)},
            args,
            lambda: render.info(f"Saved to {args.out}."),
        )
        return 0

    return _with_room_api(args, run)


#: First reconnect delay for ``rooms watch``, doubling to the cap. Reset by a
#: frame that shows the stream is healthy (an event, a cursor, a heartbeat), so
#: a healthy stream that drops once an hour reconnects at once rather than at
#: the cap. Not by ``: connected`` or an error frame: during a groups outage
#: every connection is accepted and then answered with a retryable error, and
#: resetting on either would retry once a second for as long as it lasts.
_WATCH_BACKOFF_START = 1.0
_WATCH_BACKOFF_CAP = 30.0

#: Consecutive failed attempts before ``rooms watch`` says anything. One
#: dropped connection that reconnects is routine (a deploy ends every open
#: stream) and loses nothing, since the cursor resumes the feed, so the first
#: ``_WATCH_QUIET_FAILURES - 1`` failures of a run are silent. Three tolerates
#: a drop followed by one more failed attempt (a proxy still switching over)
#: and starts reporting about three seconds into a real outage (attempts at
#: 0s, 1s and 3s). A consumer that reads any stderr line as "the feed is
#: unhealthy" therefore only hears about outages, not deploys.
_WATCH_QUIET_FAILURES = 3

#: The error slug the server sends when it is shutting down and ends the
#: stream cleanly. The replacement is already serving, so the first retry in a
#: run is immediate rather than after the backoff.
_WATCH_SERVER_RESTARTING = "server_restarting"

#: Statuses where reconnecting cannot help: a revoked or wrong credential, a
#: token that is not an agent's, a server without the stream. Anything else
#: non-2xx (5xx, 429, 408) is treated as an outage to wait out.
_WATCH_FATAL_STATUSES = frozenset({400, 401, 403, 404, 405, 410, 422})


def _kind_list(value: str) -> frozenset[str]:
    """Parse a comma-separated ``--kinds`` / ``--ignore-kinds`` value.

    Any non-empty string is accepted as a kind: the groups service adds kinds
    over time, and a CLI that only knew today's would refuse tomorrow's. An
    empty entry (``a,,b``, a trailing comma, an unset shell variable) is a
    caller bug, so it is an argument error rather than a filter that matches
    nothing.
    """
    kinds = [part.strip() for part in value.split(",")]
    if not all(kinds):
        msg = f"expected a comma-separated list of event kinds, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return frozenset(kinds)


def _watch_frame_shows_health(frame: api_client.FeedFrame) -> bool:
    """Whether ``frame`` proves the stream is past connecting and working.

    Not a ``scope`` frame: like ``: connected`` it is sent on every accepted
    connection, including each one an outage answers with a retryable error.
    """
    if frame.kind in ("error", "scope"):
        return False
    return not (frame.kind == "comment" and frame.payload.get("comment") == "connected")


#: Version of the one-line JSON a scoped watch keeps in its cursor file.
_SCOPED_CURSOR_VERSION = 1


@dataclass(frozen=True)
class _ScopedCursor:
    """A scoped watch's cursor file: the position, and the watch it belongs to.

    The feed's cursor is a position in the agent's whole feed, so a bare
    number says nothing about which room's events have been seen. Binding it
    to the server, agent, room and topic is what stops one scope's file from
    silently skipping another scope's events. ``room_id`` is the room the
    server confirmed it is serving (the survivor, when the requested room was
    merged into another).
    """

    server: str
    agent_person_id: str
    room_id: str
    topic_id: str | None
    cursor: int

    def to_line(self) -> str:
        import json

        return json.dumps(
            {
                "v": _SCOPED_CURSOR_VERSION,
                "server": self.server,
                "agent_person_id": self.agent_person_id,
                "room_id": self.room_id,
                "topic_id": self.topic_id,
                "cursor": self.cursor,
            },
            separators=(",", ":"),
        )


def _scoped_cursor_from_text(text: str) -> _ScopedCursor | None:
    """Parse a scoped cursor file's JSON line; ``None`` if it is not one."""
    import json

    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("v") != _SCOPED_CURSOR_VERSION:
        return None
    strings = [data.get(k) for k in ("server", "agent_person_id", "room_id")]
    topic, cursor = data.get("topic_id"), data.get("cursor")
    if not all(isinstance(v, str) and v for v in strings):
        return None
    if topic is not None and not (isinstance(topic, str) and topic):
        return None
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        return None
    return _ScopedCursor(
        server=data["server"],
        agent_person_id=data["agent_person_id"],
        room_id=data["room_id"],
        topic_id=topic,
        cursor=cursor,
    )


def _read_cursor_state(path: str) -> int | _ScopedCursor | None:
    """What ``path`` holds: a bare cursor (an unscoped watch's), a
    :class:`_ScopedCursor` (a scoped watch's), or ``None`` when the file does
    not exist.

    Raises ``ValueError`` for a file that exists but is neither: silently
    restarting from 0 would replay the whole feed.
    """
    from pathlib import Path

    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if text.isdecimal():
        return int(text)
    scoped = _scoped_cursor_from_text(text)
    if scoped is None:
        msg = f"cursor file {path!r} does not hold an integer cursor or a scoped watch's position"
        raise ValueError(msg)
    return scoped


def _write_cursor_file(path: str, cursor: int | _ScopedCursor) -> None:
    """Replace ``path`` with ``cursor`` atomically (write a sibling, fsync,
    rename), so a crash mid-write leaves the old cursor and never a torn one."""
    import tempfile
    from pathlib import Path

    line = cursor.to_line() if isinstance(cursor, _ScopedCursor) else str(cursor)
    target = Path(path)
    # mkstemp creates the sibling exclusively with an unpredictable name, so a
    # planted symlink at the temp path cannot redirect the write.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{line}\n")
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _watch_event_line(event: dict[str, Any], *, as_json: bool, cursor: int) -> str:
    """One printed line for one feed event."""
    if as_json:
        import json

        return json.dumps({**event, "cursor": cursor}, separators=(",", ":"))
    parts = [f"#{event.get('id')}", str(event.get("kind"))]
    if event.get("group_id"):
        parts.append(f"group={event['group_id']}")
    if event.get("person_id"):
        parts.append(f"person={event['person_id']}")
    payload = event.get("payload")
    if isinstance(payload, dict):
        for key in ("topic_id", "message_id"):
            if payload.get(key):
                parts.append(f"{key}={payload[key]}")
    # The plain form is read by people as often as by scripts; the JSON form
    # above is left exactly as the server sent it.
    return sanitize_line(" ".join(parts))


def cmd_rooms_watch(args: argparse.Namespace) -> int:
    """``pepper rooms watch [--since CURSOR] [--cursor-file PATH] [--json]
    [--kinds K,...] [--ignore-kinds K,...]``.

    Follows the agent event stream until interrupted. Each event is printed
    and flushed *before* the cursor file moves past it, so a crash between the
    two replays the event instead of losing it (the consumer de-dups by id).
    A ``cursor`` frame advances the cursor without printing, except under
    ``--json``, where it is printed so a pipe consumer can persist it too.

    ``--kinds`` / ``--ignore-kinds`` drop events from the *output* only (both
    modes). A dropped event is still consumed: the cursor, and the cursor
    file, move past it exactly as for a printed one, so a restart does not
    replay it. Nothing is printed for it, not even a cursor line, so a
    consumer woken per line is not woken by it.

    Exit codes: 0 on Ctrl-C, 1 on a refused token (401), a token that is not
    an agent's (403), a server that has no stream (404), or an ``error`` frame
    the server marks non-retryable (a suspended agent), 2 on bad arguments
    (including a cursor file a scoped watch wrote). Dropped connections, 5xx
    and ``retry: true`` error frames reconnect with
    exponential backoff from the last cursor seen. The first failures of a run
    are silent (a reconnect loses nothing); the ``Feed unavailable`` notice
    on stderr starts at the third consecutive failure and ends when the
    stream next shows a sign of life.

    ``--room`` (and ``--topic``) hand the run to :func:`_cmd_rooms_watch_scoped`.
    """
    if args.room is not None or args.topic is not None:
        return _cmd_rooms_watch_scoped(args)
    creds = _load_or_error(args)
    if creds is None:
        return 1
    cursor = args.since
    if cursor is not None and cursor < 0:
        _emit_error(args, "usage", "--since must be a non-negative cursor.")
        return 2
    if args.cursor_file:
        try:
            stored = _read_cursor_state(args.cursor_file)
        except (ValueError, OSError) as exc:
            # With --since the file is only being checked, as before.
            stored = None
            if cursor is None:
                _emit_error(args, "usage", str(exc))
                return 2
        if isinstance(stored, _ScopedCursor):
            # Even with --since: the run would overwrite another watch's position.
            _emit_error(
                args,
                "usage",
                f"The cursor file {args.cursor_file} belongs to a scoped watch (room "
                f"{stored.room_id}, topic {stored.topic_id or 'any'}), not the "
                "unscoped one requested. Nothing was streamed. Use a separate "
                "--cursor-file for each scope.",
                soft_wrap=True,
            )
            return 2
        if cursor is None:
            cursor = stored
    cursor = cursor or 0
    allowed: frozenset[str] | None = args.kinds
    ignored: frozenset[str] = args.ignore_kinds or frozenset()
    reconnecting = False
    delay = _WATCH_BACKOFF_START
    failures = 0
    try:
        with api_client.ApiClient(server_url=creds.server_url, token=creds.token) as api:
            while True:
                problem: str | None = None
                try:
                    frames = api.watch_agent_events(
                        since=cursor, last_event_id=cursor if reconnecting else None
                    )
                    for frame in frames:
                        if _watch_frame_shows_health(frame):
                            delay = _WATCH_BACKOFF_START
                            failures = 0
                        if frame.kind == "error":
                            if frame.payload.get("retry") is False:
                                _emit_error(
                                    args,
                                    "server",
                                    "The server closed the stream and will not resume it: "
                                    f"{frame.payload.get('error', 'error')}.",
                                )
                                return 1
                            problem = str(frame.payload.get("error", "error"))
                            break
                        if frame.id is not None:
                            new_cursor = frame.id
                        elif frame.kind == "cursor" and isinstance(
                            frame.payload.get("cursor"), int
                        ):
                            new_cursor = frame.payload["cursor"]
                        else:
                            continue
                        if frame.kind == "cursor":
                            if args.json:
                                _emit_line(_watch_cursor_line(new_cursor))
                        elif (allowed is None or frame.kind in allowed) and (
                            frame.kind not in ignored
                        ):
                            _emit_line(
                                _watch_event_line(
                                    frame.payload, as_json=args.json, cursor=new_cursor
                                )
                            )
                        cursor = max(cursor, new_cursor)
                        if args.cursor_file:
                            _write_cursor_file(args.cursor_file, cursor)
                except api_client.ApiError as exc:
                    if exc.status in _WATCH_FATAL_STATUSES:
                        return _report_api_error(exc, args)
                    problem = f"HTTP {exc.status}"
                except httpx.RequestError as exc:
                    problem = type(exc).__name__
                pause = delay
                if problem is not None:
                    failures += 1
                    # A server that said it is restarting has a successor up
                    # already: retry at once, once. A second consecutive
                    # failure backs off as usual, so a fleet that is all
                    # restarting is not hammered.
                    immediate = problem == _WATCH_SERVER_RESTARTING and failures == 1
                    pause = 0.0 if immediate else delay
                    if failures >= _WATCH_QUIET_FAILURES:
                        # stderr only: stdout stays one line per event for the pipe.
                        render.error(f"Feed unavailable ({problem}); retrying in {pause:g}s.")
                reconnecting = True
                if pause:
                    time.sleep(pause)
                    delay = min(delay * 2, _WATCH_BACKOFF_CAP)
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        return _watch_reader_gone()
    except OSError as exc:
        _emit_error(args, "server", f"Couldn't write the cursor file: {exc}")
        return 1


def _watch_reader_gone() -> int:
    """The reader went away (``| head -1``): nothing to report, and the
    cursor was not advanced past the line that failed to print.

    Point stdout at /dev/null so the interpreter's own flush at exit does not
    raise a second BrokenPipeError (Python's documented SIGPIPE recipe).
    """
    with contextlib.suppress(OSError, ValueError):
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    return 0


#: The capability ``GET /api/v1/groups`` lists when the server has the room-
#: and topic-scoped streams. Without it a scoped watch refuses to start: the
#: only other stream is the roster-wide one, and following that would print
#: events from rooms the caller asked not to see.
_WATCH_SCOPE_FEATURE = "watch_scope"

#: The error slug a scoped stream ends with when it cannot replay from the
#: position asked for without a gap. Its frame carries ``since`` and ``floor``.
_WATCH_HISTORY_EXPIRED = "history_expired"

#: What the scoped stream's other terminal slugs mean, for the stderr line.
_WATCH_SCOPED_ENDINGS = {
    "access_revoked": "you are no longer a member of this room",
    "room_deleted": "the room was deleted",
    "room_superseded": (
        "the room was merged into another room; run 'pepper rooms list' to find "
        "it and watch that room instead"
    ),
}


class _WatchStopError(Exception):
    """End a scoped watch with ``code``; the reason is already on stderr."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _canonical_id(value: str) -> str:
    """``value`` in the UUID spelling the server echoes (lowercase, hyphenated)."""
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return value


def _same_id(value: object, expected: str | None) -> bool:
    """Whether ``value`` names ``expected``: equal as UUIDs (so letter case does
    not matter), else equal as strings. ``None`` matches only ``None``."""
    if value is None or expected is None:
        return value is None and expected is None
    if not isinstance(value, str):
        return False
    return _canonical_id(value) == _canonical_id(expected)


def _scope_words(room_id: str, topic_id: str | None, agent: str | None = None) -> str:
    words = f"room {room_id}, topic {topic_id or 'any'}"
    return f"agent {agent}, {words}" if agent else words


def _scoped_watch_target(
    api: api_client.ApiClient, room: str, topic: str | None, args: argparse.Namespace
) -> tuple[str, str | None]:
    """Check the server can scope a watch, then resolve ``--room``/``--topic``.

    The capability check comes first and fails closed: a server that does not
    list ``watch_scope`` is never sent a scoped request, and nothing falls back
    to the roster-wide stream. A topic is resolved only when one was named,
    since "no topic" means the whole room, not the room's default topic.
    """
    listing = _group_list_or_error(api)
    if _WATCH_SCOPE_FEATURE not in listing.features:
        _emit_error(
            args,
            "server",
            "This server does not support a scoped watch (--room/--topic). Nothing was streamed.",
        )
        raise _WatchStopError(1)
    room_id = _resolve_group(api, room, rooms=listing, args=args)
    if room_id is None:
        raise _BadInputError
    room_id = _canonical_id(room_id)
    topic_id = None
    if topic is not None:
        topic_id = _resolve_topic(api, room_id, topic, args=args)
        if topic_id is None:
            raise _BadInputError
        topic_id = _canonical_id(topic_id)
    return room_id, topic_id


def _confirm_scope(
    frame: api_client.FeedFrame, room_id: str, topic_id: str | None, args: argparse.Namespace
) -> api_client.WatchScope:
    """The scope a connection's first frame names, if it is the one asked for.

    Anything else (another kind of frame first, an unreadable scope frame, a
    scope for another room or topic) ends the run: the events that follow
    cannot be trusted to be the ones asked for.
    """
    scope = api_client.watch_scope_from_frame(frame)
    if (
        scope is None
        or not _same_id(scope.requested_room_id, room_id)
        or not _same_id(scope.topic_id, topic_id)
    ):
        _emit_error(
            args,
            "server",
            "The server did not confirm the requested scope "
            f"({_scope_words(room_id, topic_id)}); stopping. Nothing from this "
            "connection was printed.",
            soft_wrap=True,
        )
        raise _WatchStopError(1)
    return scope


def _same_watch(scope: api_client.WatchScope, other: api_client.WatchScope) -> bool:
    return (
        _same_id(scope.room_id, other.room_id)
        and _same_id(scope.topic_id, other.topic_id)
        and _same_id(scope.agent_person_id, other.agent_person_id)
    )


def _check_cursor_binding(
    path: str | None,
    stored: _ScopedCursor | None,
    scope: api_client.WatchScope,
    args: argparse.Namespace,
) -> None:
    """Refuse a cursor file another scope wrote. Its position is in the agent's
    whole feed, so using it here would skip this scope's events silently."""
    if path is None or stored is None:
        return
    if (
        _same_id(stored.room_id, scope.room_id)
        and _same_id(stored.topic_id, scope.topic_id)
        and _same_id(stored.agent_person_id, scope.agent_person_id)
    ):
        return
    theirs = _scope_words(stored.room_id, stored.topic_id, stored.agent_person_id)
    ours = _scope_words(scope.room_id, scope.topic_id, scope.agent_person_id)
    _emit_error(
        args,
        "usage",
        f"The cursor file {path} belongs to a different watch ({theirs}) than the one "
        f"requested ({ours}). Nothing was streamed. Use a separate --cursor-file for each "
        f"scope; to carry the position over, pass --since {stored.cursor} with a new file.",
        soft_wrap=True,
    )
    raise _WatchStopError(2)


def _event_in_scope(event: dict[str, Any], scope: api_client.WatchScope) -> bool:
    """Whether a scoped stream's event really is in ``scope``: the server's own
    admission rule, checked again on this side so a server bug cannot widen a
    watch the caller narrowed."""
    if not _same_id(event.get("room_id"), scope.room_id):
        return False
    if not _same_id(event.get("group_id"), scope.service_group_id):
        return False
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    if "group_id" in payload and not _same_id(payload["group_id"], scope.service_group_id):
        return False
    return scope.topic_id is None or _same_id(payload.get("topic_id"), scope.topic_id)


def _scoped_watch_problem(
    frame: api_client.FeedFrame, room_id: str, topic_id: str | None, args: argparse.Namespace
) -> str:
    """The retryable problem an ``error`` frame reports, or end the run.

    ``history_expired`` exits 3 and says where the gap is; the cursor file is
    left where it was, so nothing claims the gap was read. ``room_id`` is the
    room to read the gap from: the survivor, once the server has said the
    room asked for was merged into it.
    """
    payload = frame.payload
    slug = str(payload.get("error", "error"))
    if slug == _WATCH_HISTORY_EXPIRED:
        floor, since = payload.get("floor"), payload.get("since")
        read = f"pepper messages list --room {room_id} --topic {topic_id or 'TOPIC'}"
        _emit_error(
            args,
            "gap",
            f"Replay history before position {floor} is no longer available; events "
            f"between {since} and {floor} in this scope were not delivered. Read the gap "
            f"with '{read}'{'' if topic_id else ' for each topic'}, then restart with "
            f"--since {floor}.",
            soft_wrap=True,
        )
        raise _WatchStopError(3)
    if payload.get("retry") is False:
        ending = _WATCH_SCOPED_ENDINGS.get(slug)
        code = {"access_revoked": "forbidden", "room_deleted": "not_found"}.get(slug, "server")
        if ending is not None:
            _emit_error(args, code, f"The server ended the stream: {ending}.")
        else:
            _emit_error(args, code, f"The server closed the stream and will not resume it: {slug}.")
        raise _WatchStopError(1)
    return slug


def _cmd_rooms_watch_scoped(args: argparse.Namespace) -> int:
    """``pepper rooms watch --room ROOM [--topic TOPIC] ...``: the stream
    narrowed by the server to one room, or one topic in it.

    Fails closed throughout. A server without the capability, a connection
    whose first frame does not confirm the scope asked for, and an event
    outside the scope all stop the run (exit 1) without printing, and none
    of them fall back to the roster-wide stream. A reconnect re-checks the
    scope; one that names another room, topic or agent than the run started
    with stops it too. When the room asked for was merged into another, the
    server serves the survivor and says so; that is reported once on stderr.

    The cursor file holds one JSON line binding the position to the server,
    agent, room and topic. A file another watch wrote (another scope, or the
    unscoped watch's bare number) is refused with exit 2, even with --since.

    Exit codes beyond the unscoped watch's: 2 for a room or topic that does
    not resolve, a 404 (no such room or topic, or not a member), or
    ``--topic`` without ``--room``; 3 when the server can no longer replay
    from the position asked for (``history_expired``).
    """
    if args.room is None:
        _emit_error(args, "usage", "--topic needs --room: a topic is named within one room.")
        return 2
    creds = _load_or_error(args)
    if creds is None:
        return 1
    if args.since is not None and args.since < 0:
        _emit_error(args, "usage", "--since must be a non-negative cursor.")
        return 2
    stored: _ScopedCursor | None = None
    if args.cursor_file:
        try:
            state = _read_cursor_state(args.cursor_file)
        except (ValueError, OSError) as exc:
            _emit_error(args, "usage", str(exc))
            return 2
        if isinstance(state, int):
            _emit_error(
                args,
                "usage",
                f"The cursor file {args.cursor_file} holds the unscoped watch's cursor, not a "
                "scoped one. Nothing was streamed. Use a separate --cursor-file for each "
                f"scope; to carry the position over, pass --since {state} with a new file.",
                soft_wrap=True,
            )
            return 2
        if state is not None and state.server != creds.server_url:
            _emit_error(
                args,
                "usage",
                f"The cursor file {args.cursor_file} belongs to a watch on another server "
                f"({state.server}). Nothing was streamed. Use a separate --cursor-file for "
                "each server.",
                soft_wrap=True,
            )
            return 2
        stored = state
    # --since moves the start, never the binding checked against the scope.
    cursor = stored.cursor if stored is not None else 0
    if args.since is not None:
        cursor = args.since
    try:
        with api_client.ApiClient(server_url=creds.server_url, token=creds.token) as api:
            room_id, topic_id = _scoped_watch_target(api, args.room, args.topic, args)
            return _follow_scoped_watch(
                api,
                args,
                server=creds.server_url,
                room_id=room_id,
                topic_id=topic_id,
                stored=stored,
                cursor=cursor,
            )
    except _WatchStopError as stop:
        return stop.code
    except _BadInputError:
        return 2
    except _GroupsDisabledError:
        _emit_error(args, "forbidden", "Rooms are not enabled for your account.")
        return 1
    except api_client.ApiError as exc:
        return _report_room_error(exc, args)
    except httpx.RequestError as exc:
        _emit_error(args, "network", f"Couldn't reach {creds.server_url}: {exc}")
        return 1
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        return _watch_reader_gone()
    except OSError as exc:
        _emit_error(args, "server", f"Couldn't write the cursor file: {exc}")
        return 1


def _follow_scoped_watch(
    api: api_client.ApiClient,
    args: argparse.Namespace,
    *,
    server: str,
    room_id: str,
    topic_id: str | None,
    stored: _ScopedCursor | None,
    cursor: int,
) -> int:
    """The scoped watch's connect / read / reconnect loop. Same output, cursor
    and backoff rules as the unscoped loop in :func:`cmd_rooms_watch`; it
    returns only by raising (Ctrl-C, or :class:`_WatchStopError`) or with the
    exit code of a status reconnecting cannot fix."""
    allowed: frozenset[str] | None = args.kinds
    ignored: frozenset[str] = args.ignore_kinds or frozenset()
    bound: api_client.WatchScope | None = None
    reconnecting = False
    delay = _WATCH_BACKOFF_START
    failures = 0
    while True:
        problem: str | None = None
        try:
            frames = api.watch_scoped_events(
                room_id,
                topic_id=topic_id,
                since=cursor,
                last_event_id=cursor if reconnecting else None,
            )
            with contextlib.closing(frames):
                scope: api_client.WatchScope | None = None
                for frame in frames:
                    if _watch_frame_shows_health(frame):
                        delay = _WATCH_BACKOFF_START
                        failures = 0
                    if frame.kind == "error":
                        served = scope.room_id if scope is not None else room_id
                        problem = _scoped_watch_problem(frame, served, topic_id, args)
                        break
                    if frame.kind == "comment":
                        continue
                    if scope is None or frame.kind == "scope":
                        seen = _confirm_scope(frame, room_id, topic_id, args)
                        if bound is None:
                            _check_cursor_binding(args.cursor_file, stored, seen, args)
                            bound = seen
                            if not _same_id(seen.room_id, seen.requested_room_id):
                                render.info(
                                    f"Room {seen.requested_room_id} was merged into room "
                                    f"{seen.room_id}; following {seen.room_id}."
                                )
                        elif not _same_watch(seen, bound):
                            _emit_error(
                                args,
                                "server",
                                "The server is now serving another scope "
                                f"({_scope_words(seen.room_id, seen.topic_id)}) than this "
                                f"run started with ({_scope_words(bound.room_id, bound.topic_id)})"
                                "; stopping. Re-run against the room you mean to follow.",
                                soft_wrap=True,
                            )
                            raise _WatchStopError(1)
                        scope = seen
                        continue
                    if frame.id is not None:
                        new_cursor = frame.id
                    elif frame.kind == "cursor" and isinstance(frame.payload.get("cursor"), int):
                        new_cursor = frame.payload["cursor"]
                    else:
                        continue
                    if frame.kind == "cursor":
                        if args.json:
                            _emit_line(_watch_cursor_line(new_cursor))
                    else:
                        if not _event_in_scope(frame.payload, scope):
                            _emit_error(
                                args,
                                "server",
                                f"The server sent event {new_cursor} ({frame.kind}) from "
                                "outside the requested scope "
                                f"({_scope_words(scope.room_id, scope.topic_id)}); stopping. "
                                "It was not printed and the cursor did not move past it.",
                                soft_wrap=True,
                            )
                            raise _WatchStopError(1)
                        if (allowed is None or frame.kind in allowed) and (
                            frame.kind not in ignored
                        ):
                            _emit_line(
                                _watch_event_line(
                                    frame.payload, as_json=args.json, cursor=new_cursor
                                )
                            )
                    cursor = max(cursor, new_cursor)
                    if args.cursor_file:
                        _write_cursor_file(
                            args.cursor_file,
                            _ScopedCursor(
                                server=server,
                                agent_person_id=scope.agent_person_id,
                                room_id=scope.room_id,
                                topic_id=scope.topic_id,
                                cursor=cursor,
                            ),
                        )
        except api_client.ApiError as exc:
            if exc.status in _WATCH_FATAL_STATUSES:
                return _report_room_error(exc, args)
            problem = f"HTTP {exc.status}"
        except httpx.RequestError as exc:
            problem = type(exc).__name__
        pause = delay
        if problem is not None:
            failures += 1
            immediate = problem == _WATCH_SERVER_RESTARTING and failures == 1
            pause = 0.0 if immediate else delay
            if failures >= _WATCH_QUIET_FAILURES:
                render.error(f"Feed unavailable ({problem}); retrying in {pause:g}s.")
        reconnecting = True
        if pause:
            time.sleep(pause)
            delay = min(delay * 2, _WATCH_BACKOFF_CAP)


def _watch_cursor_line(cursor: int) -> str:
    import json

    return json.dumps({"kind": "cursor", "cursor": cursor}, separators=(",", ":"))


def _emit_line(line: str) -> None:
    """Print one line and flush: a watcher is read through a pipe, and a block-
    buffered line is an event the consumer sees a minute late."""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _handle_of(
    api: api_client.ApiClient, person: str, *, args: argparse.Namespace | None = None
) -> str:
    """An email or phone as given, else the single Contacts match's handle."""
    if "@" in person or re.fullmatch(r"\+?[\d\s().-]{7,}", person):
        return person
    matches = [c for c in api.search_contacts(person) if c.get("handle")]
    match, found = _match_by_name(person, matches, name_of=lambda c: c.get("name") or "")
    if match is None:
        listing = ", ".join(f"{c.get('name')} <{c['handle']}>" for c in found)
        _emit_error(
            args,
            "ambiguous" if found else "not_found",
            f"{person!r} matches {listing or 'no contact'} — give an email or phone",
        )
        raise _BadInputError
    return match["handle"]


def _report_notes_api_error(
    exc: api_client.ApiError, args: argparse.Namespace | None = None
) -> int:
    """Map an :class:`ApiError` from a notes/artifacts call to an exit code.

    Two statuses get specific wording
    distinct from :func:`_report_api_error`'s generic handling:

    - 404 — missing room, missing note, and "not a member" are one answer
      server-side (``resolve_owner_principal``'s own doc: a prober should
      learn nothing about which ids exist), so the CLI states that
      non-distinction rather than guessing which one it was. Exit 2 (bad
      input): the caller named something that doesn't resolve for them.
    - 503 — the membership seam itself is unreachable, not a bad argument.
      Exit 1, with a message inviting a retry rather than a rename.

    Everything else (401 included) falls back to :func:`_report_api_error`.
    """
    if exc.status == 404:
        _emit_error(args, "not_found", "no such room or note, or you are not a member", status=404)
        return 2
    if exc.status == 503:
        _emit_error(args, "server", "membership check unavailable, try again", status=503)
        return 1
    return _report_api_error(exc, args)


def _resolve_note(
    api: api_client.ApiClient,
    *,
    room_principal: str | None,
    value: str,
    args: argparse.Namespace | None = None,
) -> str | None:
    """Resolve ``<note>`` to its id, scoped to ``room_principal`` (``None``
    for the caller's own notes).

    Same name-or-UUID contract as :func:`_resolve_group`/:func:`_resolve_topic`
    — a UUID passes straight through untouched (the server 404s a wrong one,
    routed through :func:`_report_notes_api_error` by the caller). Otherwise
    resolved in up to two listings, each scoped to ``room_principal`` and
    restricted to ``_NOTES_KIND``. The route has no offset or cursor, so a
    lookup gets one page (:data:`_NOTE_RESOLVE_LIMIT`) per listing, and each
    is narrowed server-side so that page holds only real candidates — never
    notes whose bodies merely mention the text, as a ``q`` filter would:

    1. ``name=value`` — the whole name, case-insensitively. An exact name
       wins outright (as in :func:`_match_by_name`), so it resolves however
       many longer names share its prefix.
    2. Only when nothing is named exactly ``value``: ``name_prefix=value``,
       for the unique-prefix promise in ``_NOTE_ARG_HELP``.

    Either page coming back truncated is refused rather than matched: the
    unseen rows could hold a same-named duplicate or a second prefix match,
    so any pick inside the page would be a guess. The error points at the
    id, which always resolves.

    :func:`_match_by_name` already treats more than one case-insensitive
    exact match as ambiguous rather than picking the first — load-bearing
    here, since artifact names are deliberately not unique
    (the server's name lookup documents this): two notes named
    "Runbook" must not silently resolve to whichever the server listed
    first.
    """
    if _valid_uuid(value):
        return value
    result = api.list_artifacts(
        principal=room_principal, kind=_NOTES_KIND, name=value, limit=_NOTE_RESOLVE_LIMIT
    )
    if not result.truncated and not result.items:
        result = api.list_artifacts(
            principal=room_principal,
            kind=_NOTES_KIND,
            name_prefix=value,
            limit=_NOTE_RESOLVE_LIMIT,
        )
    if result.truncated:
        _emit_error(
            args,
            "ambiguous",
            f"too many notes match {value!r} to resolve it by name; "
            "use the note's id (pepper notes search shows it)",
            soft_wrap=True,
        )
        return None
    match, candidates = _match_by_name(value, result.items, name_of=lambda n: n.name)
    if match is not None:
        return match.id
    if candidates:
        listing = ", ".join(f"{n.name!r} ({n.id})" for n in candidates)
        _emit_error(
            args, "ambiguous", f"{value!r} matches more than one note: {listing}", soft_wrap=True
        )
    else:
        where = "in that room" if room_principal else "of yours"
        _emit_error(args, "not_found", f"no note named {value!r} {where}")
    return None


def _note_not_found(args: argparse.Namespace | None) -> None:
    _emit_error(args, "not_found", "no such room or note, or you are not a member", status=404)


def _notes_listing(args: argparse.Namespace, *, q: str | None = None) -> int:
    """Shared body of ``notes list`` / ``notes search``: one J3 envelope, unpaged."""

    def run(api: api_client.ApiClient) -> int:
        room_principal = _opt_group(api, args.room, args=args)
        result = api.list_artifacts(
            principal=room_principal, kind=_NOTES_KIND, q=q, label=args.label
        )
        _emit_list(
            result.items,
            args,
            lambda: render.render_note_list(result),
            truncated=result.truncated,
        )
        return 0

    return _with_api(run, _report_notes_api_error, args)


def cmd_notes_list(args: argparse.Namespace) -> int:
    """``pepper notes list [--room ROOM] [--label LABEL] [--json]``."""
    return _notes_listing(args)


def cmd_notes_search(args: argparse.Namespace) -> int:
    """``pepper notes search QUERY [--room ROOM] [--label LABEL] [--json]``."""
    query = " ".join(args.query).strip()
    if not query:
        _emit_error(args, "usage", "search text cannot be empty")
        return 2
    return _notes_listing(args, q=query)


def cmd_notes_show(args: argparse.Namespace) -> int:
    """``pepper notes show <note> [--room ROOM] [--json]``.

    Non-JSON output is the note's raw markdown body on stdout, and nothing
    else — no name/labels header — so ``show > f.md`` captures exactly the
    stored body (:func:`render.render_markdown`'s byte-exactness). ``--json``
    answers the full detail instead: name, labels, ``updated_at``, ``etag``,
    ``shared``, and the body.

    The detail read itself is viewer-scoped server-side and takes no
    ``--room``-derived principal (:meth:`api_client.ApiClient.get_artifact_detail`'s
    own docstring) — ``--room`` here only scopes *name* resolution to that
    room's notes; a bare UUID resolves the same regardless of ``--room``.
    """

    def run(api: api_client.ApiClient) -> int:
        room_principal = _opt_group(api, args.room, args=args)
        note_id = _resolve_note(api, room_principal=room_principal, value=args.note, args=args)
        if note_id is None:
            return 2
        detail = api.get_artifact_detail(note_id)
        if detail.kind != _NOTES_KIND:
            # A raw UUID that resolves to a real artifact of some other
            # kind (an image, a link) is not a note as far as this
            # surface is concerned — the same "doesn't exist for you"
            # answer a 404 would give, not a confusing partial read.
            _note_not_found(args)
            return 2
        _emit(detail, args, lambda: render.render_markdown(detail.body or ""))
        return 0

    return _with_api(run, _report_notes_api_error, args)


def _stdin_is_tty() -> bool:
    """Wraps ``sys.stdin.isatty()`` so the conflict prompt's branch is
    monkeypatchable directly — pytest's own stdin capture isn't a TTY,
    same reasoning as ``window_title._stdout_is_tty``."""
    return sys.stdin.isatty()


def _read_file_or_stdin(path: str, args: argparse.Namespace | None = None) -> str | None:
    """Read a note body from ``path``, or stdin when ``path`` is ``'-'``.

    Mirrors :func:`_read_attachment`'s "read local input before any network
    call" shape, but returns decoded text — a note body is never binary —
    and no normalization: ``render.render_markdown``'s byte-exact promise
    for ``--file``/stdin means whatever is read here is exactly what gets
    sent. Prints its own error and returns ``None`` on failure — an
    unreadable path or input that isn't valid UTF-8 (``UnicodeDecodeError``
    is a ``ValueError``, not an ``OSError``) — so the caller can bail with
    exit code 2 before touching the API.
    """
    from pathlib import Path

    label = "stdin" if path == "-" else repr(path)
    try:
        if path == "-":
            return sys.stdin.read()
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _emit_error(args, "usage", f"couldn't read {label}: {exc}")
        return None


def _texts_match(original: str, edited: str) -> bool:
    """True when ``edited`` is byte-identical to ``original``, or
    ``original`` with exactly one trailing newline appended.

    The one tolerance anywhere on the write path, and applied to
    ``$EDITOR`` output only (``--file``/stdin compare with ``==``) — every
    other case keeps
    ``render.render_markdown``'s byte-exact contract. vi/vim conventionally
    append a trailing newline on save, so a straight ``==`` would treat
    "opened the editor and changed nothing" as an edit and send a body
    differing from the stored one by whitespace alone, creating a spurious
    version row for a no-op ("send nothing if the text is unchanged").
    """
    return edited == original or edited == original + "\n"


def _note_slug(name: str) -> str:
    """Filesystem-safe stem for the conflict-save filename: lowercase,
    every run of non-alphanumerics collapsed to one hyphen, trimmed. Good
    enough for a scratch file whose exact path the CLI prints back to the
    user — not a general-purpose slugify utility."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "note"


def _conflict_fallback_dir() -> str:
    r"""Where a conflict copy goes when the working directory won't take it.

    POSIX: the system temp directory; the ``0o600`` mode on the file is what
    keeps it private there. Windows: a ``pepper`` folder under
    ``%LOCALAPPDATA%`` (or ``~\AppData\Local`` when that is unset or not
    absolute), created on demand, never ``%TEMP%``, which can be pointed at a
    shared folder such as ``C:\Temp``. Windows ignores the mode bits, so the
    file's protection is the folder's inherited ACL; the profile's default
    ACL limits access to the user, SYSTEM and Administrators. No ACL is set
    here, so a profile whose ACLs were loosened is not compensated for.
    """
    import tempfile
    from pathlib import Path

    if sys.platform != "win32":
        return tempfile.gettempdir()
    local = os.environ.get("LOCALAPPDATA", "")
    base = Path(local) if local and Path(local).is_absolute() else Path.home() / "AppData" / "Local"
    directory = base / "pepper"
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory)


def _conflict_dir_override() -> str | None:
    """The directory named by ``PEPPER_CONFLICT_DIR``, or ``None`` when it is unset or blank."""
    value = os.environ.get("PEPPER_CONFLICT_DIR", "").strip()
    return value or None


def _save_conflict_copy(name: str, etag: str | None, text: str) -> tuple[str | None, str | None]:
    """Write ``text`` to ``./<note-slug>.<short-etag>.mine.md``
    (or into ``$PEPPER_CONFLICT_DIR``, created if missing, when that is set;
    the returned path is then absolute) and return ``(path, None)``. ``etag``
    is the server's *current* one as of the conflict, not the stale one the
    CLI sent.

    Never overwrites: two attempts against the same server version (a
    retry in this run, or a second ``pepper notes edit`` later) can hold
    different unsaved text, so the file is created exclusively and an
    existing one bumps a counter (``.mine.1.md``, ``.mine.2.md``, …). If
    the working directory can't be written at all, the copy goes to
    :func:`_conflict_fallback_dir` (the system temp directory on POSIX, a
    per-user folder under ``%LOCALAPPDATA%`` on Windows) instead — likewise
    if the file opens but the write fails (a full disk). Returns ``(None, why)`` only when neither
    can be written, and prints nothing: the caller reports ``why`` in its
    own output shape (a text line, or folded into the one J8 line under
    ``--json``). The human caller's diff still shows the user's lines on
    stderr in that case.

    Created owner-only (``0o600``) whatever the umask on POSIX, since a
    note — a room's especially — is private text and the temp directory is
    shared with every local user. On Windows the mode only toggles the
    read-only bit and no ACL is set: the copy is as private as the folder
    it lands in. The working directory is the user's own choice and the
    fallback is a per-user folder under the profile (see
    :func:`_conflict_fallback_dir`), both of which inherit their ACL from
    the user's profile or the directory; a cwd on a shared folder is
    therefore as readable as that folder. ``O_EXCL`` refuses an existing path,
    symlinks included, so a planted link in the temp directory can't
    redirect the write; ``O_NOFOLLOW`` says so twice where the platform
    has it.
    """
    import itertools
    from pathlib import Path

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)

    stem = f"{_note_slug(name)}.{(etag or 'unknown')[:8]}.mine"
    last_error: OSError | RuntimeError | None = None
    for fallback in (False, True):
        try:
            if fallback:
                directory = Path(_conflict_fallback_dir())
            elif (override := _conflict_dir_override()) is not None:
                directory = Path(override).expanduser().absolute()
                directory.mkdir(parents=True, exist_ok=True)
            else:
                directory = Path()
        except (OSError, RuntimeError) as exc:
            # RuntimeError: ``expanduser`` on a ``~user`` that doesn't
            # exist. Either way the override is unusable; try the fallback.
            last_error = exc
            continue
        for n in itertools.count():
            path = directory / (f"{stem}.md" if n == 0 else f"{stem}.{n}.md")
            try:
                fd = os.open(path, flags, 0o600)
            except FileExistsError:
                continue
            except OSError as exc:
                last_error = exc
                break
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
            except OSError as exc:
                # Opened but couldn't be filled (a full disk, say): drop the
                # partial file this call just created — nothing else can own
                # it, O_EXCL made it ours — and try the next directory, so
                # the caller still gets its diff and recovery prompt.
                last_error = exc
                with contextlib.suppress(OSError):
                    path.unlink()
                break
            return str(path), None
    return None, f"couldn't save your text to a file: {last_error}"


def _print_note_diff(current_body: str, mine: str) -> None:
    r"""Unified diff from the server's current body to the user's text,
    written straight to stderr rather than through
    :func:`render.error` — a note's body can hold literal ``[``/``]``
    characters that Rich's markup parser would misread.

    The diff is taken over the raw bodies, so a change that is only an
    escape sequence or a carriage return still shows up before the user
    decides whether to overwrite; each diff line is then printed with its
    control and invisible characters spelled out (``\x1b``) rather than
    sent to the terminal live. Lines are split on ``\n`` alone so a stray
    control character cannot itself end a line."""

    def lines(text: str) -> list[str]:
        return re.findall(r"[^\n]*\n|[^\n]+$", text)

    diff = difflib.unified_diff(
        lines(current_body), lines(mine), fromfile="server (current)", tofile="yours"
    )
    sys.stderr.writelines(make_visible(line) for line in diff)


def _notes_editor_refused(args: argparse.Namespace) -> bool:
    """True (after the J8 usage error) when JSON mode would need to open ``$EDITOR`` (J2)."""
    if _json_mode(args):
        _emit_error(args, "usage", "JSON mode does not open an editor; pass --file PATH (or -)")
        return True
    return False


def cmd_notes_create(args: argparse.Namespace) -> int:
    """``pepper notes create <name> [--room ROOM] [--file PATH|-] [--json]``.

    Body comes from ``$EDITOR`` by default, or from ``--file`` (a path, or
    ``-`` for stdin) read up front, before any network call — same shape as
    ``messages post --attach``. ``--room`` is resolved before the editor
    opens, so a bad room name fails before the user writes anything. Under
    ``--json`` there is no editor (J2): without ``--file`` it is a usage
    error. ``--json`` answers the created note.

    Exit codes: 0 ok · 1 auth/API/transport/membership-seam-down · 2 bad
    input (empty body, aborted editor, unreadable ``--file``).
    """
    file_body: str | None = None
    if args.file is not None:
        file_body = _read_file_or_stdin(args.file, args)
        if file_body is None:
            return 2
    elif _notes_editor_refused(args):
        return 2

    def run(api: api_client.ApiClient) -> int:
        room_principal = _opt_group(api, args.room, args=args)
        if file_body is not None:
            body = file_body
        else:
            edited = _open_in_editor("")
            if edited is None:
                return 2
            body = edited
        if not body.strip():
            _emit_error(args, "usage", "note body is empty — aborting")
            return 2
        result = api.create_artifact(name=args.name, body=body, principal=room_principal)

        def human() -> None:
            render.info(f"Created {result.name!r}.")
            print(sanitize_line(result.id))

        _emit(result, args, human)
        return 0

    return _with_api(run, _report_notes_api_error, args)


def _edit_conflict_json(
    args: argparse.Namespace, name: str, etag: str | None, text: str | None
) -> int:
    """JSON-mode answer to a stale ``If-Match``: one J8 ``conflict`` line, exit 1.

    No diff and no prompt (J2); the user's text is still saved beside the cwd and the
    path named, so a script can recover it.
    """
    saved, save_error = _save_conflict_copy(name, etag, text) if text is not None else (None, None)
    if saved:
        suffix = f" — your text is saved at {saved}"
    elif save_error:
        suffix = f" — {save_error}"
    else:
        suffix = ""
    _emit_error(args, "conflict", f"that note has changed since you read it{suffix}", status=412)
    return 1


def cmd_notes_edit(args: argparse.Namespace) -> int:
    """``pepper notes edit <note> [--room ROOM] [--file PATH|-] [--name NAME]
    [--if-match ETAG | --force] [--json]``.

    Fetches the note's current detail first: it seeds ``$EDITOR`` with the
    current body (skipped when ``--file``/stdin supplies one directly) and
    is the "fresh show" the default ``If-Match`` comes from — the same
    read serves both, so ``--file`` gets a precondition just as current as
    the editor path's. Sends no ``body`` when the text is unchanged —
    byte-identical for ``--file``/stdin, or up to one trailing newline for
    ``$EDITOR`` (:func:`_texts_match`) — and sends nothing at all when no
    ``--rename`` was given either. An empty result aborts with exit 2,
    matching the server's own blank-body refusal, before any network call.

    A stale ``If-Match`` raises :class:`api_client.ArtifactConflictError`
    (412): the user's text is saved to
    ``./<note-slug>.<short-etag>.mine.md``, a unified diff from the
    server's current body to that text is printed, and then — on a TTY —
    the user picks ``[r]e-open`` the editor (seeded with their own text,
    the new etag becomes the precondition), ``[o]verwrite`` (resend the
    same text against the new etag), or ``[q]uit``. Off a TTY, or on any
    other answer, this exits 1 with the saved path already printed. A
    rename-only edit has no text of the user's to save, diff, or re-open,
    so its conflict offers only ``[o]verwrite`` (resend the rename alone,
    never the body) or ``[q]uit``.

    Under ``--json`` there is no editor and no prompt (J2): without
    ``--file`` it is a usage error, a conflict is one J8 ``conflict`` line
    (exit 1), and success answers the updated note — or, when nothing
    changed, the note as it stands.

    Exit codes: 0 ok · 1 auth/API/transport/membership-seam-down/conflict
    left unresolved · 2 bad input (no such note, empty body, aborted
    editor, unreadable ``--file``).
    """
    file_body: str | None = None
    if args.file is not None:
        file_body = _read_file_or_stdin(args.file, args)
        if file_body is None:
            return 2
    elif _notes_editor_refused(args):
        return 2

    def run(api: api_client.ApiClient) -> int:
        room_principal = _opt_group(api, args.room, args=args)
        note_id = _resolve_note(api, room_principal=room_principal, value=args.note, args=args)
        if note_id is None:
            return 2
        current = api.get_artifact_detail(note_id)
        if current.kind != _NOTES_KIND:
            _note_not_found(args)
            return 2

        if file_body is not None:
            new_body = file_body
        else:
            edited = _open_in_editor(current.body or "")
            if edited is None:
                return 2
            new_body = edited

        # The trailing-newline tolerance is for editors that append one
        # on save; ``--file``/stdin is byte-exact (the ``show > f.md``
        # round trip), so a one-newline change there is a real change.
        if file_body is not None:
            unchanged = new_body == (current.body or "")
        else:
            unchanged = _texts_match(current.body or "", new_body)
        rename = args.name if args.name and args.name != current.name else None
        if unchanged and rename is None:
            _emit(current, args, lambda: render.info("Nothing to change."))
            return 0
        body_to_send = None if unchanged else new_body
        if body_to_send is not None and not body_to_send.strip():
            _emit_error(args, "usage", "note body is empty — aborting")
            return 2
        if args.force:
            if_match: str | None = None
        elif args.if_match:
            if_match = args.if_match
        else:
            if_match = current.etag

        while True:
            try:
                updated = api.edit_artifact(
                    note_id,
                    name=rename,
                    body=body_to_send,
                    principal=room_principal,
                    if_match=if_match,
                )
            except api_client.ArtifactConflictError as exc:
                if _json_mode(args):
                    return _edit_conflict_json(args, current.name, exc.current_etag, body_to_send)
                if body_to_send is None:
                    # Rename only: the user wrote no text, so there is
                    # nothing to save or diff, and nothing to re-open —
                    # overwriting resends the rename alone and leaves
                    # the other writer's body as it is.
                    render.error("that note has changed since you read it")
                    if not _stdin_is_tty():
                        return 1
                    choice = _prompt("[o]verwrite the name, or [q]uit? ").lower()
                    if choice in ("o", "overwrite"):
                        if_match = exc.current_etag
                        continue
                    render.info("Aborted.")
                    return 1
                saved_path, save_error = _save_conflict_copy(
                    current.name, exc.current_etag, new_body
                )
                if save_error is not None:
                    render.error(save_error)
                _print_note_diff(exc.current_body or "", new_body)
                if saved_path is not None:
                    render.error(
                        f"that note has changed — your text is saved at {saved_path}",
                        soft_wrap=True,
                    )
                else:
                    render.error("that note has changed — your text is in the diff above")
                if not _stdin_is_tty():
                    return 1
                choice = _prompt("[r]e-open the editor, [o]verwrite, or [q]uit? ").lower()
                if choice in ("o", "overwrite"):
                    if_match = exc.current_etag
                    continue
                if choice in ("r", "reopen", "re-open"):
                    reopened = _open_in_editor(new_body)
                    if reopened is None:
                        return 2
                    if not reopened.strip():
                        render.error("note body is empty — aborting")
                        return 2
                    new_body = reopened
                    body_to_send = new_body
                    if_match = exc.current_etag
                    continue
                render.info("Aborted.")
                return 1
            else:
                break
        _emit(updated, args, lambda: render.info(f"Updated {updated.name!r}."))
        return 0

    return _with_api(run, _report_notes_api_error, args)


def cmd_notes_delete(args: argparse.Namespace) -> int:
    """``pepper notes delete <note> [--room ROOM] [--yes] [--json]``.

    Confirms before deleting unless ``--yes`` (JSON mode never prompts, so it
    needs ``--yes``). The delete itself
    (``POST /api/v1/apps/notes/actions/delete``) is a soft delete — any
    room member may delete a room note — recorded with the acting
    user; there is no ``pepper notes restore`` yet, so this is the
    only undo there is until the retention window closes. ``--json``
    answers ``{"ok": true, "id": ...}``.

    Reads the detail first, for the same kind check ``show``/``edit`` make:
    the delete action itself deletes any artifact kind, so a raw UUID of an
    image or a link must be refused here, not passed through. The detail
    read is viewer-scoped, so it can succeed where the owner-scoped delete
    still refuses — a ``--room`` with the UUID of one of your own notes, or
    a note deleted between the read and the delete. The action answers
    those with 422 ("isn't in your collection"), which this maps to the
    same uniform missing-note answer as a 404; nothing else the CLI sends
    (a well-formed UUID) can draw a 422.

    Exit codes: 0 ok · 1 auth/API/transport/membership-seam-down · 2 bad
    input (no such note, or not a note).
    """

    def run(api: api_client.ApiClient) -> int:
        room_principal = _opt_group(api, args.room, args=args)
        note_id = _resolve_note(api, room_principal=room_principal, value=args.note, args=args)
        if note_id is None:
            return 2
        detail = api.get_artifact_detail(note_id)
        if detail.kind != _NOTES_KIND:
            _note_not_found(args)
            return 2
        if (
            code := _confirm_destructive(
                args, f"Delete {detail.name!r}? This cannot be undone.", yes=args.yes
            )
        ) is not None:
            return code
        try:
            name = api.delete_artifact(note_id, principal=room_principal)
        except api_client.ApiError as exc:
            if exc.status != 422:
                raise
            _note_not_found(args)
            return 2
        _emit(
            _mutation_result(None, note_id),
            args,
            lambda: render.info(f"Deleted {name!r}."),
        )
        return 0

    return _with_api(run, _report_notes_api_error, args)


def cmd_takeout(args: argparse.Namespace) -> int:
    """``pepper takeout [--out FILE]``.

    Downloads a zip archive of your Pepper data — conversations,
    trajectories, memories, artifacts. Carries no credentials: connected
    accounts are listed in the manifest for reconnecting, not exported.
    Streams straight to disk so a large archive never sits fully in
    memory. Open to every account; it only ever exports the token's own
    user.
    """

    def run(api: api_client.ApiClient) -> int:
        out_path = api.download_takeout(out_path=args.out)
        _emit(
            {"path": str(out_path), "bytes": os.path.getsize(out_path)},
            args,
            lambda: render.info(f"Wrote takeout archive to {out_path}"),
        )
        return 0

    return _run_personal(args, run)


def cmd_agents_guide(args: argparse.Namespace) -> int:
    """``pepper agents guide [--write DIR [--force]] [--json]``.

    Reads packaged data only: no credentials, no network, so it behaves the
    same under any ``--profile`` and with no login at all. Exit codes: 0 ok ·
    1 ``--write`` refused or failed (existing file without ``--force``, OS
    error) · 2 ``--force`` without ``--write``.
    """
    if args.write is None:
        if args.force:
            _emit_error(args, "usage", "--force only applies with --write DIR")
            return 2
        if _json_mode(args):
            _print_json({"markdown": agent_skill.render()})
        else:
            sys.stdout.write(agent_skill.render())
        return 0
    from pathlib import Path

    try:
        target = agent_skill.write_to(Path(args.write), force=args.force)
    except FileExistsError:
        _emit_error(
            args,
            "conflict",
            f"{Path(args.write) / 'SKILL.md'} already exists; pass --force to overwrite it.",
        )
        return 1
    except OSError as exc:
        _emit_error(args, "gap", f"couldn't write {args.write!r}: {exc.strerror or exc}")
        return 1
    if _json_mode(args):
        _print_json({"path": str(target)})
    else:
        render.info(f"Wrote {target}.")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    path = credentials.credentials_path()
    removed = credentials.delete()
    _forget_listings()
    message = (
        f"Removed credentials at {path}."
        if removed
        else f"No credentials at {path} — nothing to remove."
    )
    _emit({"ok": True}, args, lambda: render.info(message))
    return 0


# ---------------------------------------------------------------------------
# Helpers


def _valid_uuid(value: str) -> bool:
    """True if ``value`` is a canonical UUID string.

    Local pre-check so ``todos done``/``dismiss`` give a friendly error
    instead of splatting a malformed id into a URL and getting the
    server's 422.
    """
    return bool(
        re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            value,
        )
    )


def _parse_due(raw: str, *, flag: str = "--due") -> tuple[str | None, str | None]:
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
    import datetime as dt

    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None, f"couldn't parse {flag} {raw!r} (try 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM')"
    # Naive input → interpret in local zone (astimezone on a naive dt
    # assumes local), giving an aware datetime the server can store.
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.isoformat(), None


def _load_or_error(args: argparse.Namespace | None = None) -> credentials.Credentials | None:
    """Load credentials or print the error and return ``None``.

    Pulled out so every command shares the "not logged in" message
    shape — keeps the affordance consistent across ``whoami`` /
    ``chat`` / future commands.
    """
    try:
        return credentials.load()
    except credentials.CredentialsError as exc:
        _emit_error(args, "auth", str(exc))
        return None


def _report_api_error(exc: api_client.ApiError, args: argparse.Namespace | None = None) -> int:
    """Map an :class:`ApiError` to a CLI exit code + error message.

    401 gets the bespoke "log in again" hint because that's the
    overwhelmingly likely cause (revoked token, server-side reset,
    typo on login). Other statuses get the server's ``detail`` if
    it provided one, else the bare HTTP status.
    """
    code = _error_code_for_status(exc.status)
    if exc.status == 401:
        _emit_error(
            args,
            code,
            "Authentication failed (401). Run 'pepper login' to refresh your token.",
            status=401,
        )
        return 1
    if exc.detail:
        _emit_error(args, code, f"Server returned {exc.status}: {exc.detail}", status=exc.status)
    else:
        _emit_error(args, code, f"Server returned {exc.status}.", status=exc.status)
    return 1


def _prompt(prompt: str) -> str:
    """``input``-with-prompt that returns an empty string on EOF.

    EOF is reasonable for the server URL prompt (user accepts the
    default by hitting Ctrl-D); the token prompt has its own EOF
    handling because we want a clear "no token" error there.
    """
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def _prompt_secret(prompt: str) -> str:
    """``getpass`` with EOF coerced to empty. Like :func:`_prompt`
    but no echo — paste-in tokens don't show on screen, so a stray
    ``screen`` recording of the login doesn't leak the credential."""
    try:
        return getpass.getpass(prompt).strip()
    except EOFError:
        return ""


def cmd_brief(args: argparse.Namespace) -> int:
    """``pepper brief [--date YYYY-MM-DD | --now] [--json]``.

    Renders the Brief document's first edition (the server orders them newest first) or the one
    whose ``date`` field equals ``--date``. ``--now`` runs the owner-scoped ``brief.run_now``
    action, which queues a real delivery; it is the only write here.
    """
    if args.now and args.date:
        _emit_error(args, "usage", "--now runs a fresh brief; it takes no --date.")
        return 2
    date = args.date.strip() if args.date else None

    def run(api: api_client.ApiClient) -> int:
        if args.now:
            result = api.run_app_action("brief", "run_now", {}).get("result") or {}
            _emit(
                result,
                args,
                lambda: render.info(
                    sanitize_line(str(result.get("message") or "Asked for a fresh brief."))
                ),
            )
            return 0 if result.get("started", True) else 1
        try:
            brief = brief_view.pick(api.get_app_data("brief"), date)
        except brief_view.BriefError as exc:
            _emit_error(args, exc.code, str(exc))
            return 1
        _emit(brief, args, lambda: brief_view.render_brief(brief))
        return 0

    return _run_personal(args, run)


def _following_items(
    api: api_client.ApiClient, args: argparse.Namespace
) -> tuple[dict[str, Any], list[dict[str, Any]]] | int:
    """The Following document and its items (narrowed by ``--group``), or an exit code."""
    data = following_view.data_of(api.get_app_data("following"))
    items = following_view.items_of(data)
    try:
        group = following_view.resolve_group(data, args.group) if args.group else None
    except following_view.LookupFailure as exc:
        _emit_error(args, exc.code, exc.message)
        return 2
    return data, following_view.filter_group(items, group)


def cmd_following_list(args: argparse.Namespace) -> int:
    """``pepper following list [--group G] [--json]``."""

    def run(api: api_client.ApiClient) -> int:
        got = _following_items(api, args)
        if isinstance(got, int):
            return got
        data, items = got
        counts = data.get("counts") or {}
        _emit_list(
            items,
            args,
            lambda: following_render.render_following_list(items, counts),
            counts=counts,
        )
        return 0

    return _run_personal(args, run)


def cmd_following_show(args: argparse.Namespace) -> int:
    """``pepper following show ITEM [--json]``: ITEM is an id, ref, or name prefix."""

    def run(api: api_client.ApiClient) -> int:
        items = following_view.items_of(following_view.data_of(api.get_app_data("following")))
        try:
            item = following_view.resolve_item(items, args.item)
        except following_view.LookupFailure as exc:
            _emit_error(args, exc.code, exc.message)
            return 2
        _emit(item, args, lambda: following_render.render_following_show(item))
        return 0

    return _run_personal(args, run)


def _following_action(
    api: api_client.ApiClient, action: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Run one Following action over the PAT route; its ``{"result": ...}`` body's result."""
    result = api.run_app_action("following", action, params).get("result")
    return result if isinstance(result, dict) else {}


def _listing_unusable(args: argparse.Namespace, problem: numbered_view.SaveProblem | None) -> bool:
    """After saving a numbered listing: True when an older listing is still on disk and would
    answer for these numbers, so the listing must not be shown (the error is reported here)."""
    if problem is None or not problem.stale:
        return False
    _emit_error(
        args,
        "usage",
        f"{problem.message}. Its numbers would answer for this list, so it is not shown; "
        "remove that file or fix the folder's permissions.",
        soft_wrap=True,
    )
    return True


def _warn_unsaved(args: argparse.Namespace, problem: numbered_view.SaveProblem | None) -> None:
    """Say, after a listing is shown, that its numbers were not saved (and so won't resolve)."""
    if problem is not None and not _json_mode(args):
        render.error(
            f"{problem.message}; numbers from this list won't work in later commands.",
            soft_wrap=True,
        )


def _unsaved_meta(problem: numbered_view.SaveProblem | None) -> dict[str, str]:
    """The ``--json`` list envelope's note when the numbers were not saved."""
    return {} if problem is None else {"note": f"{problem.message}; numbers were not saved"}


def _forget_listings() -> None:
    """Drop this profile's saved numbered listings, so another sign-in never resolves them."""
    for path in (
        feed.VIEW.state_path(),
        artifacts_view.VIEW.state_path(),
        following_search.state_path(),
    ):
        numbered_view.forget(path)


def cmd_following_search(args: argparse.Namespace) -> int:
    """``pepper following search QUERY… [--json]``: numbered candidates, saved for ``create N``."""
    query = " ".join(args.query)

    def run(api: api_client.ApiClient) -> int:
        found = following_search.numbered(_following_action(api, "search", {"q": query}))
        problem = following_search.save(query, found)
        if _listing_unusable(args, problem):
            return 1
        _emit_list(
            found, args, lambda: following_render.render_candidates(found), **_unsaved_meta(problem)
        )
        _warn_unsaved(args, problem)
        return 0

    return _run_personal(args, run)


def cmd_following_create(args: argparse.Namespace) -> int:
    """``pepper following create N [--json]``: follow candidate N of the last ``following search``.

    The CLI never chooses a result for typed text: the person picks the number, and the Following
    app's ``follow`` action is sent that candidate's own ``ref`` and ``payload``.
    """
    if not args.number.isdigit():
        _emit_error(
            args,
            "usage",
            f"create takes a number from `pepper following search`, not {args.number!r}. "
            "Search first (a name or a web address), then `pepper following create N`.",
        )
        return 2
    n = int(args.number)

    def run(api: api_client.ApiClient) -> int:
        saved = following_search.load()
        chosen = following_search.pick(saved[1], n) if saved else None
        if chosen is None:
            _emit_error(
                args,
                "not_found",
                f"No candidate {n} in the last search; run `pepper following search QUERY` first.",
            )
            return 2
        result = _following_action(
            api, "follow", {"ref": chosen["ref"], "payload": chosen.get("payload") or {}}
        )
        _emit(result, args, lambda: render.info(str(result.get("message") or "Followed.")))
        return 0

    return _run_personal(args, run)


def cmd_following_delete(args: argparse.Namespace) -> int:
    """``pepper following delete ITEM [--yes] [--json]``: the app's ``unfollow`` for one follow."""

    def run(api: api_client.ApiClient) -> int:
        items = following_view.items_of(following_view.data_of(api.get_app_data("following")))
        try:
            item = following_view.resolve_item(items, args.item)
        except following_view.LookupFailure as exc:
            _emit_error(args, exc.code, exc.message)
            return 2
        if (
            rc := _confirm_destructive(args, f"Stop following {item.get('name')}?", yes=args.yes)
        ) is not None:
            return rc
        result = _following_action(api, "unfollow", {"ref": item["ref"]})
        _emit(result, args, lambda: render.info(str(result.get("message") or "Unfollowed.")))
        return 0

    return _run_personal(args, run)


def _feed_unranked(args: argparse.Namespace) -> int:
    """The server sent no ``data.feed``: say so; never rank on this side."""
    note = feed.NOT_RANKED_NOTE
    if _json_mode(args):
        _write_json({"items": [], "next_before": None, "note": note})
    else:
        following_render.render_feed([], note=note)
    return 0


def cmd_feed_list(args: argparse.Namespace) -> int:
    """``pepper feed list [--limit N] [--group G] [--json]``.

    The server's ranked ``data.feed`` in the server's order, numbered; the items' identities are
    remembered (``feed-view.json`` next to the credentials) for ``feed show N``. The server
    ranks and folds; this only fetches and renders.
    """
    if args.limit < 1:
        _emit_error(args, "usage", "--limit must be at least 1")
        return 2

    def run(api: api_client.ApiClient) -> int:
        data = following_view.data_of(api.get_app_data("following"))
        entries = feed.server_feed(data)
        if entries is None:
            return _feed_unranked(args)
        try:
            group = following_view.resolve_group(data, args.group, entries) if args.group else None
        except following_view.LookupFailure as exc:
            _emit_error(args, exc.code, exc.message)
            return 2
        shown = feed.only_group(entries, group)[: args.limit]
        problem = feed.VIEW.save([feed.identity(e) for e in shown], {"group": args.group})
        if _listing_unusable(args, problem):
            return 1
        _emit_list(
            numbered_view.number(shown),
            args,
            lambda: following_render.render_feed(shown),
            **_unsaved_meta(problem),
        )
        _warn_unsaved(args, problem)
        return 0

    return _run_personal(args, run)


def cmd_feed_show(args: argparse.Namespace) -> int:
    """``pepper feed show N [--transcript] [--read [--full]] [--open] [--json]``: item N of the
    last ``feed list`` in full."""
    if args.full and not args.read:
        _emit_error(args, "usage", "--full goes with --read")
        return 2
    if not numbered_view.NumberedView.is_number(args.item):
        _emit_error(
            args,
            "usage",
            f"feed show takes an item number from `pepper feed list`, not {args.item!r}",
        )
        return 2
    try:
        _, row, _meta = feed.VIEW.resolve(args.item)
    except numbered_view.RefError as exc:
        _emit_error(args, exc.code, str(exc))
        return 2
    if row is None:  # a number always resolves to a row; only a bare id would not
        _emit_error(args, "usage", "feed show takes an item number from `pepper feed list`")
        return 2

    def run(api: api_client.ApiClient) -> int:
        data = following_view.data_of(api.get_app_data("following"))
        entries = feed.server_feed(data)
        if entries is None:
            return _feed_unranked(args)
        entry = feed.find(entries, row)
        if entry is None:
            _emit_error(
                args,
                "not_found",
                f"item {args.item} of the last `pepper feed list` ({row.get('title')!r}) is gone: "
                "the server no longer lists it. Run `pepper feed list` again.",
            )
            return 2
        open_url = following_render.web_link(entry.get("url")) if args.open else None
        if args.open and open_url is None:
            _emit_error(args, "not_found", f"item {args.item} has no web link to open")
            return 2
        moment = feed.exact_time(entry, next_view.user_now(api).tzinfo)
        detail: dict[str, Any] = {
            "n": int(args.item),
            **entry,
            "when": moment.isoformat() if moment else None,
            "when_zone": moment.tzname() if moment else None,
        }
        if args.transcript:
            detail["transcript"] = _feed_transcript(api, detail)
        if args.read:
            detail["article"] = _feed_article(detail, full=args.full)
        _emit(detail, args, lambda: feed_detail_render.render_detail(detail))
        if open_url is not None:
            if feed.open_browser(open_url):
                render.info(f"opened {open_url}")
            else:
                render.info(f"no browser could be opened here; the link is {open_url}")
        return 0

    return _run_personal(args, run)


def _feed_transcript(api: api_client.ApiClient, detail: dict[str, Any]) -> dict[str, Any]:
    """The Following app's ``episode_transcript`` for a podcast entry, by the ``episode_id`` the
    server puts on it. It is a pure read on the server (it never starts a transcription), and
    it answers with the show notes, labelled, while no transcript exists.

    No matching or joining here: a podcast entry without ``episode_id`` came from a server too
    old to send it."""
    if detail.get("kind") != "podcast":
        return {"error": "this item has no podcast episode, so there is no transcript."}
    if not detail.get("episode_id"):
        return {"error": "the transcript needs a newer server (this entry carries no episode_id)."}
    try:
        result = _following_action(
            api, "episode_transcript", {"ref": detail["ref"], "item_id": detail["episode_id"]}
        )
    except api_client.ApiError as exc:
        return {"error": f"the transcript is not available ({exc.detail or exc.status})."}
    keep = ("kind", "status", "text", "note")
    if not (result.get("text") or result.get("note")):
        # the server's own kind and status stay in the answer (a script can tell "pending")
        return {
            "kind": result.get("kind"),
            "status": result.get("status"),
            "error": "the server returned no transcript for this episode.",
        }
    return {k: result.get(k) for k in keep}


def _feed_article(detail: dict[str, Any], *, full: bool) -> dict[str, Any]:
    """The item's page, fetched from its own site (no Pepper credentials go there)."""
    url = detail.get("url")
    if not url:
        return {"url": None, "error": "this item has no link to read.", "paragraphs": []}
    art = article_reader.fetch(str(url))
    return art.as_json(limit=None if full else article_reader.DEFAULT_PARAGRAPHS)


# artifacts / preferences (the account commands)

_ARTIFACT_REF_HELP = "Number from the last 'artifacts list', or an artifact id (or unique prefix)."
#: A file this big is refused before it is read; the server's own cap is lower
#: (``artifact_max_blob_bytes``, 25 MiB by default) and its 413 is the real answer.
_UPLOAD_READ_LIMIT = 100 * 1024 * 1024


def _report_artifacts_error(
    exc: api_client.ApiError, args: argparse.Namespace | None = None
) -> int:
    """404 is the server's uniform "missing, deleted or not yours"; 415/413/400 are an
    upload it refused; the rest is :func:`_report_api_error`."""
    if exc.status == 404:
        _emit_error(args, "not_found", "no such artifact, or you cannot see it", status=404)
        return 2
    if exc.status == 503:
        _emit_error(args, "server", "membership check unavailable, try again", status=503)
        return 1
    if exc.status in (400, 413, 415, 422):
        hint = f" Pepper accepts {artifacts_view.UPLOAD_TYPES_HINT}." if exc.status == 415 else ""
        _emit_error(
            args,
            "validation",
            f"the server refused that: {exc.detail or exc.status}.{hint}",
            status=exc.status,
        )
        return 1
    return _report_api_error(exc, args)


def _artifact_ref(
    args: argparse.Namespace,
) -> tuple[str, dict[str, Any] | None, dict[str, Any]] | None:
    try:
        found = artifacts_view.VIEW.resolve(args.artifact)
    except numbered_view.RefError as exc:
        _emit_error(args, exc.code, str(exc))
        return None
    if found[1] is None and not _valid_uuid(found[0]):
        _emit_error(
            args,
            "not_found",
            f"no artifact {args.artifact!r} in the last 'pepper artifacts list'; "
            "use its number or its full id",
        )
        return None
    return found


def cmd_artifacts_list(args: argparse.Namespace) -> int:
    """``pepper artifacts list [--kind K] [--label L] [--search Q] [--room R] [--limit N]``.

    Numbers what it prints and remembers them (``artifacts-view.json`` next to the credentials)
    for ``show``/``download``/``delete N``; JSON items carry ``n``.
    """
    import dataclasses

    # --kind, --label and --search go to the server as query params (kind, label, q); nothing is
    # filtered here. --kind only translates the CLI's word to the stored value the route takes.
    kind = artifacts_view.KINDS[args.kind] if args.kind else None

    def run(api: api_client.ApiClient) -> int:
        principal = _opt_group(api, args.room, args=args)
        result = api.list_artifacts(
            principal=principal, kind=kind, q=args.search, label=args.label, limit=args.limit
        )
        rows = [dataclasses.asdict(item) for item in result.items]
        problem = artifacts_view.VIEW.save(
            [artifacts_view.listing_row(r) for r in rows], {"principal": principal}
        )
        if _listing_unusable(args, problem):
            return 1
        numbered = numbered_view.number(rows)
        _emit_list(
            numbered,
            args,
            lambda: account_render.artifacts_list(numbered, truncated=result.truncated),
            truncated=result.truncated,
            **_unsaved_meta(problem),
        )
        _warn_unsaved(args, problem)
        return 0

    return _with_api(run, _report_artifacts_error, args)


def cmd_artifacts_show(args: argparse.Namespace) -> int:
    """``pepper artifacts show ARTIFACT [--json]``: metadata, and the text of a note or a text
    file. ``--json`` is the server's detail (minus ``body_html``, the rendered duplicate of
    ``body``)."""
    ref = _artifact_ref(args)
    if ref is None:
        return 2

    def run(api: api_client.ApiClient) -> int:
        detail = api.get_artifact_detail_raw(ref[0])
        detail.pop("body_html", None)
        _emit(detail, args, lambda: account_render.artifact_detail(detail))
        return 0

    return _with_api(run, _report_artifacts_error, args)


def cmd_artifacts_download(args: argparse.Namespace) -> int:
    """``pepper artifacts download ARTIFACT [--out PATH] [--force] [--json]``: a file's bytes.

    ``--out`` is a file, a directory (the artifact's name goes inside), or ``-`` for stdout.
    Without it the file is written here under the artifact's name. An existing file is not
    overwritten without ``--force``. ``--json`` emits ``{"path", "bytes", "id", "name"}``.
    """
    from pathlib import Path

    if args.out == "-" and _json_mode(args):
        _emit_error(args, "usage", "--out - writes the file to stdout; use a path with --json")
        return 2
    ref = _artifact_ref(args)
    if ref is None:
        return 2

    def run(api: api_client.ApiClient) -> int:
        detail = api.get_artifact_detail_raw(ref[0])
        if detail.get("kind") != "blob":
            what = artifacts_view.kind_label(str(detail.get("kind")))
            _emit_error(
                args,
                "usage",
                f"a {what.lower()} has no file to download; 'pepper artifacts show' prints it",
            )
            return 2
        content = api.download_artifact(ref[0])
        name = artifacts_view.download_name(detail.get("name"), detail.get("mime_type"))
        if args.out == "-":
            sys.stdout.buffer.write(content)
            return 0
        target = Path(args.out) if args.out else Path.cwd()
        if args.out is None or target.is_dir():
            target = target / name
        if target.exists() and not args.force:
            _emit_error(args, "usage", f"{str(target)!r} already exists; use --force to overwrite")
            return 2
        try:
            artifacts_view.write_download(target, content, force=args.force)
        except FileExistsError:
            _emit_error(args, "usage", f"{str(target)!r} already exists; use --force to overwrite")
            return 2
        except OSError as exc:
            _emit_error(args, "usage", f"couldn't write {str(target)!r}: {exc.strerror or exc}")
            return 2
        _emit(
            {
                "path": str(target),
                "bytes": len(content),
                "id": detail.get("id"),
                "name": detail.get("name"),
            },
            args,
            lambda: render.info(f"Saved {len(content)} bytes to {target}."),
        )
        return 0

    return _with_api(run, _report_artifacts_error, args)


def cmd_artifacts_create(args: argparse.Namespace) -> int:
    """``pepper artifacts create --file PATH [--name N] [--summary S] [--label L]… [--room R]``.

    Uploads one file as a file artifact (``POST /api/v1/artifacts/upload``). The server accepts
    pictures, PDFs, plain text, audio and MP4 video up to ``artifact_max_blob_bytes``, checks
    the bytes against the declared type, and answers ``{id, name, mime_type, byte_size}``.
    """
    from pathlib import Path

    path = Path(args.file)
    try:
        size = path.stat().st_size
        if size > _UPLOAD_READ_LIMIT:
            _emit_error(args, "usage", f"{args.file!r} is {size} bytes; far over the upload limit")
            return 2
        content = path.read_bytes()
    except OSError as exc:
        _emit_error(args, "usage", f"couldn't read {args.file!r}: {exc.strerror or exc}")
        return 2
    if not content:
        _emit_error(args, "usage", f"{args.file!r} is empty")
        return 2
    content_type = artifacts_view.guess_mime(path.name)

    def run(api: api_client.ApiClient) -> int:
        principal = _opt_group(api, args.room, args=args)
        result = api.upload_artifact(
            filename=path.name,
            content=content,
            content_type=content_type,
            name=args.name or "",
            summary=args.summary or "",
            labels=",".join(args.label or []),
            principal=principal,
        )
        _emit(
            result,
            args,
            lambda: render.info(
                f"Uploaded {result.get('name')!r} ({result.get('mime_type')}, "
                f"{result.get('byte_size')} bytes): {result.get('id')}",
                soft_wrap=True,
            ),
        )
        return 0

    return _with_api(run, _report_artifacts_error, args)


def cmd_artifacts_delete(args: argparse.Namespace) -> int:
    """``pepper artifacts delete ARTIFACT [--room R] [--yes] [--json]``: any kind.

    The server soft-deletes (``artifacts`` app ``delete``) and keeps the row for a retention
    window; there is no CLI restore. A number from a ``--room`` listing deletes in that room.
    """
    ref = _artifact_ref(args)
    if ref is None:
        return 2
    artifact_id, _row, meta = ref

    def run(api: api_client.ApiClient) -> int:
        principal = _opt_group(api, args.room, args=args) or meta.get("principal")
        detail = api.get_artifact_detail_raw(artifact_id)
        label = artifacts_view.kind_label(str(detail.get("kind"))).lower()
        gate = _confirm_destructive(
            args, f"Delete the {label} {detail.get('name')!r}?", yes=args.yes
        )
        if gate is not None:
            return gate
        try:
            name = api.delete_artifact(artifact_id, principal=principal, app="artifacts")
        except api_client.ApiError as exc:
            if exc.status != 422:  # 422: not in that owner's collection
                raise
            _emit_error(args, "not_found", "no such artifact in that collection", status=422)
            return 2
        _emit(
            _mutation_result(None, artifact_id),
            args,
            lambda: render.info(f"Deleted {name or detail.get('name')!r}."),
        )
        return 0

    return _with_api(run, _report_artifacts_error, args)


def _report_account_error(args: argparse.Namespace, exc: api_client.ApiError) -> int:
    """:func:`_fail_api`, but a 400/422 is the server declining the value: say why."""
    if exc.status in (400, 422):
        _emit_error(args, "validation", exc.detail or "the server refused that", status=exc.status)
        return 1
    return _fail_api(args, exc)


def cmd_preferences_show(args: argparse.Namespace) -> int:
    """``pepper preferences show [--json]``: ``GET /api/v1/calendars/preferences`` without the
    page's own ``timezones`` menu and ``maps`` flag."""

    def run(api: api_client.ApiClient) -> int:
        prefs = preferences_view.public(api.get_calendar_preferences())
        _emit(prefs, args, lambda: account_render.preferences(prefs))
        return 0

    return _run_personal(args, run, _report_account_error)


def _names(value: str | None, flag: str) -> list[str]:
    names = [n.strip().lower() for n in (value or "").split(",") if n.strip()]
    unknown = [n for n in names if n not in preferences_view.SWITCHES]
    if unknown:
        raise preferences_view.PrefsError(
            f"{flag}: unknown {', '.join(unknown)}; use {', '.join(preferences_view.SWITCHES)}"
        )
    return names


def cmd_preferences_edit(args: argparse.Namespace) -> int:
    """``pepper preferences edit [--timezone Z] [--home-address T] [--work-address T]
    [--work-days D] [--work-start T] [--work-end T] [--show N] [--hide N] [--meal M]…
    [--workout-at T] [--workout-minutes N] [--json]``: ``POST /api/v1/calendars/preferences``
    with only the fields given (the server keeps the rest). Answers the saved preferences.
    Times, zones and addresses are sent as typed; the server checks them and its 422 is shown."""
    pv = preferences_view
    try:
        parsed: dict[str, Any] = {
            "timezone": args.timezone,
            "home_address": args.home_address,
            "work_address": args.work_address,
            "work_days": pv.parse_days(args.work_days) if args.work_days is not None else None,
            "work_start": args.work_start,
            "work_end": args.work_end,
            "show": _names(args.show, "--show"),
            "hide": _names(args.hide, "--hide"),
            "meals": [pv.parse_meal(m) for m in args.meal or []],
            "workout_at": args.workout_at,
            "workout_minutes": args.workout_minutes,
            "run_minutes": args.run_minutes,
            "break_minutes": args.break_minutes,
        }
        both = set(parsed["show"]) & set(parsed["hide"])
        if both:
            raise pv.PrefsError(f"--show and --hide both name {', '.join(sorted(both))}")
    except pv.PrefsError as exc:
        _emit_error(args, "usage", str(exc))
        return 2
    # An empty list from --show/--hide/--meal means the flag was not given; --work-days none
    # is an empty list too, and it is a change (clear the work days), so only None counts there.
    if parsed["work_days"] is None and all(v is None or v == [] for v in parsed.values()):
        _emit_error(args, "usage", "nothing to change: give at least one flag (see --help)")
        return 2

    def run(api: api_client.ApiClient) -> int:
        needs_current = (
            bool(parsed["meals"])
            or parsed["workout_at"] is not None
            or parsed["workout_minutes"] is not None
        )
        current = api.get_calendar_preferences() if needs_current else {}
        try:
            changes = pv.build_changes(current, **parsed)
        except pv.PrefsError as exc:
            _emit_error(args, "usage", str(exc))
            return 2
        saved = pv.public(api.set_calendar_preferences(changes))
        _emit(saved, args, lambda: account_render.preferences(saved))
        return 0

    return _run_personal(args, run, _report_account_error)


if __name__ == "__main__":
    sys.exit(main())
