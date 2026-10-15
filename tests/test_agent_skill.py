"""Tests for ``pepper agent-skill`` and the drift guard on the document it prints.

The drift guard holds the shipped SKILL.md to four rules, so that nothing in it
that names a command can go unchecked. Nothing is executed: the parser is only
walked.

1. Every ``pepper ...`` invocation (a fenced line or an inline code span) has
   its subcommand path and ``--flags`` checked against the real argparse parser
   (:func:`check_document`).
2. A command may only be written as a full ``pepper ...`` invocation. A code
   span or fenced line that starts with a subcommand name instead (``messages
   unreact``, ``topics --json``, a bare ``unreact``) is a failure, because rule
   1 cannot see it (:func:`find_unchecked_commands`).
3. Every ``--flag`` written anywhere in the document, in a code span or in
   prose, exists somewhere in the parser (:func:`find_unknown_flags`).
4. Outside the section on what an agent token cannot run, every ``rooms``,
   ``topics``, ``messages`` or ``attachments`` invocation is one the server accepts an agent
   token for
   (:func:`check_agent_availability`).
"""

from __future__ import annotations

import argparse
import re
import shlex
import tomllib
from importlib import resources
from pathlib import Path

import pytest
import yaml

from pepper_cli import __version__, agent_skill, api_client, cli, credentials

# --------------------------------------------------------------------------
# Drift guard
# --------------------------------------------------------------------------

_FENCE = re.compile(r"^\s*```")
_INLINE_SPAN = re.compile(r"`([^`\n]+)`")
# Tokens that end the ``pepper`` command inside a shell line: pipes, lists,
# redirections. ``<group>``-style placeholders are NOT terminators.
_SHELL_END = re.compile(r"^(\||&&|;|\d*>|<<?-?)")
_PLACEHOLDER = re.compile(r"^<[^<>\s]*>$")
# Commands the document always shows under a profile; ``agent-skill`` and ``--version``
# need no login, so the document may show them bare.
_API_NOUNS = frozenset({"rooms", "topics", "messages", "attachments"})
_NEEDS_PROFILE = _API_NOUNS | {"login", "logout", "whoami"}
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")


# A ``--flag`` anywhere in running text. The lookbehind keeps out a third dash
# (the frontmatter fence) and a dash pair inside a word.
_FLAG = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")
_HEADING = re.compile(r"^## ")
# Heading of the one section allowed to name commands an agent token cannot use.
_CANNOT_RUN = "cannot run"
# The ``<noun> <verb>`` commands whose server route admits an agent token, and the
# flags on them that reach a route which does not. Mirrors
# the server's allowlist of routes an agent token may reach: ``rooms watch``
# is the events stream, ``messages react``/``unreact`` the two reactions routes, ``--mark``
# the read-mark route and ``--attach`` the upload route (both personal-token only).
# ``members list`` (the old ``groups roster``) joins this set when the members
# commands land (phase 2b of the CLI information architecture).
_AGENT_COMMANDS = frozenset(
    {
        "rooms list",
        "topics list",
        "messages list",
        "rooms watch",
        "messages post",
        "messages react",
        "messages unreact",
    }
)
_AGENT_REFUSED_FLAGS = frozenset({"--mark", "--attach"})


def _code_units(markdown: str) -> list[tuple[str, bool, str]]:
    """Every piece of code in the document as ``(text, in_fence, section)``.

    A unit is one line of a fenced block or one inline code span; ``section``
    is the ``## `` heading it sits under. A heredoc body inside a fence is
    skipped, since it is message text, not a command.
    """
    units: list[tuple[str, bool, str]] = []
    section = ""
    in_fence = False
    heredoc_end: str | None = None
    for line in markdown.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            heredoc_end = None
            continue
        if in_fence:
            if heredoc_end is not None:
                if line.strip() == heredoc_end:
                    heredoc_end = None
                continue
            if line.strip():
                units.append((line, True, section))
            if m := _HEREDOC.search(line):
                heredoc_end = m.group(2)
            continue
        if _HEADING.match(line):
            section = line
        units.extend((span, False, section) for span in _INLINE_SPAN.findall(line))
    return units


def _command_text(unit: str) -> str:
    return unit.strip().removeprefix("$ ").strip()


def _invocation_args(unit: str) -> list[str] | None:
    """Tokens after the leading ``pepper``, or ``None`` if ``unit`` is not an invocation."""
    line = _command_text(unit)
    if line != "pepper" and not line.startswith("pepper "):
        return None
    try:
        tokens = shlex.split(line, comments=True)
    except ValueError:
        tokens = line.split()
    args: list[str] = []
    for tok in tokens[1:]:
        if _SHELL_END.match(tok) and not _PLACEHOLDER.match(tok):
            break
        args.append(tok)
    return args


