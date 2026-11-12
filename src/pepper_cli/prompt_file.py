"""Turn a prompt file and its arguments into the one message ``pepper run`` sends.

A prompt file is plain text, optionally starting with a ``#!`` line so it can be
executed directly (``#!/usr/bin/env -S pepper run``). Its arguments reach the
prompt two ways:

- **Substitution.** ``{{1}}`` … ``{{N}}`` become the matching positional argument,
  and ``{{@}}`` becomes all of them, space-joined. ``{{{{`` is a literal ``{{``.
  Any other ``{{…}}`` is left alone, so a prompt that happens to contain template
  syntax for something else still reads as written.
- **Appended context.** Every argument the body did not reference is listed under
  an ``Arguments:`` block at the end, so a script that never mentions its
  arguments still passes them on.

Pure: no I/O, no network. The CLI reads the file and sends the result.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

#: A literal ``{{`` (``{{{{``) or a placeholder (``{{1}}``, ``{{ 2 }}``, ``{{@}}``).
_TOKEN = re.compile(r"\{\{\{\{|\{\{\s*(\d+|@)\s*\}\}")


class PromptFileError(ValueError):
    """The prompt file and its arguments cannot make a message (a usage error)."""


def strip_shebang(text: str) -> str:
    """Drop a leading ``#!`` line, the one the kernel used to start ``pepper``."""
    if text.startswith("#!"):
        _, _, rest = text.partition("\n")
        return rest
    return text


def build_prompt(text: str, args: Sequence[str]) -> str:
    """The message for ``text`` (the file's contents) with ``args`` applied.

    Raises :class:`PromptFileError` for an empty body or a placeholder with no
    matching argument, ``{{@}}`` with no arguments at all included: a silent blank
    would send a prompt that reads as complete and is not.
    """
    body = strip_shebang(text)
    used: set[int] = set()
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        ref = match.group(1)
        if ref is None:
            return "{{"
        if ref == "@":
            if not args:
                missing.append(match.group(0))
                return match.group(0)
            used.update(range(len(args)))
            return " ".join(args)
        index = int(ref)
        if not 1 <= index <= len(args):
            missing.append(match.group(0))
            return match.group(0)
        used.add(index - 1)
        return args[index - 1]

    body = _TOKEN.sub(replace, body).strip()
    if missing:
        have = f"{len(args)} argument{'' if len(args) == 1 else 's'}"
        raise PromptFileError(f"the prompt uses {', '.join(missing)} but was given {have}")
    if not body:
        raise PromptFileError("the prompt file is empty: nothing to send")
    rest = [arg for i, arg in enumerate(args) if i not in used]
    if rest:
        lines = "\n".join(f"- {arg}" for arg in rest)
        body = f"{body}\n\nArguments:\n{lines}"
    return body
