"""The intent map at the top of ``pepper --help`` names only real commands."""

from __future__ import annotations

import re

from pepper_cli import cli


def test_every_command_in_the_intent_map_exists() -> None:
    _, commands = cli._build_parser()
    named: set[str] = set()
    for _, rows in cli.INTENT_MAP:
        for _, cmd in rows:
            bare = re.sub(r"\(.*?\)", "", cmd)
            named.update(w for w in re.split(r"[\s/]+", bare) if w and not w.startswith("<"))
    named -= {"guide", "watch", "list"}  # verbs, not commands
    assert named <= set(commands), named - set(commands)


def test_help_leads_with_the_intent_map(capsys) -> None:
    parser, _ = cli._build_parser()
    parser.print_help()
    out = capsys.readouterr().out
    assert out.index("What do you want to know?") < out.index("positional arguments")
    assert "pepper feed  (manage: following)" in out
    assert "dash" not in out.split("positional arguments")[0]