def _extract_invocations(markdown: str) -> list[list[str]]:
    """Token lists (after the leading ``pepper``) for each invocation found.

    Sources: lines of fenced code blocks that start with ``pepper`` (optionally
    after ``$ ``), and inline code spans that start with ``pepper ``.
    """
    return [
        args
        for unit, _in_fence, _section in _code_units(markdown)
        if (args := _invocation_args(unit)) is not None
    ]


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser] | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return None


def _flags(parser: argparse.ArgumentParser) -> set[str]:
    return {opt for action in parser._actions for opt in action.option_strings}


def check_document(markdown: str) -> list[str]:
    """Problems found in ``markdown``'s ``pepper`` invocations; empty means none."""
    root, _ = cli._build_parser()
    root_flags = _flags(root)
    problems: list[str] = []
    for args in _extract_invocations(markdown):
        shown = "pepper " + " ".join(args)
        if args[:1] == ["--profile"]:
            args = args[2:]
        elif any(a.startswith("--profile") for a in args):
            problems.append(f"{shown!r}: --profile must lead the command")
            continue
        elif args[:1] and args[0] in _NEEDS_PROFILE:
            problems.append(f"{shown!r}: missing the leading --profile <name>")
            continue
        parser = root
        flags_seen: list[str] = []
        placeholder = False
        for tok in args:
            if tok.startswith("--"):
                flags_seen.append(tok.split("=", 1)[0])
                continue
            choices = _subparsers(parser)
            if tok.startswith("<") or tok == "...":
                placeholder = True  # stands for an elided subcommand or argument
                continue
            if choices is not None and tok != "-":
                if tok not in choices:
                    problems.append(f"{shown!r}: unknown subcommand {tok!r} for {parser.prog!r}")
                    break
                parser = choices[tok]
                # Flags given before this subcommand belong to the parent level.
        else:
            choices = _subparsers(parser)
            elided = placeholder or "--help" in flags_seen
            if choices is not None and parser is not root and not elided:
                problems.append(f"{shown!r}: incomplete, {parser.prog!r} needs a subcommand")
            known = _flags(parser) | root_flags
            problems.extend(
                f"{shown!r}: {parser.prog!r} has no flag {flag}"
                for flag in flags_seen
                if flag not in known
            )
    return problems


def _command_names() -> tuple[frozenset[str], frozenset[str]]:
    """``(top-level subcommands, verbs under the API nouns)`` of the real parser."""
    root, _ = cli._build_parser()
    top = _subparsers(root) or {}
    verbs: set[str] = set()
    for noun in _API_NOUNS:
        verbs |= set(_subparsers(top[noun]) or {})
    return frozenset(top), frozenset(verbs)


def find_unchecked_commands(markdown: str) -> list[str]:
    """Code that names a command in a form :func:`check_document` cannot check.

    The document's rule is that a command is always the full ``pepper ...``
    invocation. So these fail: a fenced line that is not one; a code span of two
    or more words whose first word is a subcommand name (``messages unreact``,
    ``topics --json``, ``login --manual``); and a one-word span that is a verb
    under an API noun (``unreact``). A one-word top-level name such as
    ``login`` is allowed, since ``login`` and ``topics`` are also ordinary words
    for the thing; a verb that shares its name with a JSON field has to be
    written without the code span.
    """
    top, verbs = _command_names()
    problems: list[str] = []
    for unit, in_fence, _section in _code_units(markdown):
        text = _command_text(unit)
        if _invocation_args(unit) is not None:
            continue
        words = text.split()
        if in_fence:
            problems.append(f"{text!r}: a fenced line must be a full `pepper ...` invocation")
        elif words[0] in (top | verbs) if len(words) > 1 else words[0] in (verbs - top):
            problems.append(
                f"{text!r}: shortened command; write the full `pepper --profile <name> ...` form"
            )
    return problems


def _text_outside_heredocs(markdown: str) -> str:
    """The document minus heredoc bodies, which are message text and may hold anything."""
    kept: list[str] = []
    heredoc_end: str | None = None
    for line in markdown.splitlines():
        if heredoc_end is not None:
            if line.strip() == heredoc_end:
                heredoc_end = None
            continue
        kept.append(line)
        if m := _HEREDOC.search(line):
            heredoc_end = m.group(2)
    return "\n".join(kept)


