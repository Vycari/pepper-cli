"""Small, optional bits of fun: ``pepper fortune`` and the rare pepper at REPL start.

Everything here is local. Nothing calls the server, reads credentials or writes a file,
so ``pepper fortune`` works logged out and offline.

The rare startup pepper only ever appears on an interactive terminal, goes to stderr (so
it never reaches a pipe), and is switched off by ``PEPPER_NO_WHIMSY`` set to any truthy
value (the same spellings as ``PEPPER_NO_TUI``). ``pepper fortune`` ignores that switch:
asking for a fortune by name is asking for it.
"""

from __future__ import annotations

import os
import random

#: Short, original lines in Pepper's voice. Keep each to one line that reads well at
#: 80 columns, and keep them kind: no jokes at anyone's expense.
FORTUNES: tuple[str, ...] = (
    "A todo left undone today is a todo with ambitions for tomorrow.",
    "Inbox zero is a place you visit, not a place you live.",
    "The meeting that could have been an email is, today, an email. Rejoice.",
    "You will find the note you were looking for in the last place you search.",
    "Somewhere, a calendar invite is being declined on your behalf. Politely.",
    "Fortune favors the person who wrote it down.",
    "Every great plan starts as a rough draft in the wrong window.",
    "Your future holds a quiet afternoon. Guard it with a calendar block.",
    "A reminder set is a worry delegated.",
    "Today's lucky number is the count of tabs you can safely close.",
    "The best time to plant a tree was twenty years ago. The second best is a todo.",
    "Not every thread needs a reply. Some just need a reaction.",
    "Pepper has read the forecast and recommends a jacket. Probably.",
    "You are about to remember why you opened this terminal.",
    "Small steps still count as steps. They also count as progress.",
    "The coffee is not a dependency. The coffee is infrastructure.",
    "An unread newsletter is a letter to a future you with more time.",
    "Ask, and it shall be summarized.",
    "Your brief will be brief. That is the whole point.",
    "A good day has one big thing in it, and room around it.",
    "The calendar has a gap at 3pm. It has your name on it.",
    "Good notes make good neighbors, especially in shared rooms.",
    "Rest is a task too. Mark it done.",
    "Somebody is glad you replied. You may never know who.",
    "The shortest path between two meetings is a walk outside.",
    "Today's weather: a light chance of getting things done.",
    "A clean terminal is the sign of a well-scrolled mind.",
    "Your patience will be rewarded, roughly at the end of this spinner.",
    "Write the email you would like to receive. Then make it shorter.",
    "Pepper remembers so you can forget, on purpose.",
    "The best todo list is the one you actually open.",
    "There is no such thing as a quick question, but there are kind answers.",
    "Look up from the screen. Something nice is probably there.",
    "Plans change. That is what plans are for.",
    "Your next idea is hiding in a shower, a walk, or a long commute.",
    "Finished is a feature.",
    "Tomorrow you will thank today you for that reminder.",
    "Even peppers need a little sun. Go find some.",
)

#: The small pepper drawn on a rare REPL start. Plain ASCII so every terminal draws it.
_PEPPER_ART_LINES: tuple[str, ...] = (
    r"      __",
    r"     /  |",
    r"  .-'   '-.___",
    r" (            `--.",
    r"  `-.___          `.",
    "        `--.__      \\",
    r"              `-.   |",
    r"                 `--'",
)
PEPPER_ART: str = "\n".join(_PEPPER_ART_LINES)

#: The big pepper the TUI shows full screen on a rare start. Plain ASCII, 13 rows by 32.
_SPLASH_ART_LINES: tuple[str, ...] = (
    "              ,--.",
    "             /  /",
    "       _.---'  '---._",
    "    .-'              '-.",
    "   /                    \\",
    "  /                      \\",
    " |                        |",
    " |                        |",
    " |                        |",
    "  \\                      /",
    "   \\        /\\          /",
    "    '.     /  \\       .'",
    "      '-.-'    '-.-.-'",
)
SPLASH_ART: str = "\n".join(_SPLASH_ART_LINES)

#: How often the startup pepper appears: about one REPL start in fifty.
STARTUP_PEPPER_CHANCE: float = 1 / 50


def whimsy_disabled() -> bool:
    """Whether ``PEPPER_NO_WHIMSY`` is set to a truthy value (not ``""``/``0``/``false``/``no``)."""
    return os.environ.get("PEPPER_NO_WHIMSY", "").strip().lower() not in ("", "0", "false", "no")


def pick_fortune(rng: random.Random | None = None) -> str:
    """One fortune, chosen by ``rng`` (the module-level generator by default)."""
    return (rng or random).choice(FORTUNES)


def startup_pepper(*, is_tty: bool, rng: random.Random | None = None) -> str | None:
    """The art and a fortune for a rare REPL start, or ``None`` on almost every start.

    ``None`` whenever the terminal is not interactive or ``PEPPER_NO_WHIMSY`` is set, so
    scripts and people who turned it off never see it.
    """
    if not is_tty or whimsy_disabled():
        return None
    gen = rng or random
    if gen.random() >= STARTUP_PEPPER_CHANCE:
        return None
    return f"{PEPPER_ART}\n  {pick_fortune(rng)}"


def splash_fortune(*, rng: random.Random | None = None) -> str | None:
    """A fortune for the TUI's rare full-screen pepper, or ``None`` on almost every start.

    The same odds and the same ``PEPPER_NO_WHIMSY`` switch as :func:`startup_pepper`. The
    TUI only ever runs on a terminal, so there is no terminal check here.
    """
    if whimsy_disabled():
        return None
    gen = rng or random
    if gen.random() >= STARTUP_PEPPER_CHANCE:
        return None
    return pick_fortune(rng)
