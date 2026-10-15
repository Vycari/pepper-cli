"""The agent-usage skill shipped inside the CLI (``pepper agents guide``).

Not to be confused with :mod:`pepper_cli.skill_doc`, which is about the
account's server-side skills (``pepper skills``). This document is packaged
data: a real ``SKILL.md`` under ``data/agent-skill/``, read through
:mod:`importlib.resources` so it travels in the wheel and always describes the
binary that prints it. Nothing here reads credentials or touches the network.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from pepper_cli import __version__

#: Placeholder in the packaged file, replaced with the CLI version on output.
VERSION_PLACEHOLDER = "@@PEPPER_CLI_VERSION@@"

_RESOURCE_PACKAGE = "pepper_cli"
_RESOURCE_PARTS = ("data", "agent-skill", "SKILL.md")


def raw_text() -> str:
    """The packaged document exactly as stored, version placeholder intact."""
    node = resources.files(_RESOURCE_PACKAGE)
    for part in _RESOURCE_PARTS:
        node = node / part
    return node.read_text(encoding="utf-8")


def render() -> str:
    """The document with the running CLI's version stamped in."""
    return raw_text().replace(VERSION_PLACEHOLDER, __version__)


def write_to(directory: Path, *, force: bool) -> Path:
    """Write ``directory/SKILL.md``, creating ``directory`` as needed.

    Without ``force`` an existing file is never touched: the file is opened
    exclusively, so the refusal (``FileExistsError``) is atomic rather than a
    check-then-write race.
    """
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "SKILL.md"
    with target.open("w" if force else "x", encoding="utf-8") as fh:
        fh.write(render())
    return target