def find_unknown_flags(markdown: str) -> list[str]:
    """Every ``--flag`` written anywhere in the document that no parser has."""
    mentioned = set(_FLAG.findall(_text_outside_heredocs(markdown)))
    return sorted(mentioned - _all_flags())


def _api_command(args: list[str]) -> str | None:
    """The ``<noun> <verb>`` an invocation names, or ``None`` (elided, ``--help``, other)."""
    rest = args[2:] if args[:1] == ["--profile"] else args
    words = [a for a in rest if not a.startswith("--")]
    if words[:1] == [] or words[0] not in _API_NOUNS or len(words) < 2 or "--help" in rest:
        return None
    return None if _PLACEHOLDER.match(words[1]) else f"{words[0]} {words[1]}"


def check_agent_availability(markdown: str) -> list[str]:
    """Invocations that present a personal-token-only command as usable by an agent.

    Outside the "cannot run" section every API-noun invocation must be one of
    :data:`_AGENT_COMMANDS` and carry none of :data:`_AGENT_REFUSED_FLAGS`; inside
    it, a command that an agent token *can* run is the mistake.
    """
    problems: list[str] = []
    for unit, _in_fence, section in _code_units(markdown):
        args = _invocation_args(unit)
        sub = _api_command(args) if args is not None else None
        if args is None or sub is None:
            continue
        shown = "pepper " + " ".join(args)
        refused = sorted(_AGENT_REFUSED_FLAGS & {a.split("=", 1)[0] for a in args})
        if _CANNOT_RUN in section:
            if sub in _AGENT_COMMANDS and not refused:
                problems.append(f"{shown!r}: listed as unavailable, but an agent token can run it")
        elif sub not in _AGENT_COMMANDS:
            problems.append(f"{shown!r}: `{sub}` needs a personal token (401 for an agent)")
        elif refused:
            problems.append(f"{shown!r}: {', '.join(refused)} needs a personal token")
    return problems


def _all_flags() -> set[str]:
    root, _ = cli._build_parser()
    out = set(_flags(root))
    stack = [root]
    while stack:
        subs = _subparsers(stack.pop()) or {}
        for sub in subs.values():
            out |= _flags(sub)
            stack.append(sub)
    return out


def test_document_matches_the_real_parser() -> None:
    assert check_document(agent_skill.raw_text()) == []


def test_document_writes_every_command_in_checkable_form() -> None:
    assert find_unchecked_commands(agent_skill.raw_text()) == []


def test_every_flag_mentioned_anywhere_exists_in_the_parser() -> None:
    text = agent_skill.raw_text()
    assert _FLAG.findall(text), "expected the document to name flags"
    assert find_unknown_flags(text) == []


def test_document_only_presents_commands_an_agent_token_can_run() -> None:
    assert check_agent_availability(agent_skill.raw_text()) == []


def test_agent_commands_are_real_and_the_permission_block_lists_exactly_them() -> None:
    """Section 8's fenced block is the allowlist: no more, no fewer, all in the parser."""
    top, verbs = _command_names()
    assert {c.split()[1] for c in _AGENT_COMMANDS} <= verbs
    assert {c.split()[0] for c in _AGENT_COMMANDS} <= top
    listed = [
        _api_command(args)
        for unit, in_fence, section in _code_units(agent_skill.raw_text())
        if in_fence and "can run" in section and (args := _invocation_args(unit)) is not None
    ]
    assert sorted(s for s in listed if s) == sorted(_AGENT_COMMANDS)


def test_document_names_the_tempting_commands_as_unavailable() -> None:
    """The commands and flags an agent is most likely to reach for are spelled out."""
    text = agent_skill.raw_text()
    cannot = text[text.index("\n## 9. What an agent token cannot run") :]
    cannot = cannot[: cannot.index("\n## 10.")]
    for needle in (
        "messages edit",
        "messages delete",
        "rooms show",
        "attachments download",
    ):
        assert f"`pepper --profile <name> {needle}`" in cannot, needle
    for flag in _AGENT_REFUSED_FLAGS:
        assert f"`{flag}`" in cannot, flag


def test_document_exercises_the_invocations_the_guard_is_meant_to_cover() -> None:
    joined = [" ".join(a) for a in _extract_invocations(agent_skill.raw_text())]
    for needle in (
        "rooms watch",
        "messages list",
        "messages post",
        "messages react",
        "messages unreact",
        "login",
    ):
        assert any(needle in j for j in joined), needle


