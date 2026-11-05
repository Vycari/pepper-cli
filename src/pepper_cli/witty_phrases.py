"""Loading phrases for the status line while a Pepper turn runs.

Source: https://github.com/google-gemini/gemini-cli/blob/89e9d25d67e7dc7cb0deb84ad2c3474b7a392d31/packages/cli/src/ui/constants/wittyPhrases.ts
Copyright 2025 Google LLC, Apache-2.0.
Modified by Vycari: Google-specific lines removed, Pepper lines added.

This mirrors pepper's ``web/static/witty_phrases.js`` (the web chat's list), so a change
should land in both. The TUI status line and the ``pepper chat`` spinner share this list and
the same policy: start on a phrase, show what the turn is doing, and rotate to a fresh
phrase when nothing has changed the line for :data:`ROTATE_SECONDS`.
"""

from __future__ import annotations

import random
from typing import Protocol

#: How long a status line may sit unchanged before it rotates to a fresh phrase.
ROTATE_SECONDS = 4.0


class _Chooser(Protocol):
    def choice(self, seq: tuple[str, ...], /) -> str: ...


WITTY_PHRASES: tuple[str, ...] = (
    "Shipping awesomeness",
    "Painting the serifs back on",
    "Navigating the slime mold",
    "Consulting the digital spirits",
    "Reticulating splines",
    "Warming up the AI hamsters",
    "Asking the magic conch shell",
    "Generating witty retort",
    "Polishing the algorithms",
    "Don't rush perfection (or my code)",
    "Brewing fresh bytes",
    "Counting electrons",
    "Engaging cognitive processors",
    "Checking for syntax errors in the universe",
    "One moment, optimizing humor",
    "Shuffling punchlines",
    "Untangling neural nets",
    "Compiling brilliance",
    "Loading wit.exe",
    "Summoning the cloud of wisdom",
    "Preparing a witty response",
    "Just a sec, I'm debugging reality",
    "Confuzzling the options",
    "Tuning the cosmic frequencies",
    "Crafting a response worthy of your patience",
    "Compiling the 1s and 0s",
    "Resolving dependencies… and existential crises",
    "Defragmenting memories… both RAM and personal",
    "Rebooting the humor module",
    "Caching the essentials (mostly cat memes)",
    "Optimizing for ludicrous speed",
    "Swapping bits… don't tell the bytes",
    "Garbage collecting… be right back",
    "Assembling the interwebs",
    "Converting coffee into code",
    "Updating the syntax for reality",
    "Rewiring the synapses",
    "Looking for a misplaced semicolon",
    "Greasin' the cogs of the machine",
    "Pre-heating the servers",
    "Calibrating the flux capacitor",
    "Engaging the improbability drive",
    "Channeling the Force",
    "Aligning the stars for optimal response",
    "So say we all",
    "Loading the next great idea",
    "Just a moment, I'm in the zone",
    "Preparing to dazzle you with brilliance",
    "Just a tick, I'm polishing my wit",
    "Hold tight, I'm crafting a masterpiece",
    "Just a jiffy, I'm debugging the universe",
    "Just a moment, I'm aligning the pixels",
    "Just a sec, I'm optimizing the humor",
    "Just a moment, I'm tuning the algorithms",
    "Warp speed engaged",
    "Mining for more Dilithium crystals",
    "Don't panic",
    "Following the white rabbit",
    "The truth is in here… somewhere",
    "Blowing on the cartridge",
    "Loading… Do a barrel roll!",
    "Waiting for the respawn",
    "Finishing the Kessel Run in less than 12 parsecs",
    "The cake is not a lie, it's just still loading",
    "Fiddling with the character creation screen",
    "Just a moment, I'm finding the right meme",
    "Pressing 'A' to continue",
    "Herding digital cats",
    "Polishing the pixels",
    "Finding a suitable loading screen pun",
    "Distracting you with this witty phrase",
    "Almost there… probably",
    "Our hamsters are working as fast as they can",
    "Giving Cloudy a pat on the head",
    "Petting the cat",
    "Slapping the bass",
    "Tasting the snozberries",
    "I'm going the distance, I'm going for speed",
    "Is this the real life? Is this just fantasy?",
    "I've got a good feeling about this",
    "Poking the bear",
    "Doing research on the latest memes",
    "Figuring out how to make this more witty",
    "Hmmm… let me think",
    "What do you call a fish with no eyes? A fsh",
    "Why did the computer go to therapy? It had too many bytes",
    "Why don't programmers like nature? It has too many bugs",
    "Why do programmers prefer dark mode? Because light attracts bugs",
    "Why did the developer go broke? Because they used up all their cache",
    "What can you do with a broken pencil? Nothing, it's pointless",
    "Applying percussive maintenance",
    "Searching for the correct USB orientation",
    "Ensuring the magic smoke stays inside the wires",
    "Rewriting in Rust for no particular reason",
    "Trying to exit Vim",
    "Spinning up the hamster wheel",
    "That's not a bug, it's an undocumented feature",
    "Engage.",
    "I'll be back… with an answer.",
    "My other process is a TARDIS",
    "Communing with the machine spirit",
    "Letting the thoughts marinate",
    "Just remembered where I put my keys",
    "Pondering the orb",
    "I've seen things you people wouldn't believe… like a user who reads loading messages.",
    "Initiating thoughtful gaze",
    "What's a computer's favorite snack? Microchips.",
    "Why do Java developers wear glasses? Because they don't C#.",
    "Charging the laser… pew pew!",
    "Dividing by zero… just kidding!",
    "Looking for an adult superviso… I mean, processing.",
    "Making it go beep boop.",
    "Buffering… because even AIs need a moment.",
    "Entangling quantum particles for a faster response",
    "Polishing the chrome… on the algorithms.",
    "Are you not entertained? (Working on it!)",
    "Summoning the code gremlins… to help, of course.",
    "Just waiting for the dial-up tone to finish",
    "Recalibrating the humor-o-meter.",
    "My other loading screen is even funnier.",
    "Pretty sure there's a cat walking on the keyboard somewhere",
    "Enhancing… Enhancing… Still loading.",
    "It's not a bug, it's a feature… of this loading screen.",
    "Have you tried turning it off and on again? (The loading screen, not me.)",
    "Constructing additional pylons",
    "Releasing the HypnoDrones",
    "Pushing the button, Frank.",
    "Checking the calendar twice",
    "Sorting the inbox by vibes",
    "Sharpening the to-do list",
    "Finding a free Tuesday",
    "Grinding fresh pepper",
    "Dusting off the reminders",
    "Flipping through the notes",
    "Penciling in a little breathing room",
    "Untangling the week",
    "Stacking the sticky notes",
    "Warming up the teapot",
    "Seasoning the answer to taste",
    "Alphabetizing my thoughts",
    "Leafing through the notebook",
)


def tail(text: str, width: int = 70) -> str:
    """``text`` on one line, cut to its last ``width`` characters (a thought's recent end)."""
    flat = " ".join(text.split())
    return flat if len(flat) <= width else "…" + flat[-(width - 1) :]


def pick(previous: str | None = None, rng: _Chooser = random) -> str:
    """A random phrase that is not ``previous``, so a rotation always visibly changes."""
    choices = tuple(p for p in WITTY_PHRASES if p != previous)
    return rng.choice(choices)