def test_document_names_the_post_json_flag_and_the_watch_kind_filters() -> None:
    """The flags this guide recommends are written in a checked invocation or span,
    so renaming one in the parser fails ``test_document_matches_the_real_parser``."""
    text = agent_skill.raw_text()
    flags = _all_flags()
    for flag in ("--json", "--kinds", "--ignore-kinds"):
        assert flag in text
        assert flag in flags
    joined = [" ".join(a) for a in _extract_invocations(text)]
    assert any(
        "rooms watch" in j and "--ignore-kinds message.link_cards_updated" in j for j in joined
    )
    # ``--json`` exists on several commands, so the flag scan alone would not notice it
    # leaving ``messages post``; a checked post invocation carrying it does.
    assert any("messages post" in j and "--json" in j for j in joined)
    assert "message.link_cards_updated" in text


@pytest.mark.parametrize(
    ("markdown", "expect"),
    [
        ("`pepper --profile p messages list --room G --bogus`", "no flag --bogus"),
        ("`pepper --profile p messages nope G`", "unknown subcommand 'nope'"),
        ("`pepper --profile p frobnicate`", "unknown subcommand 'frobnicate'"),
        ("`pepper messages list --room G`", "missing the leading --profile"),
        ("`pepper --profile p rooms`", "incomplete"),
        (
            (
                "```\npepper --profile p messages post --room G --topic T - --reply-to M "
                "--on-behalf-of-x P\n```"
            ),
            "no flag --on-behalf-of-x",
        ),
    ],
)
def test_drift_guard_reports_a_bad_document(markdown: str, expect: str) -> None:
    problems = check_document(markdown)
    assert problems, markdown
    assert any(expect in p for p in problems), problems


@pytest.mark.parametrize(
    "markdown",
    [
        "`messages unreact` with the same arguments takes it back",
        "`messages edit <message-id> <text...>` changes a message",
        "`messages delete <message-id> --yes`",
        "from `list` and `topics --json`",
        "then `unreact` it",
        "`login --manual` prompts",
        "## 4. The event stream: `rooms watch`",
        "```\nmessages unreact G M E\n```",
        "```\n$ messages list G\n```",
        "```\nPEPPER_NO_IMPLICIT_CHAT=1 pepper --profile p rooms list\n```",
    ],
)
def test_drift_guard_refuses_a_command_it_cannot_check(markdown: str) -> None:
    assert find_unchecked_commands(markdown), markdown


@pytest.mark.parametrize(
    "markdown",
    [
        "`pepper --profile p messages unreact --room G M E`",
        "`login` refuses an agent token; each entry of `items` has a `group_id`",
        "`Bash(pepper --profile <name> messages list:*)`",
        "`pepper-cli` 1.2.3, `person_id`, `-`, `bash -c`, `VAR=value`",
        '`{"kind":"cursor","cursor":N}`',
        (
            "```\npepper --profile p messages post --room G --topic T - <<'EOF'\n"
            "messages unreact\nEOF\n```"
        ),
    ],
)
def test_drift_guard_leaves_checkable_and_unrelated_code_alone(markdown: str) -> None:
    assert find_unchecked_commands(markdown) == []


@pytest.mark.parametrize(
    "markdown",
    [
        "`--bogus`",
        "`--bogus PATH` (repeatable)",
        "`messages delete <message-id> --bogus`",
        "`Bash(pepper --profile <name> messages list --bogus:*)`",
        "pass --bogus in prose, outside any code span",
        # Stricter than check_document, which stops at the pipe: the document
        # tells agents to run one plain command, so it has no other tool's flags.
        "```\npepper --profile p rooms list --json | jq --bogus\n```",
    ],
)
def test_drift_guard_reports_an_unknown_flag_wherever_it_is_written(markdown: str) -> None:
    assert find_unknown_flags(markdown) == ["--bogus"]


def test_drift_guard_flag_scan_skips_frontmatter_fences_and_heredoc_bodies() -> None:
    doc = (
        "---\nname: x\n---\n\n`--json` and a well-known--word\n"
        "```\npepper --profile p messages post --room G --topic T - <<'EOF'\n"
        "use --bogus here\nEOF\n```\n"
    )
    assert find_unknown_flags(doc) == []


@pytest.mark.parametrize(
    ("markdown", "expect"),
    [
        (
            "`pepper --profile p messages edit --room G M text`",
            "`messages edit` needs a personal token",
        ),
        ("`pepper --profile p messages delete --room G M --yes`", "`messages delete`"),
        (
            "```\npepper --profile p attachments download --room G A --out f\n```",
            "`attachments download`",
        ),
        (
            "`pepper --profile p messages list --room G --topic T --mark`",
            "--mark needs a personal token",
        ),
        (
            "`pepper --profile p messages post --room G --topic T - --attach f`",
            "--attach needs a personal token",
        ),
        (
            "## 9. What an agent token cannot run\n`pepper --profile p messages list`",
            "an agent token can run it",
        ),
    ],
)
def test_drift_guard_reports_a_command_an_agent_token_cannot_run(
    markdown: str, expect: str
) -> None:
    problems = check_agent_availability(markdown)
    assert any(expect in p for p in problems), problems


def test_drift_guard_accepts_unavailable_commands_only_in_their_own_section() -> None:
    doc = (
        "## 8. What an agent token can run\n"
        "`pepper --profile p messages list --room G --topic T --json`, "
        "`pepper --profile p messages --help`,\n"
        "`pepper --profile p messages <command> --help`, `pepper --profile p whoami`\n"
        "## 9. What an agent token cannot run\n"
        "`pepper --profile p messages edit`, `pepper --profile p messages list --room G --mark`\n"
    )
    assert check_agent_availability(doc) == []


def test_drift_guard_accepts_flags_after_a_heredoc_and_ignores_body_text() -> None:
    doc = (
        "```\n"
        "pepper --profile p messages post --room G --topic T - --reply-to M <<'EOF'\n"
        "pepper --profile p messages nope --bogus\n"
        "EOF\n"
        "```\n"
    )
    assert check_document(doc) == []


def test_drift_guard_stops_at_a_pipe() -> None:
    assert check_document("```\npepper --profile p rooms list --json | jq --bogus\n```") == []


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------


@pytest.fixture
def no_credentials(tmp_path, monkeypatch):
    """An empty home, and any attempt to read credentials or reach the server fails."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    def boom(*_a, **_k):
        raise AssertionError("agent-skill must not load credentials or call the network")

    monkeypatch.setattr(credentials, "load", boom)
    monkeypatch.setattr(api_client, "ApiClient", boom)
    return tmp_path


def _frontmatter(text: str) -> dict:
    assert text.startswith("---\n")
    head, _, _body = text[4:].partition("\n---\n")
    return yaml.safe_load(head)


def test_prints_a_valid_skill_with_no_credentials(no_credentials, capsys) -> None:
    assert cli.main(["agent-skill"]) == 0
    out = capsys.readouterr().out
    meta = _frontmatter(out)
    assert meta["name"]
    assert meta["description"]
    assert agent_skill.VERSION_PLACEHOLDER not in out
    assert f"`pepper-cli` {__version__}" in out


def test_works_under_a_profile(no_credentials, capsys) -> None:
    assert cli.main(["--profile", "agent-x", "agent-skill"]) == 0
    assert _frontmatter(capsys.readouterr().out)["name"]


def test_is_not_swallowed_by_implicit_chat(no_credentials, capsys) -> None:
    _parser, names = cli._build_parser()
    assert "agent-skill" in names


def test_write_creates_dir_and_refuses_to_overwrite(no_credentials, tmp_path, capsys) -> None:
    target = tmp_path / "skills" / "pepper-agent"
    assert cli.main(["agent-skill", "--write", str(target)]) == 0
    written = (target / "SKILL.md").read_text(encoding="utf-8")
    assert written == agent_skill.render()
    assert _frontmatter(written)["name"]

    (target / "SKILL.md").write_text("mine", encoding="utf-8")
    assert cli.main(["agent-skill", "--write", str(target)]) != 0
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "mine"

    assert cli.main(["agent-skill", "--write", str(target), "--force"]) == 0
    assert (target / "SKILL.md").read_text(encoding="utf-8") == written


def test_force_without_write_is_a_usage_error(no_credentials, capsys) -> None:
    assert cli.main(["agent-skill", "--force"]) == 2
    assert capsys.readouterr().out == ""


def test_write_into_an_unwritable_place_exits_nonzero(no_credentials, tmp_path, capsys) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert cli.main(["agent-skill", "--write", str(blocker / "sub")]) == 1


# --------------------------------------------------------------------------
# Packaging
# --------------------------------------------------------------------------


def test_document_is_packaged_data_inside_the_wheel_package() -> None:
    """The file is a real resource under ``pepper_cli`` and the wheel config ships that package.

    ``packages = ["src/pepper_cli"]`` includes non-Python files in it; this pins
    both halves so moving the file out of the package, or narrowing the wheel
    config, fails here rather than at ``pepper agent-skill`` on an installed copy.
    """
    resource = resources.files("pepper_cli") / "data" / "agent-skill" / "SKILL.md"
    assert resource.is_file()
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    wheel = config["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["src/pepper_cli"]
    assert not wheel.get("exclude")
    assert not config["tool"]["hatch"]["build"].get("exclude")
